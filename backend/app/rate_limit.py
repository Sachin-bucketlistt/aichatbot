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
CHAT_PATH = "/api/chat"
CHAT_PATHS = (CHAT_PATH, "/api/v2/chat")

_hits: dict[str, deque[float]] = defaultdict(deque)


def _client_ip(request: Request) -> str:
    forwarded = request.headers.get("x-forwarded-for")
    if forwarded:
        return forwarded.split(",")[0].strip()
    return request.client.host if request.client else "unknown"


def has_valid_chat_key(request: Request) -> bool:
    """True if CHAT_API_KEY is configured and the request's Bearer token matches it."""
    if not settings.chat_api_key:
        return False
    supplied = request.headers.get("authorization", "")
    return hmac.compare_digest(supplied.encode(), f"Bearer {settings.chat_api_key}".encode())


class RateLimitMiddleware(BaseHTTPMiddleware):
    async def dispatch(self, request: Request, call_next):
        if request.url.path not in CHAT_PATHS or has_valid_chat_key(request):
            return await call_next(request)

        now = monotonic()
        ip = _client_ip(request)
        hits = _hits[ip]

        cutoff = now - 60.0
        while hits and hits[0] < cutoff:
            hits.popleft()

        if len(hits) >= CHAT_LIMIT_PER_MINUTE:
            logger.warning("Rate limit hit: IP=%s (%d requests in last 60s)", ip, len(hits))
            return JSONResponse(
                {"detail": "Too many requests. Please slow down."},
                status_code=429,
            )

        hits.append(now)
        return await call_next(request)
