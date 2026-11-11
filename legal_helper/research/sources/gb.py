"""United Kingdom: legislation.gov.uk (The National Archives).

The official revised statute book (Acts, SIs, devolved legislation and
retained EU law such as ``eur/2004/261``), open under the OGL with a
documented API: Atom search feeds (title and full text) and CLML XML for a
whole instrument, one provision, one schedule or a past version. Fetch reads
the CLML and builds the section index from its own structure
(``section-76``, ``regulation-2``, ``article-7``, schedule paragraphs,
scheduled conventions' articles), so a provision read never depends on
guessing headings. The unapplied effects listed in the metadata (amendments
not yet written into the revised text) are reported with the document.
Fair-use limit 1,500 requests / 5 min; CloudFront answers 502 while the
origin builds an uncached document, so requests are retried patiently.

UK case law is deliberately absent: Find Case Law's Open Justice Licence
excludes computational analysis without a separate TNA licence, and BAILII
forbids automated use.
"""

from __future__ import annotations

import re
import time
from concurrent.futures import ThreadPoolExecutor
from typing import Any, Optional

import httpx
from lxml import etree

from ...connectors.base import USER_AGENT, request_with_retry
from ..base import Hit, LegalDoc, Source, SourceUnavailable

_LEG = "https://www.legislation.gov.uk"
_NS_LEG = "http://www.legislation.gov.uk/namespaces/legislation"
_NS_UKM = "http://www.legislation.gov.uk/namespaces/metadata"
_NS_ATOM = "http://www.w3.org/2005/Atom"
_LONG = httpx.Timeout(90.0, connect=10.0)
_HEADERS = {"User-Agent": USER_AGENT}

#: legislation.gov.uk type codes (the first segment of every id).
_TYPES = (
    "ukpga", "uksi", "eur", "eudr", "eudn", "eut", "asp", "ssi", "asc", "anaw", "mwa", "wsi", "nia",
    "nisr", "nisi", "ukla", "ukcm", "ukci", "ukmo", "uksro", "apni", "aosp", "aep", "apgb", "gbla",
    "ukppa", "mnia", "nisro", "ukdsi", "sdsi", "wdsi", "nidsi", "ukdpb", "eudpb",
)
_TYPE_NAMES = {
    "ukpga": "UK Public General Act", "uksi": "UK Statutory Instrument", "eur": "retained EU regulation",
    "eudr": "retained EU directive", "eudn": "retained EU decision", "asp": "Act of the Scottish Parliament",
    "ssi": "Scottish SI", "asc": "Act of Senedd Cymru", "anaw": "Act of the National Assembly for Wales",
    "wsi": "Welsh SI", "nia": "Act of the Northern Ireland Assembly", "nisr": "NI Statutory Rule",
    "ukla": "UK Local Act",
}
_ID_RE = re.compile(r"^(?:https?://www\.legislation\.gov\.uk/)?(?:id/)?(?P<path>(?P<type>[a-z]+)/(?P<rest>.+?))/?(?:data\.\w+)?$")


def _get_patient(url: str, *, params: Optional[dict[str, Any]] = None, accept: str = "application/xml",
                 budget: float = 60.0, timeout: Optional[httpx.Timeout] = None) -> httpx.Response:
    """GET through CloudFront, which answers 502/504 while the origin is still
    building an uncached document (a first request for an SI took 20 s); the
    origin finishes and the next request is a cache hit, so keep asking —
    with growing pauses — until ``budget`` seconds have passed."""
    start = time.monotonic()
    pause = 1.5
    while True:
        try:
            return request_with_retry("GET", url, params=params, headers={**_HEADERS, "Accept": accept},
                                      timeout=timeout or _LONG, attempts=1)
        except httpx.HTTPStatusError as exc:
            if exc.response.status_code not in (429, 500, 502, 503, 504):
                raise
            if time.monotonic() - start + pause > budget:
                raise
        except httpx.TransportError:
            if time.monotonic() - start + pause > budget:
                raise
        time.sleep(pause)
        pause = min(pause * 2, 10.0)


def _local(el: Any) -> str:
    return etree.QName(el).localname if isinstance(el.tag, str) else ""


def _ws(s: str) -> str:
    return " ".join((s or "").split())


# ============================================================ CLML → text

#: Elements whose content never reaches the text (amendment notes are kept
#: out: a 1982 Act carries ~700 of them; the URL shows them in context).
_SKIP = {
    "Metadata", "Commentaries", "Versions", "CommentaryRef", "FootnoteRef", "MarginNoteRef", "Footnotes",
    "ExplanatoryNotes", "EarlierOrders", "Resources", "Image", "IncludedDocument", "MarginNotes", "Notes",
}
#: Elements that run inside a line.
_INLINE = {
    "Addition", "Substitution", "Repeal", "Citation", "CitationSubRef", "Emphasis", "Strong", "Term",
    "Definition", "Character", "Abbreviation", "Acronym", "Span", "Superior", "Inferior", "InternalLink",
    "ExternalLink", "Uppercase", "SmallCaps", "Underline", "Inline", "Expanded", "Strike", "DateText",
    "Proviso", "AppendText", "Reference", "CitationListRef", "a", "span", "sup", "sub", "i", "b", "em",
    "strong", "u", "abbr", "FragmentNumber", "FragmentTitle", "Glyph", "PageNumber",
}
_CONTAINERS = {
    "Part", "Chapter", "Pblock", "PsubBlock", "Division", "EUPart", "EUTitle", "EUChapter", "EUSection",
    "EUSubsection", "Group", "Appendix", "Schedule", "Attachment", "Form", "Tabular",
}
_ORDINALS = {
    "FIRST": "1", "SECOND": "2", "THIRD": "3", "FOURTH": "4", "FIFTH": "5", "SIXTH": "6", "SEVENTH": "7",
    "EIGHTH": "8", "NINTH": "9", "TENTH": "10", "ELEVENTH": "11", "TWELFTH": "12",
}
_UNIT_PREFIX = {"section": "s", "regulation": "reg", "article": "art", "rule": "rule", "paragraph": "para"}
_ARTICLE_TITLE = re.compile(r"^(?:Articles?|ARTICLES?|Art\.)\s+(\d+[A-Za-z]{0,3})(?![\w(])")


def _inline(el: Any) -> str:
    return _ws(_inline_raw(el))


def _inline_raw(el: Any) -> str:
    """The text of an inline run, without amendment-note markers (outer spaces kept)."""
    parts: list[str] = []

    def rec(e: Any) -> None:
        if not isinstance(e.tag, str):
            return
        if _local(e) in _SKIP:
            if e.tail:
                parts.append(e.tail)
            return
        if e.text:
            parts.append(e.text)
        for c in e:
            rec(c)
            # rec() appends the child's tail itself only for skipped children
            if _local(c) not in _SKIP and c.tail:
                parts.append(c.tail)

    if el.text:
        parts.append(el.text)
    for c in el:
        rec(c)
        if _local(c) not in _SKIP and c.tail:
            parts.append(c.tail)
    return re.sub(r"\s+", " ", "".join(parts))


def _pnumber(el: Any) -> str:
    p = el.find(f"{{{_NS_LEG}}}Pnumber")
    if p is None:
        return ""
    return _ws(_inline(p))


def _punct_number(el: Any, *, eu: bool) -> str:
    """'(1)', '(a)', '1.' — as the instrument prints the number."""
    p = el.find(f"{{{_NS_LEG}}}Pnumber")
    if p is None:
        return ""
    num = _ws(_inline(p))
    if not num:
        return ""
    before, after = p.get("PuncBefore"), p.get("PuncAfter")
    if before is None and after is None:
        if eu and _local(el) == "P2":
            return f"{num}."
        return f"({num})"
    return f"{before or ''}{num}{after or ''}"


class _Builder:
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
            s = f"{self.prefix} {s}".strip() if s else self.prefix
            if not s:
                return
            self.prefix = ""
        if not s:
            return
        self.lines.append(s)
        self.pos += len(s) + 1

    def flush_prefix(self) -> None:
        if self.prefix:
            p, self.prefix = self.prefix, ""
            self.line(p)

    def mark(self, label: str, key: str, heading: str, level: str, caption: str = "") -> None:
        self.flush_prefix()
        self.marks.append((self.pos, label, key, heading[:160], level, caption))

    def text(self) -> str:
        return "\n".join(self.lines)

    def sections(self, total: int) -> list[dict[str, Any]]:
        out = []
        for i, (start, label, key, heading, level, caption) in enumerate(self.marks):
            end = self.marks[i + 1][0] if i + 1 < len(self.marks) else total
            if end <= start and level != "container":
                continue
            d = {"label": label, "key": key, "heading": heading, "start": start, "end": max(end, start), "level": level}
            if caption:
                d["caption"] = caption
            out.append(d)
        return out


class _ClmlRenderer:
    """Walk a CLML document and emit lines plus provision marks."""

    def __init__(self, eu: bool, qualify: bool = False) -> None:
        self.b = _Builder()
        self.eu = eu
        self.qualify = qualify
        self.in_unit = 0
        self.schedule: Optional[str] = None
        self.annex: Optional[str] = None
        self.prelims = 0
        #: inside a scheduled treaty article whose paragraphs follow its title
        #: as sibling P1s (Carriage by Air Act 1961 Sch 1B, the Montreal Convention)
        self.treaty = False
        #: (key, schedule) of every unit, to tell when a label repeats across schedules
        self.unit_schedules: list[tuple[str, Optional[str]]] = []

    # --------------------------------------------------------- dispatch
    def block(self, el: Any) -> None:
        name = _local(el)
        if not name or name in _SKIP:
            return
        if name in ("P2", "P3", "P4", "P5", "P6", "P7") or (name == "P1" and (self.in_unit or self.treaty)):
            self._numbered(el)
        elif name == "P1":
            self._unit_p1(el, title="")
        elif name == "P1group":
            self._p1group(el)
        elif name in _CONTAINERS:
            self._container(el)
        elif name in ("table",):
            self._table(el)
        elif name in ("OrderedList", "UnorderedList"):
            self._list(el)
        elif name in ("EUPrelims", "PrimaryPrelims", "SecondaryPrelims", "EUPreamble"):
            self.prelims += 1
            try:
                self.flow(el)
            finally:
                self.prelims -= 1
        else:
            self.flow(el)

    def flow(self, el: Any, *, skip: tuple[str, ...] = ()) -> None:
        """Mixed content: inline runs become lines, block children recurse."""
        buf = [el.text or ""]
        for c in el:
            name = _local(c)
            if not name:
                buf.append(c.tail or "")
                continue
            if name in skip:
                buf.append(c.tail or "")
                continue
            if name in _SKIP:
                buf.append(c.tail or "")
                continue
            if name in _INLINE:
                buf.append(_inline_raw(c))
                buf.append(c.tail or "")
                continue
            self.b.line("".join(buf))
            buf = []
            self.block(c)
            buf.append(c.tail or "")
        self.b.line("".join(buf))

    # ---------------------------------------------------------- handlers
    def _numbered(self, el: Any) -> None:
        num = _punct_number(el, eu=self.eu)
        if self.b.prefix:
            self.b.prefix = f"{self.b.prefix} {num}".strip()
        else:
            self.b.prefix = num
        self.flow(el, skip=("Pnumber",))
        self.b.flush_prefix()

    def _p1group(self, el: Any) -> None:
        title_el = el.find(f"{{{_NS_LEG}}}Title")
        title = _inline(title_el) if title_el is not None else ""
        m = _ARTICLE_TITLE.match(title) if self.schedule is not None and not self.in_unit else None
        if m:
            rest = re.sub(r"^[\s—–\-.:]+", "", title[m.end():])
            self._treaty_article(m.group(1), f"Article {m.group(1)} {rest}".strip())
            self.treaty = True
            self.flow(el, skip=("Title",))
            return
        self.treaty = False
        p1s = [c for c in el if _local(c) == "P1"]
        if not p1s or self.in_unit:
            if title:
                self.b.line(title)
            self.flow(el, skip=("Title",))
            return
        first = True
        for c in el:
            name = _local(c)
            if name in ("Title", "") or name in _SKIP:
                continue
            if name == "P1":
                self._unit_p1(c, title=title if first else "", group=el)
                first = False
            else:
                self.block(c)

    def _unit_label(self, el: Any, num: str) -> tuple[str, str]:
        if self.schedule is not None:
            key = f"SCH{self.schedule}.{num}".upper()
            return f"Sch{self.schedule}.{num}", key
        if self.annex is not None:
            return f"Annex{self.annex}.{num}", f"ANNEX{self.annex}.{num}".upper()
        m = re.match(r"^(?:Article|Art\.?)\s*(\S+)", num)
        if m:
            return f"Article {m.group(1)}", m.group(1).upper()
        kind = (el.get("id") or "").split("-", 1)[0]
        prefix = _UNIT_PREFIX.get(kind, "")
        if self.eu and kind == "article":
            return f"Article {num}", num.upper()
        return (f"{prefix} {num}".strip(), num.upper())

    def _unit_p1(self, el: Any, *, title: str, group: Any = None) -> None:
        num = _pnumber(el)
        if not num:
            self.flow(el)
            return
        label, key = self._unit_label(el, num)
        title = re.sub(r"\.\s*$", "", _ws(title))
        status = (el.get("Status") or (group.get("Status") if group is not None else "") or "")
        heading = f"{label} {title}".strip()
        if status.lower() == "prospective":
            heading += " [prospective]"
        self._mark(label, key, heading, "article")
        self.b.line(heading)
        self.in_unit += 1
        try:
            self.flow(el, skip=("Pnumber",))
        finally:
            self.in_unit -= 1

    def _treaty_article(self, num: str, heading: str) -> None:
        """Mark a scheduled convention article. It keeps its printed label
        ('Article 17', matched as written); where the Act has its own sections
        — or schedules more than one convention — the key carries the schedule
        ('SCH1B.ART17', shown in the outline as [Sch1B.Art17]), so 'Article 17'
        of a later schedule can still be asked for. The text keeps the
        official heading."""
        label = f"Article {num}"
        caption = re.sub(r"^Articles?\s+\S+\s*[—–\-.:]*\s*", "", heading).strip()
        if self.qualify:
            qid = f"Sch{self.schedule}.Art{num}"
            self._mark(label, qid.upper(), heading, "article", f"{caption} [{qid}]".strip())
            # the schedule shows in a provision read too ("which convention's Article 17?")
            self.b.line(f"{heading} [{qid}]")
        else:
            self._mark(label, num.upper(), heading, "article", caption)
            self.b.line(heading)

    def _mark(self, label: str, key: str, heading: str, level: str, caption: str = "") -> None:
        self.b.mark(label, key, heading, level, caption)
        self.unit_schedules.append((key, self.schedule))

    def _heading_of(self, el: Any) -> str:
        parts = []
        for tag in ("Number", "Title", "TitleBlock", "Reference"):
            for c in el.findall(f"{{{_NS_LEG}}}{tag}"):
                if tag == "TitleBlock":
                    parts.append(" ".join(_inline(x) for x in c if _local(x) in ("Title", "Subtitle")))
                else:
                    parts.append(_inline(c))
        return _ws(" ".join(p for p in parts if p))

    def _has_units(self, el: Any) -> bool:
        for d in el.iter(f"{{{_NS_LEG}}}P1"):
            if _pnumber(d):
                return True
        for d in el.iter(f"{{{_NS_LEG}}}Pblock", f"{{{_NS_LEG}}}PsubBlock", f"{{{_NS_LEG}}}P1group"):
            t = d.find(f"{{{_NS_LEG}}}Title")
            if t is not None and _ARTICLE_TITLE.match(_inline(t)):
                return True
        return False

    def _container(self, el: Any) -> None:
        name = _local(el)
        heading = self._heading_of(el)
        skip = ("Number", "Title", "TitleBlock", "Reference")
        if name == "Tabular":
            self.flow(el)
            return
        if self.in_unit or self.prelims:
            if name == "Division" and re.fullmatch(r"\(?\w{1,4}\)?\.?", heading or ""):
                # a numbered recital "(12)": one paragraph, not a heading
                self.b.prefix = heading
                self.flow(el, skip=skip)
                self.b.flush_prefix()
                return
            if heading:
                self.b.line(heading)
            self.flow(el, skip=skip)
            return
        if name in ("Pblock", "PsubBlock"):
            m = _ARTICLE_TITLE.match(heading)
            if m:
                # A treaty article scheduled to an Act (Carriage by Air Act 1961
                # Sch 1: the Warsaw Convention) — cited as "Article 17".
                self._treaty_article(m.group(1), heading)
                self.in_unit += 1
                try:
                    self.flow(el, skip=skip)
                finally:
                    self.in_unit -= 1
                return
        self.treaty = False
        saved_sch, saved_annex = self.schedule, self.annex
        if name == "Schedule":
            sid = (el.get("id") or "").split("-")
            num = sid[1] if len(sid) > 1 else ""
            num = _ORDINALS.get(num.upper(), num)
            if not num:
                m = re.search(r"SCHEDULE\s+(\w+)", heading, re.I)
                num = _ORDINALS.get(m.group(1).upper(), m.group(1)) if m else ""
            self.schedule = num or "0"
        if name == "Attachment" and self.eu:
            self.annex = (el.get("id") or "attachment-1").split("-")[-1]
        try:
            if name in ("Schedule", "Attachment") and not self._has_units(el):
                label = (f"Sch{self.schedule}" if name == "Schedule" else f"Annex{self.annex}")
                self._mark(label, label.upper(), heading or label, "article")
                self.b.line(f"{label} {heading}".strip())
                self.in_unit += 1
                try:
                    self.flow(el, skip=skip)
                finally:
                    self.in_unit -= 1
                return
            if heading:
                self.b.mark(heading, "", heading, "container")
                self.b.line(heading)
            self.flow(el, skip=skip)
        finally:
            self.schedule, self.annex = saved_sch, saved_annex
            self.treaty = False

    def _table(self, el: Any) -> None:
        for tr in el.iter("{*}tr"):
            cells = [_ws(" ".join(t for t in td.itertext())) for td in tr if _local(td) in ("td", "th")]
            line = " | ".join(c for c in cells if c)
            if line:
                self.b.line(line)

    def _list(self, el: Any) -> None:
        typ = (el.get("Type") or "").lower()
        deco = (el.get("Decoration") or "").lower()
        n = 0
        for item in el:
            if _local(item) != "ListItem":
                continue
            n += 1
            num = item.get("NumberOverride")
            if not num:
                if typ == "alpha":
                    num = chr(96 + n) if n <= 26 else str(n)
                elif typ == "roman":
                    num = _roman(n).lower()
                elif typ == "arabic":
                    num = str(n)
            if num:
                mark = f"({num})" if deco in ("parens", "brackets", "") else f"{num}{')' if deco == 'parenright' else '.'}"
                self.b.prefix = (f"{self.b.prefix} {mark}".strip())
            elif _local(el) == "UnorderedList":
                self.b.prefix = (f"{self.b.prefix} –".strip())
            self.flow(item)
            self.b.flush_prefix()


def _roman(n: int) -> str:
    vals = [(10, "X"), (9, "IX"), (5, "V"), (4, "IV"), (1, "I")]
    out = ""
    for v, s in vals:
        while n >= v:
            out += s
            n -= v
    return out


def _render_clml(root: Any) -> tuple[str, list[dict[str, Any]], list[str]]:
    """Text, provision sections and the schedules whose labels repeat earlier ones."""
    main = None
    for tag in ("Primary", "Secondary", "EURetained", "Legislation"):
        main = root.find(f"{{{_NS_LEG}}}{tag}") if tag != "Legislation" else root
        if main is not None and tag != "Legislation":
            break
    eu = main is not None and _local(main) == "EURetained"
    body = None if main is None else next((c for c in main if _local(c) in ("Body", "EUBody")), None)
    has_sections = body is not None and body.find(f".//{{{_NS_LEG}}}P1") is not None
    r = _ClmlRenderer(eu=eu, qualify=has_sections)
    for child in (main if main is not None else root):
        if _local(child) in ("Metadata", "Commentaries", "Versions", "Footnotes"):
            continue
        r.block(child)
    text = r.b.text()
    return text, r.b.sections(len(text)), _repeated_keys(r.unit_schedules)


def _repeated_keys(units: list[tuple[str, Optional[str]]]) -> list[str]:
    """Schedules whose provision labels already occur earlier in the document."""
    first: dict[str, Optional[str]] = {}
    later: list[str] = []
    for key, sch in units:
        if key not in first:
            first[key] = sch
        elif first[key] != sch and sch is not None and sch not in later:
            later.append(sch)
    return later


def _clml_metadata(root: Any) -> dict[str, Any]:
    md = next((c for c in root if _local(c) == "Metadata"), None)
    meta: dict[str, Any] = {}
    if md is None:
        return meta

    def dc(tag: str, ns: str = "http://purl.org/dc/elements/1.1/") -> Optional[str]:
        e = md.find(f"{{{ns}}}{tag}")
        return _ws("".join(e.itertext())) if e is not None else None

    title = dc("title")
    if title:
        meta["title"] = title
    if dc("description"):
        meta["long_title"] = dc("description")
    valid = dc("valid", "http://purl.org/dc/terms/")
    if valid:
        meta["version"] = valid
    if dc("modified"):
        meta["modified"] = dc("modified")
    st = md.find(f".//{{{_NS_UKM}}}DocumentStatus")
    doc_status = st.get("Value") if st is not None else None
    for tag, key in (("EnactmentDate", "enacted"), ("Made", "made")):
        e = md.find(f".//{{{_NS_UKM}}}{tag}")
        if e is not None and e.get("Date"):
            meta[key] = e.get("Date")
    cif = md.find(f".//{{{_NS_UKM}}}ComingIntoForce/{{{_NS_UKM}}}DateTime")
    if cif is not None and cif.get("Date"):
        meta["effective"] = cif.get("Date")
    if root.get("RestrictExtent"):
        meta["extent"] = root.get("RestrictExtent")
    effects = md.findall(f".//{{{_NS_UKM}}}UnappliedEffect")
    pending = []
    for e in effects:
        if e.get("Applied") == "true":
            continue
        affecting = _ws(" ".join(x.text or "" for x in e.findall(f"{{{_NS_UKM}}}AffectingTitle")[:1]))
        pending.append(
            f"{e.get('AffectedProvisions') or ''} {e.get('Type') or ''} by {affecting} "
            f"{e.get('AffectingProvisions') or ''}".strip()
            + (f" ({e.get('AffectingURI', '').split('/id/')[-1]})" if e.get("AffectingURI") else "")
        )
    status_bits = []
    if doc_status == "revised":
        status_bits.append(f"revised text, up to date to {valid or '?'} with known changes")
    elif doc_status == "final":
        status_bits.append("original (as enacted/made/adopted) text — no revised version")
    elif doc_status:
        status_bits.append(doc_status)
    if title and re.search(r"\((?:repealed|revoked)[^)]*\)", title, re.I):
        status_bits.insert(0, re.search(r"\((repealed|revoked)[^)]*\)", title, re.I).group(0).strip("()"))
    if pending:
        status_bits.append(f"{len(pending)} effect(s) not yet applied to the text")
        meta["unapplied_effects"] = pending[:20]
        if len(pending) > 20:
            meta["unapplied_effects_more"] = len(pending) - 20
        meta["unapplied_effects_note"] = (
            "These amendments are listed by legislation.gov.uk but not yet written into the revised text "
            "(some may be prospective or extent-limited); read the affecting provision before relying on the wording."
        )
    if status_bits:
        meta["status"] = "; ".join(status_bits)
    if root.find(f"{{{_NS_LEG}}}Versions") is not None:
        meta["alternative_versions"] = (
            "Some provisions have alternative versions for a different extent or period; the text shows the "
            "default version (see the URL for the others)."
        )
    return meta


# ============================================================ Atom search

def _atom_entries(xml: bytes) -> tuple[list[dict[str, Any]], dict[str, Any]]:
    root = etree.fromstring(xml)
    A = f"{{{_NS_ATOM}}}"
    out = []
    for e in root.findall(f"{A}entry"):
        ident = (e.findtext(f"{A}id") or "").strip()
        native = ident.split("/id/", 1)[-1] if "/id/" in ident else ""
        if not native:
            continue
        title = _ws(e.findtext(f"{A}title") or "")
        summary = _ws(e.findtext(f"{A}summary") or "")
        year = e.find(f"{{{_NS_UKM}}}Year")
        number = e.find(f"{{{_NS_UKM}}}Number")
        created = e.find(f"{{{_NS_UKM}}}CreationDate")
        out.append({
            "native": native, "title": title, "summary": summary,
            "year": year.get("Value") if year is not None else None,
            "number": number.get("Value") if number is not None else None,
            "date": (created.get("Date") if created is not None else None)
                    or (e.findtext(f"{A}published") or "")[:10] or None,
        })
    meta = {
        "total": root.findtext("{http://a9.com/-/spec/opensearch/1.1/}totalResults"),
        "more": root.findtext(f"{{{_NS_LEG}}}morePages"),
    }
    return out, meta


def _citation(native: str, year: Optional[str], number: Optional[str]) -> Optional[str]:
    t = native.split("/", 1)[0]
    if not number:
        return None
    if t == "ukpga":
        return f"{year} c. {number}" if year and year.isdigit() else None
    if t in ("uksi", "ssi", "wsi"):
        return f"S.I. {year}/{number}" if t == "uksi" else f"{t.upper()} {year}/{number}"
    if t == "nisr":
        return f"S.R. {year}/{number}"
    if t in ("eur", "eudr", "eudn"):
        return f"{number}/{year} (retained EU law)"
    return None


def _status_from_title(title: str) -> Optional[str]:
    m = re.search(r"\((repealed|revoked)([^)]*)\)", title, re.I)
    return (m.group(1) + m.group(2)).strip() if m else None


_CITE_PATTERNS = [
    # ukpga/1982/16, eur/2004/261/article/7 — a native id typed as-is
    (re.compile(r"^\s*(?:https?://www\.legislation\.gov\.uk/)?(?:id/)?((?:%s)/[\w./-]+?)/?\s*$" % "|".join(_TYPES), re.I), "id"),
    # S.I. 2005/975, SI 2005 No. 975
    (re.compile(r"\bS\.?\s?I\.?\s*(\d{4})\s*(?:/|No\.?\s*)(\d+)", re.I), "uksi"),
    # Regulation (EC) No 261/2004, Regulation 261/2004, EC 261/2004
    (re.compile(r"\b(?:Regulation|Reg\.?)\s*(?:\((?:EC|EU|EEC|Euratom)\)\s*)?(?:No\.?\s*)?(\d{1,4})/(\d{4})\b", re.I), "eur"),
    (re.compile(r"\b(?:EC|EU)\s*(?:No\.?\s*)?(\d{1,4})/(\d{4})\b"), "eur"),
    (re.compile(r"\bDirective\s*(?:\((?:EC|EU|EEC)\)\s*)?(?:No\.?\s*)?(\d{4})/(\d{1,4})\b", re.I), "eudr"),
    # 1982 c. 16
    (re.compile(r"\b(\d{4})\s*c\.?\s*(\d+)\b"), "ukpga"),
]


def _cited_ids(query: str) -> list[str]:
    q = query.strip()
    out: list[str] = []
    for pat, kind in _CITE_PATTERNS:
        m = pat.search(q)
        if not m:
            continue
        if kind == "id":
            out.append(m.group(1).lower().rstrip("/"))
        elif kind == "uksi":
            out.append(f"uksi/{m.group(1)}/{m.group(2)}")
        elif kind == "eur":
            num, year = m.group(1), m.group(2)
            out.append(f"eur/{year}/{int(num)}")
        elif kind == "eudr":
            out.append(f"eudr/{m.group(1)}/{int(m.group(2))}")
        elif kind == "ukpga":
            out.append(f"ukpga/{m.group(1)}/{m.group(2)}")
        break
    return out


def _feed(path: str, params: dict[str, Any]) -> tuple[list[dict[str, Any]], dict[str, Any]]:
    try:
        r = _get_patient(f"{_LEG}/{path}", params=params, accept="application/atom+xml", budget=30.0,
                         timeout=httpx.Timeout(30.0, connect=10.0))
    except httpx.HTTPStatusError as exc:
        if exc.response.status_code == 404:
            return [], {}
        raise SourceUnavailable(f"legislation.gov.uk search answered {exc.response.status_code}; try again shortly") from exc
    except httpx.TransportError as exc:
        raise SourceUnavailable(f"legislation.gov.uk unreachable ({type(exc).__name__}); try again shortly") from exc
    return _atom_entries(r.content)


def _norm(s: str) -> str:
    return re.sub(r"[^a-z0-9 ]", "", re.sub(r"\s+", " ", s.lower().replace("’", "'"))).strip()


class LegislationGovUkSource(Source):
    id = "gb.legislation"
    probe = {"query": "Civil Aviation Act 1982", "kind": "legislation", "ref": "eur/2004/261", "section": "7", "min_chars": 2000}
    name = "legislation.gov.uk (The National Archives)"
    jurisdictions = ("GB",)
    kinds = ("legislation",)
    modes = ("keyword", "fulltext")
    languages = ("en",)
    homepage = _LEG
    coverage = (
        "Official revised UK legislation: Acts (ukpga), SIs (uksi), Scottish/Welsh/NI legislation and retained "
        "EU law (eur/2004/261 = UK261). Ids are the site's paths: 'ukpga/1982/16', 'uksi/2005/975', "
        "'eur/2004/261', a provision 'ukpga/1982/16/section/76', one schedule 'ukpga/Eliz2/9-10/27/schedule/1B' "
        "(Carriage by Air Act 1961, the Montreal Convention), a past version 'ukpga/1982/16/2019-01-01'. "
        "Sections from the XML: 's 76', 'reg 2', 'Article 7', schedule paragraphs 'Sch1.3', a scheduled "
        "convention's 'Article 17' (another schedule's: 'Sch1B.Art17'). No case law. Fetch reports "
        "status, the version date and amendments not yet applied. keyword = titles (citations like "
        "'SI 2005/975', '1982 c. 16', 'Regulation 261/2004' resolve directly); fulltext = inside the text. "
        "filters: type (ukpga|uksi|eur|primary|secondary|asp|ssi|wsi|nisr…), year."
    )

    # ---------------------------------------------------------- search
    def search(self, query, *, kind="any", mode="keyword", limit=10, filters=None):
        if kind not in ("any", "legislation"):
            return []
        filters = filters or {}
        typ = str(filters.get("type") or "").strip().lower() or None
        year = str(filters.get("year") or "").strip() or None
        limit = max(1, min(int(limit or 10), 30))
        hits: list[Hit] = []
        seen: set[str] = set()

        def add(entries: list[dict[str, Any]], how: str) -> None:
            for e in entries:
                native = e["native"]
                if native in seen:
                    continue
                seen.add(native)
                hits.append(Hit(
                    ref=self.ref(native), title=e["title"] or native, source=self.id, kind="legislation",
                    url=f"{_LEG}/{native}", date=e.get("date"), status=_status_from_title(e["title"] or ""),
                    snippet=(e.get("summary") or "")[:500],
                    citation=_citation(native, e.get("year"), e.get("number")),
                    extra={"type": _TYPE_NAMES.get(native.split("/", 1)[0], native.split("/", 1)[0]), "matched": how},
                ))

        if mode != "fulltext":
            for native in _cited_ids(query):
                parts = native.split("/")
                if len(parts) >= 3 and parts[1].isdigit() and parts[2].isdigit():
                    entries, _ = _feed(f"{parts[0]}/{parts[1]}/data.feed", {"number": parts[2]})
                    entries = [e for e in entries if e["native"] == "/".join(parts[:3])]
                    if entries:
                        e = dict(entries[0])
                        e["native"] = native
                        add([e], "citation")
                        continue
                add([{"native": native, "title": native, "summary": ""}], "citation")
            if hits and len(hits) >= 1 and _cited_ids(query):
                return hits[:limit]

        if mode == "fulltext":
            path = f"{typ or 'all'}/{year + '/' if year else ''}data.feed"
            entries, meta = _feed(path, {"text": query})
            if limit > 20 and meta.get("more") not in (None, "0", ""):
                more, _ = _feed(path, {"text": query, "page": 2})
                entries += more
            add(entries, "text")
            return hits[:limit]

        # Title search. The feed orders by year, EU-origin first, so a query
        # for "civil aviation" would open with forty EU decisions: Acts and
        # SIs are asked for separately and a title that equals or starts with
        # the query goes first.
        q = query.strip()
        m = re.search(r"\b(1[2-9]\d\d|20\d\d)\b", q)
        q_year = year
        if m and not year and not re.search(r"\d+/\d{4}|\d{4}/\d+", q):
            q_year = m.group(1)
            q = _ws(q[: m.start()] + q[m.end():])
        q = re.sub(r"\b(?:the|uk|british)\b", " ", q, flags=re.I)
        q = _ws(q.replace("'", "’") if "'" in q else q) or query.strip()
        types = [typ] if typ else (["primary", "secondary", "eu-origin"] if not q_year else ["all"])

        def one(t: str) -> list[dict[str, Any]]:
            path = f"{t}/{q_year + '/' if q_year else ''}data.feed"
            entries, _ = _feed(path, {"title": q})
            if not entries and "’" in q:
                entries, _ = _feed(path, {"title": q.replace("’", "'")})
            return entries

        groups: list[list[dict[str, Any]]] = []
        errors: list[str] = []
        with ThreadPoolExecutor(max_workers=len(types)) as pool:
            for fut in [pool.submit(one, t) for t in types]:
                try:
                    groups.append(fut.result())
                except SourceUnavailable as exc:
                    errors.append(str(exc))
                    groups.append([])
        if errors and len(errors) == len(types):
            raise SourceUnavailable(errors[0])
        flat = [e for g in groups for e in g]
        if not flat and not typ:
            # nothing in the titles: the words are probably about the content
            entries, _ = _feed(f"all/{q_year + '/' if q_year else ''}data.feed", {"text": query})
            add(entries, "text")
            return hits[:limit]
        nq = _norm(q)

        def rank(e: dict[str, Any]) -> int:
            t = _norm(re.sub(r"\s*\((?:repealed|revoked)[^)]*\)", "", e["title"] or "", flags=re.I))
            t = re.sub(r"\s+(1[2-9]\d\d|20\d\d)$", "", t)
            t = re.sub(r"^the ", "", t)
            if t == nq:
                return 0
            if t.startswith(nq):
                return 1
            return 2

        ordered: list[dict[str, Any]] = []
        for e in sorted(flat, key=rank):
            ordered.append(e)
        # interleave the remaining primary/secondary/EU groups after exact matches
        top = [e for e in ordered if rank(e) < 2]
        rest_groups = [[e for e in g if rank(e) == 2] for g in groups]
        rest: list[dict[str, Any]] = []
        i = 0
        while any(i < len(g) for g in rest_groups):
            for g in rest_groups:
                if i < len(g):
                    rest.append(g[i])
            i += 1
        add(top + rest, "title")
        return hits[:limit]

    # ----------------------------------------------------------- fetch
    def fetch(self, native_id, *, language=None):
        m = _ID_RE.match(native_id.strip())
        if not m or m.group("type") not in _TYPES:
            raise ValueError(
                "legislation.gov.uk ids look like 'ukpga/1982/16', 'uksi/2005/975', 'eur/2004/261', "
                "optionally with '/section/76' or a version date '/2019-01-01'"
            )
        path = m.group("path").strip("/")
        url = f"{_LEG}/{path}/data.xml"
        try:
            r = _get_patient(url, budget=75.0)
        except httpx.HTTPStatusError as exc:
            code = exc.response.status_code
            if code == 404:
                raise ValueError(f"legislation.gov.uk has no document at {path!r}") from exc
            if code == 300:
                raise ValueError(f"{path!r} is ambiguous on legislation.gov.uk; add the type/year/number in full") from exc
            raise SourceUnavailable(f"legislation.gov.uk answered {code} for {path}; try again shortly") from exc
        except httpx.TransportError as exc:
            raise SourceUnavailable(f"legislation.gov.uk unreachable ({type(exc).__name__})") from exc
        root = etree.fromstring(r.content, parser=etree.XMLParser(huge_tree=True, resolve_entities=False))
        meta = _clml_metadata(root)
        text, sections, repeats = _render_clml(root)
        title = meta.pop("title", None) or path
        treaty_schedules = sorted({m.group(1) for s_ in sections
                                   for m in [re.match(r"^SCH(\w+)\.ART", s_.get("key") or "")] if m})
        if treaty_schedules:
            meta["scheduled_conventions"] = (
                "The convention article labels ('Article 17') repeat across the schedules and section= returns "
                f"the first (Schedule {treaty_schedules[0]}); another schedule's article is asked for by its bracketed id ('Sch1B.Art17'), or read "
                "one schedule on its own: "
                + ", ".join(f"gb.legislation:{path}/schedule/{x}" for x in treaty_schedules[:6]) + "."
            )
        if repeats:
            meta["repeated_labels"] = (
                f"Some labels in Schedule(s) {', '.join(repeats)} repeat labels used earlier in this document; "
                "section= returns the first. Read one schedule on its own with ref "
                + ", ".join(f"gb.legislation:{path}/schedule/{x}" for x in repeats[:6]) + ", or use query= / offset."
            )
        frag = re.search(r"/((?:section|regulation|article|schedule|part|chapter|paragraph|rule|crossheading|annex)/.+?)(?:/\d{4}-\d{2}-\d{2})?$", path)
        if frag:
            title = f"{title} — {frag.group(1).replace('/', ' ')}"
        when = re.search(r"/(\d{4}-\d{2}-\d{2}|enacted|made|adopted)$", path)
        if when:
            meta["point_in_time"] = when.group(1)
        meta["citation"] = _citation(path, *(path.split("/")[1:3] if len(path.split("/")) >= 3 else (None, None)))
        meta = {k: v for k, v in meta.items() if v}
        meta["note"] = "Amendment annotations (F-notes) are omitted from the text; the URL shows them."
        if not text.strip():
            meta["no_text_reason"] = "the XML carried no body text (an image-only or PDF-only instrument?); open the URL"
        return LegalDoc(
            ref=self.ref(path), title=title, source=self.id, text=text, url=f"{_LEG}/{path}",
            kind="legislation", language="en", metadata=meta, sections=sections or None,
        )


SOURCES = [LegislationGovUkSource()]
