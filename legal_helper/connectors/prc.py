"""PRC (China) legal sources — public no-auth API over ``flk.npc.gov.cn``.

The 国家法律法规数据库 is authoritative + free for 法律 / 行政法规 /
部门规章 / 司法解释 / 国函 / 国办发, but coverage is shallower than the
PKULaw catalogue. Per CLAUDE.md the **primary** PRC lookup path is the
PKULaw MCP — see ``.mcp.json`` for the nine ``pkulaw_*`` sub-services
(``pkulaw_law_search``, ``pkulaw_fatiao``, ``pkulaw_case_search``,
``pkulaw_case_list``, ``pkulaw_anhao``, ``pkulaw_law_recognition``,
``pkulaw_citation_validator``, ``pkulaw_doc_link``, ``pkulaw_nl_search``)
that share ``PKULAW_API_TOKEN`` via the WSO2 ``apikey`` header. There is
no PRC public-API equivalent for case-law (案号) lookup, so case work
must go through PKULaw. The ``flk_npc_*`` functions below stay as the
free public fallback.
"""

import json
import re
from typing import Any, Optional

import httpx
from anthropic import beta_tool

from .base import USER_AGENT, request_with_retry, truncate


_FLK_BASE = "https://flk.npc.gov.cn"
_SEARCH_URL = f"{_FLK_BASE}/law-search/search/list"
_DETAIL_URL = f"{_FLK_BASE}/law-search/search/flfgDetails"

_FLK_HEADERS = {
    "User-Agent": USER_AGENT,
    "Accept": "application/json",
    "Content-Type": "application/json;charset=utf-8",
    "Origin": _FLK_BASE,
    "Referer": f"{_FLK_BASE}/",
}

# flfgCodeId buckets confirmed against the live
# /law-search/search/enumData endpoint (2026-07). The database has NO
# 部门规章 bucket — 规章 lookups must go through PKULaw
# (pkulaw_law_search) or the MOJ 规章库.
_LEVEL_CODES = {
    "constitution": [100],
    "law": [101, 102, 110, 120, 130, 140, 150, 155, 160, 170, 180, 190, 195, 200],
    "regulation": [201, 210, 215],
    "supervision_regulation": [220],
    "local_regulation": [221, 222, 230, 260, 270, 290, 295, 300, 305, 310],
    "judicial_interpretation": [311, 320, 330, 340, 350],
}
_STATUS = {1: "已废止", 2: "已修改", 3: "有效", 4: "尚未生效"}
_TAG_RE = re.compile(r"<[^>]+>")


def _strip_markup(value: Any) -> str:
    if value is None:
        return ""
    return _TAG_RE.sub("", str(value)).strip()


@beta_tool
def flk_npc_search(
    query: str,
    level: Optional[str] = None,
    per_page: int = 8,
) -> str:
    """Search PRC 国家法律法规数据库 (flk.npc.gov.cn). Returns metadata only —
    title, type, office, publish/effective dates, status (有效 / 已修改 /
    已废止 / 尚未生效), doc_id, url, and a ~600-char snippet. **No body
    text.** ``flk_npc_fetch(doc_id)`` returns version history + related
    instruments; for 第X条 body text call
    ``pkulaw_fatiao__get_law_item_content``.

    Covers 宪法, 法律, 行政法规, 监察法规, 地方法规, 司法解释. **No 部门规章**
    — for CAAC / ministry rules use ``pkulaw_law_search``. No auth.

    Args:
        query: Free-text query in Chinese or English (e.g. "民法典 担保",
            "Cybersecurity Law", "行政处罚法").
        level: Optional filter — ``constitution`` (宪法), ``law`` (法律),
            ``regulation`` (行政法规), ``supervision_regulation`` (监察法规),
            ``local_regulation`` (地方法规), ``judicial_interpretation``
            (司法解释). Unsupported values (incl. ``rule`` / 部门规章)
            search all levels and report ``level_filter_applied: false``.
        per_page: Results per page (1-20).
    """
    per_page = max(1, min(int(per_page), 20))
    level_codes = _LEVEL_CODES.get(level or "", [])
    level_note: Optional[str] = None
    if level and not level_codes:
        # Never silently widen: say so when a requested filter has no
        # backing bucket in this database.
        level_note = (
            f"level {level!r} not supported by flk.npc.gov.cn "
            f"(supported: {', '.join(_LEVEL_CODES)}); searched all levels. "
            "For 部门规章 use pkulaw_law_search."
        )
    body: dict[str, Any] = {
        "searchRange": 1,  # 标题
        "sxrq": [],
        "gbrq": [],
        "sxx": [],
        "gbrqYear": [],
        "flfgCodeId": level_codes,
        "zdjgCodeId": [],
        "searchContent": query,
        "pageNum": 1,
        "pageSize": per_page,
    }

    try:
        # Try precise title matching first; fall back to fuzzy title search
        # when the database has no exact-ish hits for the supplied phrase.
        data: dict[str, Any] | None = None
        for search_type in (1, 2):
            resp = request_with_retry(
                "POST",
                _SEARCH_URL,
                json_body={**body, "searchType": search_type},
                headers=_FLK_HEADERS,
            )
            candidate = resp.json()
            if candidate.get("code") == 200 and candidate.get("rows"):
                data = candidate
                break
        if data is None:
            data = candidate if isinstance(candidate, dict) else {}
    except (httpx.HTTPError, ValueError) as e:
        return json.dumps({"error": f"flk.npc.gov.cn request failed: {e!s}", "query": query})

    items = data.get("rows") if isinstance(data, dict) else None
    out: list[dict[str, Any]] = []
    for hit in (items or [])[:per_page]:
        doc_id = hit.get("bbbs")
        status_code = hit.get("sxx")
        url = f"{_FLK_BASE}/detail?bbbs={doc_id}" if doc_id else None
        out.append(
            {
                "title": _strip_markup(hit.get("title")),
                "type": hit.get("flxz"),
                "office": hit.get("zdjgName"),
                "publish_date": hit.get("gbrq"),
                "effective_date": hit.get("sxrq"),
                "status": _STATUS.get(status_code, status_code),
                "doc_id": doc_id,
                "url": url,
                "snippet": truncate(
                    _strip_markup(hit.get("xgzlHighLight")) or _strip_markup(hit.get("title")),
                    600,
                ),
            }
        )
    payload: dict[str, Any] = {
        "source": "flk.npc.gov.cn",
        "query": query,
        "level_filter": level or "all",
        "level_filter_applied": bool(level_codes),
        "count": len(out),
        "results": out,
    }
    if level_note:
        payload["note"] = level_note
    return json.dumps(payload, ensure_ascii=False)


@beta_tool
def flk_npc_fetch(doc_id: str) -> str:
    """Fetch one flk.npc.gov.cn record's outline: metadata, version history
    (历史沿革), and related instruments (主席令 / 修改决定 etc.). **No body
    text** — the PDF/DOCX attachments live on an internal-only OBS endpoint
    (the previewLink API wraps an intranet 172.16.x.x URL), so for 第X条
    body text call ``pkulaw_fatiao__get_law_item_content``.

    Use this to confirm which version of a statute is current (状态 +
    历史沿革) before citing.

    Args:
        doc_id: The ``doc_id`` (``bbbs``) from a ``flk_npc_search`` hit.
    """
    doc_id = (doc_id or "").strip()
    if not doc_id:
        return json.dumps({"error": "doc_id is required"}, ensure_ascii=False)
    try:
        resp = request_with_retry(
            "GET", _DETAIL_URL, params={"bbbs": doc_id}, headers=_FLK_HEADERS
        )
        payload = resp.json()
    except (httpx.HTTPError, ValueError) as e:
        return json.dumps(
            {"error": f"flk.npc.gov.cn detail fetch failed: {e!s}", "doc_id": doc_id}
        )
    data = payload.get("data") if isinstance(payload, dict) else None
    if payload.get("code") != 200 or not isinstance(data, dict):
        return json.dumps(
            {
                "error": f"flk.npc.gov.cn detail returned code {payload.get('code')}",
                "doc_id": doc_id,
                "msg": payload.get("msg"),
            },
            ensure_ascii=False,
        )

    history = [
        {
            "doc_id": h.get("bbbs"),
            "title": _strip_markup(h.get("title")),
            "publish_date": h.get("gbrq"),
            "is_this_version": bool(h.get("highLight")),
        }
        for h in (data.get("lsyg") or [])
    ]
    related = [
        {"title": _strip_markup(x.get("title")), "type": x.get("busiType")}
        for x in (data.get("xgzl") or [])
    ]
    return json.dumps(
        {
            "source": "flk.npc.gov.cn",
            "doc_id": doc_id,
            "title": _strip_markup(data.get("title")),
            "type": data.get("flxz"),
            "office": data.get("zdjgName"),
            "publish_date": data.get("gbrq"),
            "effective_date": data.get("sxrq"),
            "status": _STATUS.get(data.get("sxx"), data.get("sxx")),
            "history": history,
            "related_instruments": related,
            "url": f"{_FLK_BASE}/detail?bbbs={doc_id}",
            "body_text": "",
            "note": (
                "flk.npc.gov.cn does not serve body text publicly; use "
                "pkulaw_fatiao__get_law_item_content for 条文 content."
            ),
        },
        ensure_ascii=False,
    )
