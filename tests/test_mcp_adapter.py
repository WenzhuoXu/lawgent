"""MCP adapter + registry tests."""

from __future__ import annotations

from legal_helper.mcp import filter_mcps, load_registry, mcp_tool_to_function_schema


def test_registry_loads_from_mcp_json() -> None:
    specs = load_registry()
    names = {s.name for s in specs}
    # PKULaw is split across 9 sub-services that share the apikey header;
    # at least one must be registered, plus the EU + in-tree CAAC MCPs.
    assert any(n.startswith("pkulaw_") for n in names), f"no pkulaw_* in {names}"
    assert "eurlex" in names
    assert "ccar_aviation" in names


def test_filter_by_pack() -> None:
    specs = load_registry()
    cn = filter_mcps(specs, ["CN"], [])
    cn_av = filter_mcps(specs, ["CN"], ["aviation"])
    assert "ccar_aviation" not in {s.name for s in cn}
    assert "ccar_aviation" in {s.name for s in cn_av}


def test_filter_by_jurisdiction() -> None:
    specs = load_registry()
    eu = filter_mcps(specs, ["EU"], [])
    names = {s.name for s in eu}
    assert "eurlex" in names
    # No PKULaw sub-service should leak into a pure-EU filter
    assert not any(n.startswith("pkulaw_") for n in names)


def test_env_var_expansion_inside_string() -> None:
    """`Bearer ${VAR}` and similar mixed strings must expand correctly."""
    import os

    from legal_helper.mcp.registry import _expand_env

    os.environ["LH_TEST_TOKEN"] = "abc-123"
    try:
        assert _expand_env("Bearer ${LH_TEST_TOKEN}") == "Bearer abc-123"
        assert _expand_env("${LH_TEST_TOKEN}") == "abc-123"
        assert _expand_env("plain string") == "plain string"
        assert _expand_env("${LH_NONEXISTENT_VAR}") == ""
    finally:
        os.environ.pop("LH_TEST_TOKEN", None)


def test_adapter_translates_mcp_tool() -> None:
    tool = {
        "name": "search",
        "description": "search tool",
        "inputSchema": {
            "type": "object",
            "properties": {"q": {"type": "string"}},
            "required": ["q"],
        },
    }
    schema = mcp_tool_to_function_schema("foo", tool)
    # `<server>__<tool>` namespace — dot would be rejected by the
    # provider tool-name regex; `__` is safe and reversible.
    assert schema["name"] == "foo__search"
    assert schema["description"]
    assert schema["input_schema"]["properties"]["q"]["type"] == "string"


def test_split_mcp_tool_name() -> None:
    from legal_helper.mcp import qualify_mcp_tool_name, split_mcp_tool_name

    q = qualify_mcp_tool_name("pkulaw_law_search", "search_article")
    assert q == "pkulaw_law_search__search_article"
    assert split_mcp_tool_name(q) == ("pkulaw_law_search", "search_article")
    assert split_mcp_tool_name("not-namespaced") is None


def test_visible_mcp_function_tools_wraps_remote_schemas(monkeypatch) -> None:
    """Each visible HTTP MCP tool surfaces as a BetaFunctionTool with the
    namespaced name + the remote inputSchema, and ``.call`` dispatches
    through the sync client."""
    from legal_helper.mcp.registry import McpServerSpec
    from legal_helper.tools import mcp_tools as mt

    fake_spec = McpServerSpec(
        name="pkulaw_law_search",
        transport="http",
        url="https://example.invalid/mcp",
        headers={"apikey": "test"},
        jurisdictions=("CN",),
    )

    # Bypass network: pretend filter_mcps returned just our fake spec, and
    # the remote tools/list returned one tool definition.
    monkeypatch.setattr(mt, "visible_mcp_specs", lambda j, p: [fake_spec])
    mt._cached_tools_list.cache_clear()
    monkeypatch.setattr(
        mt,
        "_cached_tools_list",
        lambda key: (
            {
                "name": "search_article",
                "description": "Semantic search of PRC statutes.",
                "inputSchema": {
                    "type": "object",
                    "properties": {"query": {"type": "string"}},
                    "required": ["query"],
                },
            },
        ),
    )
    captured: dict[str, object] = {}

    def fake_call(spec, tool_name, arguments):
        captured["spec_name"] = spec.name
        captured["tool"] = tool_name
        captured["args"] = arguments
        return "remote-ok"

    monkeypatch.setattr(mt, "http_call_tool", fake_call)

    tools = mt.visible_mcp_function_tools(["CN"], [])
    assert len(tools) == 1
    t = tools[0]
    assert t.name == "pkulaw_law_search__search_article"
    assert t.description.startswith("Semantic")
    assert t.input_schema["required"] == ["query"]

    out = t.call({"query": "民法典"})
    assert out == "remote-ok"
    assert captured == {
        "spec_name": "pkulaw_law_search",
        "tool": "search_article",
        "args": {"query": "民法典"},
    }
