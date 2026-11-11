"""European Union: InfoCuria, the Court of Justice's own case-law search.

InfoCuria (infocuria.curia.europa.eu) is an Angular app over two backend
services named in its public config: an Elasticsearch connector
(``elastic-connector/search``: full-text search over every CJEU, General
Court and Civil Service Tribunal document with highlighted passages) and a
blob service that hands out each document as HTML (or PDF for older ones).
Neither is documented, so every response is checked for the fields this
adapter reads, and a change of shape is reported as the source being
unavailable rather than as "nothing found".

What it adds to ``eu.eurlex``: full-text search with highlights, provisional
texts not yet in Cellar, party names and procedural metadata. A hit carries
the CELEX and ECLI where the Court has assigned them, so the same document
can also be read from Cellar.
"""

from __future__ import annotations

import re
from typing import Any, Optional

import httpx

from ...connectors.base import USER_AGENT, request_with_retry
from ..base import Hit, LegalDoc, Source, SourceUnavailable
from ..textify import bytes_to_text, html_to_text

_API = "https://infocuriaws.curia.europa.eu/elastic-connector/"
_BLOB = "https://infocuriaws.curia.europa.eu/blob/"
_SITE = "https://infocuria.curia.europa.eu"
_LONG = httpx.Timeout(60.0, connect=10.0)
_HEADERS = {"User-Agent": USER_AGENT, "Accept": "application/json", "Content-Type": "application/json"}

_DOC_TYPES = {
    "ARRET": "Judgment", "CONCL": "Opinion of the Advocate General", "ORD": "Order",
    "AVIS": "Opinion of the Court", "PRISE": "View", "COMMUNICATION": "Communication",
}
_DOC_FILTER = {
    "judgment": "ARRET", "arret": "ARRET", "opinion": "CONCL", "ag": "CONCL", "conclusions": "CONCL",
    "order": "ORD", "ordonnance": "ORD",
}
_COURTS = {"C": "Court of Justice", "T": "General Court", "F": "Civil Service Tribunal"}
_LANGS = ("EN", "FR", "DE", "NL", "IT", "ES", "PT", "PL", "EL", "HU", "CS", "SK", "SL", "SV", "DA",
          "FI", "ET", "LV", "LT", "MT", "BG", "RO", "HR", "GA")
_CASE_NO = re.compile(r"\b([CTF])\s*[-‑–]\s*(\d{1,4})\s*/\s*(\d{2})\b", re.I)
_CELEX = re.compile(r"\b6\d{4}[A-Z]{2}\d{4}\b", re.I)
_ECLI = re.compile(r"\bECLI:EU:[CTF]:\d{4}:\d{1,5}\b", re.I)
#: Native id: the document's path on the blob service without the language,
#: '<routing with _>/<logical doc number>-<part>'.
_NATIVE = re.compile(r"^(?P<routing>[CTF](?:_[0-9A-Za-z]+){3,})/(?P<doc>\d+)-(?P<part>\d+)$")
_TAG = re.compile(r"<[^>]+>")


def _post(path: str, body: dict[str, Any]) -> dict[str, Any]:
    try:
        resp = request_with_retry("POST", _API + path, json_body=body, headers=_HEADERS, timeout=_LONG)
        data = resp.json()
    except httpx.HTTPStatusError as exc:
        raise SourceUnavailable(
            f"InfoCuria {path} answered HTTP {exc.response.status_code} (the API is undocumented and may have "
            "changed); search eu.eurlex instead."
        ) from exc
    except (httpx.HTTPError, ValueError) as exc:
        raise SourceUnavailable(f"InfoCuria {path} did not answer ({type(exc).__name__}); try eu.eurlex.") from exc
    if not isinstance(data, dict) or "searchHits" not in data:
        raise SourceUnavailable(f"InfoCuria {path} answered in an unknown format; try eu.eurlex.")
    return data


def _search_body(term: str, *, size: int, lang: str, sort: str, filters: list[dict[str, Any]],
                 exact: bool = False) -> dict[str, Any]:
    return {
        "searchTerm": term, "multiSearchTerms": [],
        "sortTermList": [{"sortDirection": "DESC", "sortTerm": sort}],
        "pagination": {"pageNumber": 0, "pageSize": size}, "language": lang,
        "tabName": "jurisprudence", "isAllTabsRequest": "notNeeded", "filtersValue": filters,
        "advancedFiltersValue": [], "isSearchExact": exact, "searchSources": ["document", "metadata"],
    }


def _celexes(raw: str) -> list[str]:
    # Joined cases concatenate their CELEX numbers: "62022CC015662022CC0158…".
    return re.findall(r"6\d{4}[A-Z]{2}\d{4}", raw or "")


def _year(routing: str) -> str:
    yy = int(routing.split("/")[2][-2:])
    return str(1900 + yy if yy >= 50 else 2000 + yy)


def _native(c: dict[str, Any]) -> Optional[str]:
    routing = c.get("routing") or c.get("idProcedure") or ""
    doc = str(c.get("logicDocId") or "").replace("id_", "")
    if not routing or not doc.isdigit() or routing.count("/") < 3:
        return None
    return f"{routing.replace('/', '_')}/{doc}-{int(c.get('docNoPart') or 1)}"


def _clean_hl(s: str) -> str:
    return " ".join(_TAG.sub("", s or "").split())


def _names(routings: list[str], lang: str, term: str) -> dict[str, dict[str, Any]]:
    """Party names ('usual names') and dates of the procedures behind the hits."""
    if not routings:
        return {}
    try:
        data = _post("docIds/procedures", {"searchTerm": term, "ids": routings, "language": lang})
    except SourceUnavailable:
        return {}
    out: dict[str, dict[str, Any]] = {}
    for h in data.get("searchHits") or []:
        c = h.get("content") or {}
        names = {}
        for entry in c.get("usualNameML") or []:
            if isinstance(entry, dict):
                names.update({k.lower(): (v or "").strip() for k, v in entry.items()})
        name = names.get(lang.lower()) or names.get("en") or names.get("fr") or ""
        out[c.get("id") or h.get("id") or ""] = {
            "name": name, "introduced": c.get("introductionDate"), "closed": c.get("closeDate"),
            "state": c.get("affairState"),
        }
    return out


class CuriaSource(Source):
    id = "eu.curia"
    name = "InfoCuria (Court of Justice of the EU)"
    jurisdictions = ("EU",)
    kinds = ("case",)
    modes = ("keyword", "fulltext")
    languages = ("en", "fr", "de", "…24 official")
    homepage = _SITE
    probe = {"query": "extraordinary circumstances bird strike", "kind": "case",
             "ref": "C_0436_21_00000000RP_01_P_01/266828-1", "section": "[25]", "min_chars": 10000}
    coverage = (
        "Full-text search of Court of Justice, General Court and Civil Service Tribunal judgments, AG "
        "opinions and orders, with highlighted passages, incl. provisional texts not yet in EUR-Lex: words "
        "('extraordinary circumstances bird strike'), a case number ('C-436/21'), CELEX (62021CJ0436) or ECLI. "
        "Hits carry CELEX/ECLI (readable in eu.eurlex too) and party names. Fetch gives the judgment cut at "
        "its numbered paragraphs ('[25]'); language=en|fr|de… . filters: doc_type (judgment|opinion|order), "
        "court (C|T|F), sort (date), language (search language, default EN). Undocumented API."
    )

    def search(self, query, *, kind="any", mode="keyword", limit=10, filters=None):
        if kind not in ("any", "case"):
            return []
        filters = filters or {}
        lang = str(filters.get("language") or "EN").upper()[:2]
        if lang not in _LANGS:
            lang = "EN"
        q = " ".join((query or "").split())
        if not q:
            return []
        fv: list[dict[str, Any]] = []
        dt = _DOC_FILTER.get(str(filters.get("doc_type") or "").lower()) or (
            str(filters["doc_type"]).upper() if str(filters.get("doc_type") or "").upper() in _DOC_TYPES else None)
        if dt:
            fv.append({"field": "docTypeCode", "values": [dt]})
        court = str(filters.get("court") or "").upper()[:1]
        if court in _COURTS:
            fv.append({"field": "affairJurisdictionCode", "values": [court]})
        sort = "DOC_DATE" if str(filters.get("sort") or "").lower() in ("date", "newest", "doc_date") else "SCORE"

        # Identifiers in the query: match them exactly among the hits.
        case_no = _CASE_NO.search(q)
        celex = _CELEX.search(q)
        ecli = _ECLI.search(q)
        size = max(1, min(int(limit), 30))
        data = _post("search", _search_body(q, size=size if not (case_no or celex or ecli) else 20,
                                            lang=lang, sort=sort, filters=fv))
        rows = data.get("searchHits") or []
        if case_no or celex or ecli:
            want_case = f"{case_no.group(1).upper()}-{int(case_no.group(2))}/{case_no.group(3)}" if case_no else None

            def exact(h: dict[str, Any]) -> bool:
                c = h.get("content") or {}
                return bool(
                    (want_case and (c.get("idPublished") or "").replace("‑", "-") == want_case)
                    or (celex and celex.group(0).upper() in _celexes(c.get("celex") or ""))
                    or (ecli and (c.get("ecli") or "").upper() == ecli.group(0).upper())
                )
            matched = [h for h in rows if exact(h)]
            rows = matched + [h for h in rows if not exact(h)] if matched else rows
            if ecli and not matched:
                rows = []  # an ECLI search otherwise returns every document citing that court's ECLIs
        rows = rows[:size]
        routings = list(dict.fromkeys((h.get("content") or {}).get("routing") or (h.get("content") or {}).get("idProcedure") or "" for h in rows))
        names = _names([r for r in routings if r], lang, q)
        hits = []
        for h in rows:
            c = h.get("content") or {}
            native = _native(c)
            if not native:
                continue
            routing = c.get("routing") or c.get("idProcedure") or ""
            info = names.get(routing, {})
            dtype = _DOC_TYPES.get(c.get("docTypeCode") or "", c.get("docType") or "Document")
            case = c.get("idPublished") or ""
            celexes = _celexes(c.get("celex") or "")
            title = f"{case} {info.get('name') or ''}".strip() + f" — {dtype} of {c.get('docDate') or '?'}"
            hl = h.get("highlightFields") or {}
            passages = []
            for key, vals in hl.items():
                if key.startswith(("contentML", "descriptorML", "summaryML")):
                    passages += [_clean_hl(v) for v in vals or []]
            langs = [g.get("docLang") for g in c.get("groupByLogicalId") or [] if g.get("docLang")]
            extra: dict[str, Any] = {k: v for k, v in {
                "ecli": c.get("ecli"), "celex": celexes[0] if celexes else None,
                "court": c.get("affairJurisdiction"), "doc_type": c.get("docTypeCode"),
                "formats": c.get("docFormats"), "report": c.get("recueil"),
            }.items() if v}
            if len(celexes) > 1:
                extra["joined_celex"] = celexes
            if langs and lang not in langs:
                extra["languages"] = sorted(set(langs))
            hits.append(Hit(
                ref=self.ref(native), title=title, source=self.id, kind="case",
                url=f"https://curia.europa.eu/juris/liste.jsf?num={case}&language={lang.lower()}" if case else _SITE,
                date=c.get("docDate"), citation=c.get("ecli") or case or None,
                snippet=" … ".join(passages[:3])[:600], extra=extra,
            ))
        return hits

    # ---------------------------------------------------------------- fetch
    def _resolve(self, ident: str) -> tuple[str, dict[str, Any]]:
        """CELEX / ECLI / case number → native id, preferring the judgment."""
        data = _post("search", _search_body(ident, size=20, lang="EN", sort="SCORE", filters=[]))
        case_no = _CASE_NO.search(ident)
        want_case = f"{case_no.group(1).upper()}-{int(case_no.group(2))}/{case_no.group(3)}" if case_no else None
        best = None
        for h in data.get("searchHits") or []:
            c = h.get("content") or {}
            ok = (
                (_CELEX.search(ident) and _CELEX.search(ident).group(0).upper() in _celexes(c.get("celex") or ""))
                or (_ECLI.search(ident) and (c.get("ecli") or "").upper() == _ECLI.search(ident).group(0).upper())
                or (want_case and (c.get("idPublished") or "") == want_case)
            )
            if ok and _native(c):
                if best is None or (c.get("docTypeCode") == "ARRET" and best.get("docTypeCode") != "ARRET"):
                    best = c
        if best is None:
            raise ValueError(f"InfoCuria has no document for {ident!r}")
        return _native(best) or "", best

    def fetch(self, native_id, *, language=None):
        nid = native_id.strip()
        known: dict[str, Any] = {}
        m = _NATIVE.match(nid)
        if not m:
            if _CELEX.fullmatch(nid) or _ECLI.fullmatch(nid) or _CASE_NO.fullmatch(nid):
                nid, known = self._resolve(nid)
                m = _NATIVE.match(nid)
            if not m:
                raise ValueError("InfoCuria refs look like 'C_0436_21_00000000RP_01_P_01/266828-1' (from a hit), "
                                 "or a CELEX / ECLI / case number such as 'C-436/21'")
        routing_ = m.group("routing")
        routing = routing_.replace("_", "/")
        court = routing_[0]
        doc, part = m.group("doc"), m.group("part")
        want = (language or "en").upper()[:2]
        order = [want] + [x for x in ("EN", "FR") if x != want]
        text, used_lang, used_url = "", "", ""
        case = f"{court}-{int(routing.split('/')[1])}/{routing.split('/')[2]}"
        info = _names([routing], want if want in _LANGS else "EN", case).get(routing, {})
        years = list(dict.fromkeys(y for y in ((info.get("introduced") or "")[:4], _year(routing)) if y))
        last_err = ""
        for lang, year, fmt in ((l, y, f) for l in order for f in ("html", "pdf") for y in years):
            if text.strip():
                break
            if fmt == "html":
                url = f"{_BLOB}download-file-html/{court}/{year}/{routing_}/{doc}-{lang}-{part}.html"
            else:
                url = f"{_BLOB}download-file/{court}/{year}/{routing_}/{doc}-{lang}-{part}.pdf"
            try:
                r = request_with_retry("GET", url, headers={"User-Agent": USER_AGENT}, timeout=_LONG, attempts=2)
            except httpx.HTTPStatusError as exc:
                last_err = f"HTTP {exc.response.status_code} for {fmt}/{lang}"
                continue
            except httpx.HTTPError as exc:
                raise SourceUnavailable(f"InfoCuria blob service did not answer ({type(exc).__name__})") from exc
            if fmt == "html":
                html = r.content.decode("utf-8", errors="replace")
                # Number each paragraph "[N]" so the section index finds it.
                html = re.sub(r'<a name="point(\d+)">\s*\d+\s*</a>', r"[\1] ", html)
                text = html_to_text(html)
            else:
                text = bytes_to_text(r.content, r.headers.get("content-type", ""), name_hint=url)
            if text.strip():
                used_lang, used_url = lang, url
        meta: dict[str, Any] = {"case": case}
        if info.get("name"):
            meta["parties"] = info["name"]
        for k in ("introduced", "closed", "state"):
            if info.get(k):
                meta[k] = info[k]
        if not known:
            # The hit's metadata (ECLI, CELEX, date, type): one search by case number.
            try:
                data = _post("search", _search_body(case, size=30, lang="EN", sort="SCORE", filters=[]))
                known = next((h.get("content") or {} for h in data.get("searchHits") or []
                              if str((h.get("content") or {}).get("logicDocId") or "") == f"id_{doc}"), {})
            except SourceUnavailable:
                known = {}
        if known:
            celexes = _celexes(known.get("celex") or "")
            meta.update({k: v for k, v in {
                "ecli": known.get("ecli"), "celex": celexes[0] if celexes else None,
                "date": known.get("docDate"), "doc_type": _DOC_TYPES.get(known.get("docTypeCode") or ""),
            }.items() if v})
        head = text[:600]
        if "Provisional text" in head or "Texte provisoire" in head:
            meta["status"] = "provisional text"
        if used_lang and used_lang != want:
            meta["language_note"] = f"not available in {want}; served in {used_lang}"
        if used_url:
            meta["content_url"] = used_url
        if meta.get("ecli"):
            meta["citation"] = meta["ecli"]
        if not text.strip():
            meta["no_text_reason"] = f"InfoCuria has no HTML or PDF for this document ({last_err})"
        title = f"{meta['case']} {meta.get('parties') or ''}".strip()
        if meta.get("doc_type"):
            title += f" — {meta['doc_type']}" + (f" of {meta['date']}" if meta.get("date") else "")
        else:
            first = next((ln for ln in text.split("\n") if ln.strip() and "rovisional" not in ln
                          and "provisoire" not in ln), "") if text else ""
            if first and len(first) < 120:
                title += f" — {first}"
        return LegalDoc(
            ref=self.ref(nid), title=title, source=self.id, text=text,
            url=f"https://curia.europa.eu/juris/liste.jsf?num={meta['case']}&language={(used_lang or want).lower()}", kind="case",
            language=(used_lang or want).lower(), metadata=meta, ttl=30 * 24 * 3600,
        )


SOURCES = [CuriaSource()]
