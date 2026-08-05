"""Resilience layer contract: retry-with-backoff, TTL cache, uniform errors.

All HTTP is mocked by patching ``httpx.Client`` (the module object is shared
by every connector) or the module-level helpers; no test touches the network.
"""

from __future__ import annotations

import json

import httpx
import pytest

from legal_helper.connectors import base
from legal_helper.connectors import us_federal


class FakeResponse:
    def __init__(self, status_code=200, payload=None, text=None, headers=None):
        self.status_code = status_code
        self._payload = payload
        self.text = text if text is not None else json.dumps(payload or {})
        self.headers = headers or {}

    def raise_for_status(self):
        if self.status_code >= 400:
            raise httpx.HTTPStatusError(
                f"status {self.status_code}", request=None, response=self
            )

    def json(self):
        if self._payload is None:
            raise ValueError("not json")
        return self._payload


class FakeClient:
    """Context-manager stand-in for httpx.Client fed a script of responses.
    A response may also be an exception instance, which is raised."""

    script: list = []
    requests: list = []

    def __init__(self, *args, **kwargs):
        pass

    def __enter__(self):
        return self

    def __exit__(self, *args):
        return False

    def request(self, method, url, params=None, json=None, data=None, headers=None):
        FakeClient.requests.append((method, url, params, json, headers))
        item = FakeClient.script.pop(0)
        if isinstance(item, Exception):
            raise item
        return item


@pytest.fixture()
def fake_http(monkeypatch):
    FakeClient.script = []
    FakeClient.requests = []
    sleeps: list[float] = []
    monkeypatch.setattr(base.httpx, "Client", FakeClient)
    monkeypatch.setattr(base, "_sleep", sleeps.append)
    base._RESPONSE_CACHE.clear()
    yield FakeClient, sleeps
    base._RESPONSE_CACHE.clear()


def test_retry_on_429_honors_retry_after_then_succeeds(fake_http):
    client, sleeps = fake_http
    client.script = [
        FakeResponse(429, headers={"Retry-After": "2"}),
        FakeResponse(429, headers={"Retry-After": "1"}),
        FakeResponse(200, payload={"ok": True}),
    ]
    resp = base.request_with_retry("GET", "https://x.test/api")
    assert resp.json() == {"ok": True}
    assert sleeps == [2.0, 1.0]
    assert len(client.requests) == 3


def test_retry_on_transport_error(fake_http):
    client, sleeps = fake_http
    client.script = [httpx.ConnectError("refused"), FakeResponse(200, payload={"ok": 1})]
    resp = base.request_with_retry("GET", "https://x.test/api")
    assert resp.json() == {"ok": 1}
    assert len(sleeps) == 1 and sleeps[0] > 0


def test_retry_exhaustion_raises_last_error(fake_http):
    client, sleeps = fake_http
    client.script = [FakeResponse(503), FakeResponse(503), FakeResponse(503)]
    with pytest.raises(httpx.HTTPStatusError):
        base.request_with_retry("GET", "https://x.test/api")
    assert len(client.requests) == base.RETRY_ATTEMPTS
    assert len(sleeps) == base.RETRY_ATTEMPTS - 1


def test_non_retryable_4xx_raises_immediately(fake_http):
    client, sleeps = fake_http
    client.script = [FakeResponse(404)]
    with pytest.raises(httpx.HTTPStatusError):
        base.request_with_retry("GET", "https://x.test/api")
    assert len(client.requests) == 1
    assert sleeps == []


def test_non_json_200_raises_connector_error_caught_as_httperror(fake_http):
    client, _ = fake_http
    client.script = [FakeResponse(200, payload=None, text="<html>WAF page</html>")]
    with pytest.raises(httpx.HTTPError):
        base.http_get("https://x.test/api", {}, cache_ttl=0)


def test_search_connector_returns_uniform_error_dict_on_non_json(fake_http):
    """A 200 with an HTML body must yield {"error": ...}, not a crash."""
    client, _ = fake_http
    client.script = [FakeResponse(200, payload=None, text="<html>maintenance</html>")]
    payload = json.loads(us_federal.ecfr_search.call({"query": "manifest"}))
    assert "error" in payload
    assert "non-JSON" in payload["error"]


def test_search_connector_returns_uniform_error_dict_on_exhausted_5xx(fake_http):
    client, _ = fake_http
    client.script = [FakeResponse(502)] * base.RETRY_ATTEMPTS
    payload = json.loads(us_federal.ecfr_search.call({"query": "manifest"}))
    assert "error" in payload and payload["query"] == "manifest"


def test_http_get_ttl_cache_hits_within_ttl(fake_http):
    client, _ = fake_http
    client.script = [FakeResponse(200, payload={"n": 1})]
    a = base.http_get("https://x.test/api", {"q": "z"}, cache_ttl=60)
    b = base.http_get("https://x.test/api", {"q": "z"}, cache_ttl=60)
    assert a == b == {"n": 1}
    assert len(client.requests) == 1  # second call served from cache

    # Different auth headers must never share a cache slot.
    client.script = [FakeResponse(200, payload={"n": 2})]
    c = base.http_get(
        "https://x.test/api", {"q": "z"}, headers={"X-Api-Key": "k"}, cache_ttl=60
    )
    assert c == {"n": 2}


def test_ttl_cache_expiry_and_eviction():
    cache = base.TTLCache(maxsize=2)
    cache.set("a", 1, ttl=60)
    assert cache.get("a") == 1
    cache.set("b", 2, ttl=0)  # already expired
    assert cache.get("b") is base.TTLCache._MISS
    cache.set("c", 3, ttl=60)
    cache.set("d", 4, ttl=60)  # evicts the oldest entry
    assert cache.get("d") == 4


def test_cache_key_is_stable_sha256():
    assert base.cache_key("q", 8) == base.cache_key("q", 8)
    assert base.cache_key("q", 8) != base.cache_key("q", 9)
    assert len(base.cache_key("q")) == 64
    int(base.cache_key("q"), 16)  # hex digest


def test_ecfr_issue_date_uses_ttl_and_never_caches_failures(monkeypatch):
    us_federal._ISSUE_DATE_CACHE.clear()
    calls = {"n": 0}
    titles_payload = {"titles": [{"number": 14, "latest_issue_date": "2026-07-01"}]}

    def failing(method, url, **kwargs):
        calls["n"] += 1
        raise httpx.ConnectError("down")

    monkeypatch.setattr(us_federal, "request_with_retry", failing)
    assert us_federal._ecfr_latest_issue_date(14) == (None, None)
    assert calls["n"] == 1

    def ok(method, url, **kwargs):
        calls["n"] += 1
        return FakeResponse(200, payload=titles_payload)

    monkeypatch.setattr(us_federal, "request_with_retry", ok)
    date, resolved_at = us_federal._ecfr_latest_issue_date(14)
    assert date == "2026-07-01" and resolved_at
    assert calls["n"] == 2  # the earlier failure was not negatively cached

    monkeypatch.setattr(us_federal, "request_with_retry", failing)
    assert us_federal._ecfr_latest_issue_date(14) == (date, resolved_at)
    assert calls["n"] == 2  # served from the 24h TTL cache
    us_federal._ISSUE_DATE_CACHE.clear()
