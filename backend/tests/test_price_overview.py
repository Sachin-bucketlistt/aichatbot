"""The fixed WhatsApp price message: sent word for word for "all prices" asks, never for one activity."""
import pytest
from fastapi.testclient import TestClient

from app import llm as llm_module
from app import session_store
from app.config import settings
from app.main import app
from app.price_overview import price_overview_text, should_send_overview, wants_price_overview
from app.rate_limit import _hits

client = TestClient(app)
AUTH = {"Authorization": "Bearer k"}


@pytest.mark.parametrize("text", [
    "send me the price list", "all prices please", "what all activities do you offer?", "Price range?",
    "rate card", "details of all the activities", "sabhi activities ke prices batao", "what are your prices",
    "full details please", "tell me everything you offer", "Hi I want to know about bungee and rafting prices",
    "prices", "options", "bungee and rafting prices", "tell me about activities", "kya prices hai", "what is the cost",
])
def test_overview_asks_match(text):
    assert wants_price_overview(text)


@pytest.mark.parametrize("text", [
    "bungee price?", "what are the prices for rafting", "I want to book bungee", "hi", "",
    "rafting options", "price of 111m", "price for 4 people", "I want both with package", "I want to book bungee and rafting",
    "x " * 400 + "price list",
])
def test_specific_or_other_messages_do_not_match(text):
    assert not wants_price_overview(text)


def test_only_early_in_the_chat_and_only_once():
    ask = "prices"
    assert should_send_overview([ask], [])
    assert should_send_overview(["hi", "hello", ask], ["Hi!", "Hey"])
    assert not should_send_overview(["a", "b", "c", ask], [])                         # booking stage by now
    assert not should_send_overview([ask], [price_overview_text()])                    # already sent
    assert not should_send_overview([], [])


def test_message_is_the_one_the_user_wrote():
    t = price_overview_text()
    assert t.startswith("Hi! Welcome to Bucketlistt!") and t.endswith("📞 Call/WhatsApp: 8511838237")
    assert "~₹2,500~ ₹2,000" in t and "219m Maa Ganga Bungy, Devprayag: ₹6,000" in t


@pytest.fixture(autouse=True)
def _setup(monkeypatch):
    _hits.clear()
    monkeypatch.setattr(settings, "chat_api_key", "k")
    monkeypatch.setattr(session_store, "redis_client", None)
    yield
    _hits.clear()


def _no_llm(monkeypatch):
    async def boom(*_a, **_k):
        raise AssertionError("the model must not be called")
    monkeypatch.setattr(llm_module.litellm, "acompletion", boom)


def test_v2_whatsapp_gets_exact_message_in_one_bubble_without_calling_the_model(monkeypatch):
    _no_llm(monkeypatch)
    r = client.post("/api/v2/chat", headers=AUTH, json={
        "messages": [{"role": "user", "content": "send me the price list"}], "session_id": "wa-po-1"}).json()
    assert r["reply"] == price_overview_text()
    assert r["messages"] == [price_overview_text()]      # strikethrough kept, not split
    assert not r["handoff"] and not r["error"]


def test_discount_ask_still_goes_to_the_normal_flow(monkeypatch):
    seen = []

    async def fake(*_a, **_k):
        seen.append(1)
        raise RuntimeError("normal flow reached")
    monkeypatch.setattr(llm_module.litellm, "acompletion", fake)
    r = client.post("/api/v2/chat", headers=AUTH, json={
        "messages": [{"role": "user", "content": "all prices and any extra discount?"}], "session_id": "wa-po-2"}).json()
    assert seen and r["reply"] != price_overview_text()


def test_web_chat_never_gets_it(monkeypatch):
    async def fake(*_a, **_k):
        raise RuntimeError("normal flow reached")
    monkeypatch.setattr(llm_module.litellm, "acompletion", fake)
    body = client.post("/api/chat", json={"messages": [{"role": "user", "content": "send me the price list"}]}).text
    assert "Welcome to Bucketlistt" not in body
