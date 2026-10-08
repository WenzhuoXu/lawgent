"""An auditing agent must not be able to rewrite what it is auditing.

The citation auditor was handed the full document surface — 64 tools, including
`write_docx`, `edit_xlsx_cells` and fourteen others that mutate a deliverable.
Reviewing a draft and rewriting it are different jobs; the tools now say which
they are, and the policy reads that declaration instead of the call site
guessing from names.
"""

from __future__ import annotations

from legal_helper.tool_policy import (
    MUTATING_TOOLS,
    apply_skill_tool_policy,
    is_mutating,
    is_read_only,
    tool_name_of,
)
from legal_helper.tools import skill_tools_for_task


def test_the_auditor_holds_no_mutating_tool():
    names = {tool_name_of(t) for t in skill_tools_for_task("cite-check", "")}
    assert names, "the auditor should still have a working tool surface"
    assert not (names & MUTATING_TOOLS)


def test_the_auditor_keeps_what_it_needs_to_verify():
    """Reading, rendering a page to look at it, and reporting all survive."""
    names = {tool_name_of(t) for t in skill_tools_for_task("cite-check", "")}
    for needed in (
        "read_document",
        "render_pdf_pages",  # writes a PNG, exists so the model can read a page
        "inspect_pdf",
        "cite_check_report_tool",
        "verification_log_append_tool",
    ):
        assert needed in names, needed


def test_a_research_specialist_keeps_the_full_surface():
    names = {tool_name_of(t) for t in skill_tools_for_task("legal-response", "")}
    assert names & MUTATING_TOOLS, "drafting skills still need to write documents"


def test_classification_is_by_declaration_not_by_prefix():
    assert is_mutating("write_docx")
    assert is_mutating("edit_xlsx_cells_checked")
    # Write-shaped names that exist to support reading are not mutating.
    assert is_read_only("render_pdf_pages")
    assert is_read_only("diff_xlsx")
    assert is_read_only("inspect_xlsx_range")
    assert is_read_only("")


def test_the_policy_is_a_no_op_for_other_skills():
    tools = list(skill_tools_for_task("legal-response", ""))
    assert apply_skill_tool_policy(tools, "legal-response") == tools
    assert apply_skill_tool_policy(tools, "") == tools


def test_tool_name_of_handles_both_tool_shapes():
    assert tool_name_of({"name": "web_search"}) == "web_search"

    def some_tool() -> None:  # pragma: no cover - only its name is read
        return None

    assert tool_name_of(some_tool) == "some_tool"
    assert tool_name_of(object()) == ""


# --- which calls of one response may run side by side ------------------------


def test_concurrent_safe_tools_are_read_only_and_exist():
    from legal_helper.tool_policy import CONCURRENCY_GROUPS, CONCURRENT_SAFE_TOOLS, is_concurrent_safe
    from legal_helper.tools import _external_skill_tools, all_connector_tools, skill_tools
    from legal_helper.tools.project_tools import project_brief_read
    from legal_helper.workflow import _document_io_tools

    assert not (CONCURRENT_SAFE_TOOLS & MUTATING_TOOLS)
    # A declared name that no longer exists is a silent no-op; catch the typo.
    known = {
        tool_name_of(t)
        for t in [
            *_document_io_tools(),
            *_external_skill_tools(),
            *all_connector_tools(),
            project_brief_read,
        ]
    }
    for skill in ("brief", "cite-check", "review-contract"):
        known |= {tool_name_of(t) for t in skill_tools(skill)}
    missing = sorted(CONCURRENT_SAFE_TOOLS - known)
    assert not missing, f"declared concurrent-safe but not registered: {missing}"
    # Grouped tools are concurrent-safe against everything outside their group.
    assert all(is_concurrent_safe(name) for name in CONCURRENCY_GROUPS)


def test_connectors_and_research_mcp_servers_are_concurrent_safe():
    from legal_helper.tool_policy import is_concurrent_safe
    from legal_helper.tools import all_connector_tools

    for tool in all_connector_tools():
        assert is_concurrent_safe(tool_name_of(tool)), tool_name_of(tool)
    assert is_concurrent_safe("pkulaw_fatiao__get_law_item_content")
    assert is_concurrent_safe("courtlistener_mcp__search")


def test_anything_undeclared_runs_serially():
    from legal_helper.tool_policy import is_concurrent_safe

    for name in (
        "write_docx",  # authoring
        "edit_xlsx_cells_checked",
        "render_flowchart_image",  # authors a diagram
        "create_deck",  # a whole sub-agent
        "retrieve_legal",  # embedded Qdrant takes a file lock per client
        "aviation_source_search",  # queries the same store
        "some_new_tool",
        "unknown_server__search",  # an MCP server nobody declared
        "",
    ):
        assert not is_concurrent_safe(name), name


def test_renders_and_document_reads_have_their_own_groups():
    from legal_helper.tool_policy import concurrency_group

    renders = {concurrency_group(n) for n in ("render_docx_pages", "render_pdf_pages", "render_pptx_slides", "render_xlsx_pages")}
    assert renders == {"pdfium"}
    assert concurrency_group("read_document") == "document_converter"
    assert concurrency_group("inspect_docx") is None
