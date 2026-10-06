# Bucky ↔ WhatsApp CRM: what's built, what's not, and the production architecture

Response to the CRM team's integration note. Companion to [CHAT_API.md](CHAT_API.md).

## 0. Update: `POST /api/v2/chat` (CRM endpoint) is built

Same brain as `/api/chat`, separate contract, so the web chat is untouched. Always requires `Authorization: Bearer <CHAT_API_KEY>` (503 if the key is unset, 401 if wrong; wrong keys are rate-limited).

```http
POST /api/v2/chat
{ "session_id": "wa-919876543210-1", "request_id": "wamid.HBgM...",
  "channel": "whatsapp", "messages": [ {"role": "user", "content": "..."} ] }
```
```json
{ "session_id": "wa-919876543210-1", "reply": "...", "handoff": false,
  "handoff_reason": null, "session_capped": false, "error": null }
```

- **Handoff (2.3): done.** The model gets a `request_human_handoff(reason)` tool (v2 only). A keyword backstop on the customer's message also sets `handoff` if the model forgets. `handoff_reason` is advisory. If a handoff has no text, `reply` is a short "connecting you to our team" line. Not triggered when `session_capped`. `escalate_and_capture_lead` still exists for lead capture but is told not to be used for human requests.
- **Idempotency (3.2): done.** Same `session_id` + `request_id` within 10 min returns the stored result with header `Idempotency-Replayed: true`. A duplicate while the first is still running gets `409` (retry shortly). Errors are never stored, so a retry re-runs. Without Redis it is skipped, never failing the request.
- **Errors are in-band:** HTTP 200 with `error` set and often an empty `reply`. The CRM should retry with backoff and must not send `error` text to the customer.
- `channel` defaults to `whatsapp` on v2. The web chat never offers the handoff tool (tested).

- **Login tokens in Redis (2.5, tokens part): done.** The OTP login token and the pending phone are written through to Redis (`auth:token:<session_id>`, 30-min TTL) and reloaded on a memory miss at the start of each request. A restart or a second worker no longer logs customers out. Redis down = falls back to memory-only. The token is stored in plain text, so keep Redis private and password-protected. The per-IP rate limiter is still in memory (resets on restart; low impact).

Still NOT built: OTP skip (3.3), intent/sentiment (§4), Redis rate limiter. See §2 below (handoff and `request_id` entries there are now superseded by this section).

---

## 1. What is implemented (all opt-in; web chat unchanged)

| CRM item | Status | How it works |
|---|---|---|
| 2.1 Auth | **Done, opt-in** | Set `CHAT_API_KEY` → `/api/chat` and `/api/session/user-info` need `Authorization: Bearer <key>` (constant-time compare, 401 otherwise). Unset → open exactly as today, because the browser frontend cannot hold a secret. |
| 2.2 Rate limit | **Done** | Valid-key requests skip the per-IP limit. `CHAT_LIMIT_PER_MINUTE` (default 20) now configurable. Requests with a *wrong* key are still limited, so key-guessing is throttled. |
| 2.4 Cap signal | **Done** | The cap reply's final frame is `{"delta":"","done":true,"session_capped":true}`. The cap itself was already `MAX_MESSAGES_PER_SESSION` (env). |
| 3.1 WhatsApp formatting | **Done** | `"channel": "whatsapp"` appends a formatting-override block (no tables/headings/links, `*bold*`, bare URLs, <~1000 chars) to the *dynamic* part of the system prompt. The cached static prompt is untouched, so web prompt-caching is unaffected. Unknown/absent channel = web behaviour. |
| 3.2 `request_id` | **Field accepted, ignored** | Prevents 422s if the CRM sends it. No dedupe yet (see §2). |

Recommended deployment: a **second instance** for the CRM with `CHAT_API_KEY` set and `MAX_MESSAGES_PER_SESSION` raised (e.g. 40). The public web instance keeps the key unset. Config is per-process env, so this needs no further code.

Tests: `backend/tests/test_chat_auth.py` (6 new). Suite: 134 backend + 5 frontend passing. Two existing cap tests were updated for the new field.

## 2. What is NOT built, why, the risk, and the fix

### 2.3 Handoff signal — highest priority gap
- **Why not done:** `stream_chat_response` treats every tuple from the tool loop as a text delta (`llm.py`, `_, delta = event`). A new event type must be handled *before* that or it would leak into the customer's text. The escalation tool (`escalate_and_capture_lead`) also has no reason argument and writes to local `data/leads.json`, not to the CRM. Doing it right touches the core loop, which I would not ship without your review.
- **Risk if left:** the CRM cannot route complaints, payment issues or "get me a human" to sales. The bot will say "connecting you to our team" and nothing will happen. This is the main business risk.
- **Fix:** (1) add a `handoff` tool with an enum `reason`; (2) emit a distinct `("handoff", reason)` event, handled before the delta branch; (3) put `handoff`/`handoff_reason` on the final frame; (4) for `channel == "whatsapp"` make `escalate_and_capture_lead` set the flag instead of writing leads.json; (5) add a deterministic backstop: regex on the customer's message ("human", "agent", "refund", "complaint") sets `handoff` even if the model doesn't call the tool, since model-chosen reasons are best-effort. Tell the CRM `handoff_reason` is advisory.
- **Interim:** CRM can keyword-match customer messages itself.

### 2.5 Login tokens and rate limiter in process memory
- **Why not done:** `token_store` functions are synchronous and called from sync code; moving to Redis changes callers and tests.
- **Risk:** every deploy/restart (CI/CD restarts the single uvicorn) logs out all customers mid-checkout. A second worker would silently break logins (token cached in worker A, request lands on B) — never run `--workers >1` until fixed. Rate limiter resets on restart (low impact).
- **Fix:** store `authToken` and pending phone in Redis hashes with a 30-min TTL (reuse `session_store.redis_client`), make the functions async, and fall back to memory when Redis is down. Keep tokens out of logs. Rate limiter: Redis sliding window or `INCR`+`EXPIRE`, fail-open on Redis errors.
- **Interim:** deploy off-hours, keep one worker.

### 3.2 Idempotency (`request_id`)
- **Why not done:** the reply is a stream, so we must buffer and store frames, and handle two identical requests arriving concurrently. A bug here could replay a wrong or failed reply.
- **Risk:** a CRM timeout + retry after Bucky already ran a cart action runs it twice (duplicate cart item). Cart add is the only mutating tool, so impact is bounded.
- **Fix:** standard pattern: `SET idem:{session}:{request_id} pending NX EX 600`; loser gets 409 (CRM retries, then gets the replay); on success store the concatenated frames and mark `done`; never cache `error` frames; hash the payload so a reused id with a different body is rejected ([Redis idempotency pattern](https://redis.io/blog/what-is-idempotency-in-redis/), [SET NX in-flight handling](https://www.mindbowser.com/designing-idempotent-apis-distributed-locks-redis/)).
- **Cheaper interim:** the CRM already serialises one request per conversation and de-dupes inbound wamids, so retries mostly occur only on its own 60 s timeout. Make the CRM retry only on network failure, not on a slow-but-successful response.

### 3.3 `verified_phone` (OTP skip)
- **Why not done:** Bucky cannot mint a bucketlistt auth token; it only receives one from the MCP `verify_otp`. This needs a trusted-service login on the bucketlistt backend.
- **Risk of doing it naively:** a leaked `CHAT_API_KEY` would let anyone log in as any phone number — account takeover. Do not build on Bucky's side alone.
- **Fix:** bucketlistt exposes a service-authenticated endpoint ("issue token for phone X", scoped, audited, rate-limited, separate key from `CHAT_API_KEY`); Bucky injects the returned token like today.

### §4 Intent / sentiment, summaries to CRM
- **Not done:** needs an extra model call or structured output per turn (latency + cost). The idle-summary job already produces sentiment/topics; add a CRM webhook target (reuse `SUMMARY_WEBHOOK_URL`) for the note. Low risk, do after launch.

### Other gaps worth knowing
- **Attachments endpoint** is unauthenticated (out of scope per you, unused by the frontend). If the CRM never calls it, consider disabling it on the CRM instance.
- **Guessable session IDs:** `wa-<phone>-<n>` are guessable. With the key enabled only the CRM can use them; do not expose the CRM instance without it.
- **`leads.json`** is an unlocked local file; concurrent escalations can lose writes. Fine at low volume; replace with the handoff flow.
- **`X-Forwarded-For`** is trusted for rate-limiting, so keyless callers can evade it. Terminate at a proxy that overwrites the header.
- **Agent messages in history:** replies by human agents arrive as `assistant`, so the bot may repeat or contradict an agent. Add a system note: "earlier assistant messages may come from human staff".
- **WhatsApp 24-hour window:** free-form replies are only allowed within 24 h of the customer's last message; the CRM owns this, Bucky should not assume it can follow up.
- **Replies >4096 chars** are the CRM's to split; the WhatsApp prompt asks for ~1000 chars to make that rare.

## 3. Production architecture

```
Customer ─► Meta Cloud API ──webhook──► CRM ingress (verify X-Hub-Signature-256, ack 200 fast)
                                           │ enqueue
                                           ▼
                              Queue (Redis/SQS) ──► CRM bot worker (1 job per conversation)
                                           │  POST /api/chat  Bearer CHAT_API_KEY, channel=whatsapp
                                           ▼
                         Load balancer ─► Bucky-CRM instances (stateless, ≥2)
                                           │            │             │
                                           ▼            ▼             ▼
                                      Redis (sessions, tokens,   Weaviate    LLM provider
                                      idempotency, rate limits)  (RAG)       + bucketlistt MCP
                                           ▲
 CRM outbound queue ◄── reply text / handoff flag ◄─┘     Sales inbox ◄── handoff → human agent
```

Principles, each backed by the research below:
1. **Ack fast, work async.** Meta expects a 2xx within seconds and retries for days; delivery is at-least-once, so duplicates are normal. De-dupe on the wamid in Redis ([Hookdeck](https://hookdeck.com/webhooks/platforms/guide-to-whatsapp-webhooks-features-and-best-practices), [DEV: resilient handler](https://dev.to/lucas_ventavele/building-a-resilient-whatsapp-cloud-api-webhook-handler-in-nodejs-14lj)). This is the CRM's side and is already planned.
2. **Separate instances per channel.** Web and WhatsApp differ in auth, prompt, cap and limits; separate deployments isolate blast radius and let you scale and rotate keys independently.
3. **Make Bucky stateless.** Move tokens, rate limits and idempotency to Redis (§2.5, §3.2) so you can run several instances behind a load balancer and deploy without logging users out. Use managed Redis with persistence.
4. **One request in flight per conversation** (CRM enforces) so history order stays correct.
5. **Plan for LLM limits.** Anthropic limits are per organisation, in requests, input tokens and output tokens per minute; a 429 carries `retry-after` ([Anthropic limits overview](https://standardcompute.com/rate-limits/anthropic), [429 handling](https://devopsboys.com/blog/llm-rate-limiting-retry-production-2026)). Your prompt is large (KB + tools), so **input tokens per minute, not requests, is the likely ceiling**; prompt caching (already used) helps. Check your tier, run a load test, and keep the CRM retrying 429s with backoff and jitter. Bucky already surfaces upstream 429 as an in-band friendly error and an alert email; the CRM should treat that `error` text as retryable, not send it to the customer.
6. **Formatting is a prompt concern.** WhatsApp supports `*bold*`, `_italic_`, `~strike~`, monospace and lists, but not Markdown tables, headings or `[text](url)` ([SendPulse guide](https://sendpulse.com/blog/whatsapp-text-formatting)). Done via `channel`; the CRM can additionally strip any stray `**`/`#` defensively.
7. **Observability.** Log `session_id`, `request_id`, latency per request; alert on 401 spikes, 429s, error frames, handoff rate. Existing SMTP alerts cover LLM outages and credits.
8. **Secrets.** `CHAT_API_KEY` only in the CRM worker's secret store, rotate on a schedule; support two valid keys during rotation (small follow-up: accept a comma-separated list).

## 4. Suggested rollout
1. Deploy the CRM instance with `CHAT_API_KEY`, `MAX_MESSAGES_PER_SESSION=40`; give the CRM a staging URL (answers their Q2).
2. CRM builds against the mocked stream using the contract in §1 (final frame may carry `session_capped`; unknown fields ignored).
3. Build handoff (§2.3) before go-live; it is the one feature the sales workflow depends on.
4. Move tokens/limits to Redis and add idempotency before scaling past one instance or heavy traffic.
5. Load-test LLM limits; answer their Q4 with real numbers.
6. Design the trusted-login endpoint with the bucketlistt backend team; ship OTP-skip last.

**Answers to their questions:** (1) §2.1, 2.2, 2.4 and formatting are ready now; handoff and Redis state are next. (2) Yes, as a separate instance. (3) Field names fine. (4) Depends on your Anthropic tier; to be measured.
