import asyncio
import json

from app import dashboard, session_store
from conftest import FakeRedis


def _run(coro):
    return asyncio.run(coro)


class _FakeToolUseBlock:
    type = "tool_use"

    def __init__(self, input_dict):
        self.input = input_dict


class _FakeSummaryResponse:
    def __init__(self, input_dict):
        self.content = [_FakeToolUseBlock(input_dict)]


_FAKE_FIELDS = {
    "summary": "User asked about bungee jumping prices in Rishikesh.",
    "topics": ["bungee jumping"],
    "questions_asked": ["How much is bungee jumping?"],
    "sentiment": "positive",
    "requires_followup": False,
    "outcome": "answered",
    "had_unanswered_question": False,
    "handoff_reason": "none",
    "tool_issues": [],
}


def test_summary_schema_requires_new_fields_and_matches_handoff_reasons():
    from app.handoff import HANDOFF_REASONS

    schema = dashboard.SUMMARY_TOOL["input_schema"]
    assert {"outcome", "had_unanswered_question", "handoff_reason", "tool_issues"} <= set(schema["required"])
    assert set(schema["properties"]["handoff_reason"]["enum"]) == {"none", *HANDOFF_REASONS}
    assert set(_FAKE_FIELDS) == set(schema["required"])


# --- summary generation ------------------------------------------------


def test_generate_summary_parses_tool_use_block(monkeypatch):
    async def fake_create(**kwargs):
        assert kwargs["tool_choice"] == {"type": "tool", "name": "submit_conversation_summary"}
        return _FakeSummaryResponse(_FAKE_FIELDS)

    fake_client = type("C", (), {"messages": type("M", (), {"create": staticmethod(fake_create)})()})()
    monkeypatch.setattr(dashboard, "get_aclient", lambda: fake_client)

    result = _run(dashboard._generate_summary([{"role": "user", "content": "hi"}]))
    assert result == _FAKE_FIELDS


# --- storage (isolated from the real data/session_summaries.json) -------


def test_append_and_list_and_get_summary(monkeypatch, tmp_path):
    monkeypatch.setattr(dashboard, "SUMMARIES_FILE", tmp_path / "session_summaries.json")

    record = {"session_id": "s1", "ended_at": "2026-08-08T00:00:00Z", **_FAKE_FIELDS}
    dashboard._append_summary(record)

    assert dashboard.list_summaries() == [record]
    assert dashboard.get_summary("s1") == record
    assert dashboard.get_summary("nope") is None


def test_list_summaries_filters_by_since(monkeypatch, tmp_path):
    monkeypatch.setattr(dashboard, "SUMMARIES_FILE", tmp_path / "session_summaries.json")
    dashboard._append_summary({"session_id": "old", "ended_at": "2026-08-01T00:00:00Z"})
    dashboard._append_summary({"session_id": "new", "ended_at": "2026-08-08T00:00:00Z"})

    result = dashboard.list_summaries(since="2026-08-05T00:00:00Z")
    assert [r["session_id"] for r in result] == ["new"]


# --- end-to-end idle scan -------------------------------------------------


def test_summarize_idle_sessions_appends_and_marks_summarized(monkeypatch, tmp_path):
    monkeypatch.setattr(dashboard, "SUMMARIES_FILE", tmp_path / "session_summaries.json")

    fake_redis = FakeRedis({
        "session:s1": {
            "messages": json.dumps([{"role": "user", "content": "How much is bungee jumping?"}]),
            "user_info": json.dumps({"name": "Rahul", "phone": "123", "email": ""}),
            "verified_phone": "+911234567890",
            "message_count": "1",
            "last_activity": "1000.0",
        },
    })
    monkeypatch.setattr(session_store, "redis_client", fake_redis)
    monkeypatch.setattr(session_store, "find_idle_sessions", lambda idle_seconds: _async_list(["s1"]))

    async def fake_create(**kwargs):
        return _FakeSummaryResponse(_FAKE_FIELDS)

    fake_client = type("C", (), {"messages": type("M", (), {"create": staticmethod(fake_create)})()})()
    monkeypatch.setattr(dashboard, "get_aclient", lambda: fake_client)

    count = _run(dashboard.summarize_idle_sessions())

    assert count == 1
    stored = dashboard.list_summaries()
    assert len(stored) == 1
    assert stored[0]["session_id"] == "s1"
    assert stored[0]["user_info"] == {"name": "Rahul", "phone": "123", "email": ""}
    assert stored[0]["verified_phone"] == "+911234567890"
    assert stored[0]["summary"] == _FAKE_FIELDS["summary"]
    assert fake_redis._data["session:s1"]["summarized"] == "true"


def test_summarize_idle_sessions_pushes_webhook_and_still_marks_summarized(monkeypatch, tmp_path):
    """Webhook wiring end-to-end: fired with the appended record, and a total
    webhook failure still lets the session get marked summarized (persistence
    already happened; GET /api/admin/session-summaries is the backstop).
    """
    monkeypatch.setattr(dashboard, "SUMMARIES_FILE", tmp_path / "session_summaries.json")
    monkeypatch.setattr(dashboard.settings, "summary_webhook_url", "https://example.com/hook")

    fake_redis = FakeRedis({
        "session:s1": {
            "messages": json.dumps([{"role": "user", "content": "How much is bungee jumping?"}]),
            "message_count": "1",
            "last_activity": "1000.0",
        },
    })
    monkeypatch.setattr(session_store, "redis_client", fake_redis)
    monkeypatch.setattr(session_store, "find_idle_sessions", lambda idle_seconds: _async_list(["s1"]))

    async def fake_create(**kwargs):
        return _FakeSummaryResponse(_FAKE_FIELDS)

    fake_client = type("C", (), {"messages": type("M", (), {"create": staticmethod(fake_create)})()})()
    monkeypatch.setattr(dashboard, "get_aclient", lambda: fake_client)

    fake_http = _FakeHttpClient([RuntimeError("dashboard receiver unreachable")] * dashboard._WEBHOOK_MAX_ATTEMPTS)
    monkeypatch.setattr(dashboard, "_get_http_client", lambda: fake_http)
    monkeypatch.setattr(dashboard.asyncio, "sleep", _fake_sleep)

    count = _run(dashboard.summarize_idle_sessions())

    assert count == 1
    assert len(fake_http.calls) == dashboard._WEBHOOK_MAX_ATTEMPTS
    assert json.loads(fake_http.calls[0]["content"])["session_id"] == "s1"
    # Persisted to the JSON file and marked summarized despite the webhook
    # never succeeding — delivery failure must not undo storage.
    assert dashboard.list_summaries()[0]["session_id"] == "s1"
    assert fake_redis._data["session:s1"]["summarized"] == "true"


def test_summarize_idle_sessions_skips_empty_transcript(monkeypatch, tmp_path):
    monkeypatch.setattr(dashboard, "SUMMARIES_FILE", tmp_path / "session_summaries.json")
    fake_redis = FakeRedis({
        "session:empty": {"messages": "[]", "message_count": "0", "last_activity": "1000.0"},
    })
    monkeypatch.setattr(session_store, "redis_client", fake_redis)
    monkeypatch.setattr(session_store, "find_idle_sessions", lambda idle_seconds: _async_list(["empty"]))

    count = _run(dashboard.summarize_idle_sessions())

    assert count == 1  # scanned, but nothing generated
    assert dashboard.list_summaries() == []
    assert fake_redis._data["session:empty"]["summarized"] == "true"


async def _async_list(value):
    return value


# --- webhook delivery -------------------------------------------------


class _FakeWebhookResponse:
    def __init__(self, status_code, headers=None):
        self.status_code = status_code
        self.headers = headers or {}


class _FakeHttpClient:
    """Queue of responses (or exceptions) to hand back from .post(), in order."""

    def __init__(self, responses):
        self.responses = list(responses)
        self.calls = []

    async def post(self, url, content=None, headers=None, timeout=None):
        self.calls.append({"url": url, "content": content, "headers": headers})
        result = self.responses.pop(0)
        if isinstance(result, Exception):
            raise result
        return result


async def _fake_sleep(_seconds):
    pass  # skip real backoff delay in tests


def test_send_webhook_noop_when_unconfigured(monkeypatch):
    monkeypatch.setattr(dashboard.settings, "summary_webhook_url", "")
    fake_client = _FakeHttpClient([])
    monkeypatch.setattr(dashboard, "_get_http_client", lambda: fake_client)

    _run(dashboard._send_webhook({"session_id": "s1"}))

    assert fake_client.calls == []


def test_send_webhook_signs_and_sends_record(monkeypatch):
    monkeypatch.setattr(dashboard.settings, "summary_webhook_url", "https://example.com/hook")
    monkeypatch.setattr(dashboard.settings, "summary_webhook_secret", "shh")
    fake_client = _FakeHttpClient([_FakeWebhookResponse(200)])
    monkeypatch.setattr(dashboard, "_get_http_client", lambda: fake_client)

    _run(dashboard._send_webhook({"session_id": "s1", "summary": "hi"}))

    assert len(fake_client.calls) == 1
    call = fake_client.calls[0]
    assert call["url"] == "https://example.com/hook"
    assert json.loads(call["content"]) == {"session_id": "s1", "summary": "hi"}
    sig = call["headers"]["X-Webhook-Signature"]
    assert sig.startswith("t=") and ",v1=" in sig


def test_send_webhook_omits_signature_when_no_secret(monkeypatch):
    monkeypatch.setattr(dashboard.settings, "summary_webhook_url", "https://example.com/hook")
    monkeypatch.setattr(dashboard.settings, "summary_webhook_secret", "")
    fake_client = _FakeHttpClient([_FakeWebhookResponse(200)])
    monkeypatch.setattr(dashboard, "_get_http_client", lambda: fake_client)

    _run(dashboard._send_webhook({"session_id": "s1"}))

    assert "X-Webhook-Signature" not in fake_client.calls[0]["headers"]


def test_send_webhook_retries_on_5xx_then_succeeds(monkeypatch):
    monkeypatch.setattr(dashboard.settings, "summary_webhook_url", "https://example.com/hook")
    fake_client = _FakeHttpClient([_FakeWebhookResponse(503), _FakeWebhookResponse(200)])
    monkeypatch.setattr(dashboard, "_get_http_client", lambda: fake_client)
    monkeypatch.setattr(dashboard.asyncio, "sleep", _fake_sleep)

    _run(dashboard._send_webhook({"session_id": "s1"}))

    assert len(fake_client.calls) == 2


def test_send_webhook_does_not_retry_on_4xx(monkeypatch):
    monkeypatch.setattr(dashboard.settings, "summary_webhook_url", "https://example.com/hook")
    fake_client = _FakeHttpClient([_FakeWebhookResponse(400)])
    monkeypatch.setattr(dashboard, "_get_http_client", lambda: fake_client)

    _run(dashboard._send_webhook({"session_id": "s1"}))

    assert len(fake_client.calls) == 1  # not retried — a 400 will just fail again


def test_send_webhook_never_raises_once_retries_exhausted(monkeypatch):
    monkeypatch.setattr(dashboard.settings, "summary_webhook_url", "https://example.com/hook")
    fake_client = _FakeHttpClient([RuntimeError("boom")] * dashboard._WEBHOOK_MAX_ATTEMPTS)
    monkeypatch.setattr(dashboard, "_get_http_client", lambda: fake_client)
    monkeypatch.setattr(dashboard.asyncio, "sleep", _fake_sleep)

    _run(dashboard._send_webhook({"session_id": "s1"}))  # must not raise

    assert len(fake_client.calls) == dashboard._WEBHOOK_MAX_ATTEMPTS
