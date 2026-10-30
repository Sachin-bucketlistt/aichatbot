"""Opt-in CHAT_API_KEY auth, rate-limit exemption, channel prompt, session_capped."""
import asyncio
import json

import pytest

from fastapi.testclient import TestClient

from app import llm as llm_module
from app import main as main_module
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
    wa_dyn, web_dyn = wa[0]["content"][-1]["text"], web[0]["content"][-1]["text"]
    assert "WhatsApp" in wa_dyn and "WhatsApp" not in web_dyn
    assert "Bucky" in web[0]["content"][0]["text"]  # web keeps its own persona


def test_session_capped_flag_on_cap_reply(monkeypatch):
    async def capped(_sid):
        return settings.max_messages_per_session

    monkeypatch.setattr(llm_module, "get_message_count", capped)

    async def run():
        return [f async for f in llm_module.stream_chat_response([ChatMessage(role="user", content="hi")], "s1")]

    frames = [json.loads(f[6:]) for f in asyncio.run(run())]
    assert frames[-1] == {"delta": "", "done": True, "session_capped": True}


def test_whatsapp_style_block_is_cached_and_web_never_gets_it():
    msgs = [ChatMessage(role="user", content="hello")]
    wa = asyncio.run(llm_module.build_messages(msgs, channel="whatsapp"))[0]["content"]
    web = asyncio.run(llm_module.build_messages(msgs))[0]["content"]
    assert len(wa) == 3 and wa[1]["cache_control"] == {"type": "ephemeral"}
    assert "real customer chats" in wa[1]["text"] and len(web) == 2


def test_style_file_and_repo_hold_no_customer_personal_data():
    import re
    from pathlib import Path
    root = Path(__file__).resolve().parents[2]
    style = (root / "backend/data/whatsapp_style.md").read_text(encoding="utf-8")
    assert not re.search(r"\d[\d\s-]{7,}\d|\+91|@|https?://|₹|\bRs\b", style)  # no numbers, emails, links, prices
    ignore = (root / ".gitignore").read_text()
    assert "whatsapp chat dataset/" in ignore


def test_cache_limiter_counts_every_system_breakpoint():
    from app.cache_limiter import _enforce_cache_limits
    cc = {"type": "ephemeral"}
    msgs = [{"role": "system", "content": [{"type": "text", "text": "a", "cache_control": cc},
                                           {"type": "text", "text": "b", "cache_control": cc}]}]
    msgs += [{"role": "tool", "content": [{"type": "text", "text": str(i), "cache_control": cc}]} for i in range(4)]
    out = _enforce_cache_limits(msgs)
    total = sum("cache_control" in b for m in out for b in m["content"])
    assert total == 4


def test_female_persona_is_whatsapp_only(monkeypatch):
    monkeypatch.setattr(settings, "whatsapp_persona_name", "Prachi")
    llm_module._whatsapp_style_cache = None
    msgs = [ChatMessage(role="user", content="hello")]
    wa = asyncio.run(llm_module.build_messages(msgs, channel="whatsapp"))[0]["content"]
    web = asyncio.run(llm_module.build_messages(msgs))[0]["content"]
    other = asyncio.run(llm_module.build_messages(msgs, channel="telegram"))[0]["content"]
    wa_text = " ".join(b["text"] for b in wa)
    assert "Prachi" in wa_text and "Bucky" not in wa_text and "{name}" not in wa_text
    for blocks in (web, other):  # website and unknown channels never see the persona
        text = " ".join(b["text"] for b in blocks)
        assert "Prachi" not in text and "feminine" not in text and "Bucky" in text


def test_persona_never_claims_to_be_human_and_answers_honestly():
    from pathlib import Path
    persona = (Path(__file__).resolve().parents[1] / "data/whatsapp_persona.md").read_text(encoding="utf-8")
    assert "Never say or imply that you are a human" in persona
    assert "asks DIRECTLY whether you are a bot" in persona and "tell the truth" in persona
    assert "Never volunteer the words assistant, virtual assistant" in persona


def test_anonymous_web_caller_cannot_select_whatsapp_channel(monkeypatch):
    seen = {}

    async def spy(_msgs, _sid=None, channel=None, handoff_state=None):
        seen["channel"] = channel
        yield 'data: {"delta": "", "done": true}\n\n'

    monkeypatch.setattr(main_module, "stream_chat_response", spy)
    client.post("/api/chat", json={**PAYLOAD, "channel": "whatsapp"})
    assert seen["channel"] is None
    monkeypatch.setattr(settings, "chat_api_key", "s3cret")
    client.post("/api/chat", json={**PAYLOAD, "channel": "whatsapp"}, headers={"Authorization": "Bearer s3cret"})
    assert seen["channel"] == "whatsapp"


def _dyn(text, *prior, channel=None):
    msgs = [ChatMessage(role="user", content=t) for t in (*prior, text)]
    return asyncio.run(llm_module.build_messages(msgs, channel=channel))[0]["content"][-1]["text"]


def test_edited_video_rule_fires_only_for_reel_questions():
    for q in ["Do you provide edited reel?", "edited video chahiye", "reel milegi kya?", "Instagram reel included?"]:
        dyn = _dyn(q)
        assert "Dronecraft" in dyn and "109 m" in dyn and "Rs 1,000" in dyn and "FREE" in dyn
    for q in ["hi", "bungee price?", "is video included?", "are you a real person?"]:
        assert "Edited reel / edited video" not in _dyn(q)
    assert "Edited reel / edited video" in _dyn("and the 109m?", "do you give edited reels?")  # follow-up keeps context


def test_whatsapp_instructions_do_not_contradict_the_persona():
    msgs = [ChatMessage(role="user", content="hello")]
    text = " ".join(b["text"] for b in asyncio.run(llm_module.build_messages(msgs, channel="whatsapp"))[0]["content"])
    for bad in ("person at a desk", "person on a phone", "on their phone"):
        assert bad not in text  # persona forbids claiming a desk or phone
    assert "follow them in this order" in text and "<examples>" in text

def test_virtual_assistant_is_only_ever_an_answer_to_a_direct_question():
    import re
    from pathlib import Path
    data = Path(__file__).resolve().parents[1] / "data"
    assert "virtual assistant" not in llm_module.WHATSAPP_FORMAT_PROMPT
    examples = (data / "whatsapp_style.md").read_text(encoding="utf-8")
    pairs = re.findall(r"^C: (.*)\nYou: (.*)$", examples, flags=re.M)
    said = [(c, y) for c, y in pairs if "virtual assistant" in y]
    assert said  # the honest answer exists
    for customer, _ in said:  # and is only ever shown as the reply to a direct bot/AI/human question
        assert re.search(r"\b(bot|ai|robot|human|real person|insaan)\b", customer, re.I)
