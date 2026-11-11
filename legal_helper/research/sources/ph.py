"""Philippines: the Supreme Court E-Library (decisions) and Lawphil (statutes).

ph.elibrary — elibrary.judiciary.gov.ph, the Supreme Court's official
E-Library. Full-text search through its dtSearch engine (``dtisapi6.dll``,
relevance-sorted, with hit passages) over SC decisions (and resolutions);
fetch reads the decision page (``cmd=getdoc``). Philippine decisions do not
number their paragraphs: the bracketed numbers in the text are footnotes, so a
section request "[12]" returns footnote 12; the body is cut at its own
headings, and the official pinpoint is the Phil. Reports page (PDF linked in
the metadata). The ref carries dtSearch's DocId and index id, which the
E-Library may renumber when it re-indexes; search again if a ref stops
resolving.

ph.lawphil — The LawPhil Project (Arellano Law Foundation), the standard free
compilation of Philippine statutes: Republic Acts, Commonwealth Acts, Batas
Pambansa, Presidential Decrees and Executive Orders, searched through
Lawphil's own public JSON API (``/api/public/<collection>``, title and
number), fetched as the page text with SECTION / SEC. N provisions. Lawphil is
authoritative in practice but not the official gazette (that is
officialgazette.gov.ph, which is behind a Cloudflare challenge from here).
No status or amendment data. Both open, no key.
"""

from __future__ import annotations

import html as _html
import re
from typing import Any, Optional
from urllib.parse import parse_qs, urlparse

import httpx

from ...connectors.base import USER_AGENT, request_with_retry
from ..base import Hit, LegalDoc, Source, SourceUnavailable
from ..textify import html_to_text, normalize_lines

_UA = {"User-Agent": USER_AGENT}
_LONG = httpx.Timeout(60.0, connect=10.0)
_MONTHS = {m: i for i, m in enumerate(
    ["january", "february", "march", "april", "may", "june", "july", "august", "september", "october",
     "november", "december"], 1)}


def _date_words(s: str) -> Optional[str]:
    m = re.search(r"([A-Z][a-z]+)\s+(\d{1,2}),\s*(\d{4})", s or "")
    if not m or m.group(1).lower() not in _MONTHS:
        return None
    return f"{int(m.group(3)):04d}-{_MONTHS[m.group(1).lower()]:02d}-{int(m.group(2)):02d}"


# ================================================================== E-Library

_EL = "https://elibrary.judiciary.gov.ph"
_DT = f"{_EL}/assets/dtSearch/dtSearch_system_files/dtisapi6.dll"
_EL_INDEX = {
    "decisions": "*{81aab6aa66450ae68a1f722f505a17c7} sc_decision",
    "resolutions": "*{25228a3adcb6fca12c8ef9e582ab0310} sc_resolution",
    "all": "*{1b34d74f9bb121cc1c90c69b6ed99f05} sc_all",
}


class ElibrarySource(Source):
    id = "ph.elibrary"
    name = "Supreme Court E-Library (elibrary.judiciary.gov.ph)"
    jurisdictions = ("PH",)
    kinds = ("case",)
    modes = ("keyword", "fulltext")
    languages = ("en",)
    homepage = _EL
    coverage = (
        "Philippine Supreme Court decisions (1901 on), full-text search ranked by hits with passages "
        "(all words; quote a phrase: '\"Warsaw Convention\"'). Titles are G.R. numbers and dates. "
        "Decisions have no paragraph numbers: section='[12]' reads footnote 12; pinpoint to Phil. Reports "
        "(PDF in metadata). filters: index (decisions|resolutions|all)."
    )
    probe = {"query": "Warsaw Convention", "kind": "case", "ref": "4aeb4dbdcceeda9b59b85ae3fb22cec0/56044",
             "section": "[5]", "min_chars": 5000}

    def search(self, query, *, kind="any", mode="keyword", limit=10, filters=None):
        if kind == "legislation":
            return []
        filters = filters or {}
        q = query.strip()
        phrase = q.startswith('"') and q.endswith('"') and len(q) > 2
        form = {
            "cmd": "search", "index": _EL_INDEX.get(str(filters.get("index") or "decisions"), _EL_INDEX["decisions"]),
            "request": q.strip('"') if phrase else q, "searchType": "phrase" if phrase else "allwords",
            "sort": "Hits", "pageSize": str(max(1, min(limit, 30))), "fuzziness": "0",
            "autoStopLimit": "5000", "autoTermWeight": "1",
        }
        try:
            resp = request_with_retry("POST", _DT, data=form, headers=_UA, timeout=_LONG)
        except httpx.HTTPError as exc:
            raise SourceUnavailable(f"E-Library search unreachable: {type(exc).__name__}: {exc}") from exc
        from bs4 import BeautifulSoup

        soup = BeautifulSoup(resp.text, "lxml")
        hits = []
        for a in soup.select("a.file_link"):
            qs = parse_qs(urlparse(a.get("href") or "").query)
            doc_id, index = (qs.get("DocId") or [""])[0], (qs.get("Index") or [""])[0].lstrip("*")
            if not doc_id or not index:
                continue
            syn = a.select_one("p.synopsis_hits")
            snippet = " ".join(syn.get_text(" ").split()) if syn else ""
            for p in a.find_all("p"):
                p.decompose()
            title = " ".join(a.get_text(" ").split()).strip("[] ")
            num = re.match(r"^((?:G\.R\.|A\.M\.|A\.C\.|B\.M\.|UDK|OCA)[^,.]*?No[s]?\.\s*[^,]+?)[.,]\s*", title)
            hits.append(Hit(
                ref=self.ref(f"{index}/{doc_id}"), title=title or f"E-Library document {doc_id}", source=self.id,
                kind="case", url=f"{_DT}?cmd=getdoc&DocId={doc_id}&Index=%2a{index}",
                date=_date_words(title), citation=num.group(1) if num else None, snippet=snippet[:500],
            ))
            if len(hits) >= limit:
                break
        return hits

    def fetch(self, native_id, *, language=None):
        index, _, doc_id = native_id.strip().partition("/")
        if not re.fullmatch(r"[0-9a-f]{32}", index) or not doc_id.isdigit():
            raise ValueError("E-Library refs look like '4aeb4dbdcceeda9b59b85ae3fb22cec0/56044'; take them from legal_search")
        url = f"{_DT}?cmd=getdoc&DocId={doc_id}&Index=%2a{index}"
        resp = request_with_retry("GET", url, headers=_UA, timeout=_LONG)
        from bs4 import BeautifulSoup

        page = resp.content.decode("utf-8", errors="replace")
        soup = BeautifulSoup(page, "lxml")
        title_tag = soup.find("title")
        head = " ".join(title_tag.get_text(" ").split()).strip("[] ") if title_tag else ""
        rep = None
        pdf = None
        for a in soup.find_all("a", href=True):
            if "/assets/pdf/" in a["href"]:
                rep = " ".join(a.get_text(" ").split()) or None
                pdf = _EL + a["href"] if a["href"].startswith("/") else a["href"]
                break
        text = html_to_text(page, root_selector="body")
        text = re.sub(r"^.*click for PDF copy\s*$", "", text, count=1, flags=re.M)
        text = re.sub(r"\n\s*Source: Supreme Court E-Library.*\Z", "", text, flags=re.S)
        text = normalize_lines(text)
        if not text or len(text) < 200:
            raise ValueError(f"E-Library document {native_id} has no text (the index may have been rebuilt; search again)")
        parties = ""
        m = re.search(r"\n([^\n]{5,400}?\b(?:VS?\.|versus)\s[^\n]{3,300})\n", text, re.I)
        if m:
            parties = " ".join(m.group(1).split()).rstrip(".")
        ponente = re.search(r"^([A-Z][A-Z.\-' ,]+?),\s*(?:C\.\s*)?J\.\s*:", text, re.M)
        division = re.search(r"^(EN BANC|(?:FIRST|SECOND|THIRD) DIVISION)\s*$", text, re.M)
        title = f"{_short_parties(parties)}, {head}" if parties else head
        meta = {k: v for k, v in {
            "citation": ", ".join(x for x in (head, rep) if x) or None,
            "date": _date_words(head),
            "court": "Supreme Court of the Philippines" + (f" ({division.group(1).title()})" if division else ""),
            "ponente": ponente.group(1).title() if ponente else None,
            "parties": parties or None,
            "phil_reports_pdf": pdf,
            "note": ("No paragraph numbers in Philippine decisions: '[n]' in the text are footnotes "
                     "(section='[n]' reads footnote n). Pinpoint to the Phil. Reports page in the PDF."),
        }.items() if v}
        return LegalDoc(
            ref=self.ref(native_id.strip()), title=title or doc_id, source=self.id, text=text, url=url,
            kind="case", language="en", metadata=meta, sections=_decision_sections(text) or None,
        )


def _short_parties(parties: str) -> str:
    p = re.sub(r",?\s*(?:PETITIONERS?|RESPONDENTS?|PLAINTIFFS?(?:-APPELL[A-Z]+)?|DEFENDANTS?(?:-APPELL[A-Z]+)?|"
               r"ACCUSED(?:-APPELLANT)?|APPELL[A-Z]+|COMPLAINANTS?)\b\.?", "", parties, flags=re.I)
    p = re.sub(r"[\s,]+VS?\.\s+", " v. ", p, flags=re.I)
    p = " ".join(p.split()).strip(" ,.")
    return p[:160]


def _is_heading(line: str) -> bool:
    s = line.strip()
    if not s or len(s) > 90 or s.startswith("[") or s[-1] in ".:;,":
        return False
    if len(s.split()) > 12 or not s[0].isupper():
        return False
    return not re.match(r"^(?:SO ORDERED|WHEREFORE|ACCORDINGLY)", s)


def _decision_sections(text: str) -> list[dict[str, Any]]:
    """Headings → containers, the text under each → one unit, footnotes → "[n]"."""
    lines = text.split("\n")
    offsets = []
    pos = 0
    for ln in lines:
        offsets.append(pos)
        pos += len(ln) + 1
    # The footnote block: the last line "[1] …" that is followed by "[2] …".
    foot_start = None
    for i in range(len(lines) - 1, -1, -1):
        if re.match(r"^\[1\]\s", lines[i]) and any(re.match(r"^\[2\]\s", x) for x in lines[i + 1:]):
            foot_start = i
            break
    body_end_line = foot_start if foot_start is not None else len(lines)
    secs: list[dict[str, Any]] = []
    cur_start = 0
    cur_label = "Opening"
    part = 0

    def close(end: int) -> None:
        nonlocal part
        if end > cur_start and text[cur_start:end].strip():
            part += 1
            secs.append({"label": cur_label, "key": f"p{part}", "heading": cur_label[:120],
                         "start": cur_start, "end": end, "level": "para"})

    for i in range(body_end_line):
        ln = lines[i]
        if i > 0 and _is_heading(ln):
            if lines[i - 1].strip() or (i + 1 < len(lines) and lines[i + 1].strip()):
                continue  # a heading stands alone between blank lines
            close(offsets[i])
            secs.append({"label": ln.strip(), "key": "", "heading": ln.strip()[:120], "start": offsets[i],
                         "end": offsets[i] + len(ln), "level": "container"})
            cur_start = offsets[i] + len(ln) + 1
            cur_label = ln.strip()
    close(offsets[body_end_line] if body_end_line < len(lines) else len(text))
    if foot_start is not None:
        marks = [(offsets[i], re.match(r"^\[(\d{1,4})\]", lines[i]).group(1))
                 for i in range(foot_start, len(lines)) if re.match(r"^\[(\d{1,4})\]\s", lines[i])]
        for j, (start, n) in enumerate(marks):
            end = marks[j + 1][0] if j + 1 < len(marks) else len(text)
            secs.append({"label": f"[{n}]", "key": n, "heading": f"footnote [{n}]", "start": start, "end": end,
                         "level": "para"})
    return secs


# ==================================================================== Lawphil

_LP = "https://lawphil.net"
_LP_COLLECTIONS = {
    # api module: (path base, url prefix, number key)
    "republic": ("statutes/repacts", "ra", "ra_number"),
    "presidential": ("statutes/presdecs", "pd", "pd_number"),
    "commonwealth": ("statutes/comacts", "ca", "ca_number"),
    "bataspambansa": ("statutes/bataspam", "bp", "bp_number"),
    "execord": ("executive/execord", "eo", "execord_number"),
}
_LP_ALIASES = {"ra": "republic", "republic act": "republic", "pd": "presidential", "presidential decree": "presidential",
               "ca": "commonwealth", "bp": "bataspambansa", "batas pambansa": "bataspambansa",
               "eo": "execord", "executive order": "execord"}
_LP_NUMBER = re.compile(
    r"\b(?:(R\.?\s*A\.?|Republic\s+Act)|(P\.?\s*D\.?|Presidential\s+Decree)|(C\.?\s*A\.?|Commonwealth\s+Act)|"
    r"(B\.?\s*P\.?|Batas\s+Pambansa)|(E\.?\s*O\.?|Executive\s+Order))\s*(?:Blg\.|No\.|Nos?\.)?\s*(\d{1,5})\b", re.I)


def _lp_api(module: str, search: str, rows: int, year: str = "") -> list[dict[str, Any]]:
    params = {"search": search, "rows": str(rows), "page": "1", "sort": "newest"}
    if year:
        params["year"] = year
    try:
        resp = request_with_retry("GET", f"{_LP}/api/public/{module}", params=params,
                                  headers={**_UA, "Accept": "application/json"})
        return (resp.json() or {}).get("data") or []
    except (httpx.HTTPError, ValueError) as exc:
        raise SourceUnavailable(f"Lawphil API ({module}) unreachable: {type(exc).__name__}: {exc}") from exc


def _lp_path(module: str, url: str) -> Optional[str]:
    base, prefix, _ = _LP_COLLECTIONS[module]
    url = (url or "").strip().lstrip("/")
    if not url:
        return None
    if "/" in url:
        path = f"{base}/{url}"
    else:
        m = re.search(r"_(\d{4})\.html?$", url)
        if not m:
            return None
        path = f"{base}/{prefix}{m.group(1)}/{url}"
    return re.sub(r"\.html?$", "", path)


def _lp_description(r: dict[str, Any]) -> str:
    d = r.get("description") or r.get("citation") or r.get("subject") or ""
    return " ".join(_html.unescape(re.sub(r"<br\s*/?>", " — ", str(d), flags=re.I)).split())


_SEC = re.compile(r"^(?:SECTION|Section|SEC\.|Sec\.)\s*(\d{1,4}(?:-?[A-Z])?)\s*[.:\-–—]")
_ART = re.compile(r"^(?:ARTICLE|Article|ART\.|Art\.)\s*(\d{1,4}(?:-?[A-Z])?)\s*[.:\-–—]")
_LP_CONTAINER = re.compile(r"^(?:BOOK|TITLE|CHAPTER|ARTICLE|RULE|PART)\s+[IVXLC]+\b|^(?:Book|Title|Chapter|Part)\s+[IVXLC\d]+\b")


def _statute_sections(text: str) -> list[dict[str, Any]]:
    marks: list[tuple[int, str, str, str, str]] = []
    pos = 0
    for raw in text.split("\n"):
        line = raw.strip()
        start = pos
        pos += len(raw) + 1
        for pat, word in ((_SEC, "Section"), (_ART, "Article")):
            m = pat.match(line)
            if m:
                label = f"{word} {m.group(1)}"
                cap = re.match(r"^\s*([A-Z][^.]{2,90}?)\s*\.\s*(?:[-–—]|$)", line[m.end():])
                marks.append((start, label, m.group(1).replace("-", "").upper(), label,
                              "article", cap.group(1).strip() if cap else ""))
                break
        else:
            if _LP_CONTAINER.match(line) and len(line) <= 120:
                marks.append((start, line, "", line, "container", ""))
    secs = []
    for i, (start, label, key, heading, level, caption) in enumerate(marks):
        end = marks[i + 1][0] if i + 1 < len(marks) else len(text)
        entry = {"label": label, "key": key, "heading": heading, "start": start, "end": end, "level": level}
        if caption:
            entry["caption"] = caption
        secs.append(entry)
    return secs


class LawphilSource(Source):
    id = "ph.lawphil"
    name = "The LawPhil Project — Philippine statutes (lawphil.net)"
    jurisdictions = ("PH",)
    kinds = ("legislation",)
    modes = ("keyword",)
    languages = ("en",)
    homepage = _LP
    coverage = (
        "Republic Acts (1946 on), Presidential Decrees, Commonwealth Acts, Batas Pambansa and Executive "
        "Orders as enacted (Arellano Law Foundation compilation; not consolidated, no amendment status). "
        "Search by number ('RA 9497', 'Republic Act No. 386') or title words ('Civil Aviation Authority', "
        "'Consumer Act'); fetch reads the act with SECTION / SEC. N provisions (Article N in codes). "
        "filters: collection (ra|pd|ca|bp|eo), year."
    )
    probe = {"query": "Civil Aviation Authority", "kind": "legislation", "ref": "statutes/repacts/ra2008/ra_9497_2008",
             "section": "Section 2", "min_chars": 20000}

    def search(self, query, *, kind="any", mode="keyword", limit=10, filters=None):
        if kind == "case":
            return []
        filters = filters or {}
        coll = str(filters.get("collection") or "").strip().lower()
        coll = _LP_ALIASES.get(coll, coll)
        year = str(filters.get("year") or "")
        m = _LP_NUMBER.search(query)
        if m:
            groups = ["republic", "presidential", "commonwealth", "bataspambansa", "execord"]
            module = next(g for g, v in zip(groups, m.groups()[:5]) if v)
            plan = [(module, m.group(6))]
        elif coll in _LP_COLLECTIONS:
            plan = [(coll, query.strip())]
        else:
            plan = [(c, query.strip()) for c in ("republic", "presidential", "commonwealth", "execord")]
        words = [w.lower() for w in re.findall(r"[A-Za-z0-9]+", query) if len(w) > 2]
        scored: list[tuple[float, Hit]] = []
        for module, term in plan:
            rows = _lp_api(module, term, 50 if not m else 20, year)
            _, _, num_key = _LP_COLLECTIONS[module]
            for r in rows:
                path = _lp_path(module, r.get("url") or "")
                if not path:
                    continue
                number = " ".join(str(r.get(num_key) or "").split())
                if m and not re.search(rf"\b{re.escape(m.group(6))}\b", number):
                    continue
                desc = _lp_description(r)
                hay = f"{number} {desc}".lower()
                score = sum(1 for w in words if w in hay) / max(1, len(words))
                ql = query.strip().lower()
                if ql in hay:
                    score += 1
                    if re.sub(r"^(?:the|an?)\s+", "", desc.lower()).startswith(re.sub(r"^(?:the|an?)\s+", "", ql)):
                        score += 1
                score -= len(desc) / 3000
                date = (r.get("date") or "")[:10] or None
                scored.append((score, Hit(
                    ref=self.ref(path), title=f"{number}: {desc}"[:300] if desc else number, source=self.id,
                    kind="legislation", url=f"{_LP}/{path}.html", date=date, citation=number or None,
                    snippet=desc[:400],
                    extra={"pdf": f"{_LP}/{path.rsplit('/', 1)[0]}/pdf/{path.rsplit('/', 1)[1]}.pdf"}
                    if r.get("pdf_availability") else {},
                )))
        scored.sort(key=lambda x: -x[0])
        return [h for _, h in scored[:limit]]

    def fetch(self, native_id, *, language=None):
        path = native_id.strip().strip("/")
        path = re.sub(r"\.html?$", "", path)
        if not re.fullmatch(r"(?:statutes|executive)/[a-z]+/[a-z]+\d{4}/[a-z]+_[0-9a-z_\-]+", path):
            m = _LP_NUMBER.search(path)
            if m and m.group(1):
                rows = _lp_api("republic", m.group(6), 20)
                hit = next((r for r in rows if re.search(rf"\bNo\.\s*{m.group(6)}\b", str(r.get("ra_number")))), None)
                path = _lp_path("republic", hit.get("url")) if hit else ""
            if not path or not re.fullmatch(r"(?:statutes|executive)/[a-z]+/[a-z]+\d{4}/[a-z]+_[0-9a-z_\-]+", path):
                raise ValueError("Lawphil refs look like 'statutes/repacts/ra2008/ra_9497_2008'; take them from legal_search")
        url = f"{_LP}/{path}.html"
        try:
            resp = request_with_retry("GET", url, headers=_UA, timeout=_LONG)
        except httpx.HTTPStatusError as exc:
            if exc.response.status_code == 404:
                raise ValueError(f"Lawphil has no page {url}") from exc
            raise
        page = resp.content.decode("cp1252", errors="replace")
        text = _lawphil_text(page)
        first = next((ln for ln in text.split("\n") if re.match(r"^(?:REPUBLIC ACT|PRESIDENTIAL DECREE|COMMONWEALTH ACT|"
                                                                  r"BATAS PAMBANSA|EXECUTIVE ORDER)", ln, re.I)), "")
        title_line = re.search(r"\n(AN ACT [^\n]{10,600}|PROVIDING [^\n]{10,400})\n", text, re.I)
        number = re.match(r"^((?:REPUBLIC ACT|PRESIDENTIAL DECREE|COMMONWEALTH ACT|BATAS PAMBANSA(?: BLG\.)?|"
                          r"EXECUTIVE ORDER)\s*(?:No\.|NO\.|Blg\.)?\s*\d+)", first, re.I)
        meta = {k: v for k, v in {
            "citation": " ".join(number.group(1).split()).title() if number else None,
            "date": _date_words(first),
            "long_title": title_line.group(1).strip() if title_line else None,
            "pdf": f"{_LP}/{path.rsplit('/', 1)[0]}/pdf/{path.rsplit('/', 1)[1]}.pdf",
            "status": "as enacted (Lawphil does not track amendments or repeal)",
        }.items() if v}
        title = " — ".join(x for x in (meta.get("citation"), (meta.get("long_title") or "")[:200]) if x) or path
        return LegalDoc(
            ref=self.ref(path), title=title, source=self.id, text=text, url=url, kind="legislation",
            language="en", metadata=meta, sections=_statute_sections(text) or None, ttl=30 * 24 * 3600,
        )


def _lawphil_text(page: str) -> str:
    """The act sits in one layout cell; reading the whole page would flatten it into a
    single table-row line, so convert the element that holds the act's paragraphs."""
    from bs4 import BeautifulSoup

    soup = BeautifulSoup(page, "lxml")
    best = None
    best_n = 0
    for node in soup.find_all(["blockquote", "td", "div"]):
        n = len(node.find_all("p", recursive=False))
        if n > best_n:
            best, best_n = node, n
    text = html_to_text(str(best)) if best is not None and best_n >= 3 else html_to_text(page)
    text = re.sub(r"\n\s*The Lawphil Project - Arellano Law Foundation\s*\Z", "", text)
    return normalize_lines(text)


SOURCES = [ElibrarySource(), LawphilSource()]
