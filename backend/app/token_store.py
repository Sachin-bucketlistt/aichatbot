"""Per-session store for the bucketlistt authToken, so a logged-in user isn't
asked to re-verify an OTP on every turn.

The chat backend is otherwise stateless (the client resends the conversation
each turn), but tool-call results — including the authToken from verify_otp —
are NOT part of that resent history, so without this the token is lost between
turns and the model re-runs send_otp. Here we cache the token server-side keyed
by the client's session_id and reuse it until it expires.

The in-memory dicts below are the fast path; when Redis is configured the same
values are also written through to it (persist_*) and read back on a memory miss
(restore_session_auth), so a restart or a second worker doesn't log customers
out. Redis is best-effort: any failure falls back to memory-only, as before.
The token is never sent to the client — it lives only here (and in Redis, so keep
that private/authenticated) and is injected into authenticated MCP calls server-side.
"""
import logging
import time

from app import session_store

logger = logging.getLogger(__name__)

# session_id -> (authToken, expiry_epoch)
_store: dict[str, tuple[str, float]] = {}

TTL_SECONDS = 30 * 60  # bucketlistt sessions are short-lived; 30 min is safe.

# MCP tools that require an authToken argument.
AUTH_TOOLS = {"add_to_cart", "get_cart", "update_cart_item", "remove_from_cart", "get_my_bookings"}


def set_token(session_id: str | None, token: str | None) -> None:
    if session_id and token:
        _store[session_id] = (token, time.time() + TTL_SECONDS)


def get_token(session_id: str | None) -> str | None:
    if not session_id:
        return None
    entry = _store.get(session_id)
    if not entry:
        return None
    token, expiry = entry
    if time.time() > expiry:
        _store.pop(session_id, None)
        return None
    return token


def extract_token(result_text: str) -> str | None:
    """Pull an authToken out of a verify_otp tool result (shape may vary)."""
    import json

    try:
        data = json.loads(result_text)
    except (ValueError, TypeError):
        return None
    # Check the common locations: top-level or nested under data/result.
    for scope in (data, data.get("data") if isinstance(data, dict) else None,
                  data.get("result") if isinstance(data, dict) else None):
        if isinstance(scope, dict):
            tok = scope.get("authToken") or scope.get("token")
            if isinstance(tok, str) and tok:
                return tok
    return None


# --- verified phone number (real bucketlistt login, not the lead-capture form) --
#
# Captured from send_otp's own call arguments and only attached to the
# session once verify_otp actually succeeds — never before, since an
# unverified phone number typed into send_otp proves nothing (that's the
# whole point of the OTP step; see app/llm.py _execute_tool). Same
# single-process in-memory pattern as _store above, same TTL rationale.

_pending_phone: dict[str, tuple[str, float]] = {}

_PHONE_ARG_KEYS = ("phone", "phoneNumber", "phone_number", "mobile", "mobileNumber", "mobile_number", "msisdn")


def extract_phone(args_json: str | None) -> str | None:
    """Pull a phone number out of a send_otp/verify_otp call's arguments.
    Key name isn't ours to control (comes from the live MCP server's tool
    schema), so scan the common ones instead of hardcoding one.
    """
    import json

    try:
        args = json.loads(args_json) if args_json else {}
    except (ValueError, TypeError):
        return None
    if not isinstance(args, dict):
        return None
    for key in _PHONE_ARG_KEYS:
        val = args.get(key)
        if isinstance(val, str) and val:
            return val
    return None


def set_pending_phone(session_id: str | None, phone: str | None) -> None:
    """Remember the phone number passed to send_otp, in case verify_otp's own
    arguments don't repeat it."""
    if session_id and phone:
        _pending_phone[session_id] = (phone, time.time() + TTL_SECONDS)


def pop_pending_phone(session_id: str | None) -> str | None:
    if not session_id:
        return None
    entry = _pending_phone.pop(session_id, None)
    if not entry:
        return None
    phone, expiry = entry
    return phone if time.time() <= expiry else None


# --- Redis write-through / read-through (see module docstring) ---------------

def _tok_key(session_id: str) -> str:
    return f"auth:token:{session_id}"


def _phone_key(session_id: str) -> str:
    return f"auth:phone:{session_id}"


async def persist_token(session_id: str | None) -> None:
    """Copy this session's in-memory token to Redis, with the remaining TTL."""
    client = session_store.redis_client
    entry = _store.get(session_id) if session_id else None
    if not client or not entry:
        return
    ttl = int(entry[1] - time.time())
    if ttl <= 0:
        return
    try:
        await client.set(_tok_key(session_id), entry[0], ex=ttl)
    except Exception:  # pylint: disable=broad-except
        logger.exception("Could not persist auth token to Redis")


async def persist_pending_phone(session_id: str | None) -> None:
    client = session_store.redis_client
    entry = _pending_phone.get(session_id) if session_id else None
    if not client or not entry:
        return
    ttl = int(entry[1] - time.time())
    if ttl <= 0:
        return
    try:
        await client.set(_phone_key(session_id), entry[0], ex=ttl)
    except Exception:  # pylint: disable=broad-except
        logger.exception("Could not persist pending phone to Redis")


async def forget_pending_phone(session_id: str | None) -> None:
    """Drop the Redis copy once the pending phone has been consumed (or failed)."""
    client = session_store.redis_client
    if not client or not session_id:
        return
    try:
        await client.delete(_phone_key(session_id))
    except Exception:  # pylint: disable=broad-except
        logger.exception("Could not delete pending phone from Redis")


async def restore_session_auth(session_id: str | None) -> None:
    """On a memory miss, reload this session's token / pending phone from Redis
    (after a restart, or when another worker handled the earlier turns).
    Call once at the start of a request, before anything reads get_token()."""
    client = session_store.redis_client
    if not client or not session_id:
        return
    try:
        if get_token(session_id) is None:
            token = await client.get(_tok_key(session_id))
            if token:
                ttl = await client.ttl(_tok_key(session_id))
                _store[session_id] = (token, time.time() + (ttl if ttl and ttl > 0 else TTL_SECONDS))
        if session_id not in _pending_phone:
            phone = await client.get(_phone_key(session_id))
            if phone:
                _pending_phone[session_id] = (phone, time.time() + TTL_SECONDS)
    except Exception:  # pylint: disable=broad-except
        logger.exception("Could not restore auth state from Redis")
