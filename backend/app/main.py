"""FastAPI application entrypoint. Thin route handlers only - business
logic lives in app.llm / app.tools.
"""
import asyncio
import logging
import uuid
from contextlib import asynccontextmanager

from fastapi import FastAPI, HTTPException, Request
from fastapi.middleware.cors import CORSMiddleware
from fastapi.responses import StreamingResponse

from app.config import settings
from app.crm_chat import router as crm_router
from app.dashboard import get_summary, idle_scan_loop, list_summaries
from app.llm import stream_chat_response
from app.mcp_client import close_http_client
from app.rate_limit import RateLimitMiddleware, has_valid_chat_key
from app.schemas import ChatRequest, UserInfoRequest, AttachmentUploadResponse
from app.session_store import init_redis, close_redis, save_user_info
from app.attachments import store_attachment, AttachmentError
from fastapi import UploadFile, File

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s | %(levelname)-8s | %(name)s | %(message)s",
    datefmt="%Y-%m-%d %H:%M:%S",
)
logger = logging.getLogger(__name__)


@asynccontextmanager
async def lifespan(app: FastAPI):
    logger.info(
        "Starting up — model=%s, MCP=%s, Weaviate=%s",
        settings.llm_model,
        bool(settings.mcp_server_url),
        bool(settings.weaviate_url),
    )
    await init_redis()
    scan_task = asyncio.create_task(idle_scan_loop())
    yield
    scan_task.cancel()
    await close_redis()
    await close_http_client()
    logger.info("Shutting down")


app = FastAPI(title="Chatbot Backend", lifespan=lifespan)

app.include_router(crm_router)
app.add_middleware(RateLimitMiddleware)
app.add_middleware(
    CORSMiddleware,
    allow_origins=settings.cors_origins_list,
    allow_credentials=True,
    allow_methods=["*"],
    allow_headers=["*"],
)


@app.get("/api/health")
def health() -> dict:
    """Liveness/readiness probe reporting the currently configured model."""
    return {"status": "ok", "model": settings.llm_model}


def _require_chat_key(http_request: Request) -> None:
    """No-op unless CHAT_API_KEY is set; then 401 without a matching Bearer token."""
    if settings.chat_api_key and not has_valid_chat_key(http_request):
        raise HTTPException(401, "Unauthorized")


@app.post("/api/chat")
async def chat(request: ChatRequest, http_request: Request) -> StreamingResponse:
    """Stream a chat completion as Server-Sent Events.

    session_id resolution: an explicit value in the request body wins (a
    frontend that manages its own), else an existing session_id cookie,
    else a freshly generated one — set as a cookie on the response so a
    frontend that does nothing special still gets a stable session across
    requests, with no client-side UUID generation required.
    """
    _require_chat_key(http_request)
    session_id = request.session_id or http_request.cookies.get("session_id") or str(uuid.uuid4())
    logger.info(
        "POST /api/chat — %d messages, session=%s",
        len(request.messages),
        session_id,
    )
    response = StreamingResponse(
        stream_chat_response(request.messages, session_id, channel=request.channel),
        media_type="text/event-stream",
    )
    # ponytail: secure mirrors the scheme uvicorn itself sees — if this ever
    # sits behind a TLS-terminating proxy (e.g. Caddy) run uvicorn with
    # --proxy-headers, or the cookie's Secure flag silently stays off even
    # though users are on https.
    response.set_cookie(
        "session_id",
        session_id,
        max_age=settings.session_ttl_seconds,
        httponly=True,
        samesite="lax",
        secure=http_request.url.scheme == "https",
    )
    return response

@app.post("/api/session/user-info")
async def store_user_info(request: UserInfoRequest, http_request: Request) -> dict:
    """Store captured user contact information in their Redis session."""
    _require_chat_key(http_request)
    if not request.session_id:
        return {"status": "error", "message": "No session ID provided"}
        
    logger.info("POST /api/session/user-info — session=%s", request.session_id)
    await save_user_info(
        request.session_id,
        request.user_info.name,
        request.user_info.phone,
        request.user_info.email
    )
    return {"status": "ok"}


def _check_dashboard_auth(http_request: Request) -> None:
    """Bearer-token guard for /api/admin/*. 503 (not 401) when unconfigured —
    tells you "this feature is off", not "your token is wrong"."""
    if not settings.dashboard_api_key:
        raise HTTPException(503, "Dashboard API not configured (set DASHBOARD_API_KEY)")
    if http_request.headers.get("authorization") != f"Bearer {settings.dashboard_api_key}":
        raise HTTPException(401, "Unauthorized")


@app.get("/api/admin/session-summaries")
def get_session_summaries(http_request: Request, since: str | None = None) -> dict:
    """All session summaries, or those ended at/after `since` (ISO 8601).
    Pull endpoint for the employee dashboard — see app/dashboard.py for
    where a push-style webhook would hook in once you have a receiver.
    """
    _check_dashboard_auth(http_request)
    logger.info("GET /api/admin/session-summaries — since=%s", since)
    return {"summaries": list_summaries(since=since)}


@app.get("/api/admin/session-summaries/{session_id}")
def get_session_summary(session_id: str, http_request: Request) -> dict:
    _check_dashboard_auth(http_request)
    logger.info("GET /api/admin/session-summaries/%s", session_id)
    record = get_summary(session_id)
    if record is None:
        raise HTTPException(404, "No summary for this session (not yet idle-summarized, or unknown id)")
    return record

@app.post("/api/chat/attachments", response_model=AttachmentUploadResponse)
async def upload_attachment(file: UploadFile = File(...)) -> AttachmentUploadResponse:
    """Validate, scan, and store one attachment. Returns an id to reference
    from a subsequent /api/chat call — see schemas.ChatMessage.attachment_ids.

    Multipart, not JSON: files don't belong in a JSON body (base64 inflates
    the payload ~33% and breaks the existing text-length cap semantics on
    ChatMessage.content).
    """
    try:
        result = await store_attachment(file)
    except AttachmentError as exc:
        raise HTTPException(exc.status_code, exc.message) from exc
    logger.info("Attachment stored: id=%s type=%s size=%d", result.attachment_id, result.media_type, result.size_bytes)
    return AttachmentUploadResponse(
        attachment_id=result.attachment_id, type=result.media_type, filename=result.filename
    )
