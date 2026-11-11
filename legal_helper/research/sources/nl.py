"""Netherlands: wetten.overheid.nl (BWB) and rechtspraak.nl.

nl.wetten — the Basiswettenbestand. Search is the KOOP SRU service over
titles, abbreviations and BWB ids (it has no full-text index); fetch reads the
repository's manifest (every consolidated version of a regulation) and then
the XML of the version in force on the date asked for, cut at its own
``<artikel>`` elements. Burgerlijk Wetboek articles are labelled book:article
("Artikel 8:1350"), as they are cited. SRU answers HTTP 406 to the default
``Accept: */*``, so every call sends ``Accept: application/xml``.

nl.rechtspraak — published Dutch judgments and AG opinions: full-text search
through the search API behind uitspraken.rechtspraak.nl, fetch by ECLI from the
open-data service (``data.rechtspraak.nl``, CC0), which serves metadata, the
inhoudsindicatie and the judgment with its numbered paragraphs ("2.1.").

Both are open, Dutch only, no key. wetten.overheid.nl's HTML is used only as
the citation URL (its robots.txt disallows AI crawlers).
"""

from __future__ import annotations

import datetime as _dt
import re
from typing import Any, Optional

import httpx
from lxml import etree

from ...connectors.base import USER_AGENT, request_with_retry
from ..base import Hit, LegalDoc, Source, SourceUnavailable
from ..textify import normalize_lines

_LONG = httpx.Timeout(90.0, connect=10.0)
_XML = {"User-Agent": USER_AGENT, "Accept": "application/xml"}


def _today() -> str:
    return _dt.date.today().isoformat()


def _local(tag: Any) -> str:
    if not isinstance(tag, str):
        return ""
    return tag.rsplit("}", 1)[-1]


def _parse_xml(data: bytes) -> etree._Element:
    parser = etree.XMLParser(recover=True, huge_tree=True, resolve_entities=False, no_network=True)
    root = etree.fromstring(data, parser=parser)
    if root is None:
        raise ValueError("unreadable XML")
    return root


class _Lines:
    """Text built line by line, with character offsets for the section index."""

    def __init__(self) -> None:
        self.parts: list[str] = []
        self.pos = 0

    def add(self, line: str) -> None:
        line = " ".join(line.split())
        if not line:
            return
        if self.parts:
            self.pos += 1  # the newline before this line
        self.parts.append(line)
        self.pos += len(line)

    def blank(self) -> None:
        if self.parts and self.parts[-1] != "":
            self.pos += 1
            self.parts.append("")

    @property
    def here(self) -> int:
        """Offset where the next line will start."""
        return self.pos + 1 if self.parts else 0

    def text(self) -> str:
        return "\n".join(self.parts)


# ===================================================================== BWB

_SRU = "https://zoekservice.overheid.nl/sru/Search"
_REPO = "https://repository.officiele-overheidspublicaties.nl/bwb"
_WETTEN = "https://wetten.overheid.nl"
_BWB_ID = re.compile(r"\bBWB[RV]\d{7}\b", re.I)
_NS_SRU = {
    "sru": "http://docs.oasis-open.org/ns/search-ws/sruResponse",
    "dcterms": "http://purl.org/dc/terms/",
    "overheid": "http://standaarden.overheid.nl/owms/terms/",
    "overheidbwb": "http://standaarden.overheid.nl/bwb/terms/",
}
_TYPE_RANK = {"wet": 0, "rijkswet": 0, "verdrag": 1, "AMvB": 2, "rijksAMvB": 2, "KB": 3,
              "ministeriele-regeling": 4, "zbo": 5, "pbo": 5, "beleidsregel": 6, "circulaire": 7}

#: Elements whose content is not part of the text.
_BWB_SKIP = {
    "meta-data", "jcis", "jci", "brondata", "bwb-inputbestand", "bwb-wijzigingen",
    "redactionele-correcties", "officiele-inhoudsopgave", "kop", "lidnr", "li.nr", "colspec",
}
_BWB_CONTAINERS = {
    "boek", "deel", "hoofdstuk", "titeldeel", "afdeling", "paragraaf", "sub-paragraaf",
    "divisie", "bijlage", "circulaire.divisie", "verdrag", "wetgeving-bijlage",
}
_BWB_ARTICLES = {"artikel", "enig-artikel"}
_CONTAINER_WORD = {"boek": "Boek", "deel": "Deel", "hoofdstuk": "Hoofdstuk", "titeldeel": "Titel",
                   "afdeling": "Afdeling", "paragraaf": "Paragraaf", "sub-paragraaf": "Subparagraaf",
                   "bijlage": "Bijlage"}
_BWB_INLINE = {
    "extref", "intref", "nadruk", "sup", "sub", "inf", "unl", "ovl", "i", "b", "u", "br",
    "noot", "noot.ref", "redactie", "a", "naam", "voornaam", "achternaam", "functie",
}


def _inline(el: etree._Element) -> str:
    out: list[str] = []

    def walk(e: etree._Element) -> None:
        if _local(e.tag) in ("meta-data", "jcis"):
            if e.tail:
                out.append(e.tail)
            return
        if e.text and isinstance(e.tag, str):
            out.append(e.text)
        for ch in e:
            walk(ch)
        if e is not el and e.tail:
            out.append(e.tail)

    walk(el)
    return " ".join("".join(out).split())


def _kop(el: etree._Element) -> tuple[str, str, str]:
    """(label word, number, title) of an element's <kop>."""
    kop = next((c for c in el if _local(c.tag) == "kop"), None)
    if kop is None:
        return "", "", ""
    parts = {_local(c.tag): _inline(c) for c in kop}
    return parts.get("label", ""), parts.get("nr", ""), parts.get("titel", "")


def _bwb_text(root: etree._Element) -> tuple[str, list[dict[str, Any]]]:
    out = _Lines()
    sections: list[dict[str, Any]] = []

    def is_blockish(e: etree._Element) -> bool:
        return any(isinstance(c.tag, str) and _local(c.tag) not in _BWB_INLINE for c in e)

    def emit(e: etree._Element, prefix: str = "") -> str:
        """Emit ``e``; returns the prefix still pending (not yet put on a line)."""
        tag = _local(e.tag)
        if not tag or tag in _BWB_SKIP:
            return prefix
        if tag in _BWB_CONTAINERS or tag in _BWB_ARTICLES:
            label_word, nr, title = _kop(e)
            variabel = e.get("bwb-ng-variabel-deel") or ""
            m = re.match(r"^/Boek(\d+[a-z]?)/", variabel)
            if tag in _BWB_ARTICLES:
                num = nr or (e.get("label") or "").replace("Artikel", "").strip()
                if m and num and ":" not in num:
                    num = f"{m.group(1)}:{num}"
                label = f"{label_word or 'Artikel'} {num}".strip()
                head = f"{label} {title}".strip()
                level, key = "article", num
            else:
                word = label_word or _CONTAINER_WORD.get(tag, "")
                label = " ".join(x for x in (word, nr) if x) or (e.get("label") or tag.capitalize())
                head = f"{label} {title}".strip()
                level, key = "container", ""
            out.blank()
            start = out.here
            out.add(head)
            for ch in e:
                emit(ch)
            sections.append({"label": label, "key": key, "heading": head[:120], "start": start,
                             "end": out.pos, "level": level})
            return ""
        if tag == "lid":
            nr = next((_inline(c) for c in e if _local(c.tag) == "lidnr"), "")
            pending = f"{nr}. " if nr and not nr.endswith(".") else (f"{nr} " if nr else "")
            for ch in e:
                pending = emit(ch, pending)
            if pending.strip():
                out.add(pending)
            return prefix
        if tag == "li":
            nr = next((_inline(c) for c in e if _local(c.tag) == "li.nr"), "")
            pending = f"{nr} " if nr else ""
            for ch in e:
                pending = emit(ch, pending)
            if pending.strip():
                out.add(pending)
            return prefix
        if tag == "row":
            cells = [_inline(c) for c in e if _local(c.tag) == "entry"]
            out.add(prefix + " | ".join(c for c in cells if c))
            return ""
        if is_blockish(e):
            if e.text and e.text.strip():
                out.add(prefix + e.text)
                prefix = ""
            for ch in e:
                prefix = emit(ch, prefix)
            return prefix
        text = _inline(e)
        if text:
            out.add(prefix + text)
            return ""
        return prefix

    body = root.find(".//wetgeving")
    if body is None:
        body = root
    emit(body)
    sections.sort(key=lambda s: (s["start"], 0 if s["level"] == "container" else 1))
    return out.text(), sections


def _sru(query: str, limit: int) -> list[dict[str, Any]]:
    resp = request_with_retry(
        "GET", _SRU,
        params={"operation": "searchRetrieve", "version": "2.0", "x-connection": "BWB",
                "query": query, "maximumRecords": str(limit)},
        headers=_XML,
    )
    root = _parse_xml(resp.content)
    diag = root.find(".//{http://docs.oasis-open.org/ns/search-ws/diagnostic}message")
    if diag is not None and _local(root.tag) == "diagnostics":
        raise SourceUnavailable(f"BWB SRU rejected the query: {diag.text}")
    out = []
    for rec in root.iterfind(".//sru:recordData", _NS_SRU):
        def v(path: str) -> str:
            el = rec.find(".//" + path, _NS_SRU)
            return (el.text or "").strip() if el is not None and el.text else ""
        out.append({
            "id": v("dcterms:identifier"), "title": v("dcterms:title"), "type": v("dcterms:type"),
            "authority": v("overheid:authority"), "area": v("overheidbwb:rechtsgebied"),
            "start": v("overheidbwb:geldigheidsperiode_startdatum"),
            "end": v("overheidbwb:geldigheidsperiode_einddatum"),
            "abbr": v("overheidbwb:afkorting"),
        })
    return out


def _cql_quote(s: str) -> str:
    return '"' + s.replace("\\", "").replace('"', "") + '"'


class BwbSource(Source):
    id = "nl.wetten"
    name = "wetten.overheid.nl (Basiswettenbestand)"
    jurisdictions = ("NL",)
    kinds = ("legislation",)
    modes = ("keyword",)
    languages = ("nl",)
    homepage = _WETTEN
    probe = {"query": "Wet luchtvaart", "kind": "legislation", "ref": "BWBR0005555", "section": "1.1",
             "min_chars": 20000}
    coverage = (
        "Every Dutch national regulation (wetten, AMvB's, ministeriële regelingen, verdragen) in its "
        "consolidated text, by title words in Dutch ('Wet luchtvaart', 'Burgerlijk Wetboek Boek 8'), "
        "abbreviation or BWB id (BWBR0005555). Titles only, no full-text search. Fetch gives the version "
        "in force today ('BWBR0005555') or on a date ('BWBR0005555@2020-01-01'), article by article: "
        "'1.1' (Wet luchtvaart), '8:1350' (BW Boek 8, air carriage); metadata lists version dates and "
        "count. filters: type (wet|AMvB|ministeriele-regeling|verdrag), rechtsgebied, date (YYYY-MM-DD)."
    )

    def search(self, query, *, kind="any", mode="keyword", limit=10, filters=None):
        if kind not in ("any", "legislation"):
            return []
        filters = filters or {}
        date = str(filters.get("date") or _today())[:10]
        extra = f" and overheidbwb.geldigheidsdatum=={date}"
        if filters.get("type"):
            extra += f" and dcterms.type=={_cql_quote(str(filters['type']))}"
        if filters.get("rechtsgebied"):
            extra += f" and overheidbwb.rechtsgebied={_cql_quote(str(filters['rechtsgebied']))}"
        q = " ".join((query or "").split())
        rows: list[dict[str, Any]] = []
        ids = [m.upper() for m in _BWB_ID.findall(q)]
        if ids:
            for bid in ids[:3]:
                rows += _sru(f"dcterms.identifier=={bid}{extra}", 5)
        else:
            words = re.sub(r"[\"()]", " ", q).strip()
            if not words:
                return []
            if " " not in words and len(words) <= 10:
                rows += _sru(f"overheidbwb.afkorting=={_cql_quote(words)}{extra}", 10)
            if " " in words:
                rows += _sru(f"overheidbwb.titel adj {_cql_quote(words)}{extra}", 60)
            if len(rows) < limit:
                rows += _sru(f"overheidbwb.titel all {_cql_quote(words)}{extra}", 60)
        seen: set[str] = set()
        uniq = []
        for r in rows:
            if r["id"] and r["id"] not in seen:
                seen.add(r["id"])
                uniq.append(r)
        ql = q.lower()

        def rank(r: dict[str, Any]) -> tuple:
            t = r["title"].lower()
            return (
                0 if t == ql or r["abbr"].lower() == ql or r["id"].lower() == ql else 1,
                0 if t.startswith(ql) else 1,
                _TYPE_RANK.get(r["type"], 8),
                len(t),
            )

        uniq.sort(key=rank)
        hits = []
        for r in uniq[:limit]:
            native = r["id"] if date == _today() else f"{r['id']}@{date}"
            bits = [r["type"], r["area"], r["authority"]]
            hits.append(Hit(
                ref=self.ref(native), title=r["title"] or r["id"], source=self.id, kind="legislation",
                url=f"{_WETTEN}/{r['id']}/{r['start']}" if r["start"] else f"{_WETTEN}/{r['id']}",
                date=r["start"] or None,
                status=f"in force (version of {r['start']})" if r["start"] else None,
                snippet=" · ".join(b for b in bits if b), citation=r["id"],
                extra={"abbreviation": r["abbr"]} if r["abbr"] else {},
            ))
        return hits

    def fetch(self, native_id, *, language=None):
        nid = native_id.strip()
        m = re.match(r"^(BWB[RV]\d{7})(?:@(\d{4}-\d{2}-\d{2})|/(\d{4}-\d{2}-\d{2}_\d+))?$", nid, re.I)
        if not m:
            raise ValueError("BWB refs look like 'BWBR0005555', 'BWBR0005555@2020-01-01' or "
                             "'BWBR0005555/2026-07-01_0'")
        bid, on_date, label = m.group(1).upper(), m.group(2), m.group(3)
        man = _parse_xml(request_with_retry("GET", f"{_REPO}/{bid}/manifest.xml", headers=_XML).content)
        versions = []
        for ex in man.iterfind("expression"):
            md = ex.find("metadata")
            start = (md.findtext("datum_inwerkingtreding") if md is not None else "") or ""
            end = (md.findtext("einddatum") if md is not None else "") or "9999-12-31"
            item = ex.find(".//manifestation[@label='xml']/item")
            if item is None or item.get("_deleted") == "true":
                continue
            versions.append({"label": ex.get("label") or "", "start": start, "end": end})
        if not versions:
            raise SourceUnavailable(f"BWB manifest for {bid} lists no XML versions")
        versions.sort(key=lambda v: v["label"])
        today = _today()
        if label:
            chosen = next((v for v in versions if v["label"] == label), None)
            if chosen is None:
                raise ValueError(f"{bid} has no version {label}; versions run {versions[0]['label']} … {versions[-1]['label']}")
        else:
            d = on_date or today
            live = [v for v in versions if v["start"] <= d <= v["end"]]
            chosen = live[-1] if live else None
            if chosen is None:
                earlier = [v for v in versions if v["start"] <= d]
                chosen = earlier[-1] if earlier else versions[0]
        url = f"{_REPO}/{bid}/{chosen['label']}/xml/{bid}_{chosen['label']}.xml"
        root = _parse_xml(request_with_retry("GET", url, headers=_XML, timeout=_LONG).content)
        text, sections = _bwb_text(root)
        title = (root.findtext(".//citeertitel") or "").strip()
        if not title and root.find(".//intitule") is not None:
            title = _inline(root.find(".//intitule"))
        title = " ".join((title or bid).split())
        soort = ""
        wg = root.find(".//wetgeving")
        if wg is not None:
            soort = wg.get("soort") or ""
        status = "in force" if chosen["start"] <= today <= chosen["end"] else (
            "not yet in force" if chosen["start"] > today else "no longer in force")
        meta: dict[str, Any] = {
            "bwb_id": bid, "type": soort, "status": status, "version": chosen["label"],
            "effective": chosen["start"], "versions": len(versions),
            "first_version": versions[0]["start"], "citation": bid,
        }
        if chosen["end"] and not chosen["end"].startswith("9999"):
            meta["valid_until"] = chosen["end"]
        later = [v for v in versions if v["start"] > chosen["start"]]
        if later:
            meta["later_versions"] = [f"{v['label']} (from {v['start']})" for v in later[-5:]]
        if not text.strip():
            meta["no_text_reason"] = "the BWB XML of this version holds no text"
        return LegalDoc(
            ref=self.ref(nid), title=title, source=self.id, text=text,
            url=f"{_WETTEN}/{bid}/{chosen['start']}", kind="legislation", language="nl",
            metadata=meta, sections=sections or None, ttl=24 * 3600,
        )


# ============================================================== Rechtspraak

_ZOEK = "https://uitspraken.rechtspraak.nl/api/zoek"
_DATA = "https://data.rechtspraak.nl/uitspraken/content"
_ECLI = re.compile(r"\bECLI:NL:[A-Z0-9]{2,7}:\d{4}:[A-Z0-9.]{1,25}\b", re.I)
_RS_SKIP = {"RDF", "footnote-ref"}
_RS_INLINE = {"emphasis", "superscript", "subscript", "ulink", "link", "footnote-ref", "inlinemediaobject",
              "phrase", "nr"}
_PARA_NUM = re.compile(r"^(?P<n>\d{1,2}(?:\.\d{1,3}){0,3})\.?(?=\s|$)")


def _nl_date(s: str) -> Optional[str]:
    m = re.match(r"^(\d{2})-(\d{2})-(\d{4})", s or "")
    return f"{m.group(3)}-{m.group(2)}-{m.group(1)}" if m else (s or None)


def _rs_inline(el: etree._Element) -> str:
    out: list[str] = []

    def walk(e: etree._Element) -> None:
        if isinstance(e, etree._ProcessingInstruction):
            if (e.target or "") == "linebreak":
                out.append("\n")
            if e.tail:
                out.append(e.tail)
            return
        if isinstance(e.tag, str) and e.text:
            out.append(e.text)
        for ch in e:
            walk(ch)
        if e is not el and e.tail:
            out.append(e.tail)

    walk(el)
    return "\n".join(" ".join(seg.split()) for seg in "".join(out).split("\n"))


def _rs_text(body: etree._Element) -> tuple[str, list[dict[str, Any]]]:
    out = _Lines()
    sections: list[dict[str, Any]] = []

    def add_multi(text: str, prefix: str = "") -> None:
        first = True
        for seg in text.split("\n"):
            if seg.strip():
                out.add((prefix if first else "") + seg)
                first = False
        if first and prefix.strip():
            out.add(prefix)

    def is_blockish(e: etree._Element) -> bool:
        return any(isinstance(c.tag, str) and _local(c.tag) not in _RS_INLINE for c in e)

    def emit(e: etree._Element, prefix: str = "") -> str:
        if not isinstance(e.tag, str):
            return prefix
        tag = _local(e.tag)
        if tag in _RS_SKIP:
            return prefix
        if tag == "section":
            title_el = next((c for c in e if _local(c.tag) == "title"), None)
            head = " ".join(_rs_inline(title_el).split()) if title_el is not None else ""
            if title_el is not None:
                nr = next((_rs_inline(c).strip() for c in title_el if _local(c.tag) == "nr"), "")
                if nr and head.startswith(nr) and len(head) > len(nr) and head[len(nr)] != " ":
                    head = f"{nr} {head[len(nr):].strip()}"
            out.blank()
            start = out.here
            if head:
                out.add(head)
            for ch in e:
                if ch is not title_el:
                    emit(ch)
            if head:
                sections.append({"label": head[:80], "key": "", "heading": head[:120], "start": start,
                                 "end": out.pos, "level": "container"})
            return ""
        if tag == "paragroup":
            nr = next((_rs_inline(c) for c in e if _local(c.tag) == "nr"), "").strip()
            start = out.here
            pending = f"{nr} " if nr else ""
            for ch in e:
                if _local(ch.tag) == "nr":
                    continue
                pending = emit(ch, pending)
            if pending.strip():
                out.add(pending)
            key = nr.rstrip(".")
            if key:
                sections.append({"label": key, "key": key, "heading": nr, "start": start,
                                 "end": out.pos, "level": "para"})
            return ""
        if tag == "listitem":
            pending = (e.get("override") or "") + " " if e.get("override") else "– "
            for ch in e:
                pending = emit(ch, pending)
            return prefix
        if tag == "row":
            cells = [_rs_inline(c).replace("\n", " ") for c in e if _local(c.tag) == "entry"]
            out.add(prefix + " | ".join(c for c in cells if c))
            return ""
        if tag == "title":
            add_multi(_rs_inline(e), prefix)
            return ""
        if is_blockish(e):
            if e.text and e.text.strip():
                out.add(prefix + e.text)
                prefix = ""
            for ch in e:
                prefix = emit(ch, prefix)
            return prefix
        text = _rs_inline(e)
        if text.strip():
            add_multi(text, prefix)
            return ""
        return prefix

    emit(body)
    text = out.text()
    if not any(s["level"] == "para" for s in sections):
        # Paragraph numbers typed into the text ("4.3 De kantonrechter …").
        pos = 0
        marks = []
        for line in text.split("\n"):
            m = _PARA_NUM.match(line)
            if m and len(line) > len(m.group(0)) + 2:
                marks.append((pos, m.group("n")))
            pos += len(line) + 1
        for i, (start, n) in enumerate(marks):
            end = marks[i + 1][0] if i + 1 < len(marks) else len(text)
            sections.append({"label": n, "key": n, "heading": text[start:start + 120].split("\n")[0],
                             "start": start, "end": end, "level": "para"})
    sections.sort(key=lambda s: (s["start"], 0 if s["level"] == "container" else 1))
    return text, sections


class RechtspraakSource(Source):
    id = "nl.rechtspraak"
    name = "Rechtspraak.nl (Dutch case law)"
    jurisdictions = ("NL",)
    kinds = ("case",)
    modes = ("keyword", "fulltext")
    languages = ("nl",)
    homepage = "https://uitspraken.rechtspraak.nl"
    probe = {"query": "buitengewone omstandigheden vogelaanvaring", "kind": "case",
             "ref": "ECLI:NL:RBNHO:2021:2275", "section": "4.3", "min_chars": 2000}
    coverage = (
        "Published judgments and AG conclusions of Dutch courts (Hoge Raad, gerechtshoven, rechtbanken, "
        "Raad van State, CBb, CRvB), full-text search in Dutch ('buitengewone omstandigheden "
        "vogelaanvaring', 'Verordening 261/2004 annulering') or by ECLI (ECLI:NL:HR:2019:1). Fetch by ECLI: "
        "metadata, inhoudsindicatie and the text cut at its numbered paragraphs ('2.1', '4.3'). "
        "filters: date_from / date_to (YYYY-MM-DD) applied to the decision date of the hits returned."
    )

    def search(self, query, *, kind="any", mode="keyword", limit=10, filters=None):
        if kind not in ("any", "case"):
            return []
        filters = filters or {}
        q = " ".join((query or "").split())
        if not q:
            return []
        body = {
            "StartRow": 0, "PageSize": max(1, min(int(limit), 50)), "ShouldReturnHighlights": True,
            "ShouldCountFacets": False, "SortOrder": "Relevance",
            "SearchTerms": [{"Term": q, "Field": "AlleVelden"}],
            "Contentsoorten": [], "Rechtsgebieden": [], "Instanties": [], "DatumPublicatie": [],
            "DatumUitspraak": [], "Advanced": {"PublicatieStatus": "AlleenGepubliceerd"},
            "CorrelationId": "00000000-0000-4000-8000-000000000000", "Proceduresoorten": [],
        }
        try:
            resp = request_with_retry(
                "POST", _ZOEK, json_body=body,
                headers={"User-Agent": USER_AGENT, "Accept": "application/json", "Content-Type": "application/json"},
            )
            data = resp.json()
        except (httpx.HTTPError, ValueError) as exc:
            raise SourceUnavailable(f"rechtspraak.nl search API did not answer ({type(exc).__name__}: {str(exc)[:160]})") from exc
        if not isinstance(data, dict) or "Results" not in data:
            raise SourceUnavailable("rechtspraak.nl search API answered in an unknown format (it is undocumented and may have changed)")
        hits = []
        lo, hi = str(filters.get("date_from") or ""), str(filters.get("date_to") or "")
        for r in data.get("Results") or []:
            ecli = (r.get("TitelEmphasis") or "").strip()
            if not _ECLI.fullmatch(ecli):
                m = _ECLI.search(r.get("DeeplinkUrl") or "")
                ecli = m.group(0) if m else ""
            if not ecli:
                continue
            date = _nl_date(r.get("Uitspraakdatum") or "")
            if (lo and date and date < lo) or (hi and date and date > hi):
                continue
            hits.append(Hit(
                ref=self.ref(ecli.upper()), title=f"{ecli} — {r.get('Titel') or ''}".strip(" —"),
                source=self.id, kind="case",
                url=r.get("DeeplinkUrl") or f"https://deeplink.rechtspraak.nl/uitspraak?id={ecli}",
                date=date, citation=ecli, snippet=" ".join((r.get("Tekstfragment") or "").split())[:500],
                status=r.get("GerechtelijkProductType") or None,
                extra={k: v for k, v in {"areas": r.get("Rechtsgebieden"), "procedure": r.get("Proceduresoorten")}.items() if v},
            ))
        if not hits:
            wanted = _ECLI.search(q)
            if wanted:
                ecli = wanted.group(0).upper()
                hits.append(Hit(ref=self.ref(ecli), title=ecli, source=self.id, kind="case",
                                url=f"https://deeplink.rechtspraak.nl/uitspraak?id={ecli}", citation=ecli,
                                snippet="Not returned by the search index; fetch reads it by ECLI."))
        return hits[:limit]

    def fetch(self, native_id, *, language=None):
        m = _ECLI.search(native_id.strip())
        if not m:
            raise ValueError("rechtspraak refs are ECLIs, e.g. 'ECLI:NL:HR:2019:1'")
        ecli = m.group(0).upper()
        try:
            resp = request_with_retry("GET", _DATA, params={"id": ecli}, headers=_XML, timeout=_LONG)
        except httpx.HTTPStatusError as exc:
            if exc.response.status_code == 404:
                raise ValueError(f"{ecli} is not on data.rechtspraak.nl") from exc
            raise
        root = _parse_xml(resp.content)
        meta: dict[str, Any] = {"ecli": ecli, "citation": ecli}
        desc = None
        for d in root.iter("{http://www.w3.org/1999/02/22-rdf-syntax-ns#}Description"):
            desc = d
            break
        dc = "{http://purl.org/dc/terms/}"
        psi = "{http://psi.rechtspraak.nl/}"
        if desc is not None:
            def all_text(tag: str) -> list[str]:
                return [" ".join((e.text or "").split()) for e in desc.iter(tag) if (e.text or "").strip()]
            court = all_text(dc + "creator")
            meta.update({k: v for k, v in {
                "court": court[0] if court else None,
                "date": (all_text(dc + "date") or [None])[0],
                "published": (all_text(dc + "issued") or [None])[0],
                "case_number": "; ".join(all_text(psi + "zaaknummer")) or None,
                "type": (all_text(dc + "type") or [None])[0],
                "procedure": all_text(psi + "procedure") or None,
                "areas": all_text(dc + "subject") or None,
                "place": (all_text(dc + "spatial") or [None])[0],
            }.items() if v})
            rel = []
            for e in desc.iter(dc + "relation"):
                lab = e.get("{http://www.w3.org/2000/01/rdf-schema#}label") or ""
                target = e.get("resourceIdentifier") or (e.text or "")
                mm = _ECLI.search(target or "")
                if mm:
                    rel.append(f"{lab}: {mm.group(0)}".strip(": "))
            if rel:
                meta["related"] = rel
        summary = ""
        body = None
        for e in root.iter():
            name = _local(e.tag)
            if name == "inhoudsindicatie" and not summary:
                summary = _rs_inline(e).replace("\n", " ").strip()
            elif name in ("uitspraak", "conclusie") and body is None:
                body = e
        text, sections = ("", [])
        if body is not None:
            text, sections = _rs_text(body)
        if summary:
            head = f"Inhoudsindicatie: {summary}\n\n"
            text = head + text if text else head.strip()
            for s in sections:
                s["start"] += len(head)
                s["end"] += len(head)
        if not body_has_text(text, summary):
            meta["no_text_reason"] = "rechtspraak.nl publishes only metadata for this ECLI (no judgment text)"
            text = text if summary else ""
        title_bits = [meta.get("court"), meta.get("date"), meta.get("case_number")]
        title = f"{ecli} — " + ", ".join(str(b) for b in title_bits if b)
        return LegalDoc(
            ref=self.ref(ecli), title=title, source=self.id, text=normalize_lines(text) if not sections else text,
            url=f"https://deeplink.rechtspraak.nl/uitspraak?id={ecli}", kind="case", language="nl",
            metadata=meta, sections=sections or None, ttl=30 * 24 * 3600,
        )


def body_has_text(text: str, summary: str) -> bool:
    return len(text) - len(summary) > 200


SOURCES = [BwbSource(), RechtspraakSource()]
