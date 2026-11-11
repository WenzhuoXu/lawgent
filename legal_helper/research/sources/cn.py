"""PRC mainland: 国家法律法规数据库 (flk.npc.gov.cn) and 北大法宝 (PKULaw MCP).

flk.npc.gov.cn — the NPC's official database of 宪法、法律、行政法规、监察法规、
地方性法规、司法解释 (no 部门规章). Title search, full-text search with hit
passages (``hitDisplay``) and the official Word text of every version
(``download/pc`` hands out a presigned public OBS link; the ``urlIn`` beside
it is intranet-only). Free, no key.

PKULaw — 北大法宝's MCP gateway: semantic article and law search over 中央/地方
法规库 (incl. 部门规章), any article or range of articles by title + 条号,
semantic case search (returns 审理查明 / 本院认为), and keyword case lists with
filters (参照级别, 法院, 案号). Needs ``PKULAW_API_TOKEN`` (Bearer).
"""

from __future__ import annotations

import json
import os
import re
import threading
from concurrent.futures import ThreadPoolExecutor
from typing import Any

import httpx

from ...connectors.base import USER_AGENT, request_with_retry
from ..base import Hit, LegalDoc, Source, SourceUnavailable
from ..sections import request_key
from ..textify import bytes_to_text

# ===================================================================== flk

_FLK = "https://flk.npc.gov.cn"
_FLK_HEADERS = {
    "User-Agent": "Mozilla/5.0 (compatible; legal-helper/1.0)",
    "Accept": "application/json, text/plain, */*",
    "Content-Type": "application/json;charset=utf-8",
    "Origin": _FLK,
    "Referer": f"{_FLK}/",
}
_LEVEL_CODES = {
    "constitution": [100],
    "law": [101, 102, 110, 120, 130, 140, 150, 155, 160, 170, 180, 190, 195, 200],
    "regulation": [201, 210, 215],
    "supervision_regulation": [220],
    "local_regulation": [221, 222, 230, 260, 270, 290, 295, 300, 305, 310],
    "judicial_interpretation": [311, 320, 330, 340, 350],
}
_LEVEL_ALIASES = {
    "宪法": "constitution", "法律": "law", "行政法规": "regulation", "监察法规": "supervision_regulation",
    "地方性法规": "local_regulation", "地方法规": "local_regulation", "司法解释": "judicial_interpretation",
}
_STATUS = {"1": "已废止", "2": "已修改", "3": "有效", "4": "尚未生效"}
_STATUS_FILTER = {
    "in_force": [3], "有效": [3], "current": [3], "not_yet": [4], "尚未生效": [4],
    "amended": [2], "已修改": [2], "repealed": [1], "已废止": [1],
}
_TAG = re.compile(r"<[^>]+>")


def _strip(v: Any) -> str:
    return _TAG.sub("", str(v or "")).strip()


def _flk_post(path: str, body: dict[str, Any]) -> dict[str, Any]:
    resp = request_with_retry("POST", f"{_FLK}{path}", json_body=body, headers=_FLK_HEADERS)
    data = resp.json()
    if data.get("code") != 200:
        raise SourceUnavailable(f"flk.npc.gov.cn {path} returned code {data.get('code')}: {data.get('msg')}")
    return data


def _flk_get(path: str, params: dict[str, Any]) -> dict[str, Any]:
    resp = request_with_retry("GET", f"{_FLK}{path}", params=params, headers=_FLK_HEADERS)
    data = resp.json()
    if data.get("code") != 200:
        raise SourceUnavailable(f"flk.npc.gov.cn {path} returned code {data.get('code')}: {data.get('msg')}")
    return data


class FlkSource(Source):
    id = "cn.flk"
    probe = {"query": "民用航空法", "ref": "7ee5a369049e412bbd1a96a2fef74abb", "section": "第128条"}
    name = "国家法律法规数据库 (flk.npc.gov.cn)"
    jurisdictions = ("CN",)
    kinds = ("legislation",)
    modes = ("keyword", "fulltext")
    languages = ("zh",)
    homepage = _FLK
    coverage = (
        "Official NPC database: 宪法、法律、行政法规、监察法规、地方性法规、司法解释, every version "
        "with status (有效/已修改/已废止/尚未生效) and 历史沿革; official Word text. No 部门规章, no cases. "
        "filters: level (law|regulation|local_regulation|judicial_interpretation|…), status (in_force|repealed|…)."
    )

    def search(self, query, *, kind="any", mode="keyword", limit=10, filters=None):
        filters = filters or {}
        level = filters.get("level")
        level = _LEVEL_ALIASES.get(level, level)
        codes = _LEVEL_CODES.get(level or "", [])
        status = filters.get("status")
        sxx = _STATUS_FILTER.get(str(status), []) if status else []
        body = {
            "sxrq": [], "gbrq": [], "sxx": sxx, "gbrqYear": [], "flfgCodeId": codes,
            "zdjgCodeId": [], "searchContent": query, "pageNum": 1, "pageSize": max(limit, 10),
        }
        rows: list[dict[str, Any]] = []
        if mode == "fulltext":
            rows = _flk_post("/law-search/search/list", {**body, "searchRange": 2, "searchType": 2}).get("rows") or []
            rows.sort(key=lambda r: float(r.get("score") or 0), reverse=True)
        else:
            for search_type in (1, 2):
                rows = _flk_post("/law-search/search/list", {**body, "searchRange": 1, "searchType": search_type}).get("rows") or []
                if rows:
                    break
            if not rows:
                rows = _flk_post("/law-search/search/list", {**body, "searchRange": 2, "searchType": 2}).get("rows") or []
                rows.sort(key=lambda r: float(r.get("score") or 0), reverse=True)
                mode = "fulltext"
        rows = rows[:limit]
        snippets: dict[str, str] = {}
        if mode == "fulltext" and rows:
            def passages(bbbs: str) -> tuple[str, str]:
                try:
                    d = _flk_post("/law-search/search/hitDisplay", {
                        "bbbs": bbbs, "searchContent": query, "searchRange": 2, "searchType": 2,
                    })
                    hits = (d.get("data") or {}).get("contentHitDisplay") or []
                    return bbbs, " … ".join(_strip(h) for h in hits[:3])
                except Exception:  # noqa: BLE001 — a snippet is optional
                    return bbbs, ""
            with ThreadPoolExecutor(max_workers=5) as pool:
                for bbbs, snip in pool.map(passages, [r.get("bbbs") for r in rows[:6] if r.get("bbbs")]):
                    snippets[bbbs] = snip
        hits = []
        for r in rows:
            bbbs = r.get("bbbs")
            if not bbbs:
                continue
            hits.append(Hit(
                ref=self.ref(bbbs),
                title=_strip(r.get("title")),
                source=self.id,
                kind="legislation",
                url=f"{_FLK}/detail?bbbs={bbbs}",
                date=r.get("gbrq"),
                status=_STATUS.get(str(r.get("sxx")), str(r.get("sxx") or "")) or None,
                snippet=snippets.get(bbbs, ""),
                extra={k: v for k, v in {
                    "type": r.get("flxz"), "office": r.get("zdjgName"), "effective": r.get("sxrq"),
                }.items() if v},
            ))
        return hits

    def fetch(self, native_id, *, language=None):
        bbbs = native_id.strip()
        d = _flk_get("/law-search/search/flfgDetails", {"bbbs": bbbs}).get("data") or {}
        title = _strip(d.get("title"))
        meta: dict[str, Any] = {
            "type": d.get("flxz"),
            "office": d.get("zdjgName"),
            "published": d.get("gbrq"),
            "effective": d.get("sxrq"),
            "status": _STATUS.get(str(d.get("sxx")), d.get("sxx")),
            "versions": [
                {"ref": self.ref(h.get("bbbs")), "date": h.get("gbrq"), "this": bool(h.get("highLight"))}
                for h in (d.get("lsyg") or []) if h.get("bbbs")
            ],
        }
        related = [_strip(x.get("title")) for x in (d.get("xgzl") or []) if x.get("title")]
        if related:
            meta["related_instruments"] = related[:10]
        text = ""
        last_err = ""
        for fmt in ("docx", "pdf"):
            try:
                link = (_flk_get("/law-search/download/pc", {"bbbs": bbbs, "format": fmt}).get("data") or {}).get("url")
                if not link:
                    continue
                resp = request_with_retry("GET", link, headers={"User-Agent": USER_AGENT},
                                          timeout=httpx.Timeout(60.0, connect=10.0))
                text = bytes_to_text(resp.content, resp.headers.get("content-type", ""), name_hint=f"x.{fmt}")
                if text.strip():
                    meta["text_format"] = fmt
                    break
            except Exception as exc:  # noqa: BLE001 — try the other format
                last_err = f"{type(exc).__name__}: {exc}"
        if not text.strip():
            meta["no_text_reason"] = f"official file could not be downloaded ({last_err or 'no link'})"
        return LegalDoc(
            ref=self.ref(bbbs), title=title, source=self.id, text=text,
            url=f"{_FLK}/detail?bbbs={bbbs}", kind="legislation", language="zh", metadata=meta,
        )


# ================================================================== PKULaw

_PKU = "https://apim-gateway.pkulaw.com"
_PKU_SERVICES = {
    "law_search": "/mcp-law-search-service",
    "fatiao": "/mcp-fatiao",
    "case_search": "/mcp-case-search-service",
    "case_list": "/mcp-case",
}
# The gateway renamed every tool and moved its arguments to snake_case
# between 2026-11-05 and 11-09 without notice (search_article →
# search_law_articles, get_law_item_content → get_law_article, search_case →
# search_cases, get_case_list → search_cases_by_keyword). An unknown tool now
# raises SourceUnavailable naming what the service lists, so the next rename
# shows up in the health probe and in the search result, not as empty hits.
_CASE_CACHE: dict[str, dict[str, Any]] = {}
#: Hits by native id, so a fetch of a searched article or law reads what the
#: search returned (the article text, the exact versioned title).
_ARTICLE_CACHE: dict[str, dict[str, Any]] = {}
_LAW_CACHE: dict[str, dict[str, Any]] = {}
_CASE_LOCK = threading.Lock()
#: get_law_articles_by_range spans at most 10 numbers; a request is split into
#: such calls up to this many articles.
_RANGE_SPAN = 10
_MAX_ARTICLES = 60


def _pku_tool_names(service: str, headers: dict[str, str]) -> list[str]:
    try:
        resp = httpx.post(_PKU + _PKU_SERVICES[service], headers=headers, timeout=20.0,
                          json={"jsonrpc": "2.0", "id": 1, "method": "tools/list", "params": {}})
        raw = resp.text.strip()
        if raw.startswith(("event:", "data:")):
            raw = "\n".join(ln[5:].strip() for ln in raw.splitlines() if ln.startswith("data:"))
        return [t.get("name", "") for t in (json.loads(raw).get("result") or {}).get("tools") or []]
    except Exception:  # noqa: BLE001 — only decorates an error message
        return []


def _pku_call(service: str, tool: str, arguments: dict[str, Any]) -> Any:
    token = os.getenv("PKULAW_API_TOKEN", "").strip()
    if not token:
        raise SourceUnavailable("PKULaw needs PKULAW_API_TOKEN (mcp.pkulaw.com → 控制台 → 应用管理).")
    body = {"jsonrpc": "2.0", "id": 1, "method": "tools/call", "params": {"name": tool, "arguments": arguments}}
    headers = {
        "Authorization": f"Bearer {token}",
        "Content-Type": "application/json",
        "Accept": "application/json, text/event-stream",
        "User-Agent": USER_AGENT,
    }
    try:
        resp = request_with_retry("POST", _PKU + _PKU_SERVICES[service], json_body=body, headers=headers,
                                  timeout=httpx.Timeout(60.0, connect=10.0))
    except httpx.HTTPStatusError as exc:
        if exc.response.status_code in (401, 403):
            raise SourceUnavailable(
                f"PKULaw rejected the token ({exc.response.status_code}); renew PKULAW_API_TOKEN at mcp.pkulaw.com."
            ) from exc
        raise
    raw = resp.text.strip()
    if raw.startswith("event:") or raw.startswith("data:"):
        raw = "\n".join(ln[5:].strip() for ln in raw.splitlines() if ln.startswith("data:"))
    msg = json.loads(raw)
    result = msg.get("result") or {}
    error = msg.get("error")
    text = " ".join(c.get("text", "") for c in result.get("content") or [])
    if "Unknown tool" in (str(error or "") + (text if result.get("isError") else "")):
        listed = _pku_tool_names(service, headers)
        raise SourceUnavailable(
            f"PKULaw's {service} service no longer has tool {tool!r} (it lists {listed or 'nothing'}); "
            "the cn.pkulaw adapter needs updating."
        )
    if error:
        raise SourceUnavailable(f"PKULaw {tool}: {error}")
    if result.get("isError"):
        raise ValueError(f"PKULaw {tool}: {text[:600]}")
    sc = result.get("structuredContent")
    if sc is not None:
        return sc
    try:
        return json.loads(text)
    except ValueError:
        return {"text": text}


def _joined(value: Any, sep: str = "、") -> str:
    if isinstance(value, list):
        return sep.join(str(v) for v in value if v)
    return str(value or "")


def _url(item: dict[str, Any]) -> str | None:
    cit = item.get("citation")
    if isinstance(cit, dict):
        return cit.get("url") or item.get("url")
    return item.get("url") or (cit if isinstance(cit, str) and cit.startswith("http") else None)


def _case_text(c: dict[str, Any]) -> str:
    parts = [c.get("title") or ""]
    head = [
        ("案号", c.get("case_number")),
        ("法院", c.get("courthouse_name") or c.get("court")),
        ("审结日期", c.get("decision_date") or c.get("last_instance_date")),
        ("案由", c.get("cause_of_action") or _joined(c.get("category"), "／")),
        ("文书类型", c.get("doc_type") or _joined(c.get("document_attr"))),
        ("参照级别", _joined(c.get("case_grade"))),
        ("当事人", c.get("party")),
    ]
    parts += [f"{k}：{v}" for k, v in head if v]
    for label, key in (("裁判要点", "gist"), ("基本案情", "content"), ("诉讼请求", "plaintiff_claims"),
                       ("争议焦点", "controversial_focus"), ("审理查明", "ascertain"), ("本院认为", "identified"),
                       ("裁判依据", "referee_basis"), ("命中片段", "fulltext_snippet"),
                       ("裁判结果", "referee_result"), ("裁判结果", "judgment_result")):
        v = c.get(key)
        if v:
            parts += ["", f"【{label}】", _joined(v, "\n").strip()]
    return "\n".join(parts)


def _case_id(c: dict[str, Any]) -> str:
    m = re.search(r"/pfnl/([0-9a-f]+)", _url(c) or "")
    if m:
        return m.group(1)
    return c.get("gid") or c.get("case_number") or c.get("title") or ""


def _article_spans(request: str) -> list[tuple[str, str]]:
    """'第13条' / '13-17' / '第一条至第二十条, 第30条' → [(start, end)] in PKULaw numbering."""
    spans = []
    for part in (p.strip() for p in re.split(r"[,，、;；]", request) if p.strip()):
        rng = re.match(r"^(.+?)\s*(?:-|–|—|～|~|至|到)\s*(.+)$", part)
        a, b = (rng.group(1), rng.group(2)) if rng else (part, part)
        ka, kb = request_key(a), request_key(b)
        if not ka or not kb:
            raise ValueError(f"cannot read {part!r} as an article number or range (e.g. 第13条, 13-17)")
        spans.append((ka, kb))
    return spans


def _range_calls(title: str, start: str, end: str) -> list[dict[str, Any]]:
    lo, hi = float(start), float(end)
    if hi < lo:
        lo, hi = hi, lo
    if int(hi) - int(lo) + 1 > _MAX_ARTICLES:
        raise ValueError(f"{start}-{end} spans more than {_MAX_ARTICLES} articles; ask for a narrower range "
                         "or read the whole statute from cn.flk")
    calls = []
    first = int(lo)
    while first <= int(hi):
        last = min(int(hi), first + _RANGE_SPAN - 1)
        a = start if first == int(lo) else str(first)
        b = end if last == int(hi) else str(last)
        calls.append({"law_title": title, "start_article_number": a, "end_article_number": b})
        first = last + 1
    return calls


class PkulawSource(Source):
    id = "cn.pkulaw"
    probe = {"query": "旅客随身携带物品遗失 承运人责任", "kind": "legislation", "ref": "article/中华人民共和国民用航空法|第一百二十八条", "min_chars": 40}
    name = "北大法宝 PKULaw"
    jurisdictions = ("CN",)
    kinds = ("legislation", "case")
    modes = ("keyword", "semantic")
    languages = ("zh",)
    auth_env = ("PKULAW_API_TOKEN",)
    auth_help = "mcp.pkulaw.com → 控制台 → 应用管理 → 创建应用; put the token in legal_helper/api_key."
    homepage = "https://www.pkulaw.com"
    coverage = (
        "Legislation incl. 部门规章 and 地方: semantic article search (each hit one article, any version — "
        "check status) plus the matching laws (ref cn.pkulaw:law/…). Any article by title and number: "
        "ref cn.pkulaw:article/<法规名>|第N条, or a range |第N条-第M条 (up to 60); on a law ref pass "
        "section='第N条'. Cases: semantic 类案 search (审理查明/本院认为) and keyword lists by "
        "案号/标题/全文. filters: case_grade (指导性案例|公报案例|典型案例|参考案例|权威案例), court, "
        "province, case_number, date_from, date_to, status (现行有效…), lib (中央|地方), office. "
        "No full judgment text; whole national statutes and versions come from cn.flk."
    )

    # ------------------------------------------------------------ search
    def search(self, query, *, kind="any", mode="keyword", limit=10, filters=None):
        filters = filters or {}
        size = max(5, min(limit, 20))  # the gateway's minimum is 5
        hits: list[Hit] = []
        if kind in ("legislation", "any"):
            args: dict[str, Any] = {"query_text": query, "size": size}
            for k_in, k_out in (("status", "timeliness"), ("lib", "library"), ("office", "issue_department"),
                                ("date_from", "implement_date_start"), ("date_to", "implement_date_end")):
                if filters.get(k_in):
                    args[k_out] = filters[k_in]
            with ThreadPoolExecutor(max_workers=2) as pool:
                f_art = pool.submit(_pku_call, "law_search", "search_law_articles", args)
                f_law = pool.submit(_pku_call, "law_search", "search_laws", args)
                articles = (f_art.result() or {}).get("result") or []
                try:
                    laws = (f_law.result() or {}).get("result") or []
                except (ValueError, httpx.HTTPError):
                    laws = []
            art_hits = [self._article_hit(r) for r in articles]
            law_hits = [self._law_hit(r) for r in laws[:3]]
            # Articles answer a question, laws a title; neither may push the
            # other out of a short interleaved list.
            for i in range(max(len(art_hits), len(law_hits))):
                hits += art_hits[i:i + 1] + law_hits[i:i + 1]
        if kind in ("case", "any"):
            cases: list[dict[str, Any]] = []
            if mode == "semantic" or (mode != "keyword" and not filters.get("case_number")):
                args = {"text": query, "size": min(size, 20)}
                for k_in, k_out in (("case_grade", "case_grade"), ("court", "courthouse_name"),
                                    ("province", "courthouse_province"), ("date_from", "decision_date_start"),
                                    ("date_to", "decision_date_end")):
                    if filters.get(k_in):
                        v = filters[k_in]
                        args[k_out] = v[0] if isinstance(v, list) else v
                data = _pku_call("case_search", "search_cases", args) or {}
                cases = list(data.get("authoritative_cases") or []) + list(data.get("ordinary_cases") or [])
            if not cases:
                args = {"page_size": size}
                if filters.get("case_number"):
                    args["case_number"] = filters["case_number"]
                elif re.search(r"[（(]\d{4}[)）]", query):
                    args["case_number"] = query.strip()
                else:
                    args["fulltext"] = query.strip()[:40]
                if filters.get("case_grade"):
                    g = filters["case_grade"]
                    args["case_grade"] = g if isinstance(g, list) else [g]
                if filters.get("court"):
                    args["court"] = filters["court"]
                if filters.get("date_from"):
                    args["start_last_instance_date"] = filters["date_from"]
                if filters.get("date_to"):
                    args["end_last_instance_date"] = filters["date_to"]
                cases = (_pku_call("case_list", "search_cases_by_keyword", args) or {}).get("items") or []
            for c in cases[:size]:
                cid = _case_id(c)
                with _CASE_LOCK:
                    _CASE_CACHE[cid] = c
                hits.append(Hit(
                    ref=self.ref(f"case/{cid}"), title=c.get("title") or "", source=self.id, kind="case",
                    url=_url(c), date=c.get("decision_date") or c.get("last_instance_date") or None,
                    citation=c.get("case_number") or None, status=_joined(c.get("case_grade")) or None,
                    snippet=_joined(c.get("gist") or c.get("content") or c.get("fulltext_snippet")
                                    or c.get("identified") or "", "\n")[:500],
                    extra={k: v for k, v in {"court": c.get("courthouse_name") or c.get("court"),
                                             "cause": c.get("cause_of_action") or _joined(c.get("category"), "／")}.items() if v},
                ))
        return hits

    def _article_hit(self, r: dict[str, Any]) -> Hit:
        title = r.get("law_title") or ""
        label = r.get("article_label") or ""
        native = f"article/{title}|{label or r.get('article_number') or r.get('gid')}"
        with _CASE_LOCK:
            _ARTICLE_CACHE[native] = r
        return Hit(
            ref=self.ref(native), title=f"{title} {label}".strip(), source=self.id, kind="legislation",
            url=_url(r), date=r.get("issue_date") or None, status=r.get("timeliness") or None,
            snippet=(r.get("article_content") or "")[:600],
            extra={k: v for k, v in {"level": r.get("effectiveness"), "office": r.get("issue_department"),
                                     "doc_no": r.get("doc_no")}.items() if v},
        )

    def _law_hit(self, r: dict[str, Any]) -> Hit:
        title = r.get("title") or ""
        native = f"law/{title}"
        with _CASE_LOCK:
            _LAW_CACHE[native] = r
        bits = [r.get("doc_no"), r.get("effectiveness"), r.get("issue_department"),
                f"公布 {r['issue_date']}" if r.get("issue_date") else ""]
        return Hit(
            ref=self.ref(native), title=title, source=self.id, kind="legislation", url=_url(r),
            date=r.get("issue_date") or None, status=r.get("timeliness") or None,
            snippet=" · ".join(b for b in bits if b) + " — read articles with legal_fetch(section='第N条' or a range)",
            extra={k: v for k, v in {"level": r.get("effectiveness"), "doc_no": r.get("doc_no")}.items() if v},
        )

    # ------------------------------------------------------------- fetch
    def section_ref(self, native_id: str, section: str) -> str | None:
        """legal_fetch(section=…) on a law or article ref reads those articles."""
        kind, _, rest = native_id.partition("/")
        if kind == "law":
            return f"article/{rest}|{section}"
        if kind == "article":
            title, _, label = rest.partition("|")
            try:
                if _article_spans(section) == _article_spans(label):
                    return None
            except ValueError:
                return None
            return f"article/{title}|{section}"
        return None

    def fetch(self, native_id, *, language=None):
        kind, _, rest = native_id.partition("/")
        if kind == "article":
            return self._fetch_articles(native_id, rest)
        if kind == "law":
            with _CASE_LOCK:
                r = _LAW_CACHE.get(native_id) or {}
            title = r.get("title") or rest
            lines = [f"《{title}》"] + [f"{k}：{v}" for k, v in (
                ("发文字号", r.get("doc_no")), ("效力位阶", r.get("effectiveness")), ("时效性", r.get("timeliness")),
                ("制定机关", r.get("issue_department")), ("公布日期", r.get("issue_date")),
                ("施行日期", r.get("implementation_date"))) if v]
            lines += ["", "PKULaw serves this document article by article: call legal_fetch on this ref with "
                          "section='第N条', a range '第N条-第M条' or a list. The whole text of a national "
                          "statute and its versions come from cn.flk."]
            return LegalDoc(
                ref=self.ref(native_id), title=title, source=self.id, text="\n".join(lines), url=_url(r),
                kind="legislation", language="zh",
                metadata={k: v for k, v in {"status": r.get("timeliness"), "level": r.get("effectiveness"),
                                            "doc_no": r.get("doc_no"), "published": r.get("issue_date")}.items() if v},
                ttl=24 * 3600,
            )
        if kind == "case":
            with _CASE_LOCK:
                c = _CASE_CACHE.get(rest)
            if c is None and re.search(r"[（(]\d{4}[)）]", rest):
                items = (_pku_call("case_list", "search_cases_by_keyword",
                                   {"case_number": rest, "page_size": 5}) or {}).get("items") or []
                c = items[0] if items else None
            if not c:
                raise ValueError(f"PKULaw case {rest!r} not found; search for it again with legal_search")
            return LegalDoc(
                ref=self.ref(native_id), title=c.get("title") or rest, source=self.id, text=_case_text(c),
                url=_url(c), kind="case", language="zh",
                metadata={"note": "PKULaw serves the parts above, not the full judgment; open the URL for the rest."},
            )
        raise ValueError("PKULaw refs are article/<法规名>|<第N条 or range>, law/<法规名> or case/<id>; "
                         "take them from legal_search")

    def _fetch_articles(self, native_id: str, rest: str) -> LegalDoc:
        title, _, label = rest.partition("|")
        with _CASE_LOCK:
            hit = _ARTICLE_CACHE.get(native_id)
        if hit and hit.get("article_content"):
            return LegalDoc(
                ref=self.ref(native_id), title=f"《{hit.get('law_title')}》{hit.get('article_label') or ''}",
                source=self.id, text=str(hit["article_content"]), url=_url(hit), language="zh",
                metadata={k: v for k, v in {
                    "status": hit.get("timeliness"), "level": hit.get("effectiveness"),
                    "office": hit.get("issue_department"), "doc_no": hit.get("doc_no"),
                    "published": hit.get("issue_date"),
                }.items() if v},
                ttl=24 * 3600,
            )
        if not label:
            raise ValueError("name the article: cn.pkulaw:article/<法规名>|第N条 (or pass section=)")
        spans = _article_spans(label)
        items: list[dict[str, Any]] = []
        missing: list[str] = []
        head: dict[str, Any] = {}
        for start, end in spans:
            if start == end:
                data = _pku_call("fatiao", "get_law_article", {"law_title": title, "article_number": start}) or {}
                if data.get("article_content"):
                    items.append(data)
                    head = head or data
                else:
                    missing.append(start)
                continue
            for call in _range_calls(title, start, end):
                data = _pku_call("fatiao", "get_law_articles_by_range", call) or {}
                items += data.get("items") or []
                missing += data.get("missing_article_numbers") or []
                if data.get("law_title") and not head:
                    head = data
        if not items:
            raise ValueError(
                f"PKULaw has no {label} in the version of 《{title}》 it holds as current. Check the number, "
                "name a version (e.g. 《…(2021修正)》), or read the whole statute from cn.flk."
            )
        full_title = head.get("law_title") or title
        meta: dict[str, Any] = {k: v for k, v in {
            "status": head.get("timeliness"), "level": _joined(head.get("effectiveness")),
            "office": _joined(head.get("issue_department")), "doc_no": head.get("document_no"),
            "published": head.get("issue_date"), "effective": head.get("implement_date"),
        }.items() if v}
        meta["version_note"] = ("Without a version in the title PKULaw reads the version it marks 现行有效; "
                                "the title above names the version read.")
        if missing:
            meta["missing_articles"] = missing
        text = "\n".join(str(i.get("article_content") or "").strip() for i in items)
        return LegalDoc(
            ref=self.ref(native_id), title=f"《{full_title}》{label}", source=self.id, text=text,
            url=_url(items[0]) or _url(head), language="zh", metadata=meta, ttl=24 * 3600,
        )


# PKULaw first: it is the primary PRC source (the user's standing rule), so
# its hits lead an interleaved CN search; the national database supplies the
# official text, versions and status.
SOURCES = [PkulawSource(), FlkSource()]
