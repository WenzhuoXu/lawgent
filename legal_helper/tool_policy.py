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

A third property decides how a model's parallel tool calls are executed:

- **concurrent-safe** — the tool only reads (a document, a public source, a
  reference file) and holds no process-wide state, so several calls from one
  model response may run side by side. Read-only is necessary but not
  sufficient: two renders share PDFium, which is not thread-safe, and a
  scanned-PDF read swaps ``sys.stderr`` for the whole process. Those tools are
  concurrent-safe only against *other kinds* of tool, so each sits in a
  **concurrency group** whose members never overlap one another. Everything
  not declared here runs serially, in the order the model asked for it.
"""

from __future__ import annotations

from typing import Any, Iterable, Optional

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
        # Runs a deck sub-agent that authors and exports a new .pptx.
        "create_deck",
        # Runs an external procedure's sub-agent, which writes a document.
        "run_procedure",
        "write_xlsx",
        "edit_docx_text",
        # Tracked-change authoring and draft comparison write new deliverables.
        "redline_docx",
        "compare_docx",
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

# Tools that may run side by side when one model response asks for several.
# Exact names, for the same reason as MUTATING_TOOLS. Deliberately absent:
# `retrieve_legal` and `aviation_source_search` (each opens the embedded Qdrant
# store, whose file lock admits one client at a time), `project_memory_search`,
# `create_deck` (a whole sub-agent) and every authoring tool.
CONCURRENT_SAFE_TOOLS = frozenset(
    {
        # Document reads and inspections: pure-Python parsers over one file.
        "read_document",
        "inspect_docx",
        "inspect_pdf",
        "inspect_pptx",
        "inspect_xlsx",
        "inspect_xlsx_range",
        "extract_pdf_tables",
        "extract_clauses",
        "diff_xlsx",
        "view_image",
        # Page renders: they write a PNG, but only so the model can look.
        "render_docx_pages",
        "render_pdf_pages",
        "render_pptx_slides",
        "render_xlsx_pages",
        # In-tree reference material.
        "list_format_recipes",
        "read_format_recipe",
        "read_skill_reference",
        "list_skill_references",
        "list_skill_dir",
        "project_brief_read",
        # Public-source lookup that dispatches to the connectors below.
        "legal_source_search",
    }
)

# Members of one group never overlap each other, though each may overlap any
# tool outside its group. Process-wide: the hazard is shared library state, so
# two sub-agents rendering at once must queue as well.
CONCURRENCY_GROUPS: dict[str, str] = {
    # Every render rasterises through pypdfium2 in-process, and "PDFium is
    # inherently not thread-safe" (pypdfium2 5.8.0). Overlapping renders can
    # corrupt memory, which takes the whole server down, not one tool call.
    "render_docx_pages": "pdfium",
    "render_pdf_pages": "pdfium",
    "render_pptx_slides": "pdfium",
    "render_xlsx_pages": "pdfium",
    # A read that falls through to markitdown (a scanned PDF, an unusual
    # format) swaps `sys.stderr` process-wide while it runs; two overlapping
    # swaps can leave stderr pointing at a closed file. Reads are fast (p50
    # 0.16 s), so queueing them against each other costs almost nothing.
    "read_document": "document_converter",
}

# Remote MCP servers whose every tool is a research lookup. Their tools arrive
# namespaced as ``<server>__<tool>``; the server is the unit we configured, so
# it is what gets declared, never a bare name prefix.
READ_ONLY_MCP_SERVERS = frozenset(
    {
        "pkulaw_law_search",
        "pkulaw_fatiao",
        "pkulaw_case_search",
        "pkulaw_case_list",
        "pkulaw_anhao",
        "pkulaw_law_recognition",
        "pkulaw_citation_validator",
        "pkulaw_doc_link",
        "pkulaw_nl_search",
        "courtlistener_mcp",
        "govinfo_mcp",
    }
)


def is_mutating(tool_name: str) -> bool:
    return (tool_name or "").strip() in MUTATING_TOOLS


def is_read_only(tool_name: str) -> bool:
    return not is_mutating(tool_name)


def _connector_tool_names() -> frozenset[str]:
    """Every registered connector: each is a search or fetch against a source.

    The connector registry is their declaration, so they are not repeated
    above. Imported lazily — the connectors pull in network clients this
    module should not load just to answer a name lookup.
    """
    global _CONNECTOR_NAMES
    if _CONNECTOR_NAMES is None:
        try:
            from .connectors import REGISTRY

            _CONNECTOR_NAMES = frozenset(entry.name for entry in REGISTRY)
        except Exception:  # noqa: BLE001 — an unknown tool simply runs serially
            _CONNECTOR_NAMES = frozenset()
    return _CONNECTOR_NAMES


_CONNECTOR_NAMES: Optional[frozenset[str]] = None


def is_concurrent_safe(tool_name: str) -> bool:
    """True when this tool may run alongside the other calls of one response."""
    name = (tool_name or "").strip()
    if not name or is_mutating(name):
        return False
    if name in CONCURRENT_SAFE_TOOLS or name in _connector_tool_names():
        return True
    server, sep, _ = name.partition("__")
    return bool(sep) and server in READ_ONLY_MCP_SERVERS


def concurrency_group(tool_name: str) -> Optional[str]:
    """The group whose members must not overlap this tool, if any."""
    return CONCURRENCY_GROUPS.get((tool_name or "").strip())


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
    "CONCURRENT_SAFE_TOOLS",
    "CONCURRENCY_GROUPS",
    "READ_ONLY_MCP_SERVERS",
    "is_mutating",
    "is_read_only",
    "is_concurrent_safe",
    "concurrency_group",
    "tool_name_of",
    "apply_skill_tool_policy",
]
