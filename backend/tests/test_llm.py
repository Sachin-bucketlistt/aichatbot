"""Tests for the bungee-summary tool gate in app/llm.py.

get_activities_summary must only ever be offered to the LLM when the user's
latest message is about bungee jumping — see app/llm.py::_wants_bungee_summary.
"""
import asyncio

import pytest

from app.llm import _wants_bungee_summary


@pytest.mark.parametrize("text", [
    "What are the bungee jumping prices in Rishikesh?",
    "bungy options?",
    "Tell me about Bungee",
    "BUNGIE combo price",
])
def test_bungee_query_triggers_gate(text):
    assert _wants_bungee_summary([{"role": "user", "content": text}])


def test_non_bungee_query_does_not_trigger_gate():
    assert not _wants_bungee_summary([{"role": "user", "content": "What rafting packages do you have?"}])


def test_only_latest_user_message_is_checked():
    messages = [
        {"role": "user", "content": "bungee prices?"},
        {"role": "assistant", "content": "Here are the bungee options..."},
        {"role": "user", "content": "what about rafting instead"},
    ]
    assert not _wants_bungee_summary(messages)


def test_empty_messages_does_not_trigger_gate():
    assert not _wants_bungee_summary([])


# --- end-to-end: the tool actually leaves the list sent to the LLM --------

_FAKE_MCP_TOOLS = [
    {"type": "function", "function": {"name": "get_activities", "parameters": {}}},
    {"type": "function", "function": {"name": "get_activities_summary", "parameters": {}}},
]


class _ImmediateFinalAnswer:
    """Fake non-tool-call chunk stream — ends the loop after one LLM call."""

    def __init__(self):
        self._sent = False

    def __aiter__(self):
        return self

    async def __anext__(self):
        if self._sent:
            raise StopAsyncIteration
        self._sent = True
        choice = type("C", (), {"delta": type("D", (), {"content": "hi", "tool_calls": None})()})
        return type("Chunk", (), {"choices": [choice]})()


async def _collect_tool_names_seen(messages: list[dict], monkeypatch) -> list[str]:
    """Drive one _run_tool_loop pass and return the tool names offered on the
    first LLM call."""
    import app.llm as llm

    monkeypatch.setattr(llm, "load_catalog_tools", lambda: _async_return(_FAKE_MCP_TOOLS))
    seen_tool_names = []

    async def _fake_acompletion(*_args, **kwargs):
        seen_tool_names.append([t["function"]["name"] for t in kwargs["tools"]])
        return _ImmediateFinalAnswer()

    monkeypatch.setattr(llm.litellm, "acompletion", _fake_acompletion)

    async for _ in llm._run_tool_loop(messages):
        pass
    return seen_tool_names[0]


async def _async_return(value):
    return value


def test_tool_loop_excludes_bungee_summary_for_non_bungee_query(monkeypatch):
    messages = [{"role": "user", "content": "What rafting packages do you have?"}]
    tool_names = asyncio.run(_collect_tool_names_seen(messages, monkeypatch))

    assert "get_activities_summary" not in tool_names
    assert "get_activities" in tool_names


def test_tool_loop_includes_bungee_summary_for_bungee_query(monkeypatch):
    messages = [{"role": "user", "content": "bungee jumping prices?"}]
    tool_names = asyncio.run(_collect_tool_names_seen(messages, monkeypatch))

    assert "get_activities_summary" in tool_names


# --- StreamSanitizer (dash/activity-ID streaming backstop, §1.2/§2.2) -----

from app.stream_sanitizer import StreamSanitizer


def test_sanitizer_strips_em_dash():
    s = StreamSanitizer()
    out = s.feed("Nice pick") + s.feed("—") + s.feed(" let's go") + s.flush()
    assert "—" not in out


def test_sanitizer_maps_en_dash_to_hyphen_not_comma():
    # An en dash is very often a numeric range in this app's own content
    # ("20–130 kg") — mapping it to a comma would silently change the number.
    s = StreamSanitizer()
    out = s.feed("weight 20–130 kg") + s.flush()
    assert "–" not in out
    assert "20-130 kg" in out


def test_sanitizer_never_touches_markdown_table_syntax():
    # Regression: an earlier draft of this fix blindly stripped "--", which
    # would have corrupted this app's own comparison-table header-separator
    # rows (knowledge_base.md's mandated `|---|---|` format).
    s = StreamSanitizer()
    out = s.feed("| Feature | A | B |\n|---|---|---|\n") + s.flush()
    assert "|---|---|---|" in out


def test_sanitizer_strips_object_id_even_when_split_across_chunks():
    s = StreamSanitizer()
    out = "".join([
        s.feed("The activity is 66f1a2b3"),
        s.feed("c4d5e6f7a8b9c0d1 and it's great"),
        s.flush(),
    ])
    assert "66f1a2b3c4d5e6f7a8b9c0d1" not in out


def test_sanitizer_releases_output_progressively_once_past_the_tail_window():
    # Backs up the claim in test_chat.py's updated SSE-sequence test: the
    # tail hold-back (StreamSanitizer._TAIL) only delays short replies until
    # flush() — anything longer streams incrementally well before the
    # response ends.
    s = StreamSanitizer()
    released_before_flush = ""
    for word in ["This ", "is ", "a ", "longer ", "reply ", "well ", "past ", "the ", "buffer ", "window."]:
        released_before_flush += s.feed(word)
    assert released_before_flush != ""  # something came out before flush(), not just at the end
    full = released_before_flush + s.flush()
    assert full == "This is a longer reply well past the buffer window."


def test_sanitizer_strips_upper_case_id():
    # Live bug report: the model emitted "(ACTIVITY:69B90FEFB32379387CBAAC66)" -
    # the old [a-f0-9]-only pattern is case-sensitive and never matches upper-case
    # hex, so it sailed straight through unstripped.
    s = StreamSanitizer()
    out = s.feed("Plain River Rafting\n(ACTIVITY:69B90FEFB32379387CBAAC66)\n\nIncludes") + s.flush()
    assert "69B90FEFB32379387CBAAC66" not in out
    assert "Plain River Rafting" in out and "Includes" in out


def test_sanitizer_strips_id_regardless_of_surrounding_text():
    # Activities are referenced by name only now (no more activity: links), so
    # there's no "protected" case at all - any raw id gets stripped, always.
    s = StreamSanitizer()
    out = "".join([
        s.feed("see activity:66f1a2b3c4d5e6f7a8b9c0d1 for more, or "),
        s.feed("[Jumpin Heights](activity:77a2b3c4d5e6f7a8b9c0d1e2) here too"),
        s.flush(),
    ])
    assert "66f1a2b3c4d5e6f7a8b9c0d1" not in out
    assert "77a2b3c4d5e6f7a8b9c0d1e2" not in out


def test_sanitizer_strips_id_split_across_chunks():
    s = StreamSanitizer()
    out = "".join([
        s.feed("[Jumpin Heights]("),
        s.feed("activity:66f1a2b3"),
        s.feed("c4d5e6f7a8b9c0d1)"),
        s.flush(),
    ])
    assert "66f1a2b3c4d5e6f7a8b9c0d1" not in out


def test_sanitizer_strips_id_character_by_character_worst_case_chunking():
    # Worst-case provider chunking: one character per delta.
    s = StreamSanitizer()
    text = "[Jumpin Heights](activity:66f1a2b3c4d5e6f7a8b9c0d1) is great"
    out = "".join(s.feed(ch) for ch in text) + s.flush()
    assert "66f1a2b3c4d5e6f7a8b9c0d1" not in out
    assert "[Jumpin Heights](activity:)" in out


def test_sanitizer_strips_multiple_raw_ids_in_one_message():
    s = StreamSanitizer()
    text = (
        "Options: 66f1a2b3c4d5e6f7a8b9c0d1 and 77a2b3c4d5e6f7a8b9c0d1e2, "
        "pick one."
    )
    out = s.feed(text) + s.flush()
    assert "66f1a2b3c4d5e6f7a8b9c0d1" not in out
    assert "77a2b3c4d5e6f7a8b9c0d1e2" not in out
    assert "Options:" in out and "pick one." in out


def test_sanitizer_dash_mapping_unaffected_by_id_stripping():
    # The two jobs share one buffer/regex pass — make sure job 2 (id
    # stripping) didn't disturb job 1 (dash mapping) running alongside it.
    s = StreamSanitizer()
    out = s.feed("20–130 kg — right next to 66f1a2b3c4d5e6f7a8b9c0d1 raw") + s.flush()
    assert "20-130 kg , right next to" in out
    assert "66f1a2b3c4d5e6f7a8b9c0d1" not in out
    assert "—" not in out and "–" not in out


def test_sanitizer_empty_flush_is_harmless():
    s = StreamSanitizer()
    assert s.flush() == ""


def test_sanitizer_strips_id_glued_to_devanagari_script_with_no_space():
    # This app must reply in 26 Indian languages (knowledge_base.md's "Language:
    # mirror the user" rule). Python's `\b` is Unicode-aware - a Devanagari letter
    # counts as a "word" char to it, so an id glued directly against Hindi/Gujarati/
    # etc. text with no space had NO \b boundary at all and matched nothing,
    # leaking straight through. Anchoring on "not glued to more ASCII alnum"
    # instead closes this regardless of what script surrounds the id.
    s = StreamSanitizer()
    out = s.feed("अधिक जानकारी के लिए 66f1a2b3c4d5e6f7a8b9c0d1देखें") + s.flush()
    assert "66f1a2b3c4d5e6f7a8b9c0d1" not in out
    assert "अधिक जानकारी के लिए" in out and "देखें" in out


def test_sanitizer_strips_id_inside_inline_code_span():
    s = StreamSanitizer()
    out = s.feed("the id is `66f1a2b3c4d5e6f7a8b9c0d1` here") + s.flush()
    assert "66f1a2b3c4d5e6f7a8b9c0d1" not in out


@pytest.mark.parametrize("chunk_size", [1, 2, 3, 5, 7, 11, 16, 40, 1000])
def test_sanitizer_correct_at_every_chunk_size(chunk_size):
    # Real providers chunk unpredictably. Re-run the same realistic mixed reply
    # (multiple raw ids, an em dash and an en dash) sliced into every chunk
    # size from pathological (1 char) to a single chunk, and assert the
    # identical, fully-correct output every time.
    id_a, id_b = "66f1a2b3c4d5e6f7a8b9c0d1", "77a2b3c4d5e6f7a8b9c0d1e2"
    reply = (
        f"Compare Jumpin Heights ({id_a}) and "
        f"Splash Bungy ({id_b}) — 20–130 kg range."
    )
    expected = "Compare Jumpin Heights () and Splash Bungy () , 20-130 kg range."
    s = StreamSanitizer()
    chunks = [reply[i : i + chunk_size] for i in range(0, len(reply), chunk_size)]
    out = "".join(s.feed(c) for c in chunks) + s.flush()
    assert out == expected
    assert id_a not in out and id_b not in out


def test_sanitizer_logs_warning_when_raw_id_is_stripped(caplog):
    s = StreamSanitizer()
    with caplog.at_level("WARNING", logger="app.stream_sanitizer"):
        s.feed("raw id 66f1a2b3c4d5e6f7a8b9c0d1 here")
        s.flush()
    assert any("stripped 1 raw activity id" in r.message for r in caplog.records)


def test_sanitizer_does_not_log_for_clean_output(caplog):
    s = StreamSanitizer()
    with caplog.at_level("WARNING", logger="app.stream_sanitizer"):
        s.feed("**Jumpin Heights** — nice pick")
        s.flush()
    assert caplog.records == []


# --- _latest_user_message / _wants_catalog (§3.3 tool-choice gate) --------

from app.llm import _latest_user_message, _wants_catalog


def test_latest_user_message_handles_plain_string_content():
    messages = [{"role": "user", "content": "bungee prices?"}]
    assert _latest_user_message(messages) == "bungee prices?"


def test_latest_user_message_handles_attachment_content_blocks():
    # Regression: a message with attachment_ids has `content` as a list of
    # blocks (see build_messages()/_resolve_message_content), not a plain
    # string. Every regex-based gate in this file must not crash on that shape.
    messages = [{
        "role": "user",
        "content": [
            {"type": "text", "text": "what's in this pdf about rafting?"},
            {"type": "document", "source": {"type": "base64", "media_type": "application/pdf", "data": "..."}},
        ],
    }]
    assert "rafting" in _latest_user_message(messages)
    assert _wants_catalog(messages) is True  # doesn't raise, and matches correctly


def test_wants_catalog_true_for_catalog_terms():
    assert _wants_catalog([{"role": "user", "content": "what rafting packages do you have"}])


def test_wants_catalog_false_for_unrelated_chat():
    assert not _wants_catalog([{"role": "user", "content": "thanks so much!"}])


# --- _wants_knowledge (RAG-skip gate for small talk) -----------------------

from app.llm import _wants_knowledge


def test_wants_knowledge_false_for_greeting():
    assert not _wants_knowledge("hii how are you")


def test_wants_knowledge_false_for_capability_question():
    assert not _wants_knowledge("what can you do")


def test_wants_knowledge_true_for_catalog_question():
    assert _wants_knowledge("what rafting packages do you have in rishikesh")


def test_wants_knowledge_true_on_ambiguous_input():
    # Fail-safe direction: any word outside the small talk vocabulary means
    # retrieval still runs — never silently skipped on a miss.
    assert _wants_knowledge("hi, is bungee jumping safe for kids")


# --- flow_guard.is_protected_turn (§5.2) -----------------------------------

from app.flow_guard import is_protected_turn


@pytest.mark.parametrize("text", [
    "9876543210",      # phone number, mid-login
    "482913",           # OTP code
    "yes",               # upsell confirmation
    "is bungee safe if I have a heart condition?",
    "I'm really scared of heights",
])
def test_is_protected_turn_true_for_flow_and_safety_signals(text):
    assert is_protected_turn(text) is True


def test_is_protected_turn_false_for_ordinary_catalog_question():
    assert is_protected_turn("what rafting packages do you have in Rishikesh") is False


# --- OTP login -> verified phone capture (see app/token_store.py) --------
#
# The phone number is only ever attached to the session AFTER a real
# verify_otp success — i.e. after the user themselves read an SMS code and
# typed it back into chat. Nothing here changes how send_otp/verify_otp work;
# this just remembers the phone number that flow already proved.

import json
from types import SimpleNamespace

from app import llm as llm_module


def _fake_call(name, args_dict):
    return SimpleNamespace(function=SimpleNamespace(name=name, arguments=json.dumps(args_dict)))


def test_execute_tool_saves_verified_phone_only_after_verify_otp_succeeds(monkeypatch):
    async def fake_call_catalog_tool(call, session=None):
        if call.function.name == "verify_otp":
            return {"result": json.dumps({"authToken": "tok-123"})}
        return {"result": "{}"}

    saved = {}

    async def fake_save_verified_phone(session_id, phone):
        saved["session_id"] = session_id
        saved["phone"] = phone

    monkeypatch.setattr(llm_module, "call_catalog_tool", fake_call_catalog_tool)
    monkeypatch.setattr(llm_module, "save_verified_phone", fake_save_verified_phone)

    asyncio.run(llm_module._execute_tool(_fake_call("send_otp", {"phone": "+911234567890"}), "sess-otp"))
    asyncio.run(llm_module._execute_tool(_fake_call("verify_otp", {"otp": "482913"}), "sess-otp"))

    assert saved == {"session_id": "sess-otp", "phone": "+911234567890"}


def test_execute_tool_does_not_save_phone_when_verify_otp_fails(monkeypatch):
    async def fake_call_catalog_tool(call, session=None):
        return {"result": json.dumps({"success": False})}  # no authToken -> not verified

    async def fail_if_called(*a, **k):
        raise AssertionError("save_verified_phone must not be called on a failed verify")

    monkeypatch.setattr(llm_module, "call_catalog_tool", fake_call_catalog_tool)
    monkeypatch.setattr(llm_module, "save_verified_phone", fail_if_called)

    asyncio.run(llm_module._execute_tool(_fake_call("send_otp", {"phone": "+911234567890"}), "sess-fail"))
    asyncio.run(llm_module._execute_tool(_fake_call("verify_otp", {"otp": "000000"}), "sess-fail"))
    # No assertion error raised above == save_verified_phone was correctly skipped.


# --- RAG retrieval call site: off-thread + gated (latency fix) ------------

import threading

from app.config import settings as _settings
from app.schemas import ChatMessage as _ChatMessage


def test_rag_retrieval_runs_off_the_event_loop_and_is_gated(monkeypatch):
    seen = {}

    def fake_retrieve(query, top_k=1):
        seen["top_k"] = top_k
        seen["on_main_thread"] = threading.current_thread() is threading.main_thread()
        return "CHUNKS"

    monkeypatch.setattr(_settings, "weaviate_url", "http://weaviate:8080")
    monkeypatch.setattr(llm_module, "retrieve", fake_retrieve)

    messages = asyncio.run(
        llm_module.build_messages([_ChatMessage(role="user", content="rafting in rishikesh")])
    )
    assert seen == {"top_k": 6, "on_main_thread": False}
    assert "CHUNKS" in messages[0]["content"][1]["text"]

    # Smalltalk skips the call entirely — fake_retrieve would raise via `seen`
    # staying stale if it ran, but assert explicitly for clarity.
    seen.clear()
    asyncio.run(llm_module.build_messages([_ChatMessage(role="user", content="hii how are you")]))
    assert seen == {}


# --- Per-session message cap (§5: friendly "start a new chat" reply) ------


def _collect_frames(agen):
    async def _run():
        return [chunk async for chunk in agen]
    return asyncio.run(_run())


def test_stream_chat_response_declines_without_an_llm_call_once_capped(monkeypatch):
    # The cap is checked BEFORE build_messages/the tool loop run at all - no
    # model round-trip should happen once a session is capped out.
    async def fake_get_message_count(_session_id):
        return _settings.max_messages_per_session

    async def fail_if_called(*_a, **_k):
        raise AssertionError("litellm.acompletion must not be called once the session is capped")

    monkeypatch.setattr(llm_module, "get_message_count", fake_get_message_count)
    monkeypatch.setattr(llm_module.litellm, "acompletion", fail_if_called)

    frames = _collect_frames(llm_module.stream_chat_response(
        [_ChatMessage(role="user", content="one more question")], session_id="capped-session",
    ))

    assert len(frames) == 2
    assert '"done": false' in frames[0]
    assert "start a fresh chat" in frames[0]
    assert frames[1] == 'data: {"delta": "", "done": true, "session_capped": true}\n\n'


def test_stream_chat_response_does_not_check_the_cap_without_a_session_id(monkeypatch):
    # No session_id means nothing to key the cap on (e.g. a stateless caller) -
    # get_message_count must not even be called.
    async def fail_if_called(*_a, **_k):
        raise AssertionError("get_message_count must not be called without a session_id")

    async def raise_marker(*_a, **_k):
        raise RuntimeError("marker: got past the cap check")

    monkeypatch.setattr(llm_module, "get_message_count", fail_if_called)
    monkeypatch.setattr(llm_module, "build_messages", raise_marker)

    frames = _collect_frames(llm_module.stream_chat_response(
        [_ChatMessage(role="user", content="hi")], session_id=None,
    ))
    # stream_chat_response turns any exception into a friendly error frame -
    # reaching that (rather than an AssertionError bubbling out of the fake
    # get_message_count) proves the cap check was skipped, not tripped.
    assert any('"done": true' in f for f in frames)
