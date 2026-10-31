"""KB rule: we do adventure activities only: no flight/train/hotel advice; pickup & drop stays ours."""
import asyncio
import json

import pytest

from app import llm as llm_module
from app.schemas import ChatMessage


@pytest.mark.parametrize("channel", [None, "whatsapp"])
def test_prompt_declines_travel_advice_but_keeps_pickup_and_drop(channel):
    msgs = [ChatMessage(role="user", content="Java avanu kai rite")]
    system = json.dumps(asyncio.run(llm_module.build_messages(msgs, "s-2", channel=channel))[0]["content"], ensure_ascii=False)
    assert "We do NOT arrange or advise on how to travel to Rishikesh" in system
    assert "no flights, trains, buses, cabs" in system
    assert "Pickup & drop IS ours" in system
    assert "activities and travel through Bucketlistt" not in system   # the old scope line invited travel advice
