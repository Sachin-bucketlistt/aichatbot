"""Short catalog questions must reach the live catalog: forcing memory, re-ask on a from-memory price, prompt line."""
import asyncio
import json
from types import SimpleNamespace as NS

import litellm
import pytest

from app import llm
from app.flow_guard import is_hard_protected, is_protected_turn
from app.schemas import ChatMessage

TOOLS = [{"type": "function", "function": {"name": "get_activities", "parameters": {}}}]


class Stream:
    """Fake litellm stream: optional text, optional single tool call."""

    def __init__(self, text="", tool=None):
        chunks = []
        if text:
            chunks.append(NS(choices=[NS(delta=NS(content=text, tool_calls=None), finish_reason=None)]))
        if tool:
            tc = NS(index=0, id="t1", function=NS(name=tool, arguments="{}"))
            chunks.append(NS(choices=[NS(delta=NS(content=None, tool_calls=[tc]), finish_reason="tool_calls")]))
        self._it = iter(chunks)

    def __aiter__(self):
        return self

    async def __anext__(self):
        try:
            return next(self._it)
        except StopIteration:
            raise StopAsyncIteration


@pytest.fixture
def loop_env(monkeypatch):
    async def tools():
        return TOOLS

    async def fake_tool(call, session_id, mcp_session=None):
        return {"result": "[]"}

    class FakeStack:
        async def aclose(self):
            pass

    async def fake_session():
        return FakeStack(), object()

    monkeypatch.setattr(llm, "load_catalog_tools", tools)
    monkeypatch.setattr(llm, "_execute_tool", fake_tool)
    monkeypatch.setattr("app.mcp_client._fresh_session", fake_session)   # never open a real MCP connection


def _run(msgs, handoff_state=None):
    async def go():
        return [e async for e in llm._run_tool_loop(msgs, None, handoff_state)]
    return asyncio.run(go())


def _script(monkeypatch, *streams):
    calls = []
    it = iter(streams)

    async def fake(*_a, **kw):
        calls.append(kw)
        return next(it)

    monkeypatch.setattr(llm.litellm, "acompletion", fake)
    return calls


# --- step 3: short catalog words are not "protected" -------------------------------------------

def test_short_catalog_message_is_not_hard_protected_but_otp_phone_safety_still_are():
    assert is_protected_turn("bungee price") is True          # unchanged public behaviour
    assert is_hard_protected("bungee price") is False and is_hard_protected("rafting") is False
    for text in ["483920", "+91 98765 43210", "is it safe", "I have a heart condition"]:
        assert is_hard_protected(text) is True


# --- step 1: a rejected forced tool_choice is remembered ---------------------------------------

def test_forced_choice_rejection_is_remembered(monkeypatch, loop_env):
    seen = []

    async def fake(*_a, **kw):
        seen.append(kw["tool_choice"])
        if kw["tool_choice"] == "required":
            raise litellm.BadRequestError('tool_choice: type "any" is not supported', model="m", llm_provider="anthropic")
        return Stream("Here you go")

    monkeypatch.setattr(llm.litellm, "acompletion", fake)
    _run([{"role": "user", "content": "bungee price"}])
    _run([{"role": "user", "content": "rafting price"}])
    assert seen == ["required", "auto", "auto"]     # the second turn never sends the doomed forced request


def test_short_catalog_message_is_forced_when_the_model_allows_it(monkeypatch, loop_env):
    calls = _script(monkeypatch, Stream("Here you go"))
    _run([{"role": "user", "content": "bungee price"}])
    assert calls[0]["tool_choice"] == "required"


# --- step 2: price quoted with no tool call -> drop the draft, ask again (v2 only) -------------

PRICED = "Bungee starts at ₹2,500 🪂"


def test_price_from_memory_is_retracted_and_reasked_once(monkeypatch, loop_env):
    calls = _script(monkeypatch, Stream(PRICED), Stream(tool="get_activities"), Stream("Live price is ₹3,500 🪂"))
    events = _run([{"role": "user", "content": "bungee price"}], handoff_state={})
    assert ("retract", "") in events and "Checking live prices…" in events
    assert len(calls) == 3
    assert "never mention it" in json.dumps(calls[1]["messages"][-1])


def test_no_reask_for_the_website_or_when_a_tool_was_already_used(monkeypatch, loop_env):
    calls = _script(monkeypatch, Stream(PRICED))
    events = _run([{"role": "user", "content": "bungee price"}], handoff_state=None)   # web: text already shown
    assert ("retract", "") not in events and len(calls) == 1
    calls = _script(monkeypatch, Stream(tool="get_activities"), Stream("It is ₹3,500"))
    events = _run([{"role": "user", "content": "bungee price"}], handoff_state={})
    assert ("retract", "") not in events and len(calls) == 2


def test_no_reask_for_non_catalog_questions_or_when_no_price(monkeypatch, loop_env):
    calls = _script(monkeypatch, Stream("The GoPro add-on is ₹500"))
    assert ("retract", "") not in _run([{"role": "user", "content": "how much is the gopro"}], handoff_state={})
    calls = _script(monkeypatch, Stream("Sure! Which one?"))
    assert ("retract", "") not in _run([{"role": "user", "content": "bungee price"}], handoff_state={})
    assert len(calls) == 1


def test_reask_happens_only_once(monkeypatch, loop_env):
    calls = _script(monkeypatch, Stream(PRICED), Stream("Still ₹2,500 from memory"))
    events = _run([{"role": "user", "content": "bungee price"}], handoff_state={})
    assert events.count(("retract", "")) == 1 and len(calls) == 2


def test_retracted_draft_is_not_in_the_final_v2_reply(monkeypatch, loop_env):
    from app import session_store
    monkeypatch.setattr(session_store, "redis_client", None)
    _script(monkeypatch, Stream(PRICED), Stream(tool="get_activities"), Stream("Live price is ₹3,500 🪂"))

    async def go():
        out = []
        async for fr in llm.stream_chat_response([ChatMessage(role="user", content="bungee price")], "s1",
                                                  channel="whatsapp", handoff_state={}):
            out.append(json.loads(fr.removeprefix("data: ").strip()))
        return out

    frames = asyncio.run(go())
    parts = []
    for f in frames:            # same rule the v2 endpoint applies
        if f.get("status"):
            parts.clear()
        parts.append(f.get("delta") or "")
    assert "".join(parts).strip() == "Live price is ₹3,500 🪂"


# --- step 4: prompt line for catalog-word messages --------------------------------------------

def _dyn(text):
    return asyncio.run(llm.build_messages([ChatMessage(role="user", content=text)]))[0]["content"][-1]["text"]


def test_live_data_line_only_for_catalog_questions(monkeypatch):
    monkeypatch.setattr(llm.settings, "mcp_server_url", "http://mcp")
    assert "Live data needed" in _dyn("bungee price") and "Live data needed" in _dyn("rafting")
    assert "Live data needed" not in _dyn("hi") and "Live data needed" not in _dyn("thanks!")
    monkeypatch.setattr(llm.settings, "mcp_server_url", "")
    assert "Live data needed" not in _dyn("bungee price")
