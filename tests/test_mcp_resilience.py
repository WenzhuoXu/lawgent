"""MCP boundary resilience: sync-client retries, uniform errors, the
ccar_aviation disabled-endpoint notice, and the PKULaw health probe."""

from __future__ import annotations

import json

import httpx
import pytest

from legal_helper.connectors import base
from legal_helper.mcp import health, sync_client
from legal_helper.mcp.registry import McpServerSpec
from legal_helper.mcp.servers.ccar_aviation.__main__ import dispatch


class FakeResponse:
    def __init__(self, status_code=200, payload=None, headers=None):
        self.status_code = status_code
        self._payload = payload
        self.headers = headers or {"content-type": "application/json"}
        self.text = json.dumps(payload or {})

    def raise_for_status(self):
        if self.status_code >= 400:
            raise httpx.HTTPStatusError(
                f"status {self.status_code}", request=None, response=self
            )

    def json(self):
        return self._payload


class FakeClient:
    script: list = []
    requests: list = []

    def __init__(self, *args, **kwargs):
        pass

    def __enter__(self):
        return self

    def __exit__(self, *args):
        return False

    def request(self, method, url, params=None, json=None, data=None, headers=None):
        FakeClient.requests.append((method, url, json, headers))
        item = FakeClient.script.pop(0)
        if isinstance(item, Exception):
            raise item
        return item


@pytest.fixture()
def fake_http(monkeypatch):
    FakeClient.script = []
    FakeClient.requests = []
    monkeypatch.setattr(base.httpx, "Client", FakeClient)
    monkeypatch.setattr(base, "_sleep", lambda s: None)
    yield FakeClient


_SPEC = McpServerSpec(
    name="pkulaw_fatiao",
    transport="http",
    url="https://apim.test/mcp-fatiao",
    headers={"apikey": "t"},
)


def test_http_call_tool_retries_transient_5xx(fake_http):
    fake_http.script = [
        FakeResponse(503),
        FakeResponse(
            200,
            payload={
                "jsonrpc": "2.0",
                "id": 1,
                "result": {"content": [{"type": "text", "text": "第七百零七条 …"}]},
            },
        ),
    ]
    out = sync_client.http_call_tool(_SPEC, "get_law_item_content", {"law_name": "民法典"})
    assert out == "第七百零七条 …"
    assert len(fake_http.requests) == 2
    # WSO2 apikey header must ride along on every attempt.
    assert all(h.get("apikey") == "t" for (_, _, _, h) in fake_http.requests)


def test_http_call_tool_uniform_error_after_exhaustion(fake_http):
    fake_http.script = [FakeResponse(503)] * base.RETRY_ATTEMPTS
    out = json.loads(sync_client.http_call_tool(_SPEC, "get_law_item_content", {}))
    assert "error" in out
    assert out["server"] == "pkulaw_fatiao"


def test_http_list_tools_retries_then_soft_fails(fake_http):
    fake_http.script = [httpx.ConnectError("blip"), FakeResponse(
        200,
        payload={"jsonrpc": "2.0", "id": 1, "result": {"tools": [{"name": "x"}]}},
    )]
    assert sync_client.http_list_tools(_SPEC) == [{"name": "x"}]

    fake_http.script = [FakeResponse(500)] * base.RETRY_ATTEMPTS
    assert sync_client.http_list_tools(_SPEC) == []  # soft-fail, never raises


def test_ccar_mcp_returns_disabled_notice():
    for tool in ("search_ccar", "fetch_ccar"):
        payload = json.loads(dispatch(tool, {"query": "CCAR-145"}))
        assert payload["error"] == "ccar endpoint disabled"
        assert payload["tool"] == tool
        assert "pkulaw" in payload["detail"]
    assert "error" in json.loads(dispatch("nope", {}))


def test_ccar_mcp_faa_title14_still_dispatches(monkeypatch):
    from legal_helper.connectors import aviation

    monkeypatch.setattr(
        aviation.faa_title14_search, "call", lambda args: json.dumps({"ok": args})
    )
    payload = json.loads(dispatch("faa_title14", {"query": "121.583"}))
    assert payload["ok"]["query"] == "121.583"


def test_pkulaw_health_reports_missing_token(monkeypatch):
    monkeypatch.setattr(health, "ensure_keys_loaded", lambda: None)
    monkeypatch.delenv("PKULAW_API_TOKEN", raising=False)
    out = health.pkulaw_health()
    assert out["token_present"] is False
    assert out["reachable"] is None
    assert "primary PRC source" in out["note"]


def test_pkulaw_health_probes_gateway(monkeypatch):
    monkeypatch.setattr(health, "ensure_keys_loaded", lambda: None)
    monkeypatch.setenv("PKULAW_API_TOKEN", "tok")
    monkeypatch.setattr(health, "http_list_tools", lambda spec: [{"name": "a"}])
    out = health.pkulaw_health()
    assert out["token_present"] is True
    assert out["reachable"] is True
    assert out["tool_count"] == 1

    monkeypatch.setattr(health, "http_list_tools", lambda spec: [])
    out = health.pkulaw_health()
    assert out["reachable"] is False
    assert "flk_npc_search" in out["note"]


def test_tools_list_fallback_not_cached_forever(monkeypatch):
    """A failed live tools/list probe (manifest fallback) must expire quickly
    so a recovered server is picked up without a process restart; a live
    success is cached for the long TTL."""
    from legal_helper.tools import mcp_tools as mt

    mt._cached_tools_list.cache_clear()
    spec_key = ("pkulaw_law_search", "https://example.invalid/mcp", (("apikey", "t"),))

    calls = {"n": 0}
    live_payload: list = []

    def fake_list_tools(spec):
        calls["n"] += 1
        return list(live_payload)

    monkeypatch.setattr(mt, "http_list_tools", fake_list_tools)

    # Probe fails -> static manifest fallback, cached under the short TTL.
    first = mt._cached_tools_list(spec_key)
    assert calls["n"] == 1
    assert {t["name"] for t in first} == {
        t["name"] for t in mt.known_tools_for("pkulaw_law_search")
    }
    # Within the fallback TTL the cached fallback is served (no re-probe).
    assert mt._cached_tools_list(spec_key) == first
    assert calls["n"] == 1

    # Simulate TTL expiry: clear and let the now-live server win.
    mt._cached_tools_list.cache_clear()
    live_payload.append({"name": "search_article", "description": "live", "inputSchema": {}})
    second = mt._cached_tools_list(spec_key)
    assert calls["n"] == 2
    assert any(t.get("description") == "live" for t in second)

    # Live result is cached — no further probes.
    mt._cached_tools_list(spec_key)
    assert calls["n"] == 2
    mt._cached_tools_list.cache_clear()


def test_tools_list_fallback_ttl_shorter_than_live_ttl():
    from legal_helper.tools import mcp_tools as mt

    assert mt._TOOLS_LIST_FALLBACK_TTL < mt._TOOLS_LIST_LIVE_TTL
    assert mt._TOOLS_LIST_FALLBACK_TTL <= 120


def test_discovery_probes_servers_in_parallel_and_keeps_registry_order(monkeypatch):
    """Cold discovery used to cost the sum of every server's probe (9.4 s for 11
    servers on 2026-10-07); in parallel it costs the slowest one. A server that
    blows up loses only its own tools, and the assembled list keeps registry
    order whichever probe finishes first — it is part of every cached prefix."""
    import threading
    import time

    from legal_helper.tools import mcp_tools as mt

    names = ["pkulaw_law_search", "pkulaw_fatiao", "courtlistener_mcp", "govinfo_mcp"]
    specs = [McpServerSpec(name=n, transport="http", url=f"https://{n}.invalid/mcp") for n in names]
    monkeypatch.setattr(mt, "visible_mcp_specs", lambda j, p: specs)
    mt._cached_tools_list.cache_clear()
    barrier = threading.Barrier(len(names) - 1, timeout=5)

    def fake_list_tools(spec):
        if spec.name == "pkulaw_fatiao":
            raise RuntimeError("gateway exploded")
        barrier.wait()  # only parallel probes get past this
        time.sleep(0.01 * (len(names) - names.index(spec.name)))  # finish out of order
        return [{"name": f"{spec.name}_tool", "description": "d", "inputSchema": {"type": "object", "properties": {}}}]

    monkeypatch.setattr(mt, "http_list_tools", fake_list_tools)
    try:
        tools = mt.visible_mcp_function_tools(["CN", "US"], [])
    finally:
        mt._cached_tools_list.cache_clear()
    servers = [t.name.split("__", 1)[0] for t in tools]
    # Registry order, each server's tools contiguous; the exploding server has none.
    assert list(dict.fromkeys(servers)) == ["pkulaw_law_search", "courtlistener_mcp", "govinfo_mcp"]
    assert servers == sorted(servers, key=names.index)
    names_out = {t.name for t in tools}
    for live in ("pkulaw_law_search__pkulaw_law_search_tool", "courtlistener_mcp__courtlistener_mcp_tool", "govinfo_mcp__govinfo_mcp_tool"):
        assert live in names_out
