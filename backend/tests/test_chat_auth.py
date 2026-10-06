"""Opt-in CHAT_API_KEY auth, rate-limit exemption, channel prompt, session_capped."""
import asyncio
import json

import pytest

from fastapi.testclient import TestClient

from app import llm as llm_module
from app.config import settings
from app.main import app
from app.rate_limit import CHAT_LIMIT_PER_MINUTE, _hits
from app.schemas import ChatMessage

client = TestClient(app)


@pytest.fixture(autouse=True)
def _reset_rate_limiter():
    _hits.clear()
    yield
    _hits.clear()
PAYLOAD = {"messages": [{"role": "user", "content": "hi"}]}


def _stub_stream(monkeypatch):
    async def _s(*_a, **_k):
        yield 'data: {"delta": "", "done": true}\n\n'

    monkeypatch.setattr("app.main.stream_chat_response", _s)


def test_open_when_key_unset(monkeypatch):
    _hits.clear()
    monkeypatch.setattr(settings, "chat_api_key", "")
    _stub_stream(monkeypatch)
    assert client.post("/api/chat", json=PAYLOAD).status_code == 200


def test_401_without_or_with_wrong_key(monkeypatch):
    _hits.clear()
    monkeypatch.setattr(settings, "chat_api_key", "s3cret")
    _stub_stream(monkeypatch)
    assert client.post("/api/chat", json=PAYLOAD).status_code == 401
    bad = {"Authorization": "Bearer nope"}
    assert client.post("/api/chat", json=PAYLOAD, headers=bad).status_code == 401
    assert client.post("/api/session/user-info", json={"session_id": "x", "user_info": {}}).status_code == 401


def test_valid_key_passes_and_skips_rate_limit(monkeypatch):
    _hits.clear()
    monkeypatch.setattr(settings, "chat_api_key", "s3cret")
    _stub_stream(monkeypatch)
    ok = {"Authorization": "Bearer s3cret"}
    for _ in range(CHAT_LIMIT_PER_MINUTE + 5):
        assert client.post("/api/chat", json=PAYLOAD, headers=ok).status_code == 200


def test_invalid_key_is_still_rate_limited(monkeypatch):
    _hits.clear()
    monkeypatch.setattr(settings, "chat_api_key", "s3cret")
    _stub_stream(monkeypatch)
    codes = [client.post("/api/chat", json=PAYLOAD).status_code for _ in range(CHAT_LIMIT_PER_MINUTE + 1)]
    assert codes[-1] == 429


def test_whatsapp_channel_adds_format_prompt_and_web_does_not():
    msgs = [ChatMessage(role="user", content="hello")]
    wa = asyncio.run(llm_module.build_messages(msgs, channel="whatsapp"))
    web = asyncio.run(llm_module.build_messages(msgs))
    wa_dyn, web_dyn = wa[0]["content"][1]["text"], web[0]["content"][1]["text"]
    assert "WhatsApp" in wa_dyn and "WhatsApp" not in web_dyn
    assert wa[0]["content"][0] == web[0]["content"][0]  # cached static block unchanged


def test_session_capped_flag_on_cap_reply(monkeypatch):
    async def capped(_sid):
        return settings.max_messages_per_session

    monkeypatch.setattr(llm_module, "get_message_count", capped)

    async def run():
        return [f async for f in llm_module.stream_chat_response([ChatMessage(role="user", content="hi")], "s1")]

    frames = [json.loads(f[6:]) for f in asyncio.run(run())]
    assert frames[-1] == {"delta": "", "done": True, "session_capped": True}
