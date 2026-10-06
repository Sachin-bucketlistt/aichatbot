"""POST /api/v2/chat — the CRM-facing chat endpoint.

Same brain as /api/chat (app.llm.stream_chat_response), different contract:
always authenticated with CHAT_API_KEY, one JSON body instead of an SSE
stream, machine-readable handoff / session-cap flags, and request_id replay
so a CRM retry never re-runs tools. /api/chat is untouched by any of this.
"""
import json
import logging
import uuid

from fastapi import APIRouter, HTTPException, Request, Response
from pydantic import BaseModel

from app import session_store
from app.config import settings
from app.handoff import detect_handoff
from app.llm import stream_chat_response
from app.rate_limit import has_valid_chat_key
from app.schemas import ChatRequest

logger = logging.getLogger(__name__)
router = APIRouter()

IDEMPOTENCY_TTL_SECONDS = 600
IN_FLIGHT_TTL_SECONDS = 120   # > the CRM's 60s timeout, so a crashed request frees its key
HANDOFF_FALLBACK_REPLY = "I'm connecting you with our team. Someone will follow up with you here shortly."


class ChatV2Response(BaseModel):
    session_id: str
    reply: str
    handoff: bool = False
    handoff_reason: str | None = None
    session_capped: bool = False
    error: str | None = None


async def _claim(key: str | None) -> tuple[str, str | None]:
    """('new'|'inflight'|'replay'|'off', stored_json). 'off' = no Redis / no
    request_id / Redis error: process normally without idempotency, never fail."""
    client = session_store.redis_client
    if not client or not key:
        return "off", None
    try:
        if await client.set(key, "pending", nx=True, ex=IN_FLIGHT_TTL_SECONDS):
            return "new", None
        stored = await client.get(key)
    except Exception:  # pylint: disable=broad-except
        logger.exception("Idempotency check failed; processing without it")
        return "off", None
    if stored is None:
        return "off", None
    return ("inflight", None) if stored == "pending" else ("replay", stored)


async def _store(key: str, result: ChatV2Response) -> None:
    """Keep successful results for replay; drop the pending marker on errors so a retry can run."""
    client = session_store.redis_client
    try:
        if result.error:
            await client.delete(key)
        else:
            await client.set(key, result.model_dump_json(), ex=IDEMPOTENCY_TTL_SECONDS)
    except Exception:  # pylint: disable=broad-except
        logger.exception("Could not store idempotency result for %s", key)


@router.post("/api/v2/chat", response_model=ChatV2Response)
async def chat_v2(request: ChatRequest, http_request: Request, response: Response) -> ChatV2Response:
    if not settings.chat_api_key:
        raise HTTPException(503, "CRM chat API not configured (set CHAT_API_KEY)")
    if not has_valid_chat_key(http_request):
        raise HTTPException(401, "Unauthorized")

    session_id = request.session_id or str(uuid.uuid4())
    key = f"idem:v2:{session_id}:{request.request_id}" if request.request_id else None
    state, stored = await _claim(key)
    if state == "inflight":
        raise HTTPException(409, "A request with this request_id is still being processed; retry shortly")
    if state == "replay":
        response.headers["Idempotency-Replayed"] = "true"
        return ChatV2Response.model_validate_json(stored)

    logger.info("POST /api/v2/chat — %d messages, session=%s", len(request.messages), session_id)
    handoff_state: dict = {}
    parts: list[str] = []
    error = None
    capped = False
    try:
        async for frame in stream_chat_response(
            request.messages, session_id, channel=request.channel or "whatsapp", handoff_state=handoff_state
        ):
            payload = json.loads(frame.removeprefix("data: ").strip())
            parts.append(payload.get("delta") or "")
            error = payload.get("error") or error
            capped = capped or bool(payload.get("session_capped"))

        reply = "".join(parts).strip()
        last_user = next((m.content for m in reversed(request.messages) if m.role == "user"), "")
        reason = None if capped else handoff_state.get("reason") or detect_handoff(last_user)
        if reason and not reply:
            reply = HANDOFF_FALLBACK_REPLY
        result = ChatV2Response(
            session_id=session_id, reply=reply, handoff=bool(reason), handoff_reason=reason,
            session_capped=capped, error=error,
        )
    except BaseException:
        if state == "new":
            await _store(key, ChatV2Response(session_id=session_id, reply="", error="aborted"))
        raise
    if state == "new":
        await _store(key, result)
    return result
