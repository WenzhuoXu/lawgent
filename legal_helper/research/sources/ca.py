"""Canada: Justice Laws (federal Acts and regulations), the Lexum decision
sites of the Supreme Court, Federal Court of Appeal and Federal Court, and
BC Laws (British Columbia).

ca.justice — laws-lois.justice.gc.ca, the official consolidation, English and
French (equally authoritative). Fetch reads the consolidated XML
(``/eng/XML/A-2.xml``, ``/fra/XML/DORS-2019-150.xml``) and builds the
section index from its ``Section``/``Label`` elements. Title search runs over
the site's own index of every Act and regulation (``Legis.xml``, 5.3 MB),
reduced to a ~1 MB JSON under ``state/legal_docs/index/ca.justice/`` on first
use and refreshed weekly (one ~5 s download); full-text search is the site's
Search.aspx, whose hits are sections.

ca.lexum — decisions.scc-csc.ca and decisions.fca-caf.gc.ca (one Lexum
instance serves the FCA and the FC), searched with ``iframe=true``; a decision
is read from its page, numbered paragraphs ``[12]``.

ca.bclaws — BC Laws CiviX API. The King's Printer publishes it for third-party
applications (github.com/bcgov/bc-laws-api), but www.bclaws.gov.bc.ca's
robots.txt disallows every agent except Googlebot and Bingbot, so the source
stays off until ``BCLAWS_API_ACCEPTED`` is set.
"""

from __future__ import annotations

import html as _html
import json
import re
import threading
import time
from concurrent.futures import ThreadPoolExecutor
from pathlib import Path
from typing import Any, Optional
from urllib.parse import quote_plus, urljoin

import httpx
from lxml import etree

from ...connectors.base import USER_AGENT, request_with_retry
from ..base import Hit, LegalDoc, Source, SourceUnavailable
from ..textify import html_to_text

_LONG = httpx.Timeout(60.0, connect=10.0)
_HEADERS = {"User-Agent": USER_AGENT}


def _ws(s: str) -> str:
    return " ".join(_html.unescape(s or "").replace("\xa0", " ").replace("\u2002", " ").split())


def _get(url: str, *, params: Optional[dict[str, Any]] = None, what: str = "", timeout: Optional[httpx.Timeout] = None,
         accept: str = "*/*") -> httpx.Response:
    try:
        return request_with_retry("GET", url, params=params, headers={**_HEADERS, "Accept": accept},
                                  timeout=timeout or httpx.Timeout(30.0, connect=10.0))
    except httpx.HTTPStatusError as exc:
        if exc.response.status_code == 404:
            raise
        raise SourceUnavailable(f"{what or url} answered {exc.response.status_code}; try again shortly") from exc
    except httpx.TransportError as exc:
        raise SourceUnavailable(f"{what or url} unreachable ({type(exc).__name__})") from exc


def _state_dir() -> Path:
    from ...config import load_settings

    return Path(load_settings().state_dir)


# ================================================================ structure

class _Lines:
    def __init__(self) -> None:
        self.lines: list[str] = []
        self.pos = 0
        self.prefix = ""
        self.marks: list[tuple[int, str, str, str, str, str]] = []

    def line(self, s: str) -> None:
        s = _ws(s)
        if not s:
            return
        if self.prefix:
            s = f"{self.prefix} {s}"
            self.prefix = ""
        self.lines.append(s)
        self.pos += len(s) + 1

    def flush(self) -> None:
        if self.prefix:
            p, self.prefix = self.prefix, ""
            self.line(p)

    def mark(self, label: str, key: str, heading: str, level: str, caption: str = "") -> None:
        self.flush()
        self.marks.append((self.pos, label, key, heading[:160], level, caption))

    def text(self) -> str:
        return "\n".join(self.lines)

    def sections(self) -> list[dict[str, Any]]:
        total = len(self.text())
        out = []
        for i, (start, label, key, heading, level, caption) in enumerate(self.marks):
            end = self.marks[i + 1][0] if i + 1 < len(self.marks) else total
            if end <= start and level != "container":
                continue
            d = {"label": label, "key": key, "heading": heading, "start": start, "end": max(start, end), "level": level}
            if caption:
                d["caption"] = caption
            out.append(d)
        return out


_TREATY = re.compile(r"^Articles?\s+(\d+[A-Za-z]{0,2}|[IVXLC]+)\b\s*(?:[—–-]\s*(.*))?$")


# ============================================================ Justice Laws

_JL = "https://laws-lois.justice.gc.ca"
_JL_INLINE = {
    "XRefExternal", "XRefInternal", "DefinedTermEn", "DefinedTermFr", "Emphasis", "Language", "Sup", "Sub",
    "Leader", "LeaderRightJustified", "DefinitionRef", "CommentInline", "Repealed", "Date", "YYYY", "MM", "DD",
    "FormulaText", "AmendedText", "Ins", "Del", "Fraction", "Numerator", "Denominator", "BilingualGroup",
    "BilingualItemEn", "BilingualItemFr", "InlineFont", "AlphaText", "DefinitionEnOnly", "DefinitionFrOnly",
}
_JL_SKIP = {"FootnoteRef", "Footnote", "MarginalNote", "Label", "RecentAmendments", "ImageGroup", "Image",
            "RunningHead", "BillHistory", "colspec", "LineBreak"}
_JL_NUMBERED = {"Subsection", "Paragraph", "Subparagraph", "Clause", "Subclause", "Subsubclause", "Item",
                "Definition", "FormulaParagraph"}
_ROMAN = re.compile(r"^[IVXLC]+$")


def _jl_heading(el: Any) -> str:
    """A heading's parts (Label, TitleText, OriginatingRef) joined with spaces."""
    if el is None:
        return ""
    parts = [_jl_inline(c) for c in el if isinstance(c.tag, str) and c.tag not in ("FootnoteRef", "Footnote")]
    return _ws(" ".join(p for p in parts if _ws(p))) or _ws(_jl_inline(el))


def _jl_inline(el: Any) -> str:
    parts: list[str] = []

    def rec(e: Any) -> None:
        if not isinstance(e.tag, str):
            return
        name = e.tag
        if name in ("FootnoteRef", "Footnote", "ImageGroup", "Image"):
            return
        if name == "LineBreak":
            parts.append(" ")
            return
        if e.text:
            parts.append(e.text)
        for c in e:
            rec(c)
            if c.tail:
                parts.append(c.tail)

    rec(el)
    return re.sub(r"\s+", " ", "".join(parts))


class _JlRenderer:
    def __init__(self, *, prefix: str, qualify: bool) -> None:
        self.b = _Lines()
        self.prefix = prefix
        self.qualify = qualify
        self.schedule: Optional[str] = None
        self.in_unit = 0

    def block(self, el: Any) -> None:
        if not isinstance(el.tag, str):
            return
        name = el.tag
        if name in _JL_SKIP:
            return
        if name == "Section":
            self._section(el)
        elif name in _JL_NUMBERED:
            self._numbered(el)
        elif name == "Heading":
            text = _ws(" ".join(_jl_inline(c) for c in el if c.tag in ("Label", "TitleText")))
            if text:
                if self.in_unit:
                    self.b.line(text)
                else:
                    self.b.mark(text, "", text, "container")
                    self.b.line(text)
        elif name == "Schedule":
            self._schedule(el)
        elif name == "Group":
            self._group(el)
        elif name in ("TableGroup", "table"):
            for row in el.iter("row"):
                cells = [_ws(_jl_inline(e)) for e in row if e.tag == "entry"]
                line = " | ".join(c for c in cells if c)
                if line:
                    self.b.line(line)
        elif name == "HistoricalNote":
            items = [_ws(_jl_inline(i)) for i in el if i.tag == "HistoricalNoteSubItem"]
            items = [i for i in items if i]
            if items:
                self.b.line("[" + "; ".join(items) + "]")
        elif name in ("Text", "TitleText", "GroupHeading", "ScheduleFormHeading", "Provision") and not any(
            isinstance(c.tag, str) and c.tag not in _JL_INLINE and c.tag not in _JL_SKIP for c in el
        ):
            self.b.line(_jl_inline(el))
        else:
            self.flow(el)

    def flow(self, el: Any) -> None:
        buf = [el.text or ""]
        for c in el:
            if not isinstance(c.tag, str):
                buf.append(c.tail or "")
                continue
            if c.tag in _JL_INLINE:
                buf.append(_jl_inline(c))
                buf.append(c.tail or "")
                continue
            if c.tag in ("FootnoteRef", "LineBreak"):
                buf.append(" " if c.tag == "LineBreak" else "")
                buf.append(c.tail or "")
                continue
            self.b.line("".join(buf))
            buf = []
            self.block(c)
            buf.append(c.tail or "")
        self.b.line("".join(buf))

    def _label(self, el: Any) -> str:
        lab = el.find("Label")
        return _ws(_jl_inline(lab)) if lab is not None else ""

    def _numbered(self, el: Any) -> None:
        lab = self._label(el)
        note = el.find("MarginalNote")
        if note is not None and _ws(_jl_inline(note)):
            self.b.flush()
            self.b.line(_jl_inline(note))
        self.b.prefix = f"{self.b.prefix} {lab}".strip() if lab else self.b.prefix
        self.flow(el)
        self.b.flush()

    def _section(self, el: Any) -> None:
        lab = self._label(el)
        note = el.find("MarginalNote")
        title = _ws(_jl_inline(note)) if note is not None else ""
        if not lab or self.in_unit:
            if title:
                self.b.line(title)
            self.b.prefix = lab
            self.flow(el)
            self.b.flush()
            return
        if self.schedule is not None:
            label = f"Sch{self.schedule}.{lab}"
        else:
            label = f"{self.prefix} {lab}"
        heading = f"{label} {title}".strip()
        if el.get("in-force") == "no":
            heading += " [not in force]"
        self.b.mark(label, (lab if self.schedule is None else label).upper(), heading, "article")
        self.b.line(heading)
        self.in_unit += 1
        try:
            self.flow(el)
        finally:
            self.in_unit -= 1

    def _has_units(self, el: Any) -> bool:
        if el.find(".//Section/Label") is not None:
            return True
        for gh in el.iter("GroupHeading"):
            if _TREATY.match(_jl_heading(gh)):
                return True
        return False

    def _schedule(self, el: Any) -> None:
        head = el.find("ScheduleFormHeading")
        heading = _jl_heading(head) if head is not None else "SCHEDULE"
        lab = _ws(_jl_inline(head.find("Label"))) if head is not None and head.find("Label") is not None else ""
        m = re.match(r"^(?:SCHEDULE|ANNEXE)\s*([\w.-]*)", lab, re.I)
        num = (m.group(1) if m else "") or "1"
        saved = self.schedule
        self.schedule = num
        try:
            if not self._has_units(el):
                label = f"Sch{num}"
                self.b.mark(label, label.upper(), f"{label} {heading}", "article")
                self.b.line(f"{label} {heading}")
                self.in_unit += 1
                try:
                    for c in el:
                        if c is not head:
                            self.block(c)
                finally:
                    self.in_unit -= 1
                return
            self.b.mark(heading, "", heading, "container")
            self.b.line(heading)
            for c in el:
                if c is not head:
                    self.block(c)
        finally:
            self.schedule = saved

    def _group(self, el: Any) -> None:
        gh = el.find("GroupHeading")
        heading = _jl_heading(gh) if gh is not None else ""
        m = _TREATY.match(heading) if self.schedule is not None and not self.in_unit else None
        if m:
            num = m.group(1)
            title = f"Article {num} {m.group(2) or ''}".strip()
            caption = (m.group(2) or "").strip()
            if self.qualify:
                # printed label, schedule-qualified key (see _render_jl)
                qid = f"Sch{self.schedule}.Art{num}"
                self.b.mark(f"Article {num}", qid.upper(), title, "article", f"{caption} [{qid}]".strip())
                self.b.line(f"{title} [{qid}]")
            else:
                self.b.mark(f"Article {num}", num.upper(), title, "article", caption)
                self.b.line(title)
            self.in_unit += 1
            try:
                for c in el:
                    if c is not gh:
                        self.block(c)
            finally:
                self.in_unit -= 1
            return
        if heading:
            if self.in_unit:
                self.b.line(heading)
            else:
                self.b.mark(heading, "", heading, "container")
                self.b.line(heading)
        for c in el:
            if c is not gh:
                self.block(c)


def _render_jl(root: Any, *, lang: str) -> tuple[str, list[dict[str, Any]]]:
    body = root.find("Body")
    qualify = body is not None and body.find(".//Section/Label") is not None
    r = _JlRenderer(prefix="art" if lang == "fr" else "s", qualify=qualify)
    ident = root.find("Identification")
    if ident is not None:
        for tag in ("ShortTitle", "LongTitle", "InstrumentNumber"):
            e = ident.find(tag)
            if e is not None:
                r.b.line(_jl_inline(e))
        ch = ident.find("Chapter")
        if ch is not None:
            r.b.line(_ws(" ".join(ch.itertext())))
    for child in root:
        if child.tag in ("Identification", "RecentAmendments"):
            continue
        r.block(child)
    text = r.b.text()
    return text, r.b.sections()


def _jl_date(e: Any) -> Optional[str]:
    if e is None:
        return None
    y, m, d = (e.findtext(".//YYYY"), e.findtext(".//MM"), e.findtext(".//DD"))
    if y and m and d:
        return f"{int(y):04d}-{int(m):02d}-{int(d):02d}"
    return None


_LIMS = "{http://justice.gc.ca/lims}"


def _jl_meta(root: Any) -> dict[str, Any]:
    ident = root.find("Identification")
    meta: dict[str, Any] = {}
    meta["in_force_from"] = root.get(f"{_LIMS}inforce-start-date")
    meta["last_amended"] = root.get(f"{_LIMS}lastAmendedDate")
    meta["version"] = root.get(f"{_LIMS}pit-date")
    if root.get("in-force") == "no":
        meta["status"] = "not in force"
    if ident is not None:
        meta["registered"] = _jl_date(ident.find("RegistrationDate"))
        meta["consolidated"] = _jl_date(ident.find("ConsolidationDate"))
        ea = ident.find("EnablingAuthority")
        if ea is not None:
            v = _ws(_jl_inline(ea))
            meta["enabling_act"] = (v.title() if root.get("{http://www.w3.org/XML/1998/namespace}lang") != "fr"
                                    else v.capitalize()) if v.isupper() else v
        num = ident.findtext("InstrumentNumber") or ident.findtext("Chapter/ConsolidatedNumber")
        if num:
            meta["citation"] = _ws(num)
    am = [f"{a.findtext('AmendmentCitation')} ({a.findtext('AmendmentDate')})" for a in root.findall("RecentAmendments/Amendment")]
    if am:
        meta["recent_amendments"] = am[:10]
    return {k: v for k, v in meta.items() if v}


_CITE_IDS = [
    (re.compile(r"\b(SOR|SI|DORS|TR)\s*/\s*(\d{2,4})\s*-\s*(\d+)\b", re.I), lambda m: f"{m.group(1).upper()}-{m.group(2)}-{m.group(3)}"),
    (re.compile(r"\bC\.?R\.?C\.?,?\s*c(?:h)?\.?\s*(\d+)\b", re.I), lambda m: f"C.R.C.,_c._{m.group(1)}"),
    (re.compile(r"\bR\.?S\.?C\.?,?\s*1985,?\s*c\.?\s*([A-Z]-[\d.]+)", re.I), lambda m: m.group(1).upper()),
    (re.compile(r"^\s*([A-Z]-\d+(?:\.\d+)?)\s*$"), lambda m: m.group(1)),
    (re.compile(r"^\s*((?:SOR|SI|DORS|TR)-\d{2,4}-\d+|C\.R\.C\.,_c(?:h)?\._\d+)\s*$", re.I), lambda m: m.group(1)),
]


def _fr_id(uid: str) -> str:
    u = re.sub(r"^SOR-", "DORS-", uid)
    u = re.sub(r"^SI-", "TR-", u)
    u = u.replace("C.R.C.,_c._", "C.R.C.,_ch._")
    return u


def _en_id(uid: str) -> str:
    u = re.sub(r"^DORS-", "SOR-", uid)
    u = re.sub(r"^TR-", "SI-", u)
    return u.replace("C.R.C.,_ch._", "C.R.C.,_c._")


_INDEX_LOCK = threading.Lock()
_INDEX_MEM: dict[str, Any] = {}
_INDEX_TTL = 7 * 24 * 3600


def _jl_index() -> list[dict[str, Any]]:
    """Every Act and regulation (both languages): id, title, kind, number,
    current-to date. Built from Legis.xml on first use, refreshed weekly."""
    with _INDEX_LOCK:
        now = time.time()
        if _INDEX_MEM.get("rows") and now - _INDEX_MEM.get("at", 0) < _INDEX_TTL:
            return _INDEX_MEM["rows"]
        path = _state_dir() / "legal_docs" / "index" / "ca.justice" / "legis.json"
        if path.exists() and now - path.stat().st_mtime < _INDEX_TTL:
            try:
                rows = json.loads(path.read_text(encoding="utf-8"))
                _INDEX_MEM.update(rows=rows, at=path.stat().st_mtime)
                return rows
            except (OSError, ValueError):
                pass
        try:
            r = _get(f"{_JL}/eng/XML/Legis.xml", what="Justice Laws index", timeout=_LONG)
        except (httpx.HTTPStatusError, SourceUnavailable):
            if path.exists():  # stale beats nothing
                rows = json.loads(path.read_text(encoding="utf-8"))
                _INDEX_MEM.update(rows=rows, at=now)
                return rows
            raise
        root = etree.fromstring(r.content, parser=etree.XMLParser(huge_tree=True))
        rows = []
        for kind, tag in (("act", "Act"), ("regulation", "Regulation")):
            for e in root.iter(tag):
                rows.append({
                    "id": _ws(e.findtext("UniqueId") or ""), "lang": (e.findtext("Language") or "")[:3],
                    "title": _ws(e.findtext("Title") or ""), "kind": kind,
                    "number": _ws(e.findtext("OfficialNumber") or "") or None,
                    "current": e.findtext("CurrentToDate"),
                })
        rows = [x for x in rows if x["id"] and x["title"]]
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(json.dumps(rows, ensure_ascii=False), encoding="utf-8")
        _INDEX_MEM.update(rows=rows, at=now)
        return rows


def _fold(s: str) -> str:
    import unicodedata

    s = unicodedata.normalize("NFKD", s.lower().replace("’", "'"))
    return "".join(c for c in s if not unicodedata.combining(c))


_FR_HINT = re.compile(r"[àâçéèêëîïôûùüÿœ]|\b(loi|règlement|reglement|sur|les|des|du|aérien|passagers)\b", re.I)
_STOP = {"the", "of", "and", "act", "regulations", "regulation", "canada", "canadian", "loi", "sur", "la", "le",
         "les", "des", "de", "du", "reglement", "d", "l", "a", "an", "to", "for", "in"}


class JusticeLawsSource(Source):
    id = "ca.justice"
    probe = {"query": "Air Passenger Protection Regulations", "kind": "legislation", "ref": "SOR-2019-150",
             "section": "19", "min_chars": 20000}
    name = "Justice Laws Website (Department of Justice Canada)"
    jurisdictions = ("CA",)
    kinds = ("legislation",)
    modes = ("keyword", "fulltext")
    languages = ("en", "fr")
    homepage = _JL
    coverage = (
        "Official consolidated federal Acts and regulations, English and French (both authoritative; "
        "language=fr). Ids are Justice Laws ids: 'A-2' (Aeronautics Act), 'C-26' (Carriage by Air Act), "
        "'SOR-2019-150' (Air Passenger Protection Regulations), 'SOR-96-433' (Canadian Aviation Regulations), "
        "'C.R.C.,_c._1038'; citations 'SOR/2019-150', 'R.S.C. 1985, c. A-2' resolve directly. Sections 's 19' "
        "('art 19' in French), a scheduled convention's 'Article 17' (another schedule's 'SchVI.Art17', or one "
        "schedule alone 'C-26/SchVI'). keyword = titles (English or French); fulltext = "
        "the site's search, hits name the sections. filters: type (act|regulation)."
    )

    def search(self, query, *, kind="any", mode="keyword", limit=10, filters=None):
        if kind not in ("any", "legislation"):
            return []
        filters = filters or {}
        limit = max(1, min(int(limit or 10), 30))
        typ = str(filters.get("type") or "").lower().rstrip("s")
        lang = "fra" if (str(filters.get("language") or "").lower().startswith("fr") or _FR_HINT.search(query)) else "eng"
        if mode == "fulltext":
            return self._fulltext(query, limit, typ, lang)
        rows = _jl_index()
        by_id = {}
        for rrow in rows:
            by_id.setdefault((rrow["id"], rrow["lang"]), rrow)
        hits: list[Hit] = []
        for pat, fn in _CITE_IDS:
            m = pat.search(query)
            if m:
                uid = fn(m)
                for cand in (uid, _en_id(uid), _fr_id(uid)):
                    row = by_id.get((cand, "eng")) or by_id.get((cand, "fra"))
                    if row:
                        hits.append(self._hit(row, how="citation"))
                        break
                if hits:
                    return hits
        q = _fold(query)
        words = [w for w in re.findall(r"[a-z0-9]+", q) if w not in _STOP]
        if not words:
            words = re.findall(r"[a-z0-9]+", q)
        if not words:
            return []
        nq = " ".join(re.findall(r"[a-z0-9]+", q))
        scored = []
        for row in rows:
            if row["lang"] != lang:
                continue
            if typ in ("act", "regulation") and row["kind"] != typ:
                continue
            t = _fold(row["title"])
            tw = set(re.findall(r"[a-z0-9]+", t))
            have = sum(1 for w in words if w in tw or any(x.startswith(w) for x in tw if len(w) >= 4))
            if have < len(words):
                continue
            tn = " ".join(re.findall(r"[a-z0-9]+", t))
            exact = 0 if tn == nq else (1 if tn.startswith(nq) else 2)
            scored.append(((exact, 0 if row["kind"] == "act" else 1, len(tn)), row))
        scored.sort(key=lambda x: x[0])
        hits = [self._hit(row, how="title") for _, row in scored[:limit]]
        if not hits:
            return self._fulltext(query, limit, typ, lang)
        return hits

    def _hit(self, row: dict[str, Any], *, how: str, snippet: str = "", sections: Optional[list[str]] = None) -> Hit:
        uid = row["id"]
        native = _en_id(uid)
        fr = row["lang"] == "fra"
        path = ("fra/lois" if fr else "eng/acts") if row["kind"] == "act" else ("fra/reglements" if fr else "eng/regulations")
        extra: dict[str, Any] = {"type": row["kind"], "matched": how}
        if fr:
            extra["language"] = "fr — fetch with language='fr' for the French text"
        if sections:
            extra["sections"] = sections
        cite = row.get("number")
        if row["kind"] == "regulation":
            cite = uid.replace("-", "/", 1) if uid.startswith(("SOR", "SI", "DORS", "TR")) else uid.replace("_", " ")
        elif cite and not cite.startswith(("R.S", "S.C", "L.C", "L.R")) and re.match(r"^\d{4}, c", cite):
            cite = f"S.C. {cite}"
        elif cite and re.match(r"^[A-Z]-[\d.]+$", cite):
            cite = f"R.S.C. 1985, c. {cite}"
        return Hit(
            ref=self.ref(native), title=row["title"], source=self.id, kind="legislation",
            url=f"{_JL}/{path}/{uid}/index.html", date=row.get("current"), status="in force (consolidated)",
            citation=cite, snippet=snippet[:500], extra=extra,
        )

    def _fulltext(self, query: str, limit: int, typ: str, lang: str) -> list[Hit]:
        content = {"act": "Acts", "regulation": "Regulations"}.get(typ, "")
        base = f"{_JL}/Recherche/Recherche.aspx" if lang == "fra" else f"{_JL}/Search/Search.aspx"
        url = f"{base}?txtS3archA11={quote_plus(query)}&txtT1tl=&h1ts0n1y=0&ddC0nt3ntTyp3={content}"
        docs: dict[str, dict[str, Any]] = {}
        order: list[str] = []
        pages = 0
        while url and pages < 4 and len(order) < limit:
            html = _get(url, what="Justice Laws search").text
            pages += 1
            for m in re.finditer(r'<li class="resultType\d">(.*?)</li>\s*(?=<li class="resultType|</ol>)', html, re.S):
                item = m.group(1)
                a = re.search(r'<a class="hitTitleLink" href="([^"]+)"[^>]*>(.*?)</a>', item, re.S)
                if not a:
                    continue
                href, label = a.group(1), _ws(re.sub(r"<[^>]+>", "", a.group(2)))
                pm = re.search(r"/(eng|fra)/(acts|lois|regulations|reglements)/([^/]+)/section-([^.?]+)\.html", href)
                if not pm:
                    pm2 = re.search(r"/(eng|fra)/(acts|lois|regulations|reglements)/([^/?#]+)", href)
                    if not pm2:
                        continue
                    uid, sec = pm2.group(3), None
                    kind = "act" if pm2.group(2) in ("acts", "lois") else "regulation"
                else:
                    uid, sec = pm.group(3), pm.group(4)
                    kind = "act" if pm.group(2) in ("acts", "lois") else "regulation"
                title = re.sub(r"\s*-\s*[^-]*\((?:Section|Article|SCHEDULE|ANNEXE)[^)]*\)\s*$", "", label)
                title = re.sub(r"\s+-\s+(?:SOR|DORS|SI|TR|R\.S\.C|L\.R\.C|S\.C|L\.C|C\.R\.C)[^-]*$", "", title).strip()
                snip = _ws(html_to_text(re.sub(r"\[\.\.\.\]", " … ", item.split("</a>", 1)[-1])))[:400]
                key = _en_id(uid)
                if key not in docs:
                    docs[key] = {"row": {"id": uid, "lang": lang, "title": title or uid, "kind": kind}, "sections": [],
                                 "snippet": snip}
                    order.append(key)
                if sec and not sec.startswith("sched"):
                    docs[key]["sections"].append(sec)
                elif sec:
                    docs[key]["sections"].append("schedule")
            nxt = re.search(r"id=\"divPageNav\">.*?<a href='([^']+)'", html, re.S)
            url = urljoin(base, nxt.group(1).replace("&amp;", "&")) if nxt else ""
        return [self._hit(docs[k]["row"], how="text", snippet=docs[k]["snippet"], sections=docs[k]["sections"][:12])
                for k in order[:limit]]

    def fetch(self, native_id, *, language=None):
        uid = native_id.strip().strip("/")
        sched = None
        sm = re.match(r"^(.+?)/Sch(?:edule)?\s*([\w.-]+)$", uid, re.I)
        if sm:
            uid, sched = sm.group(1), sm.group(2)
        for pat, fn in _CITE_IDS:
            m = pat.search(uid)
            if m:
                uid = fn(m)
                break
        fr = (language or "").lower().startswith("fr")
        lid = _fr_id(uid) if fr else _en_id(uid)
        url = f"{_JL}/{'fra' if fr else 'eng'}/XML/{lid}.xml"
        try:
            # the server answers 406 to "Accept: application/xml" (it serves text/xml)
            r = _get(url, what="Justice Laws", timeout=_LONG)
        except httpx.HTTPStatusError as exc:
            raise ValueError(f"Justice Laws has no XML for {lid!r} (ids look like 'A-2', 'SOR-2019-150')") from exc
        root = etree.fromstring(r.content, parser=etree.XMLParser(huge_tree=True, resolve_entities=False))
        lang = "fr" if fr else "en"
        meta = _jl_meta(root)
        if sched:
            # one schedule on its own, so a scheduled convention's articles are "Article 17"
            part = etree.Element(root.tag, root.attrib)
            if root.find("Identification") is not None:
                part.append(root.find("Identification"))
            found = None
            for sc in root.findall("Schedule"):
                lab = _ws(_jl_inline(sc.find("ScheduleFormHeading/Label"))) if sc.find("ScheduleFormHeading/Label") is not None else ""
                m = re.match(r"^(?:SCHEDULE|ANNEXE)\s*([\w.-]*)", lab, re.I)
                if ((m.group(1) if m else "") or "1").upper() == sched.upper():
                    found = sc
                    break
            if found is None:
                raise ValueError(f"no Schedule {sched} in {lid}")
            part.append(found)
            root = part
        text, sections = _render_jl(root, lang=lang)
        treaty = list(dict.fromkeys(m.group(1) for s_ in sections
                                    for m in [re.match(r"^SCH([\w.-]+)\.ART", s_.get("key") or "")] if m))
        if treaty:
            meta["scheduled_conventions"] = (
                "The convention article labels ('Article 17') repeat across the schedules and section= returns the "
                f"first (Schedule {treaty[0]}); another schedule's article is asked for by its bracketed id "
                "('SchVI.Art17'), or read one schedule on its own: "
                + ", ".join(f"ca.justice:{_en_id(uid)}/Sch{x}" for x in treaty[:6]) + "."
            )
        ident = root.find("Identification")
        title = ""
        if ident is not None:
            title = _ws(_jl_inline(ident.find("ShortTitle"))) if ident.find("ShortTitle") is not None else ""
            if not title and ident.find("LongTitle") is not None:
                title = _ws(_jl_inline(ident.find("LongTitle")))
        try:
            row = next((x for x in _jl_index() if x["id"] == lid), None)
        except Exception:  # noqa: BLE001 — the index only adds the current-to date
            row = None
        if row and row.get("current"):
            meta["current_to"] = row["current"]
        meta.setdefault("status", "in force (consolidated)" if root.get("in-force") != "no" else "not in force")
        meta["note"] = ("The French and English versions are equally authoritative; the other one is "
                        "language='fr'/'en'. Point-in-time versions: see the PITIndex on the website.")
        is_act = root.tag == "Statute"
        path = ("fra/lois" if fr else "eng/acts") if is_act else ("fra/reglements" if fr else "eng/regulations")
        if not text.strip():
            meta["no_text_reason"] = "the XML carried no text"
        if sched:
            title = f"{title or lid} — Schedule {sched}"
        return LegalDoc(
            ref=self.ref(_en_id(uid) + (f"/Sch{sched}" if sched else "")), title=title or lid, source=self.id, text=text,
            url=f"{_JL}/{path}/{lid}/index.html", kind="legislation", language=lang, metadata=meta,
            sections=sections or None,
        )


# =================================================================== Lexum

_LEXUM_HOSTS = {
    "scc-csc": "https://decisions.scc-csc.ca",
    "fca-caf": "https://decisions.fca-caf.gc.ca",
    "fc-cf": "https://decisions.fct-cf.gc.ca",
}
#: search endpoint per instance; the FCA instance also returns FC (and Tax Court) decisions
_LEXUM_SEARCH = {
    "scc": "https://decisions.scc-csc.ca/scc-csc/{lang}/d/s/index.do",
    "fed": "https://decisions.fca-caf.gc.ca/fca-caf/{lang}/d/s/index.do",
}
_LEXUM_COL = {"scc": ("scc", "1"), "fca": ("fed", "53"), "fc": ("fed", "54"), "tcc": ("fed", "52")}


def _lexum_results(html: str) -> list[dict[str, Any]]:
    out = []
    for li in re.findall(r'<li class="(?:odd|even)[^"]*">(.*?)</li>', html, re.S):
        a = re.search(r'<span class="title"><a[^>]*href="([^"]+)"[^>]*>(.*?)</a></span>', li, re.S)
        if not a:
            continue
        href = a.group(1).split("?")[0]
        m = re.match(r"^/([\w-]+)/([\w-]+)/(en|fr)/item/(\d+)/index\.do$", href)
        if not m:
            continue
        cite = re.search(r'<span class="citation">(.*?)</span>', li, re.S)
        rep = re.search(r'<span class="report-citation">(.*?)</span>', li, re.S)
        date = re.search(r'<span class="publicationDate">(.*?)</span>', li, re.S)
        coll = re.search(r'<div class="subMetadata">\s*<span>(.*?)</span>', li, re.S)
        snip = re.search(r'<p class="snippet"\s*>(.*?)</p>', li, re.S)
        out.append({
            "native": f"{m.group(1)}/{m.group(2)}/{m.group(4)}", "lang": m.group(3),
            "title": _ws(re.sub(r"<[^>]+>", "", a.group(2))),
            "citation": _ws(re.sub(r"<[^>]+>", "", cite.group(1))) if cite else None,
            "report": _ws(re.sub(r"<[^>]+>", "", rep.group(1))) if rep else None,
            "date": _ws(date.group(1)) if date else None,
            "collection": _ws(re.sub(r"<[^>]+>", "", coll.group(1))) if coll else None,
            "snippet": _ws(re.sub(r"<[^>]+>", "", (snip.group(1) if snip else "").replace("&nbsp;", " ")))[:500],
        })
    return out


class LexumSource(Source):
    id = "ca.lexum"
    probe = {"query": "Montreal Convention", "kind": "case", "ref": "scc-csc/scc-csc/14418", "section": "[31]",
             "min_chars": 20000}
    name = "Supreme Court of Canada, Federal Court of Appeal and Federal Court decisions (Lexum)"
    jurisdictions = ("CA",)
    kinds = ("case",)
    modes = ("keyword", "fulltext")
    languages = ("en", "fr")
    homepage = "https://decisions.scc-csc.ca"
    coverage = (
        "Full-text search of Supreme Court of Canada judgments and of Federal Court of Appeal / Federal Court "
        "decisions (also Tax Court), on the courts' own Lexum sites; syntax: \"phrase\", AND, OR. Ids "
        "'scc-csc/scc-csc/14418' (Thibodeau v. Air Canada, 2014 SCC 67), 'fca-caf/decisions/521067'. "
        "Paragraphs '[31]'. language=fr reads the French version. filters: court (scc|fca|fc|tcc), "
        "from_date / to_date (YYYY-MM-DD), order (relevance|date)."
    )

    def search(self, query, *, kind="any", mode="keyword", limit=10, filters=None):
        if kind not in ("any", "case"):
            return []
        filters = filters or {}
        limit = max(1, min(int(limit or 10), 30))
        lang = "fr" if str(filters.get("language") or "").lower().startswith("fr") else "en"
        court = str(filters.get("court") or "").lower()
        targets: list[tuple[str, Optional[str]]] = []
        if court in _LEXUM_COL:
            inst, col = _LEXUM_COL[court]
            targets.append((inst, col))
        else:
            targets = [("scc", None), ("fed", None)]

        def one(t: tuple[str, Optional[str]]) -> list[dict[str, Any]]:
            inst, col = t
            params: dict[str, Any] = {"cont": query, "iframe": "true"}
            if col:
                params["col"] = col
            if filters.get("from_date"):
                params["d1"] = filters["from_date"]
            if filters.get("to_date"):
                params["d2"] = filters["to_date"]
            if str(filters.get("order") or "") == "date":
                params["or"] = "date"
            html = _get(_LEXUM_SEARCH[inst].format(lang=lang), params=params, what="Lexum decisions search").text
            return _lexum_results(html)

        groups: list[list[dict[str, Any]]] = []
        errors: list[str] = []
        with ThreadPoolExecutor(max_workers=len(targets)) as pool:
            for fut in [pool.submit(one, t) for t in targets]:
                try:
                    groups.append(fut.result())
                except (SourceUnavailable, httpx.HTTPError) as exc:
                    errors.append(str(exc))
                    groups.append([])
        if errors and len(errors) == len(targets):
            raise SourceUnavailable(errors[0])
        merged: list[dict[str, Any]] = []
        i = 0
        while len(merged) < limit and any(i < len(g) for g in groups):
            for g in groups:
                if i < len(g) and len(merged) < limit:
                    merged.append(g[i])
            i += 1
        hits = []
        for r in merged:
            host = _LEXUM_HOSTS.get(r["native"].split("/", 1)[0], "https://decisions.scc-csc.ca")
            a, b_, num = r["native"].split("/")
            cite = r["citation"] + (f", {r['report']}" if r.get("report") else "") if r.get("citation") else None
            hits.append(Hit(
                ref=self.ref(r["native"]), title=r["title"], source=self.id, kind="case",
                url=f"{host}/{a}/{b_}/{r['lang']}/item/{num}/index.do", date=r.get("date"), citation=cite,
                snippet=r.get("snippet") or "", extra={"court": r.get("collection")} if r.get("collection") else {},
            ))
        return hits

    def fetch(self, native_id, *, language=None):
        nid = native_id.strip().strip("/")
        m = re.match(r"^(?:https?://[^/]+/)?([\w-]+)/([\w-]+)/(?:(?:en|fr)/item/)?(\d+)", nid)
        if not m or m.group(1) not in _LEXUM_HOSTS:
            raise ValueError("Lexum ids look like 'scc-csc/scc-csc/14418' or 'fca-caf/decisions/521067'")
        a, b_, num = m.groups()
        lang = "fr" if (language or "").lower().startswith("fr") else "en"
        url = f"{_LEXUM_HOSTS[a]}/{a}/{b_}/{lang}/item/{num}/index.do"
        try:
            r = _get(url, params={"iframe": "true"}, what="Lexum decision", timeout=_LONG, accept="text/html")
        except httpx.HTTPStatusError as exc:
            raise ValueError(f"no decision at {url}") from exc
        html = r.text
        text = html_to_text(html, root_selector="#document-content")
        meta: dict[str, Any] = {}
        md = re.search(r'<div class="metadata">(.*?)</table>', html, re.S)
        title = ""
        if md:
            t = re.search(r'<h3 class="title">(.*?)</h3>', md.group(1), re.S)
            title = _ws(re.sub(r"<[^>]+>", "", t.group(1))) if t else ""
            for lab, val in re.findall(r'<td class="label">(.*?)</td>\s*<td class="metadata">(.*?)</(?:td|tr)>', md.group(1), re.S):
                k = _ws(re.sub(r"<[^>]+>", "", lab)).lower().replace(" ", "_")
                v = _ws(re.sub(r"<[^>]+>", " ", val))
                if k and v:
                    meta[k] = v
        meta = {{"neutral_citation": "citation", "date": "date", "report": "report", "case_number": "case_number",
                 "judges": "judges", "on_appeal_from": "on_appeal_from", "collection": "court",
                 "subjects": "subjects"}.get(k, k): v for k, v in meta.items() if k != "notes"}
        # A judgment's own paragraphs are bracketed "[12]"; the headnote's numbered
        # list ("1.", "2.") is not, so index the brackets when there are enough of them.
        sections = None
        marks = [(m_.start(), m_.group(1)) for m_ in re.finditer(r"(?m)^\[(\d{1,4})\]", text)]
        if len(marks) >= 3:
            seen: set[str] = set()
            uniq = [(p, n) for p, n in marks if not (n in seen or seen.add(n))]
            sections = []
            for i, (p, n) in enumerate(uniq):
                end = uniq[i + 1][0] if i + 1 < len(uniq) else len(text)
                sections.append({"label": f"[{n}]", "key": n, "heading": text[p:p + 120].split("\n", 1)[0],
                                 "start": p, "end": end, "level": "para"})
        if not text.strip():
            meta["no_text_reason"] = "the decision page carried no text (PDF only?); open the URL"
        return LegalDoc(
            ref=self.ref(f"{a}/{b_}/{num}"), title=title or nid, source=self.id, text=text, url=url, kind="case",
            language=lang, metadata=meta, sections=sections,
        )


# ================================================================ BC Laws

_BC = "https://www.bclaws.gov.bc.ca/civix"
_BC_NS = {"bcl": "http://www.gov.bc.ca/2013/bclegislation", "act": "http://www.gov.bc.ca/2013/legislation/act",
          "reg": "http://www.gov.bc.ca/2013/legislation/regulation"}


def _bc_local(el: Any) -> str:
    return etree.QName(el).localname if isinstance(el.tag, str) else ""


class BcLawsSource(Source):
    id = "ca.bclaws"
    probe = {"query": "Business Practices and Consumer Protection Act", "kind": "legislation", "ref": "04002_00",
             "section": "4", "min_chars": 20000}
    name = "BC Laws (King's Printer, British Columbia)"
    jurisdictions = ("CA-BC",)
    kinds = ("legislation",)
    modes = ("keyword", "fulltext")
    languages = ("en",)
    auth_env = ("BCLAWS_API_ACCEPTED",)
    auth_help = (
        "BC Laws publishes its CiviX API for third-party applications (github.com/bcgov/bc-laws-api, King's "
        "Printer Licence), but www.bclaws.gov.bc.ca/robots.txt (checked 2026-11-03) disallows every agent except "
        "Googlebot and Bingbot. Confirm with the King's Printer (or accept the published API terms) and then set "
        "BCLAWS_API_ACCEPTED=1 in legal_helper/api_key."
    )
    homepage = "https://www.bclaws.gov.bc.ca"
    coverage = (
        "British Columbia statutes and regulations, current consolidations, full-text search with fragments "
        "and status. Ids are CiviX document ids: '04002_00' (Business Practices and Consumer Protection Act), "
        "'96238_01' (Interpretation Act), '62_87'. Sections 's 4'. keyword = title words; fulltext = text."
    )

    def search(self, query, *, kind="any", mode="keyword", limit=10, filters=None):
        if kind not in ("any", "legislation"):
            return []
        limit = max(1, min(int(limit or 10), 30))
        q = query.strip()
        if mode != "fulltext":
            words = re.findall(r"[A-Za-z0-9]+", q)
            q_api = "CIVIX_DOCUMENT_TITLE:(" + " AND ".join(words) + ")" if words else q
        else:
            q_api = q
        xml = _get(f"{_BC}/search/complete/fullsearch", params={"q": q_api, "s": 0, "e": limit * 2, "nFrag": 2, "lFrag": 120},
                   what="BC Laws search", accept="application/xml").content
        root = etree.fromstring(xml)
        hits = []
        for d in root.iter("doc"):
            did = d.findtext("CIVIX_DOCUMENT_ID")
            title = _ws(d.findtext("CIVIX_DOCUMENT_TITLE") or "")
            if not did or d.findtext("CIVIX_DOCUMENT_TYPE") not in (None, "document"):
                continue
            if re.match(r"^(?:\d+(?:st|nd|rd|th) Edition )?TLC\b|Table of Legislative Changes", title):
                continue
            frags = " … ".join(_ws("".join(f.itertext())) for f in d.findall("frag"))
            hits.append(Hit(
                ref=self.ref(did), title=title, source=self.id, kind="legislation",
                url=f"https://www.bclaws.gov.bc.ca/civix/document/id/complete/statreg/{did}",
                status=d.findtext("CIVIX_DOCUMENT_STATUS") or None, snippet=frags[:500],
                extra={"location": d.findtext("CIVIX_DOCUMENT_LOC")},
            ))
            if len(hits) >= limit:
                break
        if not hits and mode != "fulltext":
            return self.search(query, kind=kind, mode="fulltext", limit=limit, filters=filters)
        return hits

    def fetch(self, native_id, *, language=None):
        did = native_id.strip().strip("/")
        b = _Lines()
        title = ""
        meta: dict[str, Any] = {"licence": "King's Printer Licence (British Columbia)"}
        first = self._doc_xml(did)
        if first is None:
            raise ValueError(f"BC Laws has no document {did!r}")
        roots = [first]
        if first.find(".//bcl:section", _BC_NS) is None and re.search(r"_00$", did):
            # A long Act is published as a table of contents (<id>_00) plus parts
            # <id>_01, <id>_02 …; read the parts in order until one is missing.
            parts = []
            for n in range(1, 16):
                part = self._doc_xml(re.sub(r"_00$", f"_{n:02d}", did))
                if part is None:
                    break
                parts.append(part)
            if parts:
                roots = parts
                meta["parts"] = len(parts)
        for root in roots:
            _bc_render(root, b)
            if not title:
                for tag in ("act:title", "reg:title", "bcl:title"):
                    e = root.find(f".//{tag}", _BC_NS)
                    if e is not None and _ws("".join(e.itertext())):
                        title = _ws("".join(e.itertext()))
                        break
            cur = root.find(".//bcl:currency", _BC_NS)
            if cur is not None and _ws("".join(cur.itertext())):
                meta.setdefault("current_to", _ws("".join(cur.itertext())))
        text = b.text()
        if not text.strip():
            meta["no_text_reason"] = "the document XML carried no text"
        return LegalDoc(
            ref=self.ref(did), title=title or did, source=self.id, text=text,
            url=f"https://www.bclaws.gov.bc.ca/civix/document/id/complete/statreg/{did}", kind="legislation",
            language="en", metadata=meta, sections=b.sections() or None,
        )

    def _doc_xml(self, did: str) -> Any:
        try:
            r = _get(f"{_BC}/document/id/complete/statreg/{did}/xml", what="BC Laws document", timeout=_LONG)
        except httpx.HTTPStatusError:
            return None
        try:
            return etree.fromstring(r.content, parser=etree.XMLParser(huge_tree=True, resolve_entities=False, recover=True))
        except etree.XMLSyntaxError:
            return None


def _bc_render(root: Any, b: _Lines) -> None:
    def inline(el: Any) -> str:
        return _ws("".join(el.itertext()))

    def walk(el: Any, in_sec: bool) -> None:
        name = _bc_local(el)
        if not name or name in ("script", "style", "head"):
            return
        if name == "section" and not in_sec:
            num = el.find("bcl:num", _BC_NS)
            note = el.find("bcl:marginalnote", _BC_NS)
            n = inline(num) if num is not None else ""
            if n:
                label = f"s {n}"
                head = f"{label} {inline(note) if note is not None else ''}".strip()
                b.mark(label, n.upper(), head, "article")
                b.line(head)
                for c in el:
                    if _bc_local(c) not in ("num", "marginalnote"):
                        walk(c, True)
                return
        if name in ("part", "division") and not in_sec:
            head = " ".join(inline(c) for c in el if _bc_local(c) in ("num", "text", "title"))[:160]
            if head:
                b.mark(head, "", head, "container")
                b.line(head)
            for c in el:
                if _bc_local(c) not in ("num", "text", "title"):
                    walk(c, in_sec)
            return
        if name in ("subsection", "paragraph", "subparagraph", "clause", "subclause", "definition"):
            num = el.find("bcl:num", _BC_NS)
            if num is not None and inline(num):
                b.prefix = f"{b.prefix} ({inline(num)})".strip()
            for c in el:
                if _bc_local(c) != "num":
                    walk(c, in_sec)
            b.flush()
            return
        if name in ("text", "title", "marginalnote", "hnote", "p", "num") or len(el) == 0:
            b.line(inline(el))
            return
        for c in el:
            walk(c, in_sec)

    walk(root, False)


# BC Laws (BcLawsSource) stays off: www.bclaws.gov.bc.ca/robots.txt disallows
# every agent but Googlebot and Bingbot, published API or not (2026-11-04).
SOURCES = [JusticeLawsSource(), LexumSource()]
