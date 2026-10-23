"""Rate-limiter spoofing/ceiling, upload auth, admin compare, session_id length."""
import json

import pytest
from fastapi.testclient import TestClient
from starlette.requests import Request

from app import rate_limit, session_store
from app.config import settings
from app.main import app

client = TestClient(app)
BODY = {"messages": [{"role": "user", "content": "hello there friend"}], "session_id": "s1"}


@pytest.fixture(autouse=True)
def _setup(monkeypatch):
    rate_limit._hits.clear()
    monkeypatch.setattr(settings, "chat_api_key", "k")
    monkeypatch.setattr(session_store, "redis_client", None)
    yield
    rate_limit._hits.clear()


def _req(xff=None, host="9.9.9.9"):
    headers = [(b"x-forwarded-for", xff.encode())] if xff else []
    return Request({"type": "http", "headers": headers, "client": (host, 1), "method": "POST", "path": "/"})


def test_client_ip_reads_xff_from_the_right(monkeypatch):
    monkeypatch.setattr(settings, "trusted_proxy_hops", 1)
    assert rate_limit._client_ip(_req("6.6.6.6, 1.2.3.4")) == "1.2.3.4"   # forged left entry ignored
    monkeypatch.setattr(settings, "trusted_proxy_hops", 2)
    assert rate_limit._client_ip(_req("6.6.6.6, 1.2.3.4, 10.0.0.1")) == "1.2.3.4"
    monkeypatch.setattr(settings, "trusted_proxy_hops", 0)
    assert rate_limit._client_ip(_req("6.6.6.6")) == "9.9.9.9"            # header ignored


def test_spoofed_xff_no_longer_dodges_the_limit(monkeypatch):
    monkeypatch.setattr(settings, "trusted_proxy_hops", 1)
    monkeypatch.setattr(rate_limit, "CHAT_LIMIT_PER_MINUTE", 3)
    codes = [
        client.post("/api/v2/chat", json=BODY, headers={"Authorization": "Bearer bad", "X-Forwarded-For": f"203.0.113.{i}, 1.2.3.4"}).status_code
        for i in range(5)
    ]
    assert codes == [401, 401, 401, 429, 429]


def test_valid_key_has_a_total_ceiling(monkeypatch):
    monkeypatch.setattr(rate_limit, "KEY_LIMIT_PER_MINUTE", 2)
    ok = {"Authorization": "Bearer k"}
    codes = [client.post("/api/chat", json={"messages": []}, headers=ok).status_code for _ in range(3)]
    assert codes == [422, 422, 429]   # 422 = got past the limiter into validation


def test_idle_buckets_are_swept(monkeypatch):
    monkeypatch.setattr(rate_limit, "_MAX_TRACKED", 2)
    for i in range(5):
        rate_limit._hits[f"ip{i}"]  # empty deques, as left behind by expired hits
    rate_limit._over_limit("new", 10)
    assert list(rate_limit._hits) == ["new"]


def test_attachment_upload_requires_the_key():
    files = {"file": ("a.txt", b"hello", "text/plain")}
    assert client.post("/api/chat/attachments", files=files).status_code == 401
    assert client.post("/api/chat/attachments", files=files, headers={"Authorization": "Bearer k"}).status_code != 401


def test_overlong_session_id_rejected():
    r = client.post("/api/v2/chat", json={**BODY, "session_id": "x" * 129}, headers={"Authorization": "Bearer k"})
    assert r.status_code == 422


def test_admin_key_check(monkeypatch):
    monkeypatch.setattr(settings, "dashboard_api_key", "adm")
    assert client.get("/api/admin/session-summaries", headers={"Authorization": "Bearer nope"}).status_code == 401
    assert client.get("/api/admin/session-summaries", headers={"Authorization": "Bearer adm"}).status_code == 200
