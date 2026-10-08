"""A remote MCP gateway that keeps timing out is skipped, not waited on."""

from __future__ import annotations

import json

import httpx

from legal_helper.mcp import sync_client
from legal_helper.mcp.registry import McpServerSpec


def _spec(name: str = "pkulaw_fatiao") -> McpServerSpec:
    return McpServerSpec(name=name, transport="http", url="https://gw.example.com/" + name)


def _fail_524(*args, **kwargs):
    request = httpx.Request("POST", "https://gw.example.com/x")
    raise httpx.HTTPStatusError(
        "Server error '524'", request=request, response=httpx.Response(524, request=request)
    )


def test_breaker_opens_after_repeated_gateway_failures_and_closes_on_success(monkeypatch):
    sync_client._BREAKER.clear()
    calls = {"n": 0}

    def failing(*args, **kwargs):
        calls["n"] += 1
        return _fail_524()

    monkeypatch.setattr(sync_client, "request_with_retry", failing)
    for _ in range(2):
        out = json.loads(sync_client.http_call_tool(_spec(), "get_law_item_content", {}))
        assert "mcp call failed" in out["error"]
    assert calls["n"] == 2

    # Open: a sibling service on the same gateway host is not called at all.
    out = json.loads(sync_client.http_call_tool(_spec("pkulaw_law_search"), "search_article", {}))
    assert out["unavailable"] is True and calls["n"] == 2
    assert "not called" in out["error"]

    # After the cooldown one call goes through; a success closes the breaker.
    monkeypatch.setattr(sync_client, "_BREAKER_COOLDOWN_SECONDS", 0.0)

    class _Resp:
        headers = {"content-type": "application/json"}

        def json(self):
            return {"jsonrpc": "2.0", "id": 1, "result": {"content": [{"type": "text", "text": "第五百七十七条"}]}}

        @property
        def text(self):
            return json.dumps(self.json())

    monkeypatch.setattr(sync_client, "request_with_retry", lambda *a, **k: _Resp())
    monkeypatch.setattr(sync_client, "_parse_jsonrpc", lambda resp: resp.json())
    assert sync_client.http_call_tool(_spec(), "get_law_item_content", {}) == "第五百七十七条"
    assert "gw.example.com" not in sync_client._BREAKER


def test_a_client_error_does_not_open_the_breaker(monkeypatch):
    sync_client._BREAKER.clear()

    def bad_request(*args, **kwargs):
        request = httpx.Request("POST", "https://gw.example.com/x")
        raise httpx.HTTPStatusError("400", request=request, response=httpx.Response(400, request=request))

    monkeypatch.setattr(sync_client, "request_with_retry", bad_request)
    for _ in range(3):
        sync_client.http_call_tool(_spec(), "get_law_item_content", {})
    assert sync_client._breaker_open("gw.example.com") == 0.0
