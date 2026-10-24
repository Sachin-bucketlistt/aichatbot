"""Per-IP sliding-window rate limiter as a Starlette middleware.

ponytail: in-memory single-process — timestamps live in a dict, so limits reset
on restart and don't sync across workers. Fine for a single uvicorn worker on a
demo/free-tier deploy. Upgrade to a Redis-backed limiter (e.g. slowapi) if you
run multiple workers or need limits to survive restarts.
"""
import hmac
import logging
from collections import defaultdict, deque
from time import monotonic

from starlette.middleware.base import BaseHTTPMiddleware
from starlette.requests import Request
from starlette.responses import JSONResponse

from app.config import settings

logger = logging.getLogger(__name__)

CHAT_LIMIT_PER_MINUTE = settings.chat_limit_per_minute
KEY_LIMIT_PER_MINUTE = settings.chat_key_limit_per_minute
CHAT_PATH = "/api/chat"
CHAT_PATHS = (CHAT_PATH, "/api/v2/chat", "/api/v2/chat/stream")
_KEY_BUCKET = "__valid_key__"
_MAX_TRACKED = 10_000  # sweep idle buckets past this, so spoofed/rotating IPs can't grow memory forever

_hits: dict[str, deque[float]] = defaultdict(deque)


def _client_ip(request: Request) -> str:
    """Client IP for rate limiting. X-Forwarded-For is read from the RIGHT: each
    trusted proxy appends the address it saw, so the left-hand entries are whatever
    the caller typed and can be forged to dodge the limit."""
    hops = settings.trusted_proxy_hops
    forwarded = request.headers.get("x-forwarded-for")
    if forwarded and hops > 0:
        parts = [p.strip() for p in forwarded.split(",") if p.strip()]
        if parts:
            return parts[-hops] if len(parts) >= hops else parts[0]
    return request.client.host if request.client else "unknown"


def has_valid_chat_key(request: Request) -> bool:
    """True if CHAT_API_KEY is configured and the request's Bearer token matches it."""
    if not settings.chat_api_key:
        return False
    supplied = request.headers.get("authorization", "")
    return hmac.compare_digest(supplied.encode(), f"Bearer {settings.chat_api_key}".encode())


def _sweep(now: float) -> None:
    for bucket in [k for k, v in _hits.items() if not v or v[-1] < now - 60.0]:
        del _hits[bucket]


def _over_limit(bucket: str, limit: int) -> bool:
    """Sliding 60 s window. Records the hit and returns False when under the limit."""
    now = monotonic()
    if len(_hits) > _MAX_TRACKED:
        _sweep(now)
    hits = _hits[bucket]
    cutoff = now - 60.0
    while hits and hits[0] < cutoff:
        hits.popleft()
    if len(hits) >= limit:
        return True
    hits.append(now)
    return False


class RateLimitMiddleware(BaseHTTPMiddleware):
    async def dispatch(self, request: Request, call_next):
        if request.url.path not in CHAT_PATHS:
            return await call_next(request)

        if has_valid_chat_key(request):
            # Not per-IP limited (the gateway is one IP), but capped in total to bound LLM spend.
            if KEY_LIMIT_PER_MINUTE and _over_limit(_KEY_BUCKET, KEY_LIMIT_PER_MINUTE):
                logger.warning("Valid-key request ceiling hit (%d per 60s)", KEY_LIMIT_PER_MINUTE)
                return JSONResponse({"detail": "Too many requests. Please slow down."}, status_code=429)
            return await call_next(request)

        ip = _client_ip(request)
        if _over_limit(ip, CHAT_LIMIT_PER_MINUTE):
            logger.warning("Rate limit hit: IP=%s", ip)
            return JSONResponse({"detail": "Too many requests. Please slow down."}, status_code=429)
        return await call_next(request)
