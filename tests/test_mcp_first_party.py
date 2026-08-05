"""First-party MCP adoption: official CourtListener + GovInfo servers.

These are SECONDARY paths — direct-API connectors stay primary for US
sources and PKULaw stays primary for PRC. Tests are offline: they only
exercise registry composition, header construction, and the static
manifests."""

from __future__ import annotations

import json

from legal_helper.mcp import filter_mcps, load_registry
from legal_helper.mcp import first_party
from legal_helper.mcp.known_tools import known_tools_for


def _no_keyfile(monkeypatch):
    """Keep env under test control — don't let api_key repopulate it."""
    monkeypatch.setattr(first_party, "ensure_keys_loaded", lambda: None)


def test_registry_appends_first_party_specs(monkeypatch):
    _no_keyfile(monkeypatch)
    specs = {s.name: s for s in load_registry()}
    assert specs["courtlistener_mcp"].url == "https://mcp.courtlistener.com/"
    assert specs["courtlistener_mcp"].transport == "http"
    assert specs["govinfo_mcp"].url == "https://api.govinfo.gov/mcp"
    # PKULaw entries from .mcp.json must still be present untouched.
    assert any(n.startswith("pkulaw_") for n in specs)


def test_mcp_json_declaration_overrides_first_party(monkeypatch, tmp_path):
    _no_keyfile(monkeypatch)
    cfg = tmp_path / ".mcp.json"
    cfg.write_text(
        json.dumps(
            {
                "mcpServers": {
                    "courtlistener_mcp": {
                        "transport": "http",
                        "url": "https://override.example/mcp",
                        "jurisdictions": ["US"],
                    }
                }
            }
        ),
        encoding="utf-8",
    )
    specs = [s for s in load_registry(cfg) if s.name == "courtlistener_mcp"]
    assert len(specs) == 1  # no duplicate spec for the same server
    assert specs[0].url == "https://override.example/mcp"


def test_first_party_headers_only_when_secrets_present(monkeypatch):
    _no_keyfile(monkeypatch)
    monkeypatch.delenv("COURTLISTENER_MCP_TOKEN", raising=False)
    monkeypatch.delenv("GOVINFO_API_KEY", raising=False)
    by_name = {s.name: s for s in first_party.first_party_specs()}
    # Never ship an empty Authorization / X-Api-Key header.
    assert "Authorization" not in by_name["courtlistener_mcp"].headers
    assert "X-Api-Key" not in by_name["govinfo_mcp"].headers

    monkeypatch.setenv("COURTLISTENER_MCP_TOKEN", "tok-1")
    monkeypatch.setenv("GOVINFO_API_KEY", "key-2")
    by_name = {s.name: s for s in first_party.first_party_specs()}
    assert by_name["courtlistener_mcp"].headers["Authorization"] == "Bearer tok-1"
    assert by_name["govinfo_mcp"].headers["X-Api-Key"] == "key-2"


def test_first_party_gated_to_us_jurisdiction(monkeypatch):
    """PRC-first routing: a pure-CN context must not surface the US MCPs,
    keeping PKULaw the primary PRC path."""
    _no_keyfile(monkeypatch)
    cn = {s.name for s in filter_mcps(load_registry(), ["CN"], [])}
    assert "courtlistener_mcp" not in cn
    assert "govinfo_mcp" not in cn
    us = {s.name for s in filter_mcps(load_registry(), ["US"], [])}
    assert {"courtlistener_mcp", "govinfo_mcp"} <= us


def test_first_party_static_manifests_exist():
    gi = {t["name"] for t in known_tools_for("govinfo_mcp")}
    # Names mirror the live tools/list from api.govinfo.gov/mcp.
    assert gi == {"searchGovInfo", "describePackageOrGranule"}
    cl = {t["name"] for t in known_tools_for("courtlistener_mcp")}
    assert "verify_citations" in cl
    assert {"get_endpoint_schema", "call_endpoint"} <= cl
    for entry in known_tools_for("govinfo_mcp") + known_tools_for("courtlistener_mcp"):
        assert entry.get("inputSchema", {}).get("type") == "object"
