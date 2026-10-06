# Bucky chat API for the bucketlistt CRM

Reply to your integration note. Everything in §2–§3 is built and tested on our side. Your
original request is mapped item by item in §8.

## 1. Overview

The CRM stays the WhatsApp gateway. For every inbound customer message, call Bucky once and
send the returned `reply` through your outbound queue. Bucky never talks to WhatsApp.

```
Customer ─► Meta ─► CRM ─► POST /api/v2/chat (Bucky) ─► JSON reply ─► CRM queue ─► Customer
                      └── if handoff=true: route the conversation to a salesperson
```

## 2. The endpoint

`POST {BASE_URL}/api/v2/chat` — a dedicated endpoint for the CRM. (The existing `/api/chat`
SSE endpoint is for our website and is not what you should use.)

- **Staging URL / production URL / API key:** supplied separately by us. The key is a shared
  secret; keep it server-side in the CRM worker only. We can rotate it on request.
- **Headers:** `Authorization: Bearer <CHAT_API_KEY>`, `Content-Type: application/json`

### Request

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

| Field | Required | Notes |
|---|---|---|
| `messages` | yes | 1–40 items, oldest first, last one is the customer's new message. `role` is `user` or `assistant` (agent replies are `assistant`). `content` 1–8000 chars. Send the customer's text unchanged (OTP codes and phone numbers included). |
| `session_id` | **yes in practice** | Your `wa-<E.164>-<n>`. All server state hangs off it (login, message count, replay). |
| `request_id` | recommended | Send the WhatsApp message id. Enables safe retries (§4). |
| `channel` | no | Defaults to `whatsapp` on this endpoint. Applies WhatsApp formatting rules (no tables/headings/links, `*bold*`, bare URLs, short replies). |

Unknown extra fields are ignored.

### Response (HTTP 200)

```json
{
  "session_id": "wa-919876543210-1",
  "reply": "Bungee in Rishikesh starts at ₹3,550 ...",
  "handoff": false,
  "handoff_reason": null,
  "session_capped": false,
  "error": null
}
```

| Field | Meaning |
|---|---|
| `reply` | Text to send to the customer. Already WhatsApp-formatted. May be empty if `error` is set. Typically under ~1000 chars, but you should still split at 4096. |
| `handoff` | `true` → put the conversation in a salesperson's queue and stop calling Bucky until an agent hands it back. |
| `handoff_reason` | One of `customer_requested_human`, `cannot_answer`, `complaint`, `payment_issue`, `booking_change`, `other`. **Advisory**: set by the model or a keyword check, so treat as a hint, not a classification. |
| `session_capped` | `true` → this session hit its message limit. Do **not** show `reply`. Rotate `session_id` (e.g. `-1` → `-2`) and resend the same customer message. |
| `error` | Non-null means the request failed. See §5. |

If `handoff` is true, `reply` is the message to deliver to the customer ("connecting you to our
team…"); we always provide one.

## 3. Behaviour you should know

- **Stateless history.** Bucky only sees the `messages` you send, so always send the recent
  history. Human-agent replies should be sent as `assistant`; Bucky is told they may not be its own.
- **Login (OTP) for bookings.** When a customer wants to book, Bucky asks for their phone
  number, sends an OTP, and the customer types the code in chat. The login is kept
  server-side per `session_id` for 30 minutes (survives our restarts) and is applied to cart
  and booking calls automatically. Rotating `session_id` mid-booking means the customer must
  log in again, so rotate only when `session_capped` is true.
- **Message cap.** Counted per `session_id`. Default 15 user messages; the CRM instance will
  be configured higher (proposed: 40; tell us your preferred value).
- **Session lifetime.** Conversation state expires after 2 h of inactivity.
- **Latency.** Typically a few seconds; can reach 20–40 s when Bucky looks up live
  availability. Use your 60 s timeout. One request in flight per conversation, as you planned.
- **Rate limiting.** Requests with a valid key are not limited per IP. Requests with a wrong
  key are (429).

## 4. Retries and idempotency

Send the WhatsApp message id as `request_id`. For the same `session_id` + `request_id`
(within 10 minutes):

| Situation | What you get |
|---|---|
| First time | Normal response |
| Repeat after a completed request | The stored response, header `Idempotency-Replayed: true`, **no tools re-run** (no duplicate cart items) |
| Repeat while the first is still running | `409` — wait a few seconds and retry |
| Repeat after a request that returned `error` | Runs again (errors are never stored) |

Retry only on network failure, 5xx, 409, or a response with `error`. Back off (e.g. 2 s, 5 s,
10 s) and stop after ~3 tries, then hand the conversation to a human.

## 5. Errors

| HTTP | Meaning | Action |
|---|---|---|
| 200 + `error` set | Failure inside Bucky or the LLM provider (overloaded, rate-limited, hiccup). `reply` is usually empty. | Retry with backoff. **Never forward `error` text to the customer.** If `handoff` is also true, deliver `reply` and hand off. |
| 401 | Missing/wrong key | Fix configuration |
| 409 | Same `request_id` in flight | Retry shortly |
| 422 | Invalid body (empty content, >40 messages, content >8000 chars) | Fix request |
| 429 | Too many requests with a wrong key | Fix key |
| 503 | `CHAT_API_KEY` not configured on this instance | Tell us |

## 6. Suggested CRM pseudo-code

```python
resp = post("/api/v2/chat", json={
    "session_id": conv.session_id, "request_id": wamid, "channel": "whatsapp",
    "messages": last_40(conv)}, headers=AUTH, timeout=60)

if resp.status in (409, 502, 503, 504) or (resp.status == 200 and resp.json["error"] and not resp.json["handoff"]):
    retry_with_backoff()                       # then escalate to a human after ~3 tries
elif resp.json["session_capped"]:
    conv.session_id = next_session_id(conv)    # wa-<phone>-<n+1>
    resend_same_message()                      # customer never sees the cap
else:
    queue_whatsapp_reply(conv, resp.json["reply"])
    if resp.json["handoff"]:
        assign_to_salesperson(conv, reason=resp.json["handoff_reason"])
```

## 7. Quick test

```bash
curl -s -X POST "$BASE_URL/api/v2/chat" \
  -H "Authorization: Bearer $CHAT_API_KEY" -H "Content-Type: application/json" \
  -d '{"session_id":"wa-test-1","request_id":"t1","messages":[{"role":"user","content":"I want to talk to a human"}]}'
# expect: handoff=true, handoff_reason="customer_requested_human"
```

A good end-to-end check on staging: ask a question → start a booking and complete the OTP →
add to cart → say "human please" → resend the same `request_id` (expect a replay).

## 8. Your requests, item by item

| Your item | Status |
|---|---|
| 2.1 Auth | Done: Bearer key on `/api/v2/chat` |
| 2.2 Rate limit | Done: valid-key calls are not IP-limited |
| 2.3 Handoff signal | Done: `handoff` + `handoff_reason`. "Create support ticket" is replaced by handoff on this endpoint |
| 2.4 `session_capped` + higher cap | Done; cap value set per instance |
| 2.5 Login state across restarts | Done for OTP logins (stored in Redis). The in-memory rate limiter still resets on restart; no customer impact |
| 3.1 WhatsApp formatting | Done via `channel` (default `whatsapp`) |
| 3.2 Idempotency | Done via `request_id` (§4) |
| 3.3 `verified_phone` (skip OTP) | **Not built.** Needs a trusted-login endpoint on the bucketlistt backend; until then customers do the OTP in chat. Worth a separate discussion |
| §4 Intent/sentiment | Not built. Send your value list if you still want it; idle-session summaries as CRM notes can follow later |

**Your questions:** (1) see above; (2) staging URL to follow, separate from production;
(3) field names accepted as proposed; (4) LLM concurrency limits depend on our provider tier;
we will share measured numbers after a load test, until then keep CRM concurrency modest
(a handful of simultaneous conversations) and retry on `error` with backoff.

## 9. What we need from you

1. Your preferred message cap.
2. Confirmation that you will rotate `session_id` only on `session_capped`.
3. Your intent/sentiment value list if you want those labels.
4. Your outbound IPs if you want us to allow-list them additionally.
