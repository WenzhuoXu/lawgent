"""Japan: e-Gov 法令API v2 (official text) and Japanese Law Translation (English).

e-Gov 法令API Version 2 (laws.e-gov.go.jp/api/2, Digital Agency) — every
current Act, Cabinet Order and Ministerial Ordinance with its revision
history. Title search (``/laws``), full-text keyword search with highlighted
sentences (``/keyword``; AND by spaces), the whole law as structured XML
(``/law_data``) and the revision list (``/law_revisions``). The XML carries
the structure, so this adapter hands the section index exact provisions:
第九十七条 → key "97", 第七十一条の二 → "71.2" (from the Article ``Num``
attribute, never from position: ``Article[97]`` in an ``elm`` path is the 97th
article *by position*, which in 航空法 is 第七十一条の二). Free, no key.

Japanese Law Translation (japaneselawtranslation.go.jp, Ministry of Justice)
— English translations, **not official** ("only the original Japanese texts
have legal effect"), often several amendments behind the current text. Title
and full-text search through the site's own form (CakePHP: a session cookie
and CSRF token), fetch through the bilingual TXT download the law page links.
Content under the Public Data License 1.0.
"""

from __future__ import annotations

import re
from typing import Any, Optional

import httpx
from lxml import etree

from ...connectors.base import USER_AGENT, request_with_retry
from ..base import Hit, LegalDoc, Source, SourceUnavailable
from ..sections import request_key
from ..textify import normalize_lines

_LONG = httpx.Timeout(60.0, connect=10.0)

# ===================================================================== e-Gov

_EGOV = "https://laws.e-gov.go.jp/api/2"
_EGOV_SITE = "https://laws.e-gov.go.jp"
_LAW_ID = re.compile(r"^\d{3}[A-Z]{2}\d{10}$")
_REVISION_ID = re.compile(r"^\d{3}[A-Z]{2}\d{10}_\d{8}_[0-9A-Z]{15}$|^\d{3}[A-Z]{2}\d{10}_\d{8}_\d{3}[A-Z]{2}\d{10}$")
_LAW_NUM = re.compile(r"(明治|大正|昭和|平成|令和).{1,4}年.*第.+号")

_TYPE_FILTER = {
    "law": "Act", "act": "Act", "法律": "Act", "constitution": "Constitution", "憲法": "Constitution",
    "cabinet_order": "CabinetOrder", "政令": "CabinetOrder", "regulation": "CabinetOrder,MinisterialOrdinance",
    "ministerial_ordinance": "MinisterialOrdinance", "省令": "MinisterialOrdinance", "rule": "Rule", "規則": "Rule",
    "imperial_order": "ImperialOrder", "勅令": "ImperialOrder",
}
_REPEAL = {"Repeal": "repealed", "Expire": "expired", "Suspend": "suspended", "LossOfEffectiveness": "lost effect"}
_REVISION_STATUS = {
    "CurrentEnforced": "in force", "UnEnforced": "not yet in force",
    "PreviousEnforced": "superseded version", "Repeal": "repealed",
}

_CONTAINERS = {
    "Part": "PartTitle", "Chapter": "ChapterTitle", "Section": "SectionTitle",
    "Subsection": "SubsectionTitle", "Division": "DivisionTitle",
}
_APPDX = {
    "AppdxTable": "AppdxTableTitle", "AppdxNote": "AppdxNoteTitle", "AppdxStyle": "AppdxStyleTitle",
    "AppdxFormat": "AppdxFormatTitle", "AppdxFig": "AppdxFigTitle", "Appdx": "ArithFormulaNum",
}
_ITEM = re.compile(r"^(Item|Subitem\d+)$")
_LIST = re.compile(r"^(List|Sublist\d+)$")


def _egov_get(path: str, params: dict[str, Any], *, timeout: Optional[httpx.Timeout] = None) -> httpx.Response:
    try:
        return request_with_retry(
            "GET", f"{_EGOV}{path}", params=params,
            headers={"User-Agent": USER_AGENT, "Accept": "application/json, application/xml"},
            timeout=timeout,
        )
    except httpx.HTTPStatusError as exc:
        code = exc.response.status_code
        msg = ""
        try:
            msg = exc.response.json().get("message") or ""
        except ValueError:
            msg = exc.response.text[:200]
        if code in (400, 404):
            raise ValueError(f"e-Gov {path}: {code} {msg}".strip()) from exc
        raise SourceUnavailable(f"e-Gov 法令API answered {code} {msg}".strip()) from exc
    except httpx.TransportError as exc:
        raise SourceUnavailable(f"e-Gov 法令API unreachable ({type(exc).__name__}); try again later.") from exc


def _status_of(rev: dict[str, Any]) -> str:
    rs = rev.get("repeal_status")
    if rs and rs != "None":
        return _REPEAL.get(rs, rs)
    return _REVISION_STATUS.get(rev.get("current_revision_status") or "", rev.get("current_revision_status") or "")


def _common_params(filters: dict[str, Any]) -> dict[str, Any]:
    p: dict[str, Any] = {}
    t = filters.get("law_type") or filters.get("type") or filters.get("level")
    if t:
        p["law_type"] = _TYPE_FILTER.get(str(t).lower(), _TYPE_FILTER.get(str(t), str(t)))
    for k in ("category_cd", "asof", "promulgation_date_from", "promulgation_date_to"):
        if filters.get(k):
            p[k] = filters[k]
    if filters.get("date_from"):
        p["promulgation_date_from"] = filters["date_from"]
    if filters.get("date_to"):
        p["promulgation_date_to"] = filters["date_to"]
    return p


def _highlight(s: str) -> str:
    s = re.sub(r"</?span>", "**", s or "")
    return re.sub(r"<[^>]+>", "", s)


def _xtext(el: Optional[etree._Element]) -> str:
    """Text of an e-Gov element: ruby readings dropped, columns spaced."""
    if el is None:
        return ""

    def walk(e: etree._Element) -> str:
        out = [e.text or ""]
        for ch in e:
            tag = ch.tag if isinstance(ch.tag, str) else ""
            if tag == "Rt":
                pass
            elif tag == "Column":
                out.append("　" + walk(ch))
            else:
                out.append(walk(ch))
            out.append(ch.tail or "")
        return "".join(out)

    s = walk(el)
    s = re.sub(r"\s*\n\s*", "", s)
    return s.strip()


def _num_key(num: str) -> str:
    """Article ``Num`` attribute → section key: '97' → '97', '71_2' → '71.2', '40:42' → '40'."""
    num = (num or "").split(":")[0]
    return ".".join(p for p in num.split("_") if p)


class _Writer:
    """Accumulates lines and the provisions that start on them (offsets into the text)."""

    def __init__(self) -> None:
        self.parts: list[str] = []
        self.pos = 0
        self.marks: list[tuple[int, str, str, str, str]] = []

    def line(self, s: str) -> Optional[int]:
        s = (s or "").strip()
        if not s:
            return None
        start = self.pos
        self.parts.append(s + "\n")
        self.pos += len(s) + 1
        return start

    def mark(self, start: Optional[int], label: str, key: str, heading: str, level: str) -> None:
        if start is not None:
            self.marks.append((start, label, key, heading, level))

    def result(self) -> tuple[str, list[dict[str, Any]]]:
        text = "".join(self.parts).rstrip("\n")
        marks = sorted(self.marks, key=lambda m: m[0])
        secs = []
        for i, (start, label, key, heading, level) in enumerate(marks):
            end = marks[i + 1][0] if i + 1 < len(marks) else len(text)
            secs.append({"label": label, "key": key, "heading": heading, "start": start,
                         "end": max(start, min(end, len(text))), "level": level})
        return text, secs


class _EgovRenderer:
    """e-Gov law XML → text with one block per line, plus the provisions."""

    def __init__(self) -> None:
        self.w = _Writer()
        self.amend_seen = False
        self.appdx_seen = False

    def render(self, law: etree._Element) -> tuple[str, list[dict[str, Any]]]:
        body = law.find("LawBody")
        self.w.line(_xtext(law.find("LawBody/LawTitle")))
        self.w.line(_xtext(law.find("LawNum")))
        if body is not None:
            for ch in body:
                if ch.tag in ("LawTitle", "TOC"):
                    continue
                self.node(ch, suppl=False)
        return self.w.result()

    # ------------------------------------------------------------------ nodes
    def node(self, el: etree._Element, *, suppl: bool) -> None:
        tag = el.tag if isinstance(el.tag, str) else ""
        if not tag or tag == "TOC":
            return
        if tag in _CONTAINERS:
            title = _xtext(el.find(_CONTAINERS[tag]))
            start = self.w.line(title)
            if not suppl and title:
                self.w.mark(start, title, "", title, "container")
            for ch in el:
                if ch.tag != _CONTAINERS[tag]:
                    self.node(ch, suppl=suppl)
            return
        if tag == "Article":
            self.article(el, suppl=suppl)
            return
        if tag == "Paragraph":
            self.paragraph(el, suppl=suppl)
            return
        if _ITEM.match(tag):
            self.item(el, suppl=suppl)
            return
        if tag == "TableStruct":
            self.table(el, suppl=suppl)
            return
        if tag == "FigStruct":
            self.w.line("［図］" + _xtext(el.find("FigStructTitle")))
            return
        if _LIST.match(tag):
            for ch in el:
                if ch.tag.endswith("Sentence"):
                    self.w.line(_xtext(ch))
                else:
                    self.node(ch, suppl=suppl)
            return
        if tag == "SupplProvision":
            self.suppl(el)
            return
        if tag in _APPDX:
            self.appendix(el, tag, suppl=suppl)
            return
        if tag.startswith("SupplProvisionAppdx"):
            title_tag = next((c.tag for c in el if c.tag.endswith("Title")), None)
            self.w.line(" ".join(x for x in (_xtext(el.find(title_tag)) if title_tag else "",
                                              _xtext(el.find("RelatedArticleNum"))) if x))
            for ch in el:
                if ch.tag != title_tag and ch.tag != "RelatedArticleNum":
                    self.node(ch, suppl=True)
            return
        if tag in ("Sentence",) or tag.endswith(("Sentence", "Title", "Label", "Caption", "Statement")):
            self.w.line(_xtext(el))
            return
        if tag in ("Preamble", "MainProvision", "Remarks", "Class", "AmendProvision", "NewProvision"):
            for ch in el:
                self.node(ch, suppl=suppl)
            return
        if len(el):
            for ch in el:
                self.node(ch, suppl=suppl)
        else:
            self.w.line(_xtext(el))

    def article(self, el: etree._Element, *, suppl: bool) -> None:
        caption = _xtext(el.find("ArticleCaption"))
        title = _xtext(el.find("ArticleTitle"))
        start = self.w.line(caption) if caption else None
        first = True
        for ch in el:
            if ch.tag in ("ArticleCaption", "ArticleTitle"):
                continue
            if ch.tag == "Paragraph":
                s = self.paragraph(ch, suppl=suppl, article_title=title if first else None)
                if first and start is None:
                    start = s
                first = False
            else:
                self.node(ch, suppl=suppl)
        if first:  # an article with no paragraph ("第五条　削除" is still a paragraph; be safe)
            s = self.w.line(title)
            start = start if start is not None else s
        if not suppl and title:
            self.w.mark(start, title, _num_key(el.get("Num", "")), f"{title}{caption}", "article")

    def paragraph(self, el: etree._Element, *, suppl: bool, article_title: Optional[str] = None) -> Optional[int]:
        start = None
        cap = _xtext(el.find("ParagraphCaption"))
        if cap:
            start = self.w.line(cap)
        num = _xtext(el.find("ParagraphNum"))
        sent = _xtext(el.find("ParagraphSentence"))
        head = article_title or num
        s = self.w.line(f"{head}　{sent}" if head and sent else (head or sent))
        start = start if start is not None else s
        for ch in el:
            if ch.tag in ("ParagraphCaption", "ParagraphNum", "ParagraphSentence"):
                continue
            self.node(ch, suppl=suppl)
        return start

    def item(self, el: etree._Element, *, suppl: bool) -> None:
        tag = el.tag
        title = _xtext(el.find(f"{tag}Title"))
        sent = _xtext(el.find(f"{tag}Sentence"))
        self.w.line(f"{title}　{sent}" if title and sent else (title or sent))
        for ch in el:
            if ch.tag in (f"{tag}Title", f"{tag}Sentence"):
                continue
            self.node(ch, suppl=suppl)

    def table(self, el: etree._Element, *, suppl: bool) -> None:
        for ch in el:
            if ch.tag == "TableStructTitle":
                self.w.line(_xtext(ch))
            elif ch.tag == "Table":
                for row in ch:
                    if row.tag not in ("TableRow", "TableHeaderRow"):
                        continue
                    cells = []
                    for col in row:
                        parts = [_xtext(c) for c in col] if len(col) else [_xtext(col)]
                        cells.append(" ".join(p for p in parts if p))
                    self.w.line(" | ".join(c if c else "—" for c in cells))
            else:
                self.node(ch, suppl=suppl)

    def suppl(self, el: etree._Element) -> None:
        amend = el.get("AmendLawNum") or ""
        label = f"附則（{amend}）" if amend else "附則"
        line = label + ("　抄" if el.get("Extract") == "true" else "")
        start = self.w.line(line)
        if not amend:
            self.w.mark(start, "附則", "", "附則", "container")
            self.w.mark(start, label, label, line, "article")
            return self._suppl_body(el)
        if not self.amend_seen:
            n = len(el.getparent().findall("SupplProvision[@AmendLawNum]")) if el.getparent() is not None else 0
            title = f"改正法の附則（{n}件; read one by its label, e.g. 附則（{amend}））"
            self.w.mark(start, title, "", title, "container")
            self.amend_seen = True
        # Amendment 附則 are addressable and searchable but kept out of the outline
        # (level "para"): 航空法 has 75 of them.
        self.w.mark(start, label, label, line, "para")
        self._suppl_body(el)

    def _suppl_body(self, el: etree._Element) -> None:
        for ch in el:
            if ch.tag == "SupplProvisionLabel":
                continue
            self.node(ch, suppl=True)

    def appendix(self, el: etree._Element, tag: str, *, suppl: bool) -> None:
        title = _xtext(el.find(_APPDX[tag]))
        related = _xtext(el.find("RelatedArticleNum"))
        line = f"{title}{related}"
        start = self.w.line(line)
        short = re.split(r"[（(]", title, maxsplit=1)[0].strip() if title else ""
        if short and not suppl:
            if not self.appdx_seen:
                self.w.mark(start, "別表等", "", "別表等", "container")
                self.appdx_seen = True
            self.w.mark(start, short, short, line, "article")
        for ch in el:
            if ch.tag in (_APPDX[tag], "RelatedArticleNum"):
                continue
            self.node(ch, suppl=suppl)


class EgovSource(Source):
    id = "jp.egov"
    probe = {"query": "航空法", "kind": "legislation", "ref": "327AC0000000231", "section": "第九十七条", "min_chars": 5000}
    name = "e-Gov 法令検索 / 法令API v2 (Digital Agency)"
    jurisdictions = ("JP",)
    kinds = ("legislation",)
    modes = ("keyword", "fulltext")
    languages = ("ja",)
    homepage = "https://laws.e-gov.go.jp"
    coverage = (
        "Official current text of Japanese 憲法・法律・政令・勅令・府省令・規則, with revision history (asof "
        "dates from 2017-04-01). Search in Japanese: keyword = title (航空法, 民法), law number "
        "(昭和二十七年法律第二百三十一号) or law ID; fulltext = words inside the text (AND by spaces, "
        "hit sentences highlighted). Refs: law ID (327AC0000000231), a revision ID, or '<law ID>@YYYY-MM-DD' "
        "for the version in force on a date. Provisions: 第九十七条 / 97, 第七十一条の二 / 71.2, 附則, 別表第一. "
        "filters: law_type (law|cabinet_order|ministerial_ordinance|rule), asof, category_cd, date_from, date_to. "
        "No case law; English via jp.jlt."
    )

    def search(self, query, *, kind="any", mode="keyword", limit=10, filters=None):
        if kind == "case":
            return []
        filters = filters or {}
        q = (query or "").strip().strip("「」『』《》")
        if not q:
            return []
        params = _common_params(filters)
        if mode == "fulltext":
            return self._keyword(q, limit, params)
        hits = self._titles(q, limit, params)
        if not hits:
            hits = self._keyword(q, limit, params)
        return hits

    def _titles(self, q: str, limit: int, params: dict[str, Any]) -> list[Hit]:
        p = dict(params)
        words = [w for w in re.split(r"[\s　]+", q) if w]
        if _LAW_ID.match(q):
            p["law_id"] = q
        elif _LAW_NUM.search(q):
            p["law_num"] = q
        else:
            p["law_title"] = max(words, key=len) if words else q
        p["limit"] = max(30, limit * 3)
        rows = _egov_get("/laws", p).json().get("laws") or []
        if len(words) > 1 and "law_title" in p:
            rows = [r for r in rows if all(w in ((r.get("revision_info") or {}).get("law_title") or "") for w in words)]

        def rank(r: dict[str, Any]) -> tuple:
            rev = r.get("revision_info") or {}
            t = rev.get("law_title") or ""
            repealed = (rev.get("repeal_status") or "None") != "None"
            return (repealed, t != q, not t.startswith(q), len(t))

        rows.sort(key=rank)
        return [self._hit(r) for r in rows[:limit]]

    def _keyword(self, q: str, limit: int, params: dict[str, Any]) -> list[Hit]:
        p = dict(params)
        p.update({"keyword": q, "limit": limit, "sentences_limit": 3, "sentence_text_size": 160})
        try:
            items = _egov_get("/keyword", p).json().get("items") or []
        except ValueError as exc:
            # A query the search-expression parser rejects (stray symbols) — retry it as plain words.
            plain = re.sub(r"[^\w\s　]", " ", q).strip()
            if not plain or plain == q:
                raise
            p["keyword"] = plain
            items = _egov_get("/keyword", p).json().get("items") or []
            if not items:
                raise exc
        hits = []
        for it in items:
            h = self._hit(it)
            sents = it.get("sentences") or []
            h.snippet = " … ".join(_highlight(s.get("text", "")) for s in sents[:3])[:700]
            hits.append(h)
        return hits

    def _hit(self, r: dict[str, Any]) -> Hit:
        info = r.get("law_info") or {}
        rev = r.get("revision_info") or r.get("current_revision_info") or {}
        law_id = info.get("law_id") or ""
        extra = {k: v for k, v in {
            "type": info.get("law_type"), "category": rev.get("category"), "abbrev": rev.get("abbrev"),
            "last_amended": rev.get("amendment_enforcement_date"),
            "scheduled_amendment": rev.get("amendment_scheduled_enforcement_date"),
        }.items() if v}
        return Hit(
            ref=self.ref(law_id), title=rev.get("law_title") or law_id, source=self.id, kind="legislation",
            url=f"{_EGOV_SITE}/law/{law_id}", date=info.get("promulgation_date"), status=_status_of(rev) or None,
            citation=info.get("law_num"), extra=extra,
        )

    def fetch(self, native_id, *, language=None):
        nid = native_id.strip()
        asof = None
        if "@" in nid:
            nid, _, asof = nid.partition("@")
        params: dict[str, Any] = {"response_format": "xml", "law_full_text_format": "xml"}
        if asof:
            params["asof"] = asof
        resp = _egov_get(f"/law_data/{nid}", params, timeout=_LONG)
        root = etree.fromstring(resp.content, parser=etree.XMLParser(huge_tree=True, recover=True))
        info = {c.tag: (c.text or "") for c in (root.find("law_info") if root.find("law_info") is not None else [])}
        rev = {c.tag: (c.text or "") for c in (root.find("revision_info") if root.find("revision_info") is not None else [])}
        law = root.find("law_full_text/Law")
        if law is None:
            law = root.find(".//Law")
        text, secs = ("", [])
        if law is not None:
            text, secs = _EgovRenderer().render(law)
        law_id = info.get("law_id") or nid.split("_")[0]
        title = rev.get("law_title") or _xtext(root.find(".//LawTitle")) or law_id
        meta: dict[str, Any] = {
            "law_id": law_id,
            "citation": f"{title}（{info.get('law_num')}）" if info.get("law_num") else title,
            "law_num": info.get("law_num"),
            "type": info.get("law_type"),
            "category": rev.get("category"),
            "promulgated": info.get("promulgation_date"),
            "version": rev.get("law_revision_id"),
            "effective": rev.get("amendment_enforcement_date") or info.get("promulgation_date"),
            "status": _status_of(rev),
            "last_amended_by": " ".join(x for x in (rev.get("amendment_law_title"), rev.get("amendment_law_num")) if x) or None,
        }
        if asof:
            meta["asof"] = asof
        if rev.get("amendment_scheduled_enforcement_date"):
            meta["scheduled_amendment"] = rev["amendment_scheduled_enforcement_date"]
        try:
            revs = _egov_get(f"/law_revisions/{law_id}", {}).json().get("revisions") or []
            versions = []
            for r in revs[:12]:
                versions.append({k: v for k, v in {
                    "ref": self.ref(r.get("law_revision_id")),
                    "enforced": r.get("amendment_enforcement_date"),
                    "status": _status_of(r),
                    "amended_by": r.get("amendment_law_title"),
                    "this": r.get("law_revision_id") == rev.get("law_revision_id") or None,
                }.items() if v})
            meta["versions"] = versions
            meta["version_count"] = len(revs)
            pending = [r for r in revs if r.get("current_revision_status") == "UnEnforced"]
            if pending:
                meta["not_yet_in_force"] = [
                    f"{self.ref(r.get('law_revision_id'))} (from {r.get('amendment_enforcement_date') or '?'}; "
                    f"{r.get('amendment_law_title') or ''})" for r in pending[:5]
                ]
        except (ValueError, SourceUnavailable, httpx.HTTPError):
            pass
        meta = {k: v for k, v in meta.items() if v not in (None, "", [], {})}
        url = f"{_EGOV_SITE}/law/{law_id}"
        rid = rev.get("law_revision_id") or ""
        if rid and rev.get("current_revision_status") != "CurrentEnforced" and rid.startswith(law_id + "_"):
            url += "/" + rid[len(law_id) + 1:]
        if not text.strip():
            meta["no_text_reason"] = "e-Gov returned no law body for this id"
        return LegalDoc(
            ref=self.ref(native_id.strip()), title=title, source=self.id, text=text, url=url,
            kind="legislation", language="ja", metadata=meta, sections=secs or None,
        )


# ======================================================== Japanese Law Translation

_JLT = "https://www.japaneselawtranslation.go.jp"
_JA_CHARS = re.compile(r"[぀-ヿ㐀-鿿！-～]")
_KANJI_NUM = "〇一二三四五六七八九十百千"
_JA_ARTICLE = re.compile(rf"^第([{_KANJI_NUM}]+)条((?:の[{_KANJI_NUM}]+)*)(?:[\s　]|$)")
_JA_CONTAINER = re.compile(rf"^第[{_KANJI_NUM}]+[編章節款目](?:の[{_KANJI_NUM}]+)*(?:[\s　]|$)")
_JA_CAPTION = re.compile(r"^（[^（）]{1,60}）$")
_JA_SUPPL = re.compile(r"^附[\s　]*則")
_JA_APPDX = re.compile(r"^別表")
_TOC_RANGE = re.compile(r"（第.+?条.*?）$|（附則.*?）$")
_EN_ARTICLE = re.compile(r"^Article\s+(\d+(?:-\d+)*)")


def _is_ja(line: str) -> bool:
    head = line[:40]
    return bool(head) and len(_JA_CHARS.findall(head)) >= max(1, len(head) // 4)


def _kanji(n: str) -> int:
    from ...documents.clauses import parse_number

    return parse_number(n) or 0


class _JltSession:
    """One search: GET the form page (session cookie + CSRF token), POST it back."""

    def __init__(self) -> None:
        self.client = httpx.Client(timeout=httpx.Timeout(30.0, connect=10.0), follow_redirects=True,
                                   headers={"User-Agent": USER_AGENT})

    def __enter__(self) -> "_JltSession":
        return self

    def __exit__(self, *exc: Any) -> None:
        self.client.close()

    def search(self, form: dict[str, str], page: str) -> str:
        last: Optional[Exception] = None
        for _attempt in range(2):
            try:
                r = self.client.get(f"{_JLT}{page}")
                r.raise_for_status()
                m = re.search(r'name="_csrfToken"[^>]*value="([^"]+)"', r.text)
                if not m:
                    raise SourceUnavailable("Japanese Law Translation: no CSRF token on the search page (layout changed?)")
                r = self.client.post(f"{_JLT}/en/laws/result/", data={"_csrfToken": m.group(1), **form})
                r.raise_for_status()
                return r.text
            except httpx.HTTPError as exc:
                last = exc
        raise SourceUnavailable(f"Japanese Law Translation unreachable ({type(last).__name__}: {str(last)[:120]})")


class JltSource(Source):
    id = "jp.jlt"
    probe = {"query": "Civil Aeronautics Act", "kind": "legislation", "ref": "4039", "section": "Article 97", "min_chars": 5000}
    name = "Japanese Law Translation (Ministry of Justice) — English, not official"
    jurisdictions = ("JP",)
    kinds = ("legislation",)
    modes = ("keyword", "fulltext")
    languages = ("en", "ja")
    homepage = _JLT
    default = False
    coverage = (
        "Unofficial English translations of ~1,000 Japanese laws by the MOJ; only the Japanese text has "
        "legal effect and a translation may be many amendments old (each fetch states the amendment it "
        "reflects) — read the current text in jp.egov. Search in English: keyword = title words, fulltext = "
        "words in the translation. Refs are the site's law ids ('4039' = Civil Aeronautics Act). "
        "language=en (default, English only) or ja-en (bilingual, Japanese line then English). "
        "Provisions: 'Article 97', 'Article 71-2', 'Supplementary Provisions'."
    )

    def search(self, query, *, kind="any", mode="keyword", limit=10, filters=None):
        if kind == "case":
            return []
        q = (query or "").strip()
        if not q:
            return []
        if mode == "fulltext":
            form = {"ky": q, "co": "1", "ia": "03"}
            page = "/en/laws/search-ft"
        else:
            form = {"yo": q, "amb": "on", "ia": "03"}
            page = "/en/laws"
        with _JltSession() as s:
            html = s.search(form, page)
            hits = self._parse(html)
            if not hits and mode != "fulltext":
                hits = self._parse(s.search({"ky": q, "co": "1", "ia": "03"}, "/en/laws/search-ft"))
        return hits[:limit]

    def _parse(self, html: str) -> list[Hit]:
        from bs4 import BeautifulSoup

        soup = BeautifulSoup(html, "lxml")
        hits = []
        for li in soup.select("ul.search-result > li"):
            a = li.select_one(".result-title-text a")
            if a is None:
                continue
            m = re.search(r"/en/laws/view/(\d+)", a.get("href") or "")
            if not m:
                continue
            info = [x.get_text(" ", strip=True) for x in li.select(".result-info-item")]
            fields = {}
            for x in info:
                k, _, v = x.partition(":")
                fields[k.strip()] = v.strip()
            past = bool(li.select_one(".result-title-text .material-icons"))
            typ = li.select_one(".result-law-type")
            hits.append(Hit(
                ref=self.ref(m.group(1)), title=a.get_text(" ", strip=True), source=self.id, kind="legislation",
                url=f"{_JLT}/en/laws/view/{m.group(1)}", date=fields.get("Translated Date"),
                status="older translation" if past else "unofficial translation",
                citation=fields.get("Law Number"),
                extra={k: v for k, v in {"type": typ.get_text(strip=True) if typ else None,
                                         "dictionary": fields.get("Dictionary Version")}.items() if v},
            ))
        return hits

    def fetch(self, native_id, *, language=None):
        from bs4 import BeautifulSoup

        lid = native_id.strip().split("/")[0]
        if not lid.isdigit():
            raise ValueError("Japanese Law Translation refs are numeric law ids, e.g. jp.jlt:4039")
        bilingual = (language or "en").lower() in ("ja", "je", "ja-en", "en-ja", "bilingual", "jp")
        view_url = f"{_JLT}/en/laws/view/{lid}"
        try:
            view = request_with_retry("GET", view_url, headers={"User-Agent": USER_AGENT}, timeout=_LONG)
        except httpx.HTTPStatusError as exc:
            if exc.response.status_code == 404:
                raise ValueError(f"no Japanese Law Translation law with id {lid}") from exc
            raise
        soup = BeautifulSoup(view.text, "lxml")
        info = soup.select_one("#lawInfo")
        titles = [d.get_text(" ", strip=True) for d in (info.select(".title") if info else [])]
        versions = [d.get_text(" ", strip=True) for d in (info.select(".last-version") if info else [])]
        link = None
        for a in soup.select('a[href*="/en/laws/download/"]'):
            if re.search(r"/download/\d+/13/", a.get("href", "")) or a.get("href", "").endswith(".txt"):
                link = a["href"]
                break
        en_title = next((t for t in titles if not _is_ja(t)), titles[0] if titles else lid)
        ja_title = next((t for t in titles if _is_ja(t)), "")
        last_en = next((v for v in versions if "Last Version" in v), "")
        last_ja = next((v for v in versions if "最終更新" in v), "")
        meta: dict[str, Any] = {
            "official": False,
            "japanese_title": ja_title or None,
            "reflects_amendments_up_to": re.sub(r"^Last Version[：:]\s*", "", last_en) or None,
            "reflects_amendments_up_to_ja": re.sub(r"^最終更新[：:]\s*", "", last_ja) or None,
            "note": ("Unofficial translation by the Ministry of Justice; only the original Japanese text has legal "
                     "effect. Compare with the current official text in jp.egov (search the Japanese title)."),
        }
        if not link:
            meta["no_text_reason"] = "the law page offers no text download (it may be an outline or untranslated)"
            return LegalDoc(ref=self.ref(lid), title=en_title, source=self.id, text="", url=view_url,
                            language="en", metadata={k: v for k, v in meta.items() if v not in (None, "")})
        txt = request_with_retry("GET", _JLT + link, headers={"User-Agent": USER_AGENT}, timeout=_LONG)
        raw = txt.content.decode("utf-8-sig", errors="replace")
        text, secs = _jlt_text(raw, bilingual=bilingual)
        meta["text_format"] = "bilingual TXT" if bilingual else "English lines of the bilingual TXT"
        return LegalDoc(
            ref=self.ref(lid), title=re.sub(r"（[^）]*）$", "", en_title).strip() or en_title, source=self.id,
            text=text, url=view_url, kind="legislation", language="ja-en" if bilingual else "en",
            metadata={k: v for k, v in meta.items() if v not in (None, "")}, sections=secs or None,
            ttl=7 * 24 * 3600,
        )


def _jlt_text(raw: str, *, bilingual: bool) -> tuple[str, list[dict[str, Any]]]:
    """Bilingual TXT (a Japanese line, then its English) → text and provisions.

    The section index cannot read this file on its own: English "Section 1"
    is a 節 (a container) and every provision appears twice, once per
    language. The Japanese line classifies each block. In English-only text
    the caption joins its article's first line ("Article 97 (Flight Plan and
    Approval Thereof)"), the way EUR-Lex prints a titled article, so the
    outline can show it.
    """
    lines = [ln.strip() for ln in normalize_lines(raw).split("\n")]
    blocks: list[tuple[str, list[str]]] = []
    for ln in lines:
        if not ln:
            continue
        if _is_ja(ln) or not blocks:
            blocks.append((ln, []))
        else:
            blocks[-1][1].append(ln)
    w = _Writer()
    caption: Optional[tuple[str, list[str]]] = None
    in_toc = False
    suppl_n = 0
    used_keys: set[str] = set()

    def emit(out: list[str]) -> Optional[int]:
        start = None
        for ln in out:
            s = w.line(ln)
            start = s if start is None else start
        return start

    def flush_caption() -> Optional[int]:
        nonlocal caption
        if caption is None:
            return None
        ja_c, en_c = caption
        caption = None
        return emit(([ja_c] + en_c) if bilingual or not en_c else en_c)

    for ja, en in blocks:
        if ja in ("目次",):
            in_toc = True
            continue
        if in_toc:
            if (_JA_CONTAINER.match(ja) or ja.startswith(("附則", "附　則"))) and _TOC_RANGE.search(ja):
                continue
            in_toc = False
        if _JA_CAPTION.match(ja):
            flush_caption()
            caption = (ja, en)
            continue
        m = _JA_ARTICLE.match(ja)
        if m:
            num = str(_kanji(m.group(1)))
            subs = [str(_kanji(x)) for x in re.findall(rf"の([{_KANJI_NUM}]+)", m.group(2) or "")]
            en_i = next((i for i, x in enumerate(en) if _EN_ARTICLE.match(x)), None)
            if bilingual or not en:
                start = flush_caption()
                s2 = emit([ja] + en)
                start = s2 if start is None else start
                label = ja.split("　")[0].split(" ")[0]
                key = ".".join([num] + subs)
            else:
                dashed = "-".join([num] + subs)
                number = _EN_ARTICLE.match(en[en_i]).group(1) if en_i is not None else dashed
                label = f"Article {number}"
                cap = " ".join(caption[1]) if caption and caption[1] else ""
                caption = None
                body = list(en)
                if en_i is not None:
                    body[en_i] = en[en_i][_EN_ARTICLE.match(en[en_i]).end():].strip()
                start = emit([f"{label} {cap}".strip()] + [b for b in body if b])
                key = number
            if start is not None:
                w.mark(start, label, key, label, "article")
            continue
        start = flush_caption()
        s2 = emit(([ja] + en) if bilingual or not en else en)
        start = s2 if start is None else start
        if start is None:
            continue
        if _JA_CONTAINER.match(ja):
            label = (en[0] if en and not bilingual else ja)[:120]
            w.mark(start, label, "", label, "container")
        elif _JA_SUPPL.match(ja):
            suppl_n += 1
            label = "Supplementary Provisions" if not bilingual else "附則"
            if suppl_n == 1:
                w.mark(start, label, "", label, "container")
            # A request reduces at its first space ("Supplementary Provisions" → "Supplementary"),
            # so the key is what the label reduces to; later ones get a distinct, spaceless key.
            shown = label if suppl_n == 1 else f"{label} ({suppl_n})"
            key = re.split(r"\s", label)[0] if suppl_n == 1 else f"{label}{suppl_n}".replace(" ", "")
            w.mark(start, shown, key, (en[0] if en else ja)[:120], "article")
        elif _JA_APPDX.match(ja):
            label = re.split(r"[（(　 ]", ja, maxsplit=1)[0]
            if not bilingual and en:
                label = re.split(r"\s*\(", en[0], maxsplit=1)[0][:60]
            key = request_key(label) or label
            if key in used_keys:  # "Appended Table 2" reduces to "Appended" too
                key = label.replace(" ", "")
            used_keys.add(key)
            w.mark(start, label, key, (en[0] if en else ja)[:120], "article")
    flush_caption()
    return w.result()


SOURCES = [EgovSource(), JltSource()]
