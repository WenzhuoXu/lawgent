"""Singapore: eLitigation — written judgments of the Supreme Court and others.

Search: the eLitigation judgment search page (``/gd/Home/Index``, server
rendered, relevance-sorted, 10 per page; quote a phrase for an exact match).
Fetch: the judgment page (``/gd/s/<YYYY>_<COURT>_<N>``, the neutral citation),
whose paragraphs carry the court's own numbers; they are written "[N]" so a
pinpoint reads as Singapore cites it ("at [12]").

Not covered: legislation. Singapore Statutes Online (sso.agc.gov.sg) refuses
every request from this host (CloudFront 403, robots.txt included), so there
is no Singapore legislation source here. Open, no key.
"""

from __future__ import annotations

import re
from typing import Any, Optional

import httpx

from ...connectors.base import USER_AGENT, request_with_retry
from ..base import Hit, LegalDoc, Source, SourceUnavailable
from ..textify import html_to_text, normalize_lines

_EL = "https://www.elitigation.sg"
_UA = {"User-Agent": USER_AGENT}
_CIT = re.compile(r"\[?\s*(\d{4})\s*\]?\s*(SG[A-Z()]+(?:\([A-Z]\))?|SLR(?:\(R\))?)\s*(\d+)", re.I)
_ID = re.compile(r"^(\d{4})_(SG[A-Z]+(?:\([A-Z]\))?)_(\d+)$", re.I)
_MONTHS = {m: i for i, m in enumerate(["jan", "feb", "mar", "apr", "may", "jun", "jul", "aug", "sep", "oct",
                                         "nov", "dec"], 1)}


def _iso(d: str) -> Optional[str]:
    m = re.search(r"(\d{1,2})\s+([A-Za-z]{3})[a-z]*\s+(\d{4})", d or "")
    if not m or m.group(2).lower() not in _MONTHS:
        return None
    return f"{m.group(3)}-{_MONTHS[m.group(2).lower()]:02d}-{int(m.group(1)):02d}"


def _native(text: str) -> Optional[str]:
    t = text.strip()
    m = _ID.match(t)
    if m:
        return f"{m.group(1)}_{m.group(2).upper()}_{m.group(3)}"
    m = _CIT.search(t)
    if m and m.group(2).upper().startswith("SG"):
        return f"{m.group(1)}_{m.group(2).upper()}_{m.group(3)}"
    return None


class ElitigationSource(Source):
    id = "sg.elitigation"
    name = "eLitigation — Singapore judgments (elitigation.sg)"
    jurisdictions = ("SG",)
    kinds = ("case",)
    modes = ("keyword", "fulltext")
    languages = ("en",)
    homepage = f"{_EL}/gd/"
    coverage = (
        "Written judgments of the Court of Appeal, High Court (incl. SICC, Family Division), and tribunals "
        "published on eLitigation (1990s on), full-text search ranked by relevance with passages; quote a "
        "phrase ('\"Montreal Convention\"', '\"carriage by air\"'). Refs are neutral citations "
        "('1999_SGCA_62' = [1999] SGCA 62); paragraphs read as '[12]'. No legislation (SSO is blocked "
        "from here). filters: year."
    )
    probe = {"query": "\"Warsaw Convention\"", "kind": "case", "ref": "1999_SGCA_62", "section": "[18]",
             "min_chars": 5000}

    def search(self, query, *, kind="any", mode="keyword", limit=10, filters=None):
        if kind == "legislation":
            return []
        filters = filters or {}
        year = str(filters.get("year") or "All")
        hits: list[Hit] = []
        seen: set[str] = set()
        cit = _native(query)
        if cit:
            hits.append(Hit(ref=self.ref(cit), title=f"[{cit.split('_')[0]}] {cit.split('_')[1]} {cit.split('_')[2]}",
                            source=self.id, kind="case", url=f"{_EL}/gd/s/{cit}", citation=cit.replace("_", " ", 2),
                            snippet="Resolved from the neutral citation; fetch to read it."))
            seen.add(cit)
        pages = max(1, min(3, (limit + 9) // 10))
        from bs4 import BeautifulSoup

        for page in range(1, pages + 1):
            params = {"Filter": "SUPCT", "YearOfDecision": year, "SortBy": "Score", "CurrentPage": str(page),
                      "SortAscending": "False", "SearchPhrase": query}
            try:
                resp = request_with_retry("GET", f"{_EL}/gd/Home/Index", params=params, headers=_UA,
                                          timeout=httpx.Timeout(45.0, connect=10.0))
            except httpx.HTTPError as exc:
                if hits:
                    break
                raise SourceUnavailable(f"eLitigation search unreachable: {type(exc).__name__}: {exc}") from exc
            soup = BeautifulSoup(resp.text, "lxml")
            cards = soup.select("div.card-body.gd")
            for card in cards:
                a = card.select_one("a.gd-heardertext")
                if not a:
                    continue
                m = re.search(r"/gd/s/([^/?#]+)", a.get("href") or "")
                if not m or m.group(1) in seen:
                    continue
                native = m.group(1)
                seen.add(native)
                info = [" ".join(x.get_text(" ").split()).strip(" |") for x in card.select(".gd-addinfo-text")]
                citation = next((x for x in info if re.match(r"^\[\d{4}\]", x)), None)
                date = next((_iso(x) for x in info if x.startswith("Decision Date")), None)
                case_no = next((x for x in info if x and not x.startswith(("[", "Decision Date"))), None)
                cw = card.select_one("a.gd-cw")
                hl = card.select_one(".search-highlight")
                hits.append(Hit(
                    ref=self.ref(native), title=" ".join(a.get_text(" ").split()), source=self.id, kind="case",
                    url=f"{_EL}/gd/s/{native}", date=date, citation=citation,
                    snippet=" ".join(hl.get_text(" ").split())[:500] if hl else "",
                    extra={k: v for k, v in {
                        "case_number": case_no,
                        "catchwords": " ".join(cw.get_text(" ").split()).strip("[]") if cw else None,
                    }.items() if v},
                ))
            if len(cards) < 10 or len(hits) >= limit:
                break
        return hits[:limit]

    def fetch(self, native_id, *, language=None):
        native = _native(native_id)
        if not native:
            raise ValueError("eLitigation refs are neutral citations like '1999_SGCA_62' or '[1999] SGCA 62'")
        url = f"{_EL}/gd/s/{native}"
        try:
            resp = request_with_retry("GET", url, headers=_UA, timeout=httpx.Timeout(60.0, connect=10.0))
        except httpx.HTTPStatusError as exc:
            if exc.response.status_code == 404:
                raise ValueError(f"eLitigation has no judgment {native}") from exc
            raise
        from bs4 import BeautifulSoup

        soup = BeautifulSoup(resp.text, "lxml")
        root = soup.select_one("#divJudgement")
        if root is None:
            raise ValueError(f"eLitigation page for {native} carries no judgment (#divJudgement missing)")

        def field(sel: str) -> Optional[str]:
            n = root.select_one(sel)
            return " ".join(n.get_text(" ").split()) if n else None

        meta = {k: v for k, v in {
            "citation": field(".HN-NeutralCit") or "[{}] {} {}".format(*native.split("_")),
            "case_number": field(".CaseNumber"),
            "catchwords": field(".catchwords"),
            "coram": field(".HN-Coram"),
            "date": _decision_date(root),
            "court": _court_name(native),
            "pdf": f"{_EL}/gd/gd/{native}/pdf",
        }.items() if v}
        # Paragraph numbers are separated from the text by an em space; write them
        # "[N]" so the section index (and a Singapore pinpoint) finds them.
        for div in root.select("div[class*='Judg-1'], div[class*='Judg-2'], div[class*='Judg-3']"):
            txt = div.get_text("")
            m = re.match(r"^\s*(\d{1,4})[\s   ]+(\S.*)?$", txt, re.S)
            if m:
                rest = (m.group(2) or "").strip()
                div.clear()
                div.append(f"[{m.group(1)}] {rest}".rstrip())
        text = normalize_lines(html_to_text(str(root)).replace(" ", " "))
        title = field(".HN-CaseName") or native
        return LegalDoc(
            ref=self.ref(native), title=f"{title} {meta.get('citation') or ''}".strip(), source=self.id, text=text,
            url=url, kind="case", language="en", metadata=meta,
        )


def _decision_date(root: Any) -> Optional[str]:
    for sel in (".HN-Coram", ".Judg-Date-Reserved", ".Judg-Hearing-Date"):
        n = root.select_one(sel)
        if n:
            d = _iso(" ".join(n.get_text(" ").split()))
            if d:
                return d
    return None


def _court_name(native: str) -> Optional[str]:
    code = native.split("_")[1].upper()
    return {
        "SGCA": "Court of Appeal", "SGHC": "High Court", "SGHCF": "High Court (Family Division)",
        "SGHC(A)": "Appellate Division of the High Court", "SGHC(I)": "Singapore International Commercial Court",
        "SGHCI": "Singapore International Commercial Court", "SGCAI": "Court of Appeal (SICC appeals)",
        "SGHCR": "High Court (Registrar)", "SGDC": "District Court", "SGMC": "Magistrate's Court",
        "SGSCT": "Small Claims Tribunals", "SGFC": "Family Court",
    }.get(code, code)


SOURCES = [ElitigationSource()]
