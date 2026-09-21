"""Tool registry: build function-tool sets for orchestrator and sub-agents.

Each tool is a ``@beta_tool``-decorated function exposing a JSON Schema that
both the Anthropic and OpenAI providers register as a plain function tool.
Provider-native MCP is intentionally not used; see ``CLAUDE.md`` for the
parity contract.
"""

from __future__ import annotations

from typing import Any, Iterable

from ..skills import SKILL_NAMES, load_skill_frontmatter
from ..tool_policy import apply_skill_tool_policy
from .aviation_search import aviation_source_search
from .citations import (
    cite_check_report_tool,
    extract_citations_tool,
    ground_answer_tool,
    provenance_audit_tool,
    quote_roundtrip_tool,
    validate_citations_tool,
    verification_log_append_tool,
)
from .connectors_tools import all_connector_tools, visible_connector_tools
from .contract import extract_clauses
from .diagrams import render_flowchart_image
from .documents import (
    copy_xlsx_sheet,
    edit_docx_text,
    edit_pptx_text,
    edit_xlsx_cells,
    edit_xlsx_cells_checked,
    extract_pdf_tables,
    diff_xlsx,
    inspect_docx,
    inspect_pdf,
    inspect_pptx,
    inspect_xlsx,
    inspect_xlsx_range,
    merge_pdfs,
    read_document,
    render_docx_pages,
    render_pdf_pages,
    read_deck_stylesheet,
    render_pptx_slides,
    render_xlsx_pages,
    view_image,
    write_pptx_from_html,
    reshape_docx,
    reshape_pptx,
    reshape_xlsx,
    rotate_pdf_pages,
    split_pdf,
    write_docx,
    write_pdf,
    write_pptx,
    write_xlsx,
)
from .fetch_attach import fetch_url_to_artifact
from .format_recipes import list_format_recipes, read_format_recipe
from .legal_search import (
    courtlistener_search,
    ecfr_search,
    federal_register_search,
    govinfo_search,
    legal_source_search,
)
from .mcp_tools import visible_mcp_function_tools
from .orchestrator import run_skill
from .project_tools import project_brief_read, project_memory_search, project_memory_write
from .rag_tools import retrieve_legal
from .skill_resources import (
    list_skill_references,
    list_skill_sections,
    read_playbook_section,
    read_skill_reference,
    read_skill_section,
)


_ALL_SKILL_NAMES = set(SKILL_NAMES)

# RAG access is opt-in. Every skill currently declares a ``rag_collections``
# field in its SKILL.md frontmatter; expose ``retrieve_legal`` whenever that
# field is non-empty (computed inside ``skill_tools_for_task``).
_RAG_OPT_IN_FALLBACK_SKILLS = set(_ALL_SKILL_NAMES)

_CITE_TOOL_SKILLS = {"cite-check", "review-contract", "legal-response", "brief"}


def _skill_allowed_connectors(skill_name: str) -> set[str] | None:
    """Read the ``allowed_connectors`` field from the skill's SKILL.md
    frontmatter. Returns ``None`` when the file or field is missing so
    callers fall back to pure jurisdiction + pack filtering.

    Entries support three forms, each matched by ``_matches_allow_list``:

    - exact connector name (``ecfr_search``)
    - MCP server name (``pkulaw_law_search``) — admits every tool from that
      server
    - prefix wildcard (``pkulaw_*``) — admits every tool whose namespaced
      name shares the prefix, e.g. all nine PKULaw sub-services

    An explicit empty list means "no connectors" — returned as an empty set.
    """
    try:
        fm = load_skill_frontmatter(skill_name)
    except FileNotFoundError:
        return None
    if "allowed_connectors" not in fm:
        return None
    allowed = fm.get("allowed_connectors") or []
    return {str(name).strip() for name in allowed if str(name).strip()}


def _active_pack_jurisdictions(packs: Iterable[str]) -> list[str]:
    """Return the union of declared jurisdictions for the active packs.

    Lets a pack widen the connector-filter jurisdiction set to its own
    declared jurisdictions (e.g. aviation → ET/ICAO), so pack-gated connectors
    scoped outside the user's default jurisdictions still surface. Soft-fails
    per pack so a malformed pack.yaml never breaks tool assembly.
    """
    from ..domains import load_pack

    out: list[str] = []
    for name in packs:
        try:
            out.extend(load_pack(name).jurisdictions)
        except Exception:  # noqa: BLE001 — soft-fail per pack
            continue
    return out


def _matches_allow_list(tool_name: str, allow: set[str]) -> bool:
    """Return True when ``tool_name`` is admitted by the allow-list.

    Supports exact match, ``<server>__<tool>`` ⇒ ``<server>`` server-name
    match, and trailing-``*`` prefix wildcard (``pkulaw_*``).
    """
    if tool_name in allow:
        return True
    # Server-name match for namespaced MCP tools (e.g. allow lists
    # "pkulaw_law_search" → admit "pkulaw_law_search__search_article").
    if "__" in tool_name:
        server = tool_name.split("__", 1)[0]
        if server in allow:
            return True
    # Prefix wildcard.
    for entry in allow:
        if entry.endswith("*") and tool_name.startswith(entry[:-1]):
            return True
    return False


def _skill_rag_collections(skill_name: str) -> list[str]:
    """Read the ``rag_collections`` field from SKILL.md. Empty list when
    missing or empty — the skill should not see ``retrieve_legal`` then."""
    try:
        fm = load_skill_frontmatter(skill_name)
    except FileNotFoundError:
        return []
    raw = fm.get("rag_collections") or []
    return [str(name).strip() for name in raw if str(name).strip()]


def orchestrator_tools() -> list[Any]:
    """Tools available to the parent orchestrator."""
    return [
        run_skill,
        render_flowchart_image,
        write_docx,
        write_pdf,
        inspect_docx,
        edit_docx_text,
        reshape_docx,
        render_docx_pages,
        inspect_pdf,
        extract_pdf_tables,
        render_pdf_pages,
        merge_pdfs,
        split_pdf,
        rotate_pdf_pages,
        write_xlsx,
        write_pptx,
        inspect_xlsx,
        inspect_xlsx_range,
        edit_xlsx_cells,
        edit_xlsx_cells_checked,
        reshape_xlsx,
        copy_xlsx_sheet,
        diff_xlsx,
        render_xlsx_pages,
        inspect_pptx,
        edit_pptx_text,
        reshape_pptx,
        render_pptx_slides,
        view_image,
        write_pptx_from_html,
        read_deck_stylesheet,
        read_document,
        fetch_url_to_artifact,
        list_format_recipes,
        read_format_recipe,
        project_memory_write,
        project_memory_search,
        project_brief_read,
        ground_answer_tool,
    ]


def skill_tools(skill_name: str) -> list[Any]:
    return skill_tools_for_task(skill_name)


def skill_tools_for_task(
    skill_name: str,
    task: str = "",
    jurisdictions: Iterable[str] | None = None,
    active_packs: Iterable[str] | None = None,
) -> list[Any]:
    """Task-scoped tools for a sub-agent.

    Every specialist can lazily inspect its own skill and the playbook. Other
    tools are exposed only when the task shape and active config imply they
    are useful, keeping provider tool schemas small.
    """
    base: list[Any] = [
        list_skill_sections,
        read_skill_section,
        list_skill_references,
        read_skill_reference,
        read_playbook_section,
    ]
    base.extend([
        read_document,
        render_flowchart_image,
        write_docx,
        write_pdf,
        inspect_docx,
        edit_docx_text,
        reshape_docx,
        render_docx_pages,
        inspect_pdf,
        extract_pdf_tables,
        render_pdf_pages,
        merge_pdfs,
        split_pdf,
        rotate_pdf_pages,
        write_xlsx,
        write_pptx,
        inspect_xlsx,
        inspect_xlsx_range,
        edit_xlsx_cells,
        edit_xlsx_cells_checked,
        reshape_xlsx,
        copy_xlsx_sheet,
        diff_xlsx,
        render_xlsx_pages,
        inspect_pptx,
        edit_pptx_text,
        reshape_pptx,
        render_pptx_slides,
        view_image,
        write_pptx_from_html,
        read_deck_stylesheet,
        fetch_url_to_artifact,
        list_format_recipes,
        read_format_recipe,
    ])
    if skill_name in {"review-contract", "triage-nda"}:
        base.append(extract_clauses)

    # Connector + MCP surface — driven entirely by the skill's
    # ``allowed_connectors`` frontmatter so SKILL.md is the source of truth.
    # ``None`` means "field absent" → expose every jurisdiction + pack
    # connector. ``set()`` means "explicitly empty" → expose nothing.
    allowed = _skill_allowed_connectors(skill_name)
    if allowed is None or allowed:
        juris = list(jurisdictions or ["CN", "US", "EU"])
        packs = list(active_packs or [])
        # An active domain pack widens the jurisdiction filter to its declared
        # jurisdictions, so pack-gated connectors scoped to a jurisdiction the
        # user did not list (e.g. the aviation pack's ET-scoped ECAA tools, or
        # its EU-scoped EASA tools) still surface. Pack gating + allow-list
        # still apply on top of this.
        juris = list(dict.fromkeys([*juris, *_active_pack_jurisdictions(packs)]))
        connector_fns = visible_connector_tools(juris, packs)
        if allowed is not None:
            connector_fns = [
                fn for fn in connector_fns
                if _matches_allow_list(getattr(fn, "name", ""), allowed)
            ]
        if connector_fns:
            # Expose both the namespaced connector calls and the
            # ``legal_source_search`` dispatcher so models can use either.
            base.extend(connector_fns)
            base.append(legal_source_search)
        if "aviation" in packs:
            base.append(aviation_source_search)
        mcp_fns = visible_mcp_function_tools(juris, packs)
        if allowed is not None:
            mcp_fns = [
                fn for fn in mcp_fns
                if _matches_allow_list(getattr(fn, "name", ""), allowed)
            ]
        base.extend(mcp_fns)

    if _skill_rag_collections(skill_name):
        base.append(retrieve_legal)

    if skill_name in _CITE_TOOL_SKILLS:
        base.extend([extract_citations_tool, validate_citations_tool])
    if skill_name == "cite-check":
        base.extend(
            [
                quote_roundtrip_tool,
                ground_answer_tool,
                provenance_audit_tool,
                cite_check_report_tool,
                verification_log_append_tool,
            ]
        )

    # An auditing skill reviews work; it must not be able to rewrite it.
    return apply_skill_tool_policy(base, skill_name)


__all__ = [
    "all_connector_tools",
    "aviation_source_search",
    "cite_check_report_tool",
    "copy_xlsx_sheet",
    "courtlistener_search",
    "ecfr_search",
    "extract_citations_tool",
    "extract_clauses",
    "edit_xlsx_cells",
    "edit_xlsx_cells_checked",
    "edit_pptx_text",
    "edit_docx_text",
    "extract_pdf_tables",
    "diff_xlsx",
    "federal_register_search",
    "fetch_url_to_artifact",
    "govinfo_search",
    "ground_answer_tool",
    "inspect_xlsx",
    "inspect_xlsx_range",
    "inspect_pptx",
    "inspect_docx",
    "inspect_pdf",
    "legal_source_search",
    "list_format_recipes",
    "list_skill_references",
    "list_skill_sections",
    "merge_pdfs",
    "orchestrator_tools",
    "project_brief_read",
    "project_memory_search",
    "project_memory_write",
    "provenance_audit_tool",
    "quote_roundtrip_tool",
    "read_document",
    "read_format_recipe",
    "render_flowchart_image",
    "render_pptx_slides",
    "render_docx_pages",
    "render_pdf_pages",
    "render_xlsx_pages",
    "view_image",
    "write_pptx_from_html",
    "read_deck_stylesheet",
    "read_playbook_section",
    "read_skill_reference",
    "read_skill_section",
    "reshape_docx",
    "reshape_pptx",
    "reshape_xlsx",
    "retrieve_legal",
    "run_skill",
    "skill_tools",
    "skill_tools_for_task",
    "rotate_pdf_pages",
    "split_pdf",
    "validate_citations_tool",
    "verification_log_append_tool",
    "visible_connector_tools",
    "write_docx",
    "write_pdf",
    "write_pptx",
    "write_xlsx",
]
