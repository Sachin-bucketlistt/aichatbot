# Temporary free test deployment (Render)

Free, no credit card, about 10 minutes. Not for production: the free web service sleeps after
15 min idle (first request after that takes ~1 min), and the free Redis (25 MB) does not persist.

## Steps (you do these; they need your Render account)
1. Sign up at https://render.com (GitHub login is easiest) and allow it to read `Sachin-bucketlistt/aichatbot`.
2. **New → Blueprint** → pick the repo → choose branch **`feat/crm-v2-chat`** → Apply.
3. When prompted, paste: `ANTHROPIC_API_KEY`, `OPENAI_API_KEY`, `WEAVIATE_URL`, `WEAVIATE_API_KEY`
   (same values as `backend/.env`).
4. Wait for `bucky-chat-test` to show **Live** (first build ~5–8 min).
5. Open the service → **Environment** → copy `CHAT_API_KEY`. Your URL is shown at the top
   (`https://bucky-chat-test-xxxx.onrender.com`).

## Test
```bash
BASE=https://bucky-chat-test-xxxx.onrender.com   # your URL
KEY=<CHAT_API_KEY from the dashboard>
curl -s $BASE/api/health
curl -s -X POST $BASE/api/v2/chat -H "Authorization: Bearer $KEY" -H "Content-Type: application/json" \
  -d '{"session_id":"wa-test-1","request_id":"t1","messages":[{"role":"user","content":"bungee price in Rishikesh?"}]}'
# repeat the exact same call: expect the same reply back fast with header Idempotency-Replayed: true
# (add -i to see headers)
```
Give the CRM developer the URL and key (not the repo secrets).

## Cleanup
Dashboard → each service → Settings → **Delete**. Rotate `CHAT_API_KEY` or delete the service
when the test ends, since anyone with the URL and key can spend your LLM credit.

## Caveats
- The web service is single-instance with 512 MB RAM; the app measured ~254 MB after a real request.
- Redis persistence is off on the free tier, so a Redis restart logs customers out (fine for a test).
- Do not test real OTP logins with customers' numbers on a shared test URL.
