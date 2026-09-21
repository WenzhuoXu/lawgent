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
