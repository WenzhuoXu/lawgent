"""Static manifest of known external MCP tools.

Used as a fallback by ``tools/mcp_tools.py`` when the live ``tools/list`` for
a server returns nothing — e.g. when ``PKULAW_API_TOKEN`` is unset, the WSO2
gateway is briefly unreachable, or the MCP server itself rate-limits us.

Surfacing the tool **names** even without live schemas lets the sub-agent
route to PKULaw and see a structured error from the call site, rather than
silently falling back to ``flk_npc_search`` (which is the public fallback,
not the primary source).

Each entry follows the same shape an MCP server would return from
``tools/list``: ``{"name", "description", "inputSchema"}``. The schemas are
intentionally permissive — the live PKULaw server is the authority on
arguments. When ``tools/list`` succeeds, its richer schemas override these.
"""

from __future__ import annotations

from typing import Any


_STRING = {"type": "string"}
_OPT_INT = {"type": "integer"}


_PKULAW_TOOLS: dict[str, list[dict[str, Any]]] = {
    "pkulaw_law_search": [
        {
            "name": "search_article",
            "description": (
                "Semantic / keyword search over PRC statutes, regulations, "
                "and judicial interpretations. Returns matching 法条 with "
                "title, article number, effective date, and document URL. "
                "Primary entry for PRC statute lookups; prefer over "
                "flk_npc_search."
            ),
            "inputSchema": {
                "type": "object",
                "properties": {
                    "query": _STRING,
                    "law_type": _STRING,
                    "level": _STRING,
                    "per_page": _OPT_INT,
                },
                "required": ["query"],
            },
        },
        {
            "name": "get_article",
            "description": (
                "Retrieve a single PRC 法条 by document id or precise "
                "(law title, article number) pinpoint."
            ),
            "inputSchema": {
                "type": "object",
                "properties": {
                    "article_id": _STRING,
                    "law_name": _STRING,
                    "article_number": _STRING,
                },
            },
        },
    ],
    "pkulaw_fatiao": [
        {
            "name": "get_law_item_content",
            "description": (
                "Precise PRC 法条 lookup by (law title, 条号). The primary "
                "tool for resolving citations like 《民法典》第七百零七条 "
                "into authoritative article text."
            ),
            "inputSchema": {
                "type": "object",
                "properties": {
                    "law_name": _STRING,
                    "article_number": _STRING,
                },
                "required": ["law_name", "article_number"],
            },
        },
    ],
    "pkulaw_case_search": [
        {
            "name": "search_case",
            "description": (
                "Semantic search over PRC 司法案例 (judgments + 案例库 "
                "entries). Use for fact-pattern or legal-issue lookups; "
                "use pkulaw_case_list for keyword scans by title."
            ),
            "inputSchema": {
                "type": "object",
                "properties": {
                    "query": _STRING,
                    "court_level": _STRING,
                    "per_page": _OPT_INT,
                },
                "required": ["query"],
            },
        },
    ],
    "pkulaw_case_list": [
        {
            "name": "get_case_list",
            "description": (
                "Keyword case-judgment list over PRC 案例库. Returns 标题 / "
                "正文 matches with 案号, 裁判日期, court, and URL."
            ),
            "inputSchema": {
                "type": "object",
                "properties": {
                    "keyword": _STRING,
                    "court": _STRING,
                    "per_page": _OPT_INT,
                },
                "required": ["keyword"],
            },
        },
    ],
    "pkulaw_anhao": [
        {
            "name": "anhao_recognition",
            "description": (
                "Extract and normalize PRC 案号 (judgment case numbers) "
                "from arbitrary prose, returning canonical formatting and "
                "links to the matching PKULaw case-record entries."
            ),
            "inputSchema": {
                "type": "object",
                "properties": {"text": _STRING},
                "required": ["text"],
            },
        },
    ],
    "pkulaw_law_recognition": [
        {
            "name": "law_recognition",
            "description": (
                "Extract and resolve PRC 法条 / statute references inside a "
                "draft. Returns each reference with its normalized law title, "
                "article number, and authoritative URL. Use to batch-validate "
                "before citing."
            ),
            "inputSchema": {
                "type": "object",
                "properties": {"text": _STRING},
                "required": ["text"],
            },
        },
    ],
    "pkulaw_citation_validator": [
        {
            "name": "adjust_provisions",
            "description": (
                "Anti-hallucination citation corrector for PRC 法条 / 案号 "
                "chains. Pass the text containing citations; receive a "
                "corrected version where each citation has been resolved "
                "against PKULaw or flagged as unverifiable. Run before "
                "emitting any PRC citation chain."
            ),
            "inputSchema": {
                "type": "object",
                "properties": {"text": _STRING},
                "required": ["text"],
            },
        },
    ],
    "pkulaw_doc_link": [
        {
            "name": "get_linked_content",
            "description": (
                "Auto-link 法规 references inside finished prose to "
                "pkulaw.com URLs. Use as the final step before emitting "
                "user-facing Chinese legal writing."
            ),
            "inputSchema": {
                "type": "object",
                "properties": {"text": _STRING},
                "required": ["text"],
            },
        },
    ],
    "pkulaw_nl_search": [
        {
            "name": "ai_pkulaw_search",
            "description": (
                "Broad natural-language search across PKULaw statutes, "
                "cases, 检察文书, 律所文章, and academic papers. Use for "
                "open-ended PRC legal questions where the right tool / "
                "law / case is not yet known."
            ),
            "inputSchema": {
                "type": "object",
                "properties": {
                    "query": _STRING,
                    "scope": _STRING,
                    "per_page": _OPT_INT,
                },
                "required": ["query"],
            },
        },
    ],
}


# First-party MCP servers (secondary paths — the direct-API connectors in
# ``connectors/`` remain the primary US route; PKULaw remains primary for
# PRC). The govinfo_mcp schemas mirror the live ``tools/list`` from
# ``api.govinfo.gov/mcp`` (verified 2026-07). courtlistener_mcp sits behind
# OAuth so its manifest is permissive: it surfaces routing when no
# ``COURTLISTENER_MCP_TOKEN`` bearer is configured, and the live server's
# schemas override once the token is in place.
_FIRST_PARTY_TOOLS: dict[str, list[dict[str, Any]]] = {
    "courtlistener_mcp": [
        {
            "name": "verify_citations",
            "description": (
                "Official Free Law Project citation verification: check "
                "case citations in a block of text against the "
                "CourtListener database. Back-stops eyecite when grounding "
                "US case citations. Requires COURTLISTENER_MCP_TOKEN "
                "(OAuth bearer); without it calls return the server's "
                "auth error — fall back to courtlistener_search."
            ),
            "inputSchema": {
                "type": "object",
                "properties": {"text": _STRING},
                "required": ["text"],
            },
        },
        {
            "name": "get_endpoint_schema",
            "description": (
                "Hybrid API surface: return the schema of one "
                "CourtListener REST endpoint before invoking it via "
                "call_endpoint."
            ),
            "inputSchema": {
                "type": "object",
                "properties": {"endpoint": _STRING},
                "required": ["endpoint"],
            },
        },
        {
            "name": "call_endpoint",
            "description": (
                "Hybrid API surface: invoke a CourtListener REST endpoint "
                "with parameters previously described by "
                "get_endpoint_schema."
            ),
            "inputSchema": {
                "type": "object",
                "properties": {
                    "endpoint": _STRING,
                    "parameters": {"type": "object"},
                },
                "required": ["endpoint"],
            },
        },
    ],
    "govinfo_mcp": [
        {
            "name": "searchGovInfo",
            "description": (
                "Official GPO GovInfo MCP search over official US federal "
                "documents and metadata. Secondary to the direct "
                "govinfo_search connector."
            ),
            "inputSchema": {
                "type": "object",
                "properties": {
                    "userQuery": _STRING,
                    "pageSize": _OPT_INT,
                    "offsetMark": _STRING,
                    "sortField": _STRING,
                    "sortOrder": _STRING,
                    "startDate": _STRING,
                    "endDate": _STRING,
                    "timePhrase": _STRING,
                },
                "required": ["userQuery"],
            },
        },
        {
            "name": "describePackageOrGranule",
            "description": (
                "Official GPO GovInfo MCP: describe a package or granule "
                "by accessId (e.g. BILLS-115sres97ats, STATUTE-124-Pg3), "
                "including rendition + metadata links."
            ),
            "inputSchema": {
                "type": "object",
                "properties": {"accessId": _STRING},
                "required": ["accessId"],
            },
        },
    ],
}


_KNOWN_TOOLS: dict[str, list[dict[str, Any]]] = {
    **_PKULAW_TOOLS,
    **_FIRST_PARTY_TOOLS,
}


def known_tools_for(server_name: str) -> list[dict[str, Any]]:
    """Return the static manifest for ``server_name`` (empty when unknown)."""
    return list(_KNOWN_TOOLS.get(server_name, ()))


def has_known_tools(server_name: str) -> bool:
    return server_name in _KNOWN_TOOLS


def known_server_names() -> list[str]:
    return list(_KNOWN_TOOLS.keys())


__all__ = [
    "has_known_tools",
    "known_server_names",
    "known_tools_for",
]
