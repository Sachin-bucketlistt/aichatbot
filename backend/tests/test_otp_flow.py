"""End-to-end OTP login through POST /api/v2/chat (real tool loop, scripted model).

Turn 1: customer gives phone -> model calls send_otp.
Turn 2: customer sends the code -> model calls verify_otp (MCP returns a token).
Turn 3: "add to cart" -> model calls add_to_cart; the server must inject the saved
        authToken itself and the model must be told not to ask for an OTP again.
"""
import json

import pytest
from fastapi.testclient import TestClient

from app import llm as llm_module
from app import mcp_client, session_store, token_store
from app.config import settings
from app.main import app
from app.rate_limit import _hits

client = TestClient(app)
AUTH = {"Authorization": "Bearer k"}


class _Stream:
    def __init__(self, content=None, tool=None):
        delta = type("D", (), {
            "content": content,
            "tool_calls": [type("T", (), {"index": 0, "id": "c", "function": type(
                "F", (), {"name": tool[0], "arguments": json.dumps(tool[1])})()})()] if tool else None,
        })()
        self._chunks = [type("C", (), {"choices": [type("Ch", (), {"delta": delta})()]})()]

    def __aiter__(self):
        return self

    async def __anext__(self):
        if not self._chunks:
            raise StopAsyncIteration
        return self._chunks.pop(0)


def _install(monkeypatch, script):
    """script: list of ('tool', name, args) | ('text', str), consumed one per model call."""
    seen = {"system": [], "mcp_calls": []}

    async def no_tools():
        return []

    async def fake_session():
        class _Stack:
            async def aclose(self):
                pass
        return _Stack(), object()

    async def fake_mcp(call, session=None):
        args = json.loads(call.function.arguments)
        seen["mcp_calls"].append((call.function.name, args))
        if call.function.name == "verify_otp":
            return {"result": json.dumps({"authToken": "tok-123"})}
        return {"result": json.dumps({"success": True})}

    async def fake_acompletion(*_a, **kw):
        seen["system"].append(json.dumps(kw["messages"][0]["content"]))
        kind = script.pop(0)
        return _Stream(tool=(kind[1], kind[2])) if kind[0] == "tool" else _Stream(content=kind[1])

    monkeypatch.setattr(llm_module, "load_catalog_tools", no_tools)
    monkeypatch.setattr(mcp_client, "_fresh_session", fake_session)
    monkeypatch.setattr(llm_module, "call_catalog_tool", fake_mcp)
    monkeypatch.setattr(llm_module.litellm, "acompletion", fake_acompletion)
    return seen


@pytest.fixture(autouse=True)
def _setup(monkeypatch):
    _hits.clear()
    token_store._store.clear()
    token_store._pending_phone.clear()
    monkeypatch.setattr(settings, "chat_api_key", "k")
    monkeypatch.setattr(session_store, "redis_client", None)
    yield
    _hits.clear()
    token_store._store.clear()
    token_store._pending_phone.clear()


def _say(text, sid="wa-otp-1"):
    r = client.post("/api/v2/chat", headers=AUTH, json={
        "session_id": sid, "messages": [{"role": "user", "content": text}]})
    assert r.status_code == 200 and r.json()["error"] is None, r.text
    return r.json()


def test_otp_login_then_cart_uses_saved_token(monkeypatch):
    seen = _install(monkeypatch, [
        ("tool", "send_otp", {"phone": "+919876543210"}), ("text", "OTP sent, what is the code?"),
        ("tool", "verify_otp", {"otp": "482913"}), ("text", "You're logged in."),
        ("tool", "add_to_cart", {"activityId": 1}), ("text", "Added to your cart."),
    ])
    assert "OTP sent" in _say("book rafting, my number is +919876543210")["reply"]
    assert "logged in" in _say("482913")["reply"]
    assert "Added" in _say("please add it to my cart")["reply"]

    names = [n for n, _ in seen["mcp_calls"]]
    assert names == ["send_otp", "verify_otp", "add_to_cart"]       # no second OTP
    assert dict(seen["mcp_calls"])["add_to_cart"]["authToken"] == "tok-123"  # server injected it
    assert "ALREADY LOGGED IN" not in seen["system"][0]
    assert "ALREADY LOGGED IN" in seen["system"][-1]                 # model told not to re-ask


def test_failed_otp_does_not_log_in(monkeypatch):
    _install(monkeypatch, [("tool", "verify_otp", {"otp": "000000"}), ("text", "Wrong code.")])

    async def bad(call, session=None):
        return {"result": json.dumps({"success": False})}

    monkeypatch.setattr(llm_module, "call_catalog_tool", bad)
    _say("000000")
    assert token_store.get_token("wa-otp-1") is None


# --- Redis-backed login survives a restart ---------------------------------

from tests.conftest import FakeRedis


def test_login_survives_restart_via_redis(monkeypatch):
    redis = FakeRedis()
    monkeypatch.setattr(session_store, "redis_client", redis)
    seen = _install(monkeypatch, [
        ("tool", "send_otp", {"phone": "+919876543210"}), ("text", "Code?"),
        ("tool", "verify_otp", {"otp": "482913"}), ("text", "Logged in."),
        ("tool", "add_to_cart", {"activityId": 1}), ("text", "Added."),
    ])
    _say("my number is +919876543210")
    _say("482913")
    assert redis._strings["auth:token:wa-otp-1"] == "tok-123"
    assert "auth:phone:wa-otp-1" not in redis._strings          # consumed after verify

    token_store._store.clear()                                    # simulate restart / other worker
    token_store._pending_phone.clear()
    _say("add it to my cart")

    assert dict(seen["mcp_calls"])["add_to_cart"]["authToken"] == "tok-123"
    assert "ALREADY LOGGED IN" in seen["system"][-1]


def test_pending_phone_survives_restart_between_send_and_verify(monkeypatch):
    redis = FakeRedis()
    monkeypatch.setattr(session_store, "redis_client", redis)
    _install(monkeypatch, [
        ("tool", "send_otp", {"phone": "+919876543210"}), ("text", "Code?"),
        ("tool", "verify_otp", {"otp": "482913"}), ("text", "Logged in."),
    ])
    saved = {}

    async def fake_save(sid, phone):
        saved[sid] = phone

    monkeypatch.setattr(llm_module, "save_verified_phone", fake_save)
    _say("my number is +919876543210")
    token_store._pending_phone.clear()                            # restart before the code arrives
    _say("482913")
    assert saved == {"wa-otp-1": "+919876543210"}


def test_redis_failure_falls_back_to_memory(monkeypatch):
    class Broken(FakeRedis):
        async def set(self, *a, **k):
            raise RuntimeError("down")

        async def get(self, *a, **k):
            raise RuntimeError("down")

    monkeypatch.setattr(session_store, "redis_client", Broken())
    seen = _install(monkeypatch, [
        ("tool", "verify_otp", {"otp": "482913"}), ("text", "Logged in."),
        ("tool", "add_to_cart", {"activityId": 1}), ("text", "Added."),
    ])
    _say("482913")
    _say("add it")
    assert dict(seen["mcp_calls"])["add_to_cart"]["authToken"] == "tok-123"
