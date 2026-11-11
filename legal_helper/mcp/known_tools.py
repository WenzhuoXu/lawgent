"""Static manifest of known external MCP tools.

Used as a fallback by ``tools/mcp_tools.py`` when the live ``tools/list`` for
a server returns nothing — e.g. when ``PKULAW_API_TOKEN`` is unset, the WSO2
gateway is briefly unreachable, or the MCP server itself rate-limits us.

Surfacing the tool **names** even without live schemas lets the sub-agent
route to PKULaw and see a structured error from the call site.

Each entry follows the same shape an MCP server would return from
``tools/list``: ``{"name", "description", "inputSchema"}``. The schemas are
intentionally permissive — the live PKULaw server is the authority on
arguments. When ``tools/list`` succeeds, its richer schemas override these.
"""

from __future__ import annotations

from typing import Any


_STRING = {"type": "string"}
_OPT_INT = {"type": "integer"}


# Names and arguments as the gateway lists them on 2026-11-09 (it renamed
# every tool and moved to snake_case arguments between 11-05 and 11-09).
_SEARCH_FILTERS = {
    "query_text": _STRING,
    "library": _STRING,
    "timeliness": _STRING,
    "issue_department": _STRING,
    "implement_date_start": _STRING,
    "implement_date_end": _STRING,
    "size": _OPT_INT,
}


def _entry(name: str, description: str, properties: dict[str, Any], required: list[str]) -> dict[str, Any]:
    return {
        "name": name,
        "description": description,
        "inputSchema": {"type": "object", "properties": properties, "required": required},
    }


_PKULAW_TOOLS: dict[str, list[dict[str, Any]]] = {
    "pkulaw_law_search": [
        _entry("search_law_articles",
               "Semantic search over PRC 法条 text (中央/地方 法规库, incl. 部门规章). Returns each "
               "article with law title, 条号, 时效性, 效力位阶 and PKULaw link.",
               dict(_SEARCH_FILTERS), ["query_text"]),
        _entry("search_laws",
               "Semantic search over PRC 法规 titles. Returns laws with 发文字号, 时效性, 效力位阶, "
               "制定机关, dates and PKULaw link; no article text.",
               dict(_SEARCH_FILTERS), ["query_text"]),
    ],
    "pkulaw_fatiao": [
        _entry("get_law_article",
               "One PRC 法条 by law title (full or short, e.g. 民法典) and 条号 (577, 第五百七十七条, "
               "120.1 = 第一百二十条之一). Current version unless the title names one.",
               {"law_title": _STRING, "article_number": _STRING}, ["law_title", "article_number"]),
        _entry("get_law_articles_by_range",
               "Consecutive PRC 法条 of one law, at most 10 numbers per call.",
               {"law_title": _STRING, "start_article_number": _STRING, "end_article_number": _STRING},
               ["law_title", "start_article_number", "end_article_number"]),
    ],
    "pkulaw_case_search": [
        _entry("search_cases",
               "Semantic 类案 search over PRC 司法案例; authoritative and ordinary cases with 案号, "
               "court, 本院查明, 本院认为 and PKULaw link.",
               {"text": _STRING, "case_type": _STRING, "case_grade": _STRING, "courthouse_name": _STRING,
                "courthouse_province": _STRING, "decision_date_start": _STRING,
                "decision_date_end": _STRING, "size": _OPT_INT},
               ["text"]),
    ],
    "pkulaw_case_list": [
        _entry("search_cases_by_keyword",
               "Keyword case list over PRC 案例库 by 案号, 标题, 正文词, 案由, 当事人, 参照级别, court, "
               "dates; returns structured parts of each judgment, not its full text.",
               {"title": _STRING, "fulltext": _STRING, "case_number": _STRING, "party": _STRING,
                "court": _STRING, "page_size": _OPT_INT},
               []),
    ],
    "pkulaw_anhao": [
        _entry("recognize_case_numbers",
               "Extract and normalise PRC 案号 in a text, with links to the matching PKULaw cases.",
               {"text": _STRING}, ["text"]),
    ],
    "pkulaw_law_recognition": [
        _entry("recognize_law_citations",
               "Extract PRC statute references from a draft and resolve each to its law title, 条号 "
               "and PKULaw text.",
               {"text": _STRING}, ["text"]),
    ],
    "pkulaw_citation_validator": [
        _entry("verify_law_citations",
               "Check PRC 法条 citations against PKULaw: exact (law, 条号) pairs in user_law, claimed "
               "provisions or summaries in answer_law; returns per-item checks and candidates.",
               {"user_law": {"type": "array"}, "answer_law": {"type": "array"}, "prompt": _STRING}, []),
    ],
    "pkulaw_doc_link": [
        _entry("add_law_links",
               "Add PKULaw hyperlinks to the 法规 references in finished prose (message).",
               {"message": _STRING}, ["message"]),
    ],
    "pkulaw_nl_search": [
        _entry("ai_pkulaw_search",
               "Broad natural-language search across PKULaw statutes, cases, 检察文书, 律所文章 and "
               "papers (name as of 2026-11-04; this service answers 401 to the current token).",
               {"query": _STRING}, ["query"]),
    ],
}


_KNOWN_TOOLS: dict[str, list[dict[str, Any]]] = {
    **_PKULAW_TOOLS,
}


def known_tools_for(server_name: str) -> list[dict[str, Any]]:
    """Return the static manifest for ``server_name`` (empty when unknown)."""
    return list(_KNOWN_TOOLS.get(server_name, ()))


__all__ = ["known_tools_for"]
