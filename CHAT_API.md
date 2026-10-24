# Chat REST API Reference

Integration reference for the Bucky chatbot backend, written for the team wiring it into the
**WhatsApp Business API**. Everything here is derived from the code in `backend/app/`
(`main.py`, `schemas.py`, `llm.py`, `rate_limit.py`, `session_store.py`, `attachments.py`,
`stream_sanitizer.py`, `config.py`).

Production base URL (per `Caddyfile`): `https://chatbot.bucketlistt.com`
Test deployment (Render, free tier): `https://bucky-chat-test.onrender.com`
Local dev: `http://localhost:8000`

> **For WhatsApp / CRM integration use `POST /api/v2/chat` (see [section 12](#12-apiv2chat-crm--whatsapp-endpoint)).**
> It returns one JSON body (no SSE parsing), requires an API key, is exempt from the per-IP
> rate limit when the key is valid, and adds machine-readable `handoff` and `session_capped`
> flags. Sections 3 to 11 describe the SSE endpoint `/api/chat` used by the website; where the
> v2 work changed its behaviour, the text below already reflects that.

---

## 1. Read this first: what matters for WhatsApp

| # | Fact | Consequence for the WhatsApp gateway |
|---|---|---|
| 1 | The API is **stateless about history**. The server does **not** rebuild the conversation from Redis; it only uses the `messages` array you send. | Your gateway must store each WhatsApp user's history and resend it every turn (max 40 messages). |
| 2 | Rate limit is **20 requests/minute per client IP** (configurable via `CHAT_LIMIT_PER_MINUTE`) on `POST /api/chat` and `POST /api/v2/chat`. IP is taken from `X-Forwarded-For` (first entry), else the socket address. **Requests with a valid `CHAT_API_KEY` skip this limit.** | A gateway sending a valid key is not throttled. Without a key, all users share one IP's 20 req/min. See §6. |
| 3 | The response is **Server-Sent Events (SSE)**, not a single JSON body. | Gateway must read the stream, concatenate `delta` text, and send one WhatsApp message when `done: true`. |
| 4 | Replies are **Markdown**. WhatsApp uses its own syntax (`*bold*`, `_italic_`). | Convert Markdown before sending (see §9). |
| 5 | Each session is capped at **15 user messages** (default), then the bot replies with a "start a fresh chat" message. | Rotate the `session_id` when a session caps out (or after the 2h TTL). |
| 6 | A WhatsApp text message is limited to **4096 characters**; the API allows up to **8000** in, and replies can be longer. | Split long replies on paragraph boundaries. |
| 7 | Auth is **opt-in**: when `CHAT_API_KEY` is set on the server, `/api/chat` and `/api/session/user-info` need `Authorization: Bearer <key>`, and `/api/v2/chat` **always** needs it (503 if the key is not configured). With the variable unset, `/api/chat` is open. | Set `CHAT_API_KEY` on any internet-facing deployment, and send the key from the gateway only (never from a browser). |

---

## 2. Endpoint summary

| Method | Path | Purpose | Auth | Rate limited |
|---|---|---|---|---|
| `GET` | `/api/health` | Liveness probe, returns configured model | none | no |
| `POST` | `/api/v2/chat` | **CRM/WhatsApp endpoint:** one JSON reply, handoff and cap flags, idempotent retries | Bearer `CHAT_API_KEY` (required) | yes, 20/min/IP, **skipped with a valid key** |
| `POST` | `/api/v2/chat/stream` | Same as v2 but streamed (SSE): status and text chunks, then one final frame with the full v2 fields | Bearer `CHAT_API_KEY` (required) | same as v2 |
| `POST` | `/api/chat` | Send conversation, receive streamed answer (SSE) | Bearer `CHAT_API_KEY` if configured | yes, 20/min/IP, skipped with a valid key |
| `POST` | `/api/chat/attachments` | Upload one file, get an `attachment_id` | Bearer `CHAT_API_KEY` if configured | no |
| `POST` | `/api/session/user-info` | Save name/phone/email into a session | Bearer `CHAT_API_KEY` if configured | no |
| `GET` | `/api/admin/session-summaries` | List idle-session summaries | Bearer token | no |
| `GET` | `/api/admin/session-summaries/{session_id}` | One session summary | Bearer token | no |

The first five are what a chat client needs. The admin endpoints are for the employee dashboard
(see `SUMMARY_WEBHOOK_API.md`) and are listed in §8 for completeness.

---

## 3. `POST /api/chat`

Streams the assistant's reply for the supplied conversation.

### 3.1 Request

`Content-Type: application/json`

```json
{
  "session_id": "wa-919876543210",
  "messages": [
    { "role": "user", "content": "What can I do in Rishikesh?" },
    { "role": "assistant", "content": "Rishikesh has rafting, bungee ..." },
    { "role": "user", "content": "How much is bungee?", "attachment_ids": [] }
  ]
}
```

#### Body fields

| Field | Type | Required | Constraints | Notes |
|---|---|---|---|---|
| `messages` | array of `ChatMessage` | yes | 1 to **40** items | Full history, oldest first. The **last** item is the one being answered, and should have `role: "user"`. |
| `session_id` | string \| null | no | max 128 chars | See §3.2. |
| `channel` | string \| null | no | max 32 chars | `"whatsapp"` appends WhatsApp formatting rules to the prompt (no tables, headings or links; `*bold*`; short replies). Unknown values behave like web chat. Defaults to `whatsapp` on `/api/v2/chat` only. |
| `request_id` | string \| null | no | max 256 chars | Caller's message id. Used for idempotent retries on `/api/v2/chat` only; ignored by `/api/chat`. |

#### `ChatMessage`

| Field | Type | Required | Constraints | Notes |
|---|---|---|---|---|
| `role` | `"user"` \| `"assistant"` | yes | `"system"` is **rejected (422)** | Clients cannot inject a system prompt. |
| `content` | string | yes | **1 to 8000 chars** | Empty string is rejected. |
| `attachment_ids` | array of string | no, default `[]` | max **5** items | IDs from `POST /api/chat/attachments`. Only the **last** message's attachments are read by the model. |

#### 3.2 `session_id` resolution and cookie

Resolved in this order: body `session_id` → `session_id` cookie → newly generated UUID.
The resolved ID is always returned as a `Set-Cookie: session_id=...` (HttpOnly, SameSite=Lax,
`Secure` on https, `Max-Age` = `SESSION_TTL_SECONDS`, default 7200).

**For WhatsApp: always send `session_id` explicitly in the body**; do not rely on cookies.
Use a stable per-conversation ID, e.g. `wa-<E.164 phone>` or `wa-<phone>-<n>`, where `n`
increments when you start a fresh chat. The session ID keys all server-side state:

| State keyed on `session_id` | Lifetime | Storage |
|---|---|---|
| Message count (for the 15-message cap and login/contact prompts) | 2h, refreshed each turn | Redis |
| Saved transcript (used for dashboard summaries) | 2h, refreshed each turn | Redis |
| Logged-in bucketlistt token (after OTP verify) | 30 min | In-process memory **plus Redis** (restored after a restart) |

If Redis is not configured, history persistence, the message cap and login prompts silently
no-op; chat itself still works.

### 3.3 Response

`200 OK`, `Content-Type: text/event-stream`. The body is a sequence of SSE frames. Each frame is
one line `data: <json>` followed by a blank line. **Every frame has a `done` boolean.**

```
data: {"status": "Fetching activities…", "done": false}

data: {"delta": "Bungee in Rishikesh ", "done": false}

data: {"delta": "starts at ₹3,550 ...", "done": false}

data: {"delta": "", "done": true}

```

#### Frame types

| Frame | Shape | Meaning | What to do |
|---|---|---|---|
| Status | `{"status": "<text>", "done": false}` | Model is running a tool (catalog lookup etc.). Can appear several times, before or between text. | Ignore, or send a typing indicator. |
| Text chunk | `{"delta": "<text>", "done": false}` | Next piece of the reply. Already sanitised. | **Append** to a buffer. Chunks are arbitrary slices, may split words. |
| Login prompt | `{"prompt_login": true, "done": false}` | Legacy UI flag, sent once when the session reaches `LOGIN_PROMPT_AFTER` (default 3) user messages and no contact info is saved. The same ask is also written into the reply text itself. | Safe to ignore on WhatsApp. |
| Final | `{"delta": "", "done": true}` | Stream finished successfully. | Flush the buffer to the user. |
| Final with error | `{"delta": "", "done": true, "error": "<friendly text>"}` | The request failed mid-way. HTTP status is still **200**. | Discard/append per your policy and show `error` to the user. |

Guarantees: the stream **always** ends with exactly one `done: true` frame, even on failure
(errors are in-band, not exceptions). Partial `delta` text may already have been sent before an
error frame.

**Status strings** (for reference): `Fetching destinations…`, `Looking up providers…`,
`Fetching activities…`, `Fetching activity details…`, `Searching activities…`,
`Checking availability…`, `Checking time slots…`, `Fetching add-ons…`, `Sending OTP…`,
`Verifying OTP…`, `Adding to cart…`, `Fetching cart…`, `Updating cart…`, `Removing from cart…`,
`Fetching bookings…`, `Searching the web…`, `Creating support ticket…`, `Working…` (fallback).
Do not match on these strings programmatically, the list can change.

#### In-band `error` messages

| `error` text | Cause |
|---|---|
| `I hit a snag trying to find that for you. Mind asking me again?` | Generic failure (default) |
| `You have reached the limit of your messages. Please wait a moment before trying again.` | Upstream LLM provider rate limit (429) |
| `My servers are a bit overloaded right now. Give me just a second to catch my breath and try again!` | Upstream LLM 5xx/overloaded |
| `This conversation got a bit too long for me to remember everything! Try starting a New Chat to clear my head.` | Model context length exceeded; trim history or rotate session |

#### Session cap reply (not an error)

When the session's saved message count reaches `MAX_MESSAGES_PER_SESSION` (default **15**; the
Render test deployment sets 40), no LLM call is made. You get a single text chunk and a final
frame that carries the flag `session_capped`:

```
data: {"delta": "Whew, we've covered a lot in this chat! I can only handle so many messages in one conversation, could you start a fresh chat so I can keep helping you properly? 🪂", "done": false}

data: {"delta": "", "done": true, "session_capped": true}
```

Detect it with `session_capped: true` on the final frame (on `/api/v2/chat` it is a field of the
JSON body). Requires Redis; without Redis the cap never triggers.

### 3.4 Non-200 responses

| Status | When | Body |
|---|---|---|
| `422` | Validation failed: empty `messages`, more than 40 messages, `content` empty or over 8000 chars, bad `role` (incl. `"system"`), more than 5 `attachment_ids`, malformed JSON | FastAPI format, see below |
| `429` | Rate limit exceeded (§6) | `{"detail": "Too many requests. Please slow down."}` |
| `500` | Unhandled failure *before* the stream begins | `Internal Server Error` |

422 body example:

```json
{
  "detail": [
    {
      "type": "string_too_short",
      "loc": ["body", "messages", 0, "content"],
      "msg": "String should have at least 1 character",
      "input": "",
      "ctx": { "min_length": 1 }
    }
  ]
}
```

Failures **after** streaming starts are reported in-band (§3.3), never as a non-200.

### 3.5 Output processing applied to `delta` text

The server edits text before you see it:
- Em dash `—` is replaced with `,`; en dash `–` with `-`.
- Any raw 24-hex-character database ID is stripped.
- Output is capped at **1500 tokens** per model call; the tool loop runs at most **8** iterations.
- Reply language mirrors the user's language (the knowledge base instructs the model to do so).

### 3.6 Latency

The reply is produced by a tool loop (resolve tools non-streaming, then stream the final answer).
Simple questions stream in a few seconds; catalog/price/availability questions involve live MCP
calls first, so the first `delta` can take noticeably longer. **There is no server-side request
timeout on the stream**, so set your own (suggest 60s total) on the gateway.

### 3.7 curl

```bash
curl -N -X POST https://chatbot.bucketlistt.com/api/chat \
  -H "Content-Type: application/json" \
  -d '{"session_id":"wa-919876543210","messages":[{"role":"user","content":"hi"}]}'
```

`-N` disables curl's buffering so frames appear live.

---

## 4. `POST /api/chat/attachments`

Uploads **one** file. Reference the returned ID in `attachment_ids` of a later `/api/chat` call.

`Content-Type: multipart/form-data`, single field **`file`**.

```bash
curl -X POST https://chatbot.bucketlistt.com/api/chat/attachments -F "file=@passport.pdf"
```

### Success `200`

```json
{ "attachment_id": "3f9a1c0e7b2d4e5f8a6b9c0d1e2f3a4b", "type": "pdf", "filename": "passport.pdf" }
```

`type` is one of `image`, `pdf`, `text`, `docx`. (The schema comment lists only three, but `.docx`
uploads return `docx`.)

### Allowed types and size caps

| Type | Accepted | Max size (default) | Env override |
|---|---|---|---|
| Image | JPEG, PNG | 10 MB | `ATTACHMENT_MAX_IMAGE_MB` |
| PDF | `application/pdf` | 32 MB | `ATTACHMENT_MAX_PDF_MB` |
| Word | `.docx` | 8 MB | `ATTACHMENT_MAX_DOCX_MB` |
| Text | plain UTF-8 text | 2 MB | fixed |

Type is detected from file **content** (magic bytes), not the filename or declared MIME type.

### Errors

| Status | `detail` | Cause |
|---|---|---|
| 413 | `File too large — <type> attachments are capped at <N>MB.` | Over size cap |
| 415 | `Unsupported file type. Allowed: jpg, png, pdf, txt, docx.` | Other type (WebP, GIF, audio, video, etc.) |
| 422 | `This file failed a security scan and can't be uploaded.` | ClamAV found malware |
| 422 | `This document is too large to process.` | docx decompresses past 50 MB (zip bomb guard) |
| 422 | `This doesn't look like a valid Word document.` | Malformed docx |
| 503 | `Attachment scanning is temporarily unavailable — try again shortly.` | ClamAV unreachable (**fails closed**) |
| 503 | `Attachments are temporarily unavailable — try again shortly.` | Redis unavailable |

### Behaviour notes

- Attachments **expire after 2 hours**. An expired/unknown ID is **silently skipped**; the chat
  still answers, just without the file.
- Max **5** attachments per message. Only the **last** message in `messages` has its attachments
  resolved.
- This endpoint is **not rate limited**. It requires the `CHAT_API_KEY` bearer token when one is configured.
- **WhatsApp specifics:** Meta delivers media as a media ID you must download (with your access
  token) and then re-upload here. WhatsApp audio, video, stickers, WebP and GIF are **not
  supported** by this API (415). Handle those in the gateway (for example, ask the user to send text).
  Attached file contents are treated as data; instructions inside them are ignored by the model.

---

## 5. `POST /api/session/user-info`

Stores contact details in the session (used in the dashboard summary).

```json
{ "session_id": "wa-919876543210", "user_info": { "name": "Asha", "phone": "+919876543210", "email": "" } }
```

| Field | Type | Required | Notes |
|---|---|---|---|
| `session_id` | string | yes | |
| `user_info.name` / `phone` / `email` | string | no, default `""` | No format validation |

Response `200`: `{"status": "ok"}`.
If `session_id` is empty: `200` with `{"status": "error", "message": "No session ID provided"}`
(error is in the body, not the status code). A missing field gives `422`.
No-op if Redis is unavailable, but still returns `{"status": "ok"}`.

The WhatsApp gateway already knows the user's phone number, so this is a convenient place to save
it. Note that the chatbot can also capture contact details itself, in conversation, via its lead tool.

---

## 6. Rate limiting

Implemented in `rate_limit.py` as middleware.

| Property | Value |
|---|---|
| Scope | **`POST /api/chat` and `POST /api/v2/chat`** (exact path match). Every other endpoint is unlimited. |
| Limit | **20 requests per 60 seconds** per client IP |
| Algorithm | Sliding window (timestamps of the last 60s) |
| Client IP | The `X-Forwarded-For` entry `TRUSTED_PROXY_HOPS` places from the **right** (default 1, the address the nearest proxy saw). The left side is caller-controlled and ignored. `0` ignores the header and uses the TCP peer address |
| Valid key | Not limited per IP, but all valid-key requests together are capped at `CHAT_KEY_LIMIT_PER_MINUTE` (default **120**, `0` = off) to bound LLM spend. Over it: `429` |
| Over limit | `429` with `{"detail": "Too many requests. Please slow down."}` |
| Headers | **No** `Retry-After` or `X-RateLimit-*` headers are sent |
| Storage | In-process memory: resets on restart, **not shared across workers** |
| Configurable | `CHAT_LIMIT_PER_MINUTE` (default 20), `CHAT_KEY_LIMIT_PER_MINUTE` (default 120), `TRUSTED_PROXY_HOPS` (default 1) |
| Memory | Idle buckets are swept once more than 10,000 are tracked |

Requests that are rejected with 429 are not counted. Validation failures (422) **do** count,
since the limiter runs before validation.

### Why this matters for WhatsApp

Every WhatsApp user arrives via your gateway's single IP. **Send the `CHAT_API_KEY` bearer token
and the gateway is exempt from this limit** (the recommended solution, already built). Only
keyless or wrong-key traffic is limited. If you cannot use a key, the older options are:

1. **Raise the limit** with the `CHAT_LIMIT_PER_MINUTE` env var.
2. **Exempt/allow-list the gateway IP** in `RateLimitMiddleware` (code change).
3. **Key by session** instead of IP, by passing `X-Forwarded-For: <per-user-value>` from the
   gateway. Works today with no backend change, but it relies on a header that the proxy (Caddy)
   also sets, so confirm the order in which the first entry is chosen, and never expose the
   backend directly to the public internet with this trick.
4. Queue in the gateway and retry on 429 with backoff (recommended regardless). Use a delay of
   about 3 seconds and cap the retries.

### Other limits that affect throughput

| Limit | Value | Where |
|---|---|---|
| Max messages per request | 40 | `schemas.py` |
| Max chars per message | 8000 | `schemas.py` |
| Max attachments per message | 5 | `schemas.py` |
| Max user messages per session | 15 (`MAX_MESSAGES_PER_SESSION`) | `config.py` |
| Session / attachment TTL | 7200 s (`SESSION_TTL_SECONDS`) | `config.py` |
| Login token TTL | 30 min | `token_store.py` |
| Tool iterations per reply | 8 | `llm.py` |
| Max output tokens per model call | 1500 | `llm.py` |

Upstream LLM provider limits also apply and surface as the in-band error in §3.3.

---

## 7. `GET /api/health`

```bash
curl https://chatbot.bucketlistt.com/api/health
# {"status":"ok","model":"anthropic/claude-sonnet-5-5"}
```

Returns `200` whenever the process is up. It does **not** check Redis, Weaviate, the MCP server
or ClamAV.

---

## 8. Admin endpoints (dashboard only)

Not needed for WhatsApp chat. Requires `Authorization: Bearer <DASHBOARD_API_KEY>`.

| Method / path | Notes |
|---|---|
| `GET /api/admin/session-summaries?since=<ISO 8601>` | Returns `{"summaries": [...]}`; `since` filters to sessions ended at/after it |
| `GET /api/admin/session-summaries/{session_id}` | Returns one record |

Errors: `503` if `DASHBOARD_API_KEY` is unset (feature off), `401` on wrong/missing token,
`404` if the session has no summary yet (summaries are generated after `IDLE_SUMMARY_MINUTES`,
default 15, of inactivity). Full schema and the push webhook are in `SUMMARY_WEBHOOK_API.md`.

---

## 9. WhatsApp integration guide

### 9.1 Recommended flow

```
WhatsApp user ─► Meta webhook ─► Gateway ─► POST /api/chat ─► SSE stream
      ▲                                                          │
      └────────── Meta send-message API ◄── buffer deltas ◄──────┘
```

Per inbound message, the gateway should:

1. **Acknowledge Meta's webhook immediately (200)** and process asynchronously. Meta retries
   on slow responses, which would duplicate messages. De-duplicate on the WhatsApp message ID.
2. Load the user's stored history (`[{role, content}, ...]`), append the new user message, and
   trim to the last 40. Keep each `content` at 8000 chars or fewer.
3. `POST /api/chat` with `session_id = "wa-<phone>[-n]"`.
4. Read the SSE stream line by line. Collect `delta`, ignore `status` and `prompt_login`, stop on
   `done: true`. Optionally send a typing indicator while waiting.
5. If the final frame has `error`, send that text to the user and **do not** store that turn.
6. Convert Markdown to WhatsApp formatting, split to 4096 chars or fewer, send via Meta.
7. Store the user message and the full assistant text in the user's history.

Serialise requests **per user** (one in flight per `session_id`) so turns arrive in order.

### 9.2 Reference parsing (Python)

```python
import json, httpx

async def ask(base, session_id, history):
    text, error = [], None
    async with httpx.AsyncClient(timeout=httpx.Timeout(60, read=60)) as c:
        async with c.stream("POST", f"{base}/api/chat",
                            json={"session_id": session_id, "messages": history}) as r:
            if r.status_code == 429:
                raise RuntimeError("rate limited")      # back off and retry
            r.raise_for_status()
            async for line in r.aiter_lines():
                if not line.startswith("data: "):
                    continue
                frame = json.loads(line[6:])
                text.append(frame.get("delta", ""))
                if frame.get("done"):
                    error = frame.get("error")
                    break
    return "".join(text), error
```

### 9.3 Formatting differences (Markdown to WhatsApp)

The model writes GitHub-flavoured Markdown (see `GFM_RENDERING_GUIDE.md`). WhatsApp renders only:

| Markdown from API | WhatsApp equivalent |
|---|---|
| `**bold**` | `*bold*` |
| `*italic*` / `_italic_` | `_italic_` |
| `~~strike~~` | `~strike~` |
| `` `code` `` / code fences | ` ```code``` ` |
| `- item` / `* item` | supported (`- item`) |
| `# Heading` | not supported, convert to `*Heading*` |
| `[text](url)` | not supported, send `text: url` (bare URLs auto-link) |
| Tables `\|---\|` | **not supported**, convert to lines or bullets |
| Images `![](url)` | not supported, send as a separate image message |

The system prompt is tuned for a web UI; if tables and headings show up often, consider a
WhatsApp-specific knowledge-base file (`SYSTEM_PROMPT_FILE`) telling the model to avoid them.
This is configuration only, no code change.

### 9.4 Session strategy

- One `session_id` per WhatsApp phone number, with a numeric suffix you bump when starting over.
- Start a **new suffix** when: the cap reply appears, the `context length` error appears, the
  user says "reset"/"new chat", or the last activity was over 2 hours ago (server state has
  expired anyway; the gateway history may be kept or cleared at your discretion).
- The logged-in bucketlistt state (after OTP) is held per `session_id` for 30 minutes (memory,
  mirrored to Redis). A new suffix means the user logs in again.

### 9.5 Things to know

- **Booking/payment is not done in chat.** The bot adds items to a cart and sends the link
  `https://www.bucketlistt.com/experiences/cart`; the user pays on the website.
- **OTP login goes through chat** (the bot sends/verifies an OTP via the catalog server). A user
  who replies with a bare 4 to 8 digit code is treated as an OTP answer, so never pre-process or
  drop numeric-only messages.
- Messages that look like phone numbers or OTPs are always passed through the full pipeline.
- **CORS** settings (`CORS_ORIGINS`) only affect browsers; server-to-server calls ignore them.
- **Proxy:** Caddy forwards to `backend:8000`. SSE needs no response buffering; Caddy's
  `reverse_proxy` streams by default. Any proxy you add in front must not buffer responses.

---

## 10. Configuration reference (API-relevant env vars)

| Variable | Default | Effect |
|---|---|---|
| `LLM_MODEL` | `anthropic/claude-sonnet-5-5` | Model used (via litellm) |
| `SESSION_TTL_SECONDS` | 7200 | Conversation state lifetime. The Render blueprint sets 86400 (24 h) to match WhatsApp's customer-service window. The bucketlistt login lasts 30 min and attachments 2 h regardless |
| `CHAT_API_KEY` | blank | Bearer key. Blank: `/api/chat` open, `/api/v2/chat` returns 503. Set: required on `/api/v2/chat`, `/api/chat`, `/api/session/user-info` |
| `CHAT_LIMIT_PER_MINUTE` | 20 | Per-IP limit for requests without a valid key |
| `CHAT_KEY_LIMIT_PER_MINUTE` | 120 | Combined limit for all requests with a valid key (0 = off) |
| `TRUSTED_PROXY_HOPS` | 1 | Reverse proxies appending to `X-Forwarded-For`; set to match your hosting |
| `DOCS_ENABLED` | false | `/docs`, `/redoc`, `/openapi.json` are hidden when `CHAT_API_KEY` is set unless this is `true` |
| `SYSTEM_PROMPT_FILE` | `data/knowledge_base.md` | Bot personality and knowledge |
| `MCP_SERVER_URL` | blank | Enables live catalog tools |
| `WEAVIATE_URL` | blank | Enables RAG retrieval |
| `REDIS_URL` | `redis://localhost:6379/0` | Sessions, message cap, attachments, cache |
| `SESSION_TTL_SECONDS` | 7200 | Session and attachment lifetime |
| `LOGIN_PROMPT_AFTER` | 3 | User messages before the contact/login ask |
| `MAX_MESSAGES_PER_SESSION` | 15 | Per-session user message cap |
| `CORS_ORIGINS` | `http://localhost:5173` | Allowed browser origins |
| `ATTACHMENT_MAX_IMAGE_MB` / `_PDF_MB` / `_DOCX_MB` | 10 / 32 / 8 | Upload caps |
| `CLAMD_HOST` / `CLAMD_PORT` | `localhost` / 3310 | Malware scanner |
| `DASHBOARD_API_KEY` | blank | Enables admin endpoints |

---

## 11. Known gaps to decide on before launch

1. **Authentication is opt-in on `/api/chat`.** If `CHAT_API_KEY` is unset anyone with the URL can use it and spend LLM credit. Always set it on public deployments, and rotate it if it is ever shared in a chat or ticket.
2. **Shared-IP rate limit** (§6) applies to keyless traffic; the key bypass solves it for the gateway.
3. **Login state** has no flag in the API; the model asks for the OTP in chat (§12.8).
4. **No server-side timeout** on streams; gateway must enforce one.
   Also: the per-session message cap can be dodged by sending a new `session_id` each time; the
   per-key ceiling (§6) is what bounds total cost.
5. **The rate limiter lives in process memory**: it resets on restart and is not shared across
   workers. OTP login tokens are now also written to Redis and restored after a restart.
6. **Message cap, history persistence and attachments depend on Redis.** Attachments return 503 without it.

---

## 12. `/api/v2/chat`: CRM / WhatsApp endpoint

Same model, knowledge base and tools as `/api/chat`, with a contract built for a server-to-server
gateway: **one JSON response instead of an SSE stream**, mandatory auth, a human-handoff signal,
a message-cap flag, and safe retries. Source: `backend/app/crm_chat.py`, `handoff.py`.
A fuller hand-off letter for the CRM team is in `CRM_INTEGRATION_GUIDE.md`.

### 12.1 Request

```
POST {BASE_URL}/api/v2/chat
Authorization: Bearer <CHAT_API_KEY>
Content-Type: application/json
```

```json
{
  "session_id": "wa-919876543210-1",
  "request_id": "wamid.HBgM...",
  "channel": "whatsapp",
  "messages": [
    { "role": "user", "content": "How much is bungee in Rishikesh?" }
  ]
}
```

| Field | Required | Constraints | Notes |
|---|---|---|---|
| `messages` | yes | 1 to 40 items, oldest first | Same `ChatMessage` rules as §3.1 (`role` is `user` or `assistant`, `content` 1 to 8000 chars, up to 5 `attachment_ids`). Human-agent replies go in as `assistant`. Send the customer's text unchanged, including OTP codes and phone numbers. |
| `session_id` | yes in practice | string | If omitted a random UUID is generated and returned, so you would lose all session state. Use `wa-<E.164>-<n>`. |
| `request_id` | recommended | max 256 chars | The WhatsApp message id. Enables replay protection (§12.4). |
| `channel` | no | max 32 chars | Defaults to `whatsapp` here. |

Unknown extra fields are ignored. The key is a shared secret: keep it in the gateway worker,
never in a browser or mobile app.

### 12.2 Response (HTTP 200)

```json
{
  "session_id": "wa-919876543210-1",
  "reply": "Here are the main single-jump prices in Rishikesh...",
  "messages": ["Here are the main single-jump prices in Rishikesh.", "Want me to check slots for your date?"],
  "handoff": false,
  "handoff_reason": null,
  "session_capped": false,
  "error": null
}
```

| Field | Type | Meaning |
|---|---|---|
| `session_id` | string | Echo of the session used. |
| `reply` | string | Text to send to the customer, already WhatsApp-formatted. Empty when `error` is set. Split at 4096 chars if needed. |
| `messages` | string[] | The same text as `reply`, split into short WhatsApp-sized bubbles: one per paragraph, at most 4 (extra paragraphs are merged into the last), none over 4000 chars, and a bullet list stays in one bubble. **Send these one by one, in order, instead of `reply`**, ideally with a typing indicator and a short pause (about 1 to 3 s) before each. Empty when `reply` is empty. Older clients can keep using `reply`; the text is identical. |
| `handoff` | bool | `true`: queue the conversation for a human and stop calling the bot until an agent hands it back. `reply` then holds a short "connecting you to our team" message, which you should deliver. |
| `handoff_reason` | string \| null | `customer_requested_human`, `cannot_answer`, `complaint`, `payment_issue`, `booking_change` or `other`. Advisory only: it is set by the model or by a keyword check on the customer's last message. |
| `session_capped` | bool | `true`: the session hit its message cap. **Do not show `reply`.** Start a new `session_id` and resend the same customer message. |
| `error` | string \| null | Non-null means the request failed (LLM overloaded, rate-limited, internal error). **Never forward this text to the customer.** |

Handoff is decided two ways: the model calls its `request_human_handoff` tool, or a keyword
backstop on the customer's last message matches (refund, charged twice, complaint, scam, "talk to
a human", "customer care", "manager", and similar). A capped session never reports a handoff.

### 12.3 Errors and status codes

| HTTP | Body | Meaning | Action |
|---|---|---|---|
| 200 with `error` set | normal JSON | Failure inside the bot or the LLM provider | Retry with backoff (§12.4). If `handoff` is also true, deliver `reply` and hand off. |
| 401 | `{"detail":"Unauthorized"}` | Missing or wrong key (it must be exactly `Bearer <key>`) | Fix configuration |
| 409 | `{"detail":"A request with this request_id is still being processed; retry shortly"}` | Same `session_id` + `request_id` is still running | Wait a few seconds and retry |
| 422 | FastAPI validation list (see §3.4) | Empty message, over 40 messages, content over 8000 chars, bad role | Fix the request |
| 429 | `{"detail":"Too many requests. Please slow down."}` | Over 20/min per IP **without a valid key** (a valid key bypasses it) | Check the key |
| 503 | `{"detail":"CRM chat API not configured (set CHAT_API_KEY)"}` | The server has no `CHAT_API_KEY` | Configure the server |

Unlike `/api/chat`, there is no 200-stream: the whole answer is produced first (typically a few
seconds, up to 20 to 40 s when live availability is looked up), then returned. Use a 60 s client
timeout and keep one request in flight per conversation.

### 12.4 Idempotency (`request_id`)

Replay results are kept for **10 minutes** per `session_id` + `request_id` (needs Redis; without
Redis `request_id` is accepted but every call runs normally).

| Situation | Result |
|---|---|
| First request | Runs normally |
| Same ids after a successful request | The stored response, with header `Idempotency-Replayed: true`. **No tools re-run**, so no duplicate cart items or OTP sends |
| Same ids while the first is still running (in-flight marker lasts 120 s) | `409` |
| Same ids after a response that had `error` | Runs again (errors are never stored) |

Retry only on a network failure, 5xx, 409, or a 200 with `error`. Suggested backoff 2 s, 5 s, 10 s,
then hand the conversation to a human.

### 12.5 Session cap handling

The cap is per `session_id` (`MAX_MESSAGES_PER_SESSION`: default 15, Render test service 40).
When `session_capped` is `true`, switch to the next id (`-1` to `-2`) and resend the same
customer message so the customer never sees the cap. Do not rotate earlier, because a new id
discards the login (§12.8).

### 12.6 Message formatting

With `channel: "whatsapp"` the model is told to text like a person: answer first in 1 to 3 short
sentences, match the length of the customer's message, put a blank line between thoughts (each
paragraph becomes one entry in `messages`), use a bullet list only when prices or options were
asked for, aim for under about 400 characters (800 at most unless a full price list was asked
for), and end with at most one short question. It is also told never to use tables, headings or
Markdown links, and to use WhatsApp syntax. Real output from the test deployment:

```
*Thrill Factory* (54-55m)
- Freestyle 54M: ~~₹2,500~~ ₹2,000
```

WhatsApp's strikethrough is a single `~text~`. The model is told to use it, and as a backstop
`/api/v2/chat` rewrites any `~~text~~` to `~text~` in `reply` when the channel is `whatsapp`, so
customers never see stray tildes. The prompt also tells the model to apply the formatting rules
silently, without mentioning WhatsApp, tables or formatting in the reply. Text is also sanitised as in §3.5 (dashes replaced, raw database ids removed).

### 12.7 Examples

Test deployment (replace the key):

```bash
curl -s -X POST https://bucky-chat-test.onrender.com/api/v2/chat \
  -H "Authorization: Bearer $CHAT_API_KEY" -H "Content-Type: application/json" \
  -d '{"session_id":"wa-test-1","request_id":"t1","messages":[{"role":"user","content":"I want to talk to a human"}]}'
# expect: handoff=true, handoff_reason="customer_requested_human"
```

Repeating the exact call (same `session_id` and `request_id`) returns the same body quickly with
`Idempotency-Replayed: true` (add `-i` to see headers).

Gateway logic:

```python
r = post("/api/v2/chat", json={"session_id": sid, "request_id": wamid,
         "channel": "whatsapp", "messages": last_40(conv)},
         headers={"Authorization": f"Bearer {KEY}"}, timeout=60)
body = r.json() if r.status_code == 200 else None

if r.status_code in (409, 502, 503, 504) or (body and body["error"] and not body["handoff"]):
    retry_with_backoff()                       # escalate to a human after ~3 tries
elif body["session_capped"]:
    sid = next_session_id(sid); resend_same_message()
else:
    send_whatsapp(body["reply"])
    if body["handoff"]:
        assign_to_salesperson(conv, body["handoff_reason"])
```

### 12.8 Booking and login behaviour

- The bot cannot take payment. After adding to the cart it sends
  `https://www.bucketlistt.com/experiences/cart` for the customer to pay on the website.
- To book, the bot asks for the phone number, sends an OTP through the catalog server, and the
  customer types the code in chat. A bare 4 to 8 digit message is treated as an OTP answer, so
  never filter or rewrite numeric-only messages.
- The login is stored per `session_id` for 30 minutes and survives a server restart (Redis).
  There is no way yet to skip the OTP by passing a verified phone number from the CRM.

### 12.9 Streaming variant: `POST /api/v2/chat/stream`

Same request body, auth and rate limits as `/api/v2/chat`, but the answer arrives as Server-Sent
Events so a gateway can react before the full reply is ready (the website's `/api/chat` works the
same way). Frames, in order:

```
data: {"status": "Checking availability…", "done": false}     (0 or more, while tools run)
data: {"delta": "Nice! ", "done": false}                       (text chunks, raw as the model writes them)
data: {"done": true, "session_id": "...", "reply": "...", "messages": ["..."], "handoff": false,
       "handoff_reason": null, "session_capped": false, "error": null}      (exactly one, last)
```

- **The final frame is the canonical answer.** Its fields are the same as the JSON response of §12.2
  (WhatsApp strikethrough fixed, `messages` split, handoff and cap flags). Deltas are raw, so they
  may still contain `~~`. For WhatsApp, normally ignore deltas and use `status` for a typing
  indicator and the final frame's `messages` for sending.
- **No idempotency.** `request_id` is ignored here; use the JSON endpoint when you need replay
  protection.
- Failures arrive in the final frame (`error` set), never as a dropped stream. 401 and 503 are
  returned as normal HTTP errors before the stream starts.

```bash
curl -N -X POST $BASE_URL/api/v2/chat/stream -H "Authorization: Bearer $CHAT_API_KEY" \
  -H "Content-Type: application/json" \
  -d '{"session_id":"wa-test-1","messages":[{"role":"user","content":"bungee price in Rishikesh?"}]}'
```

If a reply would otherwise be empty with no error (the model returned nothing), both v2 endpoints
now report `error: "empty_reply"` instead of an empty `reply`, so a gateway never sends a blank
message. Treat it like any other `error`: retry.

### 12.10 Test deployment notes (Render free tier)

- The service sleeps after 15 minutes without traffic; the first request afterwards can take
  about a minute. Redis does not persist, so a Redis restart logs customers out.
- Single instance with 512 MB RAM. Not for production, and not for real OTP logins with
  customers' numbers.
- **Cold start:** on the free plan, keep the service awake during test sessions with an external
  pinger on `/api/health` every 10 minutes, or use a paid instance (Starter or above), which
  never sleeps. Production should use a paid instance.
- **Attachments on v2:** `attachment_ids` on the **last** message are read by v2 exactly as in
  §4. The upload endpoint (`/api/chat/attachments`) needs Redis and a ClamAV daemon; the Render
  test blueprint has no ClamAV, so uploads there return 503 (fail closed). Also note that the
  upload endpoint does not require `CHAT_API_KEY`.
- `/docs` and `/openapi.json` are **hidden (404) whenever `CHAT_API_KEY` is set**, unless
  `DOCS_ENABLED=true`. Even when shown, Swagger cannot send the key (no security scheme is
  declared, so no Authorize button). Use curl or Postman.
- If `/api/v2/chat` returns 200 with only the generic "I hit a snag" error, the real cause is in
  the server logs (search for `Chat request failed`).
