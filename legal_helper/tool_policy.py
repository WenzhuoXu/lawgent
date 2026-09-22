"""Declarative tool capability, and the policies that read it.

A tool's properties belong to its definition, not to an `if name ==` at each
call site: that is how a citation auditor ended up holding `write_docx`,
`edit_xlsx_cells` and fourteen other document-mutating tools — 64 tools in
total, of which sixteen could rewrite the very deliverable it was reviewing.

Two properties are declared here, both of which this harness actually acts on:

- **mutating** — the tool changes a document or produces a new deliverable.
  An auditing agent must not hold one; reviewing a draft and rewriting it are
  different jobs, and an auditor that can edit will eventually edit.
- **read-only** — everything else, including the page-render family
  (`render_pptx_slides`, `render_pdf_pages`, …). Rendering writes a PNG, but it
  exists so the model can *look* at a page to confirm a pinpoint, which is
  exactly what an auditor needs. `render_flowchart_image` is the exception: it
  authors a diagram rather than viewing a document, so it is mutating.

Dropping the mutating tools from an audit agent also removes ~3K tokens of
schemas it never uses, which is the same budget the tool-result limits protect.
"""

from __future__ import annotations

from typing import Any, Iterable

# Tools that create or alter a document. Exact names rather than prefixes: the
# `render_*` and `inspect_*` families look write-shaped and are not, and a
# prefix rule would quietly capture the next one added.
MUTATING_TOOLS = frozenset(
    {
        # Authors a text artifact, and runs a skill's own scripts (which write
        # files). Both are production rather than inspection, so an auditing
        # skill must not hold either.
        "write_text_file",
        "run_skill_script",
        "write_docx",
        "write_pdf",
        "write_pptx",
        "write_pptx_from_html",
        "write_xlsx",
        "edit_docx_text",
        "edit_pptx_text",
        "edit_xlsx_cells",
        "edit_xlsx_cells_checked",
        "reshape_docx",
        "reshape_pptx",
        "reshape_xlsx",
        "copy_xlsx_sheet",
        "merge_pdfs",
        "split_pdf",
        "rotate_pdf_pages",
        "fetch_url_to_artifact",
        "project_memory_write",
        # The one render_* that is not a page render: it authors a diagram, so
        # it produces a deliverable rather than a view of an existing one.
        "render_flowchart_image",
    }
)

# Skills whose agents review or verify someone else's work, and therefore get a
# surface that cannot change it.
READ_ONLY_SKILLS = frozenset({"cite-check"})


def is_mutating(tool_name: str) -> bool:
    return (tool_name or "").strip() in MUTATING_TOOLS


def is_read_only(tool_name: str) -> bool:
    return not is_mutating(tool_name)


def tool_name_of(tool: Any) -> str:
    """Best-effort name for a decorated function tool or a dict tool def."""
    if isinstance(tool, dict):
        return str(tool.get("name") or "")
    return str(getattr(tool, "name", None) or getattr(tool, "__name__", "") or "")


def apply_skill_tool_policy(tools: Iterable[Any], skill_name: str) -> list[Any]:
    """Drop the tools a skill's agent is not allowed to hold.

    Only the read-only policy exists today; the signature takes the skill name
    so a second policy lands here rather than at the call site.
    """
    items = list(tools)
    if (skill_name or "").strip() not in READ_ONLY_SKILLS:
        return items
    return [tool for tool in items if not is_mutating(tool_name_of(tool))]


__all__ = [
    "MUTATING_TOOLS",
    "READ_ONLY_SKILLS",
    "is_mutating",
    "is_read_only",
    "tool_name_of",
    "apply_skill_tool_policy",
]
