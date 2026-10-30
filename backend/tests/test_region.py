"""State tag on the first WhatsApp message -> reply-language prompt; tag never reaches the model."""
import asyncio
import json

import pytest

from app import llm as llm_module
from app import session_store
from app.region import region_from_messages, split_region_tag
from app.schemas import ChatMessage
from tests.conftest import FakeRedis

FIRST = "Hi! I want to book Bungee or Rafting in Rishikesh. Please share price. "


def _msgs(first, *more):
    out = [ChatMessage(role="user", content=first)]
    for i, text in enumerate(more):
        out.append(ChatMessage(role="assistant" if i % 2 == 0 else "user", content=text))
    return out


@pytest.mark.parametrize("tag,code", [("(GJ)", "GJ"), ("(mh)", "MH"), ("(DL)", "DL"), ("[gj]", "GJ"), ("( DL )", "DL")])
def test_tag_is_found_and_stripped(tag, code):
    clean, found = split_region_tag(FIRST + tag)
    assert found == code
    assert clean == FIRST.strip()


def test_unknown_or_unbracketed_codes_are_ignored():
    for text in ["bungee price (UP)", "gj bungee", "dl price", "I am from GJ", "call me (HR)"]:
        assert split_region_tag(text) == (text, None)


def test_only_the_first_user_message_counts_and_is_cleaned():
    msgs = _msgs(FIRST + "(GJ)", "Sure! Kitne log?", "2 people (DL)")
    cleaned, code = region_from_messages(msgs)
    assert code == "GJ"
    assert "(GJ)" not in cleaned[0].content and cleaned[2].content == "2 people (DL)"
    assert msgs[0].content.endswith("(GJ)")          # the caller's object is untouched


def test_tag_only_message_still_has_text():
    cleaned, code = region_from_messages(_msgs("(MH)"))
    assert code == "MH" and cleaned[0].content == "Hi"


def _system(messages):
    return json.dumps(messages[0]["content"]), json.dumps(messages[1:])


@pytest.mark.parametrize("code,phrase", [
    ("GJ", "Gujarati written in English letters"), ("MH", "Marathi written in English letters"),
    ("DL", "Hinglish (Hindi in English letters)"),
])
def test_whatsapp_gets_language_prompt_and_model_never_sees_tag(code, phrase):
    messages = asyncio.run(llm_module.build_messages(_msgs(FIRST + f"({code})"), "wa-1", channel="whatsapp"))
    system, rest = _system(messages)
    assert phrase in system and "Roman script only" in system
    assert f"({code})" not in rest and "(GJ)" not in system


def test_untagged_whatsapp_and_web_get_no_language_prompt():
    for channel in ("whatsapp", None):
        messages = asyncio.run(llm_module.build_messages(_msgs(FIRST + "(GJ)" if channel is None else FIRST), "s-1", channel=channel))
        system, _ = _system(messages)
        assert "Reply language — set by the customer's region" not in system


def test_region_survives_a_trimmed_history_via_redis(monkeypatch):
    monkeypatch.setattr(session_store, "redis_client", FakeRedis())
    asyncio.run(llm_module.build_messages(_msgs(FIRST + "(MH)"), "wa-2", channel="whatsapp"))
    later = [ChatMessage(role="user", content="2 people")]        # CRM dropped the first message
    messages = asyncio.run(llm_module.build_messages(later, "wa-2", channel="whatsapp"))
    assert "Marathi written in English letters" in _system(messages)[0]
