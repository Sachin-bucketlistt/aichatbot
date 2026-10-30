"""Employee-dashboard session summaries.

Runs a backend-only idle-timeout scan (no frontend involvement — see
CLAUDE.md conversation history for why: there is no reliable "chat closed"
signal in a stateless HTTP/SSE API, so idle timeout is the standard proxy
production chat platforms use instead). Every IDLE_SCAN_INTERVAL_SECONDS,
find sessions quiet for IDLE_SUMMARY_MINUTES, ask Claude for a structured
summary of the transcript already sitting in Redis, and append it to
data/session_summaries.json — the same "local JSON file" pattern this repo
already uses for backend/data/leads.json.

Storage is deliberately decoupled from delivery: GET /api/admin/session-
summaries* (see app/main.py) lets a dashboard pull this data today, and
_send_webhook() below pushes each freshly generated summary out too. The
pull endpoint is left in place on purpose as the push side's backstop: a
webhook send that exhausts its retries just logs and moves on (see
_send_webhook's docstring for why), and GET .../session-summaries?since=
lets the receiver backfill anything a failed delivery missed.
"""
import asyncio
import hashlib
import hmac
import json
import logging
import time
from pathlib import Path

from app import session_store
from app.config import settings
from app.handoff import HANDOFF_REASONS
from app.mcp_client import _get_http_client
from app.tools import get_aclient

logger = logging.getLogger(__name__)

SUMMARIES_FILE = Path(__file__).parent.parent / "data" / "session_summaries.json"

SUMMARY_TOOL = {
    "name": "submit_conversation_summary",
    "description": "Submit a structured summary of this chat conversation for the internal sales/support dashboard.",
    "input_schema": {
        "type": "object",
        "properties": {
            "summary": {
                "type": "string",
                "description": "2-4 sentence recap of what the user wanted and how the conversation went.",
            },
            "topics": {
                "type": "array",
                "items": {"type": "string"},
                "description": "Short topic tags, e.g. 'bungee jumping', 'group discount', 'cancellation policy'.",
            },
            "questions_asked": {
                "type": "array",
                "items": {"type": "string"},
                "description": "The user's distinct questions or requests, lightly cleaned up.",
            },
            "sentiment": {
                "type": "string",
                "enum": ["positive", "neutral", "frustrated"],
            },
            "requires_followup": {
                "type": "boolean",
                "description": "True if a human should follow up — unresolved question, clear booking intent, or a complaint.",
            },
            "outcome": {
                "type": "string",
                "enum": ["booked", "lead_captured", "handed_off", "answered", "abandoned"],
                "description": (
                    "How the chat ended: booked (a booking/payment was completed), lead_captured (contact "
                    "details were left), handed_off (passed to a human), answered (user's questions were "
                    "answered, nothing further), abandoned (user dropped off mid-flow or unresolved)."
                ),
            },
            "had_unanswered_question": {
                "type": "boolean",
                "description": "True if the assistant failed to answer something the user asked (said it didn't know, deflected, or gave a non-answer).",
            },
            "handoff_reason": {
                "type": "string",
                "enum": ["none", *HANDOFF_REASONS],
                "description": "Why a human was or should have been brought in; 'none' if no handoff was needed.",
            },
            "tool_issues": {
                "type": "array",
                "items": {"type": "string"},
                "description": "Short notes on any point where the assistant said a lookup, availability check, login or booking step failed or was unavailable. Empty if none.",
            },
        },
        "required": [
            "summary", "topics", "questions_asked", "sentiment", "requires_followup",
            "outcome", "had_unanswered_question", "handoff_reason", "tool_issues",
        ],
    },
}


async def _generate_summary(messages: list[dict]) -> dict:
    """Force a structured summary out of Claude via tool use — see CLAUDE.md
    conversation history for why forced tool_choice beats parsing free text.
    """
    transcript = "\n".join(f"{m.get('role', '?')}: {m.get('content', '')}" for m in messages)
    response = await get_aclient().messages.create(
        model=settings.web_search_model,  # cheap/fast model, same one used for search_web
        max_tokens=900,
        tools=[SUMMARY_TOOL],
        tool_choice={"type": "tool", "name": "submit_conversation_summary"},
        messages=[{
            "role": "user",
            "content": f"Summarize this customer chat for an internal support/sales dashboard:\n\n{transcript}",
        }],
    )
    for block in response.content:
        if block.type == "tool_use":
            return block.input
    return {}


def _ensure_summaries_file() -> None:
    if not SUMMARIES_FILE.exists():
        SUMMARIES_FILE.parent.mkdir(parents=True, exist_ok=True)
        SUMMARIES_FILE.write_text("[]", encoding="utf-8")


def _append_summary(record: dict) -> None:
    _ensure_summaries_file()
    try:
        data = json.loads(SUMMARIES_FILE.read_text(encoding="utf-8"))
    except Exception:
        logger.warning("Could not read %s, starting fresh", SUMMARIES_FILE)
        data = []
    data.append(record)
    SUMMARIES_FILE.write_text(json.dumps(data, indent=2), encoding="utf-8")


_WEBHOOK_TIMEOUT_SECONDS = 5.0
_WEBHOOK_MAX_ATTEMPTS = 3          # 1 try + 2 retries — the pull endpoint is the real backstop
_WEBHOOK_RETRY_STATUSES = {408, 429}  # plus any 5xx; see _send_webhook


def _sign_webhook(body: bytes, timestamp: str) -> str:
    """HMAC-SHA256 over "<timestamp>.<body>", Stripe-style, so a captured
    request can't be replayed indefinitely — the receiver checks both the
    signature and that the timestamp is recent.
    """
    mac = hmac.new(settings.summary_webhook_secret.encode("utf-8"), b"%s.%s" % (timestamp.encode(), body), hashlib.sha256)
    return mac.hexdigest()


async def _send_webhook(record: dict) -> None:
    """Best-effort push of a freshly generated summary to an external system.

    Off entirely if SUMMARY_WEBHOOK_URL isn't set — same convention as every
    other optional feature here. Never raises: the record is already
    durably saved to SUMMARIES_FILE by the time this runs, so a delivery
    failure here must never re-trigger summarization or block
    mark_summarized() — callers can always backfill via
    GET /api/admin/session-summaries?since=.

    Retries a bounded number of times, with backoff, on transient failures
    (connection errors, timeouts, 408/429, any 5xx). Does not retry on other
    4xx — that means the request itself is wrong (bad URL, auth, payload),
    and retrying it just fails again identically.
    """
    if not settings.summary_webhook_url:
        return

    body = json.dumps(record).encode("utf-8")
    client = _get_http_client()
    delay = 1.0
    for attempt in range(1, _WEBHOOK_MAX_ATTEMPTS + 1):
        timestamp = str(int(time.time()))
        headers = {"Content-Type": "application/json"}
        if settings.summary_webhook_secret:
            headers["X-Webhook-Signature"] = f"t={timestamp},v1={_sign_webhook(body, timestamp)}"
        try:
            resp = await client.post(
                settings.summary_webhook_url,
                content=body,
                headers=headers,
                timeout=_WEBHOOK_TIMEOUT_SECONDS,
            )
            if resp.status_code < 300:
                return
            if resp.status_code not in _WEBHOOK_RETRY_STATUSES and resp.status_code < 500:
                logger.warning(
                    "Webhook rejected summary for %s: HTTP %d (not retrying)",
                    record.get("session_id"), resp.status_code,
                )
                return
            logger.warning(
                "Webhook attempt %d/%d for %s failed: HTTP %d",
                attempt, _WEBHOOK_MAX_ATTEMPTS, record.get("session_id"), resp.status_code,
            )
            retry_after = resp.headers.get("retry-after")
            if retry_after:
                try:
                    delay = max(delay, float(retry_after))
                except ValueError:
                    pass
        except Exception as e:
            # Broad on purpose: a webhook delivery failure (network error,
            # bad URL, whatever) must never propagate into the idle-scan
            # loop and be mistaken for a summarization failure.
            logger.warning(
                "Webhook attempt %d/%d for %s failed: %s",
                attempt, _WEBHOOK_MAX_ATTEMPTS, record.get("session_id"), e,
            )
        if attempt < _WEBHOOK_MAX_ATTEMPTS:
            await asyncio.sleep(delay)
            delay *= 2
    logger.error(
        "Webhook delivery permanently failed for session %s after %d attempts — "
        "still available via GET /api/admin/session-summaries",
        record.get("session_id"), _WEBHOOK_MAX_ATTEMPTS,
    )


def list_summaries(since: str | None = None) -> list[dict]:
    """All stored summaries, optionally filtered to ended_at >= since (ISO 8601)."""
    _ensure_summaries_file()
    try:
        data = json.loads(SUMMARIES_FILE.read_text(encoding="utf-8"))
    except Exception:
        return []
    if since:
        data = [r for r in data if r.get("ended_at", "") >= since]
    return data


def get_summary(session_id: str) -> dict | None:
    for record in list_summaries():
        if record.get("session_id") == session_id:
            return record
    return None


async def summarize_idle_sessions() -> int:
    """One scan pass: summarize every session idle past the threshold.
    Returns how many were summarized (used by tests / logging).
    """
    idle_ids = await session_store.find_idle_sessions(settings.idle_summary_minutes * 60)
    for session_id in idle_ids:
        try:
            data = await session_store.get_session_data(session_id)
            if not data or not data.get("messages"):
                await session_store.mark_summarized(session_id)
                continue

            fields = await _generate_summary(data["messages"])
            record = {
                "session_id": session_id,
                "ended_at": time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime(data["last_activity"])),
                "message_count": data["message_count"],
                "user_info": data.get("user_info"),
                "verified_phone": data.get("verified_phone"),
                **fields,
            }
            _append_summary(record)
            await _send_webhook(record)
            await session_store.mark_summarized(session_id)
            logger.info("Summarized idle session %s (%d messages)", session_id, data["message_count"])
        except Exception:
            logger.exception("Failed to summarize session %s — will retry next scan", session_id)
            # Deliberately not marked summarized on failure, so the next
            # scan pass retries it instead of silently dropping the lead.
    return len(idle_ids)


async def idle_scan_loop() -> None:
    """Background loop started from main.py's lifespan. No-ops quietly if
    Redis isn't configured (find_idle_sessions returns []) — same
    "off if unconfigured" convention as every other optional feature here.
    """
    while True:
        try:
            await asyncio.sleep(settings.idle_scan_interval_seconds)
            await summarize_idle_sessions()
            _sweep_expired_attachments()
        except asyncio.CancelledError:
            break
        except Exception:
            logger.exception("Idle session scan pass failed")


def _sweep_expired_attachments() -> None:
    """Delete on-disk attachment files older than the attachment TTL.

    Redis's own TTL already expires the small metadata key pointing at each
    file (see app/attachments.py); this removes the orphaned bytes on disk
    that metadata key used to point at, since Redis expiry doesn't touch the
    filesystem. Reuses this loop instead of a second background task.
    """
    from app.attachments import _ATTACHMENT_TTL_SECONDS

    directory = Path(settings.attachments_dir)
    if not directory.exists():
        return
    cutoff = time.time() - _ATTACHMENT_TTL_SECONDS
    for f in directory.iterdir():
        if f.is_file() and f.stat().st_mtime < cutoff:
            try:
                f.unlink(missing_ok=True)
            except OSError:
                logger.exception("Failed to remove expired attachment file %s", f)
