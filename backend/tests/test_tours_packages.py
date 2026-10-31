"""KB rule: we sell activities and activity packages, not tours; the prompt must say so (web and WhatsApp)."""
import asyncio
import json

import pytest

from app import llm as llm_module
from app.schemas import ChatMessage


@pytest.mark.parametrize("channel", [None, "whatsapp"])
def test_prompt_tells_the_bot_to_decline_tours_but_offer_activity_packages(channel):
    msgs = [ChatMessage(role="user", content="do you have tour packages?")]
    system = json.dumps(asyncio.run(llm_module.build_messages(msgs, "s-1", channel=channel))[0]["content"])
    assert "we don't provide tours and packages" in system
    assert "we do provide activity packages" in system
