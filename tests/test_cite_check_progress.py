"""Unit tests for the cite-check progress translator."""

from __future__ import annotations

from legal_helper.workflow import _cite_check_progress_from_tool_call


def test_read_document_progress():
    out = _cite_check_progress_from_tool_call(
        {"name": "read_document", "arguments": {"path": "/tmp/draft.pdf"}},
        state={},
        total_claims=12,
    )
    assert out is not None
    assert out["stage"] == "reading_draft"
    assert out["total"] == 12


def test_extract_progress_known_total():
    out = _cite_check_progress_from_tool_call(
        {"name": "extract_citations_tool", "arguments": {"text": "..."}},
        state={},
        total_claims=7,
    )
    assert out["stage"] == "extracting"
    assert "7" in out["message"]


def test_fetch_progress_increments_with_lookup_tool():
    state = {}
    a = _cite_check_progress_from_tool_call(
        {"name": "courtlistener_search", "arguments": {"query": "Smith v. Jones"}},
        state=state,
        total_claims=4,
    )
    b = _cite_check_progress_from_tool_call(
        {"name": "ecfr_search", "arguments": {"query": "14 CFR 121"}},
        state=state,
        total_claims=4,
    )
    assert a["stage"] == "fetching" and a["current"] == 1 and a["total"] == 4
    assert b["stage"] == "fetching" and b["current"] == 2 and b["total"] == 4
    # Query snippet appears in the label so the UI can show what's happening.
    assert "Smith v. Jones" in a["message"]


def test_fetch_progress_recognises_namespaced_pkulaw_tools():
    """A PKULaw MCP tool name like ``pkulaw_law_search__search_article`` must
    still count as a primary-source fetch."""
    state = {}
    out = _cite_check_progress_from_tool_call(
        {
            "name": "pkulaw_law_search__search_article",
            "arguments": {"query": "民法典 担保"},
        },
        state=state,
        total_claims=5,
    )
    assert out["stage"] == "fetching"
    assert out["current"] == 1
    assert "pkulaw_law_search" in out["message"]
    assert "民法典 担保" in out["message"]


def test_roundtrip_and_logging_increments():
    state = {}
    r1 = _cite_check_progress_from_tool_call(
        {"name": "quote_roundtrip_tool", "arguments": {"quote": "x"}},
        state=state,
        total_claims=3,
    )
    r2 = _cite_check_progress_from_tool_call(
        {"name": "quote_roundtrip_tool", "arguments": {"quote": "y"}},
        state=state,
        total_claims=3,
    )
    l1 = _cite_check_progress_from_tool_call(
        {"name": "verification_log_append_tool", "arguments": {}},
        state=state,
        total_claims=3,
    )
    assert r1["stage"] == "roundtripping" and r1["current"] == 1
    assert r2["stage"] == "roundtripping" and r2["current"] == 2
    assert l1["stage"] == "logging" and l1["current"] == 1


def test_compose_and_audit_stages():
    audit = _cite_check_progress_from_tool_call(
        {"name": "provenance_audit_tool", "arguments": {}},
        state={"roundtrips": 5},
        total_claims=5,
    )
    compose = _cite_check_progress_from_tool_call(
        {"name": "cite_check_report_tool", "arguments": {}},
        state={},
        total_claims=5,
    )
    assert audit["stage"] == "auditing"
    assert compose["stage"] == "composing"
    assert compose["current"] == 5


def test_unrelated_tool_call_returns_none():
    """Bootstrapping calls (list_skill_sections, read_skill_section, …) must
    not emit a progress update — they would just confuse the singleton card.
    """
    assert (
        _cite_check_progress_from_tool_call(
            {"name": "list_skill_sections", "arguments": {}},
            state={},
            total_claims=8,
        )
        is None
    )
    assert (
        _cite_check_progress_from_tool_call(
            {"name": "read_skill_section", "arguments": {"heading": "Workflow"}},
            state={},
            total_claims=8,
        )
        is None
    )
