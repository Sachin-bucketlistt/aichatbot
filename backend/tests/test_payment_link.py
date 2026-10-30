"""create_payment_link is offered only to a logged-in WhatsApp session; the web chat never sees it."""
import asyncio
import json

from app import llm as llm_module
from app import token_store
from app.schemas import ChatMessage
from tests.test_otp_flow import _Stream

SID = "wa-pay-1"
USER = [ChatMessage(role="user", content="send me the payment link")]


def _schema(name):
    return {"type": "function", "function": {"name": name, "description": "", "parameters": {"type": "object"}}}


def _offered_tools(monkeypatch, channel, logged_in):
    token_store._store.clear()
    if logged_in:
        token_store.set_token(SID, "tok-123")
    seen = {}

    async def tools():
        return [_schema("get_cart"), _schema("create_payment_link")]

    async def fake_acompletion(*_a, **kw):
        seen["tools"] = [t["function"]["name"] for t in kw.get("tools") or []]
        return _Stream(content="ok")

    monkeypatch.setattr(llm_module, "load_catalog_tools", tools)
    monkeypatch.setattr(llm_module.litellm, "acompletion", fake_acompletion)

    async def run():
        messages = await llm_module.build_messages(USER, SID, channel=channel)
        return [e async for e in llm_module._run_tool_loop(messages, SID, None, channel)], messages

    _, messages = asyncio.run(run())
    return seen["tools"], json.dumps(messages[0]["content"])


def test_whatsapp_logged_in_gets_tool_and_choice_prompt(monkeypatch):
    tools, system = _offered_tools(monkeypatch, "whatsapp", logged_in=True)
    assert "create_payment_link" in tools
    assert "OVERRIDES the 'you cannot take payment'" in system


def test_whatsapp_not_logged_in_gets_neither(monkeypatch):
    tools, system = _offered_tools(monkeypatch, "whatsapp", logged_in=False)
    assert "create_payment_link" not in tools
    assert "OVERRIDES the 'you cannot take payment'" not in system


def test_web_never_gets_it_even_when_logged_in(monkeypatch):
    tools, system = _offered_tools(monkeypatch, None, logged_in=True)
    assert "create_payment_link" not in tools
    assert "OVERRIDES the 'you cannot take payment'" not in system


def test_auth_token_is_injected_into_payment_call():
    token_store._store.clear()
    token_store.set_token(SID, "tok-123")
    call = type("C", (), {"function": type("F", (), {"name": "create_payment_link", "arguments": "{}"})()})()
    llm_module._inject_auth_token(call, SID)
    assert json.loads(call.function.arguments)["authToken"] == "tok-123"


def test_payment_call_keeps_only_schema_args():
    token_store._store.clear()
    token_store.set_token(SID, "tok-123")
    item = {"experienceId": "e", "activityId": "a", "timeSlotId": "t", "timeSlotLabel": "09:00 - 10:00",
            "bookingDate": "2026-11-01", "totalParticipants": 2, "price": 1, "amount": 1}
    args = {"contactPersonName": "A", "contactNumber": "9", "items": [item], "amount": 1,
            "totalAmount": 1, "discount": 99, "callbackUrl": "https://evil.example"}
    call = type("C", (), {"function": type("F", (), {"name": "create_payment_link", "arguments": json.dumps(args)})()})()
    llm_module._inject_auth_token(call, SID)
    out = json.loads(call.function.arguments)
    assert out == {"contactPersonName": "A", "contactNumber": "9", "authToken": "tok-123",
                   "items": [{k: v for k, v in item.items() if k not in ("price", "amount")}]}
    assert out["items"][0]["totalParticipants"] == 2   # the old regex wrongly dropped this ("total")
