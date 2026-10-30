"""Gujarati-ask coupon: given only when the customer asks for a discount as a Gujarati, never upfront."""
import asyncio
import json

import pytest

from app import llm as llm_module
from app.handoff import GUJARATI_COUPON, asks_gujarati_discount, detect_handoff
from app.schemas import ChatMessage


@pytest.mark.parametrize("text", [
    "Gujarti ke liye special discount", "I am Gujarati, any discount?", "gujju hu kuch kam karo",
    "Gujarat se hu, koi offer hai?", "GUJRATI discount milega",
])
def test_gujarati_discount_asks_are_detected(text):
    assert asks_gujarati_discount([text])
    assert detect_handoff(text) == "discount_request"   # and still handed to a human


@pytest.mark.parametrize("texts", [
    ["I'm from Gujarat, bungee price?"], ["any discount?"], ["Hi! I want to book bungee (GJ)"],
    ["I am Gujarati", "any discount?"],            # the two facts must be in one message
    ["gujarat se hu", "rafting price kitna hai"],
])
def test_other_messages_are_not_gujarati_discount_asks(texts):
    assert not asks_gujarati_discount(texts)


def _system(messages, handoff=True, channel="whatsapp"):
    msgs = [ChatMessage(role="user" if i % 2 == 0 else "assistant", content=t) for i, t in enumerate(messages)]
    built = asyncio.run(llm_module.build_messages(msgs, "wa-g-1", channel=channel, handoff_enabled=handoff))
    return json.dumps(built[0]["content"])


def test_coupon_only_in_prompt_after_a_gujarati_ask():
    assert GUJARATI_COUPON not in _system(["Hi, bungee price?"])                    # never upfront
    assert GUJARATI_COUPON not in _system(["any discount?"])
    assert GUJARATI_COUPON in _system(["Gujarti ke liye special discount"])
    assert GUJARATI_COUPON in _system(["Gujarti ke liye special discount", "ok", "what was the code?"])


def test_coupon_never_reaches_web_or_non_handoff_callers():
    ask = ["Gujarti ke liye special discount"]
    assert GUJARATI_COUPON not in _system(ask, handoff=False, channel=None)
    assert GUJARATI_COUPON not in _system(ask, handoff=False)


def test_payment_prompt_warns_that_a_coupon_needs_the_website(monkeypatch):
    from app import token_store
    token_store.set_token("wa-g-1", "tok")
    assert "payment link cannot apply it" in _system(["bungee price?"])
    token_store._store.clear()
