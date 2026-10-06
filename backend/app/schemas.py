"""Pydantic request/response models for the chat API.

Restricting `role` to "user"/"assistant" (no "system") means FastAPI's
normal request validation already rejects a client-supplied system message
with a 422 - no custom validator needed.
"""
from typing import Literal, Optional

from pydantic import BaseModel, Field


class ChatMessage(BaseModel):
    """A single message in the conversation, as sent by the client."""

    role: Literal["user", "assistant"]
    content: str = Field(min_length=1, max_length=8000)
    attachment_ids: list[str] = Field(default_factory=list, max_length=5)


class AttachmentUploadResponse(BaseModel):
    attachment_id: str
    type: str        # "image" | "pdf" | "text"
    filename: str


class ChatRequest(BaseModel):
    messages: list[ChatMessage] = Field(
        min_length=1,
        max_length=40,
        description="Full conversation history. The client resends full history each turn, so capping this bounds the worst-case request size."
    )
    session_id: str | None = Field(
        default=None,
        description="Optional client-generated session UUID, used to persist auth tokens across requests."
    )
    channel: str | None = Field(
        default=None,
        max_length=32,
        description='Optional delivery channel, e.g. "whatsapp". Unknown values behave like the default web chat.',
    )
    request_id: str | None = Field(
        default=None,
        max_length=256,
        description="Accepted for forward compatibility (caller's message id); not acted on yet.",
    )

class UserInfo(BaseModel):
    name: str = ""
    phone: str = ""
    email: str = ""

class UserInfoRequest(BaseModel):
    session_id: str
    user_info: UserInfo
