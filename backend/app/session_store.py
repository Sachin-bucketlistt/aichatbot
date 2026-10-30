import hashlib
import hmac
import json
import logging
import secrets
import time
from typing import Any

from redis.asyncio import Redis, from_url

from app.config import settings

logger = logging.getLogger(__name__)

# Used when LOG_HASH_KEY is unset. ponytail: ids then correlate only within one process run; set
# LOG_HASH_KEY to keep them stable across restarts and workers.
_FALLBACK_LOG_KEY = secrets.token_bytes(32)


def mask_session_id(session_id: str | None) -> str:
    """Log-safe session id: a keyed hash (HMAC-SHA256, 12 hex chars). WhatsApp ids are wa-<phone>-<n>, so
    nothing of the phone number is kept, and the key stops anyone reversing it by trying every number
    (a plain hash would fall to that). The same id always maps to the same value, so one chat can still
    be followed through the logs. Redis keys keep the real id; logs and alert emails use this."""
    if not session_id:
        return ""
    key = settings.log_hash_key.encode() or _FALLBACK_LOG_KEY
    return "sid-" + hmac.new(key, session_id.encode(), hashlib.sha256).hexdigest()[:12]


# Global redis connection pool
redis_client: Redis | None = None

async def init_redis():
    """Initialize the Redis connection pool."""
    global redis_client
    if settings.redis_url:
        try:
            redis_client = from_url(settings.redis_url, decode_responses=True)
            await redis_client.ping()
            logger.info(f"Connected to Redis at {settings.redis_url}")
        except Exception as e:
            logger.error(f"Failed to connect to Redis: {e}")
            from app.notifier import send_critical_alert
            import asyncio
            asyncio.create_task(send_critical_alert("redis_down", str(e), "Failed to initialize Redis connection pool on startup"))
            redis_client = None

async def close_redis():
    """Close the Redis connection pool."""
    global redis_client
    if redis_client:
        await redis_client.aclose()
        redis_client = None

async def save_turn(session_id: str, user_msg: str, assistant_msg: str) -> None:
    """Persist a conversation turn to Redis and increment the message count.
    Refreshes the TTL on the session.
    """
    if not redis_client or not session_id:
        return

    key = f"session:{session_id}"
    try:
        # Load existing session or create new
        session_data = await redis_client.hgetall(key)
        
        # Parse existing messages or start empty list
        messages = []
        if session_data and "messages" in session_data:
            try:
                messages = json.loads(session_data["messages"])
            except json.JSONDecodeError:
                pass
                
        # Append the new turn
        messages.append({"role": "user", "content": user_msg})
        messages.append({"role": "assistant", "content": assistant_msg})
        
        # Calculate new message count
        count = int(session_data.get("message_count", 0)) + 1 if session_data else 1
        
        # Build update dictionary
        update = {
            "messages": json.dumps(messages),
            "message_count": str(count),
            "last_activity": str(time.time()),
        }
        
        # Write to redis and set TTL
        await redis_client.hset(key, mapping=update)
        await redis_client.expire(key, settings.session_ttl_seconds)
        logger.info(f"Saved turn for session {mask_session_id(session_id)} (count: {count})")
    except Exception as e:
        logger.error(f"Failed to save turn to Redis for session {mask_session_id(session_id)}: {e}")
        from app.notifier import send_critical_alert
        import asyncio
        asyncio.create_task(send_critical_alert("redis_down", str(e), f"Failed to save turn for session {mask_session_id(session_id)}"))

async def get_message_count(session_id: str) -> int:
    """Get the number of user messages in this session."""
    if not redis_client or not session_id:
        return 0
        
    try:
        count = await redis_client.hget(f"session:{session_id}", "message_count")
        return int(count) if count else 0
    except Exception:
        return 0

async def _prompt_eligible(session_id: str, min_count: int) -> bool:
    """Shared guard: min_count turns already saved, not yet prompted, no user_info."""
    if not redis_client or not session_id:
        return False
    try:
        session_data = await redis_client.hgetall(f"session:{session_id}")
        if not session_data:
            return False
        count = int(session_data.get("message_count", 0))
        prompted = session_data.get("login_prompted", "false") == "true"
        has_info = "user_info" in session_data
        return count >= min_count and not prompted and not has_info
    except Exception as e:
        logger.error(f"Failed to check prompt eligibility for session {mask_session_id(session_id)}: {e}")
        return False


async def should_prompt_login(session_id: str) -> bool:
    """True once LOGIN_PROMPT_AFTER messages are already saved. Drives the
    legacy `prompt_login` SSE flag for frontends with dedicated UI for it.
    """
    return await _prompt_eligible(session_id, settings.login_prompt_after)


async def should_nudge_for_contact(session_id: str) -> bool:
    """True one turn earlier than should_prompt_login — checked BEFORE the
    Nth response is generated, so the assistant's own reply can ask for
    contact info as ordinary chat text. This is what makes the feature work
    with any frontend: no special UI or flag-handling required, since the
    ask (and, via escalate_and_capture_lead, the capture) both ride inside
    the normal chat stream.
    """
    return await _prompt_eligible(session_id, settings.login_prompt_after - 1)

async def mark_login_prompted(session_id: str) -> None:
    """Mark that the login prompt has been shown so it doesn't re-trigger."""
    if not redis_client or not session_id:
        return
        
    try:
        key = f"session:{session_id}"
        await redis_client.hset(key, "login_prompted", "true")
        await redis_client.expire(key, settings.session_ttl_seconds)
    except Exception as e:
        logger.error(f"Failed to mark login prompted: {e}")

async def save_user_info(session_id: str, name: str, phone: str, email: str) -> None:
    """Store the captured user info in the session."""
    if not redis_client or not session_id:
        return

    try:
        key = f"session:{session_id}"
        user_info = json.dumps({"name": name, "phone": phone, "email": email})
        await redis_client.hset(key, "user_info", user_info)
        await redis_client.expire(key, settings.session_ttl_seconds)
        logger.info(f"Saved user info for session {mask_session_id(session_id)}")
    except Exception as e:
        logger.error(f"Failed to save user info: {e}")


async def save_verified_phone(session_id: str, phone: str) -> None:
    """Store the phone number from a *completed* real bucketlistt OTP login
    (send_otp + successful verify_otp — see app/token_store.py). Kept
    separate from user_info (the lead-capture form) since this one carries a
    stronger guarantee: it only exists if the user actually proved they
    hold that phone.
    """
    if not redis_client or not session_id:
        return
    try:
        key = f"session:{session_id}"
        await redis_client.hset(key, "verified_phone", phone)
        await redis_client.expire(key, settings.session_ttl_seconds)
        logger.info(f"Saved verified phone for session {mask_session_id(session_id)}")
    except Exception as e:
        logger.error(f"Failed to save verified phone: {e}")


async def save_region(session_id: str, region: str) -> None:
    """Remember the first-message state tag so the reply language survives a trimmed history."""
    if not redis_client or not session_id:
        return
    try:
        key = f"session:{session_id}"
        await redis_client.hset(key, "region", region)
        await redis_client.expire(key, settings.session_ttl_seconds)
    except Exception as e:
        logger.error(f"Failed to save region: {e}")


async def get_region(session_id: str) -> str | None:
    if not redis_client or not session_id:
        return None
    try:
        return await redis_client.hget(f"session:{session_id}", "region")
    except Exception as e:
        logger.error(f"Failed to read region: {e}")
        return None


async def find_idle_sessions(idle_seconds: int) -> list[str]:
    """Session ids idle >= idle_seconds, with at least one turn, not yet
    summarized. Used by the dashboard idle-scan loop (see app/dashboard.py)
    to find conversations to summarize once the user's gone quiet.

    ponytail: SCAN over every session:* key once per poll interval — fine at
    this traffic level (a handful of concurrent sessions). If this ever runs
    against thousands of live sessions, keep a Redis SET of active session
    ids (added in save_turn, removed here) instead of scanning the keyspace.
    """
    if not redis_client:
        return []
    idle_ids = []
    now = time.time()
    try:
        async for key in redis_client.scan_iter(match="session:*"):
            data = await redis_client.hgetall(key)
            if not data or data.get("summarized") == "true":
                continue
            if int(data.get("message_count", 0)) < 1:
                continue
            last_activity = float(data.get("last_activity", 0) or 0)
            if last_activity and (now - last_activity) >= idle_seconds:
                idle_ids.append(key.split("session:", 1)[1])
    except Exception as e:
        logger.error(f"Failed to scan for idle sessions: {e}")
    return idle_ids


async def get_session_data(session_id: str) -> dict[str, Any]:
    """Raw session record for summarization: parsed messages/user_info,
    plus message_count and last_activity. Empty dict if not found.
    """
    if not redis_client or not session_id:
        return {}
    try:
        data = await redis_client.hgetall(f"session:{session_id}")
        if not data:
            return {}
        return {
            "messages": json.loads(data.get("messages", "[]")),
            "user_info": json.loads(data["user_info"]) if data.get("user_info") else None,
            "verified_phone": data.get("verified_phone"),
            "message_count": int(data.get("message_count", 0)),
            "last_activity": float(data.get("last_activity", 0) or 0),
        }
    except Exception as e:
        logger.error(f"Failed to load session data for {mask_session_id(session_id)}: {e}")
        return {}


async def mark_summarized(session_id: str) -> None:
    """Mark a session as summarized so the idle scan doesn't reprocess it."""
    if not redis_client or not session_id:
        return
    try:
        await redis_client.hset(f"session:{session_id}", "summarized", "true")
        logger.info(f"Marked session {mask_session_id(session_id)} as summarized")
    except Exception as e:
        logger.error(f"Failed to mark session {mask_session_id(session_id)} summarized: {e}")
