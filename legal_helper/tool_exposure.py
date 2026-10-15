"""Which tools are loaded on every request, and which wait behind tool search.

The author held 56 tools, ~20K tokens of schemas, on every request — a greeting
paid for the spreadsheet and slide tools. Following Codex: a small core of
tools is always loaded (reading, research, methods, packs, the workflow) and
every capability-side tool — Word / PDF / spreadsheet / slide work, diagrams,
per-jurisdiction connectors, pack sources — is *deferred*: declared with
`defer_loading`, invisible to the model until the provider's own tool search
finds it, then called by its real name.

Both providers implement this natively, so the request's `tools` array never
changes (the cached prefix survives — found schemas are appended to the
conversation, not swapped into the list):

- OpenAI Responses: a `{"type": "tool_search"}` tool, deferred functions
  grouped in `namespace` tools whose one-line descriptions the model sees;
- Anthropic: `tool_search_tool_bm25_20251119` with `defer_loading: true` on
  the deferred tools (the search tool and at least one other tool stay loaded).

Live-probed 2026-10-14 on every offered model (gpt-6.1-sol, gpt-6-luna,
claude-opus-5-5, claude-sonnet-5): each searched, found and called a deferred
tool in one response. There is no fallback path; a model added later must
pass the same probe.
"""

from __future__ import annotations

import copy
from functools import lru_cache
from typing import Any, Iterable, Optional

ANTHROPIC_TOOL_SEARCH = {"type": "tool_search_tool_bm25_20251119", "name": "tool_search_tool_bm25"}
OPENAI_TOOL_SEARCH = {"type": "tool_search"}

#: Always loaded: the harness's operational tools only, as in Codex (shell,
#: patch, plan, ask-user, view-image, web search, tool search). Reading the
#: user's files, methods, packs, the workflow, project memory, and a
#: specialist's own skill and audit tools. Functional tools — every search,
#: connector and calculator, however often used — wait behind tool search.
DIRECT_TOOLS = frozenset(
    {
        "read_document",
        "load_skill",
        "read_skill_file",
        "load_pack",
        "run_workflow",
        "project_memory_write",
        "project_memory_search",
        "project_brief_read",
        # specialists
        "list_skill_references",
        "read_skill_reference",
        "extract_clauses",
        "extract_citations_tool",
        "validate_citations_tool",
        "quote_roundtrip_tool",
        "ground_answer_tool",
        "provenance_audit_tool",
        "cite_check_report_tool",
        "verification_log_append_tool",
        # external procedures (never deferred as a set, listed for completeness)
        "edit_text_file",
        "write_text_file",
        "run_skill_script",
        "list_skill_dir",
    }
)

#: Deferred tool → the group it is found under. Groups become OpenAI
#: namespaces and the "Tools on demand" list in the author prompt.
_GROUP_OF: dict[str, str] = {
    **dict.fromkeys(
        ("write_docx", "inspect_docx", "edit_docx_text", "reshape_docx", "redline_docx", "compare_docx", "render_docx_pages"),
        "word",
    ),
    **dict.fromkeys(
        ("write_pdf", "inspect_pdf", "extract_pdf_tables", "render_pdf_pages", "merge_pdfs", "split_pdf", "rotate_pdf_pages"),
        "pdf",
    ),
    **dict.fromkeys(
        (
            "write_xlsx", "inspect_xlsx", "inspect_xlsx_range", "edit_xlsx_cells", "edit_xlsx_cells_checked",
            "reshape_xlsx", "copy_xlsx_sheet", "diff_xlsx", "render_xlsx_pages",
        ),
        "spreadsheet",
    ),
    **dict.fromkeys(("create_deck", "inspect_pptx", "edit_pptx_text", "reshape_pptx", "render_pptx_slides"), "slides"),
    **dict.fromkeys(
        ("fetch_url_to_artifact", "view_image", "list_format_recipes", "read_format_recipe", "render_flowchart_image", "run_procedure"),
        "files",
    ),
    **dict.fromkeys(("legal_source_search", "retrieve_legal"), "legal_research"),
    "compute_period": "deadlines",
}
_PREFIX_GROUPS: tuple[tuple[str, str], ...] = (
    ("flk_npc_", "prc_law"),
    ("pkulaw_", "prc_law"),
    ("courtlistener_", "us_law"),
    ("ecfr_", "us_law"),
    ("federal_register_", "us_law"),
    ("govinfo_", "us_law"),
    ("edgar_", "us_law"),
    ("eurlex_", "eu_law"),
)

GROUP_DESCRIPTIONS: dict[str, str] = {
    "legal_research": "Legal research: one search across US, EU and PRC official sources, and semantic search of the local legal corpus (statutes, treaties, playbooks, prior memos).",
    "deadlines": "Deadline and limitation-period calculation (期间计算) on the PRC holiday and 调休 calendar.",
    "word": "Word documents: write, inspect, edit text, restructure, tracked-change redlines, compare, render pages.",
    "pdf": "PDFs: write, inspect, extract tables, render pages, merge, split, rotate.",
    "spreadsheet": "Excel workbooks: write, inspect ranges, guarded cell edits, row/column reshapes, sheet copies, diffs, render.",
    "slides": "Slide decks (made by ppt-master) and existing .pptx files: create, inspect, edit text, reshape, render.",
    "files": "Fetch a URL into a file, view an image closely, format recipes, diagrams as pictures, installed document procedures.",
    "prc_law": "PRC legal sources: national law database search and statute fetch (and PKULaw when connected).",
    "us_law": "US legal sources: CourtListener case law, eCFR, Federal Register, GovInfo.",
    "eu_law": "EU legal sources: EUR-Lex search and document fetch.",
    "other_law": "Other jurisdictions' legal sources (national law catalogues and texts outside CN/US/EU).",
}


@lru_cache(maxsize=1)
def _pack_groups() -> dict[str, str]:
    """Pack source tool → its pack: the connectors a pack gates, and the pack's
    own `<pack>_source_search` dispatcher."""
    out: dict[str, str] = {}
    try:
        from .connectors import REGISTRY
        from .domains import available_packs

        packs = set(available_packs())
        for entry in REGISTRY:
            for pack in entry.domain_packs:
                if pack in packs:
                    out.setdefault(entry.name, pack)
        for pack in packs:
            out.setdefault(f"{pack}_source_search", pack)
    except Exception:  # noqa: BLE001 — grouping is advisory; never break tool assembly
        pass
    return out


@lru_cache(maxsize=1)
def pack_group_descriptions() -> dict[str, str]:
    out: dict[str, str] = {}
    try:
        from .domains import available_packs, load_pack

        for name in available_packs():
            out[name] = (
                f"{load_pack(name).display_name} pack sources — load the pack with "
                "`load_pack` first for its playbook and routing."
            )
    except Exception:  # noqa: BLE001
        pass
    return out


def tool_name(tool: Any) -> str:
    if isinstance(tool, dict):
        return str(tool.get("name") or tool.get("type") or "")
    return str(getattr(tool, "name", "") or "")


def group_for(name: str) -> Optional[str]:
    """The group a deferred tool belongs to; None for a tool that stays loaded."""
    if name in DIRECT_TOOLS:
        return None
    packs = _pack_groups()
    if name in packs:
        return packs[name]
    if name in _GROUP_OF:
        return _GROUP_OF[name]
    for prefix, group in _PREFIX_GROUPS:
        if name.startswith(prefix):
            return group
    return _connector_groups().get(name)


@lru_cache(maxsize=1)
def _connector_groups() -> dict[str, str]:
    """Every other registry connector, by its jurisdiction."""
    by_jurisdiction = {"CN": "prc_law", "US": "us_law", "EU": "eu_law"}
    out: dict[str, str] = {}
    try:
        from .connectors import REGISTRY

        for entry in REGISTRY:
            juris = next((j for j in entry.jurisdiction if j in by_jurisdiction), None)
            out[entry.name] = by_jurisdiction[juris] if juris else "other_law"
    except Exception:  # noqa: BLE001
        pass
    return out


def _with_defer(tool: Any, value: Optional[bool]) -> Any:
    if isinstance(tool, dict):
        if value:
            return {**tool, "defer_loading": True}
        return {k: v for k, v in tool.items() if k != "defer_loading"}
    if hasattr(tool, "_defer_loading"):
        if getattr(tool, "_defer_loading", None) == value:
            return tool
        out = copy.copy(tool)
        out._defer_loading = value
        return out
    return tool


def mark_deferred(tools: Iterable[Any]) -> list[Any]:
    """Mark every capability-side tool `defer_loading` (hosted tools and the
    always-loaded core untouched)."""
    out: list[Any] = []
    for tool in tools:
        name = tool_name(tool)
        if isinstance(tool, dict) and "type" in tool and tool.get("type") != "function":
            out.append(tool)  # hosted / server tools (web_search, …)
        elif group_for(name):
            out.append(_with_defer(tool, True))
        else:
            out.append(tool)
    return out


def is_deferred(tool: Any) -> bool:
    if isinstance(tool, dict):
        return bool(tool.get("defer_loading"))
    return bool(getattr(tool, "_defer_loading", None))


def anthropic_tools(tools: list[Any]) -> list[Any]:
    """The Anthropic `tools` list: loaded tools, the search tool, then the
    deferred ones."""
    deferred = [t for t in tools if is_deferred(t)]
    if not deferred:
        return tools
    loaded = [t for t in tools if not is_deferred(t)]
    return [*loaded, dict(ANTHROPIC_TOOL_SEARCH), *deferred]


def openai_tools(defs: list[dict[str, Any]]) -> list[dict[str, Any]]:
    """The OpenAI `tools` list: loaded functions and hosted tools, the hosted
    tool search, and the deferred functions grouped into namespaces."""
    deferred = [d for d in defs if d.get("defer_loading")]
    if not deferred:
        return defs
    descriptions = {**GROUP_DESCRIPTIONS, **pack_group_descriptions()}
    loaded = [d for d in defs if not d.get("defer_loading")]
    groups: dict[str, list[dict[str, Any]]] = {}
    for d in deferred:
        group = group_for(str(d.get("name") or "")) or "other"
        groups.setdefault(group, []).append(d)
    namespaces = [
        {
            "type": "namespace",
            "name": group,
            "description": descriptions.get(group, f"Tools in the {group} group."),
            "tools": members,
        }
        for group, members in groups.items()
    ]
    return [*loaded, dict(OPENAI_TOOL_SEARCH), *namespaces]


def on_demand_block() -> str:
    """The author prompt's list of what tool search can find (Codex's source list)."""
    descriptions = {**GROUP_DESCRIPTIONS, **pack_group_descriptions()}
    lines = "\n".join(f"- {group}: {text}" for group, text in descriptions.items())
    return (
        "# Tools on demand\n"
        "Besides the tools you see, these groups are loaded through tool search "
        "when a request needs them — search with the job's plain words (\"redline "
        "a Word contract\", \"edit a spreadsheet cell\", \"eCFR\") and call what it "
        "finds by name:\n" + lines
    )


__all__ = [
    "ANTHROPIC_TOOL_SEARCH",
    "DIRECT_TOOLS",
    "GROUP_DESCRIPTIONS",
    "OPENAI_TOOL_SEARCH",
    "pack_group_descriptions",
    "anthropic_tools",
    "group_for",
    "is_deferred",
    "mark_deferred",
    "on_demand_block",
    "openai_tools",
    "tool_name",
]
