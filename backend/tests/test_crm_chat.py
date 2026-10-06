"""POST /api/v2/chat: auth, JSON contract, handoff (tool + backstop), idempotency."""
import asyncio
import json

import pytest
from fastapi.testclient import TestClient

from app import llm as llm_module
from app import session_store
from app.config import settings
from app.main import app
from app.rate_limit import _hits

client = TestClient(app)
AUTH = {"Authorization": "Bearer k"}


def _body(text="hi", **extra):
    return {"messages": [{"role": "user", "content": text}], "session_id": "wa-1-1", **extra}


def _frame(**payload):
    return f"data: {json.dumps(payload)}\n\n"


@pytest.fixture(autouse=True)
def _setup(monkeypatch):
    _hits.clear()
    monkeypatch.setattr(settings, "chat_api_key", "k")
    monkeypatch.setattr(session_store, "redis_client", None)
    yield
    _hits.clear()


def _fake_stream(monkeypatch, frames, reason=None, calls=None):
    async def _s(_msgs, _sid, channel=None, handoff_state=None):
        if calls is not None:
            calls.append(channel)
        if reason:
            handoff_state["reason"] = reason
        for f in frames:
            yield f

    monkeypatch.setattr("app.crm_chat.stream_chat_response", _s)


OK_FRAMES = [_frame(delta="Hello ", done=False), _frame(delta="there", done=False), _frame(delta="", done=True)]


def test_auth_required_and_503_when_unconfigured(monkeypatch):
    _fake_stream(monkeypatch, OK_FRAMES)
    assert client.post("/api/v2/chat", json=_body()).status_code == 401
    assert client.post("/api/v2/chat", json=_body(), headers={"Authorization": "Bearer x"}).status_code == 401
    monkeypatch.setattr(settings, "chat_api_key", "")
    assert client.post("/api/v2/chat", json=_body(), headers=AUTH).status_code == 503


def test_json_contract_and_default_channel(monkeypatch):
    calls = []
    _fake_stream(monkeypatch, OK_FRAMES, calls=calls)
    r = client.post("/api/v2/chat", json=_body(), headers=AUTH)
    assert r.status_code == 200
    assert r.json() == {
        "session_id": "wa-1-1", "reply": "Hello there", "handoff": False,
        "handoff_reason": None, "session_capped": False, "error": None,
    }
    assert calls == ["whatsapp"]


def test_handoff_from_model_tool(monkeypatch):
    _fake_stream(monkeypatch, OK_FRAMES, reason="payment_issue")
    j = client.post("/api/v2/chat", json=_body("what is the price"), headers=AUTH).json()
    assert j["handoff"] is True and j["handoff_reason"] == "payment_issue" and j["reply"] == "Hello there"


def test_handoff_backstop_and_fallback_reply(monkeypatch):
    _fake_stream(monkeypatch, [_frame(delta="", done=True)])
    j = client.post("/api/v2/chat", json=_body("I want to talk to a human please"), headers=AUTH).json()
    assert j["handoff"] and j["handoff_reason"] == "customer_requested_human"
    assert "team" in j["reply"]


def test_no_false_handoff_for_ordinary_question(monkeypatch):
    _fake_stream(monkeypatch, OK_FRAMES)
    j = client.post("/api/v2/chat", json=_body("bungee price in rishikesh?"), headers=AUTH).json()
    assert j["handoff"] is False


def test_session_capped_passthrough_and_no_handoff(monkeypatch):
    _fake_stream(monkeypatch, [_frame(delta="start fresh", done=False), _frame(delta="", done=True, session_capped=True)])
    j = client.post("/api/v2/chat", json=_body("human please"), headers=AUTH).json()
    assert j["session_capped"] is True and j["handoff"] is False


def test_error_frame_surfaces_in_error_field(monkeypatch):
    _fake_stream(monkeypatch, [_frame(delta="", done=True, error="overloaded")])
    j = client.post("/api/v2/chat", json=_body(), headers=AUTH).json()
    assert j["error"] == "overloaded"


class _IdemRedis:
    def __init__(self):
        self.d = {}

    async def set(self, k, v, nx=False, ex=None):
        if nx and k in self.d:
            return None
        self.d[k] = v
        return True

    async def get(self, k):
        return self.d.get(k)

    async def delete(self, k):
        self.d.pop(k, None)


def test_request_id_replays_instead_of_rerunning(monkeypatch):
    monkeypatch.setattr(session_store, "redis_client", _IdemRedis())
    calls = []
    _fake_stream(monkeypatch, OK_FRAMES, calls=calls)
    first = client.post("/api/v2/chat", json=_body(request_id="wamid.1"), headers=AUTH)
    second = client.post("/api/v2/chat", json=_body(request_id="wamid.1"), headers=AUTH)
    assert len(calls) == 1
    assert second.json() == first.json()
    assert second.headers["Idempotency-Replayed"] == "true"
    # a different request_id is a new request
    client.post("/api/v2/chat", json=_body(request_id="wamid.2"), headers=AUTH)
    assert len(calls) == 2


def test_in_flight_duplicate_gets_409_and_errors_are_not_cached(monkeypatch):
    r = _IdemRedis()
    monkeypatch.setattr(session_store, "redis_client", r)
    r.d["idem:v2:wa-1-1:wamid.9"] = "pending"
    _fake_stream(monkeypatch, OK_FRAMES)
    assert client.post("/api/v2/chat", json=_body(request_id="wamid.9"), headers=AUTH).status_code == 409

    _fake_stream(monkeypatch, [_frame(delta="", done=True, error="boom")])
    client.post("/api/v2/chat", json=_body(request_id="wamid.7"), headers=AUTH)
    assert "idem:v2:wa-1-1:wamid.7" not in r.d  # retry may run again


def test_idempotency_skipped_without_redis(monkeypatch):
    calls = []
    _fake_stream(monkeypatch, OK_FRAMES, calls=calls)
    client.post("/api/v2/chat", json=_body(request_id="a"), headers=AUTH)
    client.post("/api/v2/chat", json=_body(request_id="a"), headers=AUTH)
    assert len(calls) == 2


def test_wrong_key_on_v2_is_rate_limited(monkeypatch):
    _fake_stream(monkeypatch, OK_FRAMES)
    codes = [client.post("/api/v2/chat", json=_body(), headers={"Authorization": "Bearer x"}).status_code for _ in range(25)]
    assert codes[-1] == 429


# --- real tool loop: handoff tool is v2-only, and v1 is unchanged -----------


class _ToolCallStream:
    def __init__(self, name, args):
        self._chunks = [type("C", (), {"choices": [type("Ch", (), {"delta": type("D", (), {
            "content": None,
            "tool_calls": [type("T", (), {"index": 0, "id": "c1", "function": type("F", (), {"name": name, "arguments": args})()})()],
        })()})()]})()]

    def __aiter__(self):
        return self

    async def __anext__(self):
        if not self._chunks:
            raise StopAsyncIteration
        return self._chunks.pop(0)


class _TextStream(_ToolCallStream):
    def __init__(self):
        self._chunks = [type("C", (), {"choices": [type("Ch", (), {"delta": type("D", (), {"content": "Team will follow up.", "tool_calls": None})()})()]})()]


def _drive(monkeypatch, handoff_state):
    async def no_tools():
        return []

    monkeypatch.setattr(llm_module, "load_catalog_tools", no_tools)
    offered, n = [], {"i": 0}

    async def fake_acompletion(*_a, **kw):
        offered.append({t["function"]["name"] for t in kw["tools"]})
        n["i"] += 1
        return _ToolCallStream("request_human_handoff", '{"reason": "complaint"}') if n["i"] == 1 else _TextStream()

    monkeypatch.setattr(llm_module.litellm, "acompletion", fake_acompletion)

    async def run():
        return [e async for e in llm_module._run_tool_loop([{"role": "user", "content": "x"}], "s", handoff_state)]

    return asyncio.run(run()), offered


def test_handoff_tool_sets_state_when_enabled(monkeypatch):
    state = {}
    events, offered = _drive(monkeypatch, state)
    assert state == {"reason": "complaint"}
    assert "request_human_handoff" in offered[0]
    assert ("delta", "Team will follow up.") in events


def test_handoff_tool_not_offered_to_web_chat(monkeypatch):
    async def no_tools():
        return []

    monkeypatch.setattr(llm_module, "load_catalog_tools", no_tools)
    seen = []

    async def fake_acompletion(*_a, **kw):
        seen.append({t["function"]["name"] for t in kw["tools"]})
        return _TextStream()

    monkeypatch.setattr(llm_module.litellm, "acompletion", fake_acompletion)

    async def run():
        return [e async for e in llm_module._run_tool_loop([{"role": "user", "content": "x"}], "s")]

    asyncio.run(run())
    assert "request_human_handoff" not in seen[0]


def test_handoff_still_gets_a_reply_when_the_model_call_fails(monkeypatch):
    _fake_stream(monkeypatch, [_frame(delta="", done=True, error="overloaded")])
    j = client.post("/api/v2/chat", json=_body("I want a human"), headers=AUTH).json()
    assert j["handoff"] is True and "team" in j["reply"] and j["error"] == "overloaded"
