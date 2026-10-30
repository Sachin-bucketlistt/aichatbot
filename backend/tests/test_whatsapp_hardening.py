"""Real-chat fixes for the WhatsApp v2 path: Hinglish handoff, no contact nudge, no preamble, masked ids."""
import asyncio
import json
import logging

import pytest
from fastapi.testclient import TestClient

from app import llm as llm_module
from app import session_store
from app.config import settings
from app.handoff import detect_handoff
from app.main import app
from app.rate_limit import _hits
from app.schemas import ChatMessage
from app.session_store import mask_session_id

client = TestClient(app)
AUTH = {"Authorization": "Bearer k"}


@pytest.fixture(autouse=True)
def _setup(monkeypatch):
    _hits.clear()
    monkeypatch.setattr(settings, "chat_api_key", "k")
    monkeypatch.setattr(session_store, "redis_client", None)
    yield
    _hits.clear()


# --- 1. handoff backstop ----------------------------------------------------------------------

@pytest.mark.parametrize("text", [
    "mujhe team se baat karni hai", "insaan se baat karni hai", "Call me", "call back please",
    "mujhe call karo", "kisi se baat karwa do", "I want to talk to a human", "manager se baat karao",
    "speak to your manager", "customer care number do",
])
def test_handoff_catches_hinglish_and_call_requests(text):
    assert detect_handoff(text) == "customer_requested_human"


@pytest.mark.parametrize("text,reason", [
    ("refund chahiye", "payment_issue"), ("mere paise kat gaye", "payment_issue"),
    ("this is a scam", "complaint"), ("meri shikayat hai", "complaint"), ("worst experience ever", "complaint"),
])
def test_handoff_other_reasons(text, reason):
    assert detect_handoff(text) == reason


@pytest.mark.parametrize("text", [
    "hi", "bungee price?", "worst case if it rains, can I reschedule?", "the weather was terrible last time",
    "my hotel manager recommended you", "I called the office yesterday", "are you a real person?",
    "kya aap insaan ho?", "are you a bot?", "aap robot ho?", "what time is the call for pickup",
])
def test_handoff_does_not_fire_on_casual_or_identity_questions(text):
    assert detect_handoff(text) is None


# --- 2. website contact nudge never appears on WhatsApp --------------------------------------

def test_whatsapp_skips_contact_nudge_but_web_keeps_it(monkeypatch):
    async def always(_sid):
        return True

    seen = []

    async def spy_build(chat_messages, session_id=None, nudge_contact=False, channel=None, handoff_enabled=False):
        seen.append((channel, nudge_contact))
        return [{"role": "system", "content": "x"}]

    async def no_loop(*_a, **_k):
        return
        yield  # pragma: no cover

    monkeypatch.setattr(llm_module, "should_nudge_for_contact", always)
    monkeypatch.setattr(llm_module, "should_prompt_login", always)
    monkeypatch.setattr(llm_module, "build_messages", spy_build)
    monkeypatch.setattr(llm_module, "_run_tool_loop", no_loop)

    async def run(channel):
        return [f async for f in llm_module.stream_chat_response(
            [ChatMessage(role="user", content="hi")], "wa-919876543210-1", channel=channel)]

    wa_frames = asyncio.run(run("whatsapp"))
    web_frames = asyncio.run(run(None))
    assert seen == [("whatsapp", False), (None, True)]
    assert not any("prompt_login" in f for f in wa_frames) and any("prompt_login" in f for f in web_frames)


# --- 4. text before a tool call is not part of the customer's reply ---------------------------

def _stub(monkeypatch, frames):
    async def _s(_msgs, _sid, channel=None, handoff_state=None):
        for f in frames:
            yield f

    monkeypatch.setattr("app.crm_chat.stream_chat_response", _s)


def _f(**p):
    return f"data: {json.dumps(p)}\n\n"


PREAMBLE = [_f(delta="Let me check that for you. ", done=False), _f(status="Checking time slots…", done=False),
            _f(delta="Tomorrow has slots at 9 and 11 🙂", done=False), _f(delta="", done=True)]


def test_json_endpoint_drops_the_preamble(monkeypatch):
    _stub(monkeypatch, PREAMBLE)
    body = client.post("/api/v2/chat", json={"messages": [{"role": "user", "content": "slots?"}], "session_id": "wa-1-1"},
                       headers=AUTH).json()
    assert body["reply"] == "Tomorrow has slots at 9 and 11 🙂" and "Let me check" not in " ".join(body["messages"])


def test_stream_endpoint_final_frame_drops_the_preamble(monkeypatch):
    _stub(monkeypatch, PREAMBLE)
    r = client.post("/api/v2/chat/stream", json={"messages": [{"role": "user", "content": "slots?"}], "session_id": "wa-1-1"},
                    headers=AUTH)
    final = json.loads([ln for ln in r.text.split("\n\n") if '"done": true' in ln][-1].removeprefix("data: "))
    assert final["reply"] == "Tomorrow has slots at 9 and 11 🙂"


def test_plain_reply_without_tools_is_unchanged(monkeypatch):
    _stub(monkeypatch, [_f(delta="Hi! ", done=False), _f(delta="Looking at bungee?", done=False), _f(delta="", done=True)])
    body = client.post("/api/v2/chat", json={"messages": [{"role": "user", "content": "hi"}], "session_id": "wa-1-1"},
                       headers=AUTH).json()
    assert body["reply"] == "Hi! Looking at bungee?"


# --- 5. phone numbers in session ids never reach logs ----------------------------------------

def test_mask_session_id():
    assert mask_session_id("wa-919876543210-1") == "wa-********3210-1"
    assert mask_session_id("3f2a-uuid") == "3f2a-uuid" and mask_session_id(None) == ""


def test_v2_logs_do_not_contain_the_phone_number(monkeypatch, caplog):
    _stub(monkeypatch, [_f(delta="ok", done=False), _f(delta="", done=True)])
    with caplog.at_level(logging.INFO):
        client.post("/api/v2/chat", json={"messages": [{"role": "user", "content": "hi"}], "session_id": "wa-919876543210-1"},
                    headers=AUTH)
        client.post("/api/v2/chat/stream", json={"messages": [{"role": "user", "content": "hi"}],
                    "session_id": "wa-919876543210-2"}, headers=AUTH)
    assert "919876543210" not in caplog.text and "3210" in caplog.text


# --- 4. extra-discount requests hand off ------------------------------------------------------

@pytest.mark.parametrize("text", [
    "Gujarti ke liye special discount", "extra discount milega?", "discount?", "any discount",
    "thoda kam karo", "price kam karo", "last price batao", "best price?", "koi offer hai?",
    "do you have a coupon code", "can you reduce the price", "student discount hai kya", "bargain karna hai",
])
def test_discount_requests_hand_off(text):
    assert detect_handoff(text) == "discount_request"


@pytest.mark.parametrize("text", [
    "what is the discounted price of rafting", "price kitna hai", "bungee price", "is the offer price per person?",
    "kitne ka hai 16 km rafting", "worst case if it rains?",
])
def test_plain_price_questions_do_not_hand_off(text):
    assert detect_handoff(text) is None
