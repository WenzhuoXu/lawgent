"""United States (federal): eCFR, Federal Register, GovInfo, CourtListener.

Search reuses the connector layer's tested query code; fetch is done here so
the text keeps its structure (§ sections, opinion paragraphs) instead of the
one-line body the per-source tools return.
"""

from __future__ import annotations

import os
import re
import threading
import time
from collections import deque
from typing import Any, Optional

import httpx

from ...connectors.base import USER_AGENT, ensure_keys_loaded, request_with_retry
from ..base import Hit, LegalDoc, Source, SourceUnavailable
from ..textify import bytes_to_text, html_to_text
from . import _usfed

_LONG = httpx.Timeout(60.0, connect=10.0)


# ------------------------------------------------------------------- eCFR

class EcfrSource(Source):
    id = "us.ecfr"
    probe = {"query": "baggage liability", "ref": "14/part/254", "section": "§ 254.4"}
    name = "eCFR (Code of Federal Regulations, current)"
    jurisdictions = ("US",)
    kinds = ("legislation",)
    modes = ("keyword", "fulltext")
    languages = ("en",)
    homepage = "https://www.ecfr.gov"
    coverage = (
        "Current CFR, full-text search; fetch a section ('14/254.4') or a part ('14/part/254'). "
        "filters: title (e.g. 14 aeronautics, 49 transportation)."
    )

    def search(self, query, *, kind="any", mode="keyword", limit=10, filters=None):
        filters = filters or {}
        rows = _usfed.search_ecfr(query, title=int(filters.get("title") or 0), per_page=limit)
        hits = []
        for r in rows:
            title, section, part = r.get("title"), r.get("section"), r.get("part")
            native = f"{title}/{section}" if section else f"{title}/part/{part}"
            label = (r.get("headings") or {}).get("section") or (f"part {part}" if part else "")
            hits.append(Hit(
                ref=self.ref(native), title=f"{title} CFR {label}".strip(), source=self.id,
                url=f"https://www.ecfr.gov/current/title-{title}/section-{section}" if section else None,
                snippet=re.sub(r"<[^>]+>", "", r.get("snippet") or "")[:500],
                citation=f"{title} C.F.R. § {section}" if section else None,
                date=r.get("effective_on"),
            ))
        return hits

    def fetch(self, native_id, *, language=None):
        parts = native_id.strip().split("/")
        title = int(parts[0])
        params: dict[str, Any] = {}
        if len(parts) >= 3 and parts[1] == "part":
            params["part"] = parts[2]
        elif len(parts) == 2:
            params["section"] = parts[1]
        else:
            raise ValueError("eCFR refs look like '14/254.4' (section) or '14/part/254' (part)")
        issue = _usfed.ecfr_latest_issue_date(title)
        if not issue:
            raise SourceUnavailable(f"eCFR has no issue date for title {title}")
        url = f"https://www.ecfr.gov/api/versioner/v1/full/{issue}/title-{title}.xml"
        resp = request_with_retry("GET", url, params=params, headers={"User-Agent": USER_AGENT}, timeout=_LONG)
        text = html_to_text(resp.text, xml=True)
        head = text.split("\n", 1)[0][:200]
        return LegalDoc(
            ref=self.ref(native_id), title=f"{title} CFR — {head}", source=self.id, text=text,
            url=f"https://www.ecfr.gov/current/title-{title}" + (
                f"/section-{params['section']}" if "section" in params else f"/part-{params['part']}"),
            language="en", metadata={"version": issue, "status": "current (eCFR)"}, ttl=24 * 3600,
        )


# -------------------------------------------------------- Federal Register

class FederalRegisterSource(Source):
    id = "us.federal_register"
    probe = {"query": "airline refunds", "ref": "2026-13675"}
    name = "Federal Register"
    jurisdictions = ("US",)
    kinds = ("legislation",)
    modes = ("keyword", "fulltext")
    languages = ("en",)
    homepage = "https://www.federalregister.gov"
    coverage = "Rules, proposed rules and notices since 1994, full-text search; fetch by document number. filters: agency (slug)."

    def search(self, query, *, kind="any", mode="keyword", limit=10, filters=None):
        filters = filters or {}
        rows = _usfed.search_federal_register(query, agency=filters.get("agency", ""), per_page=limit)
        return [
            Hit(
                ref=self.ref(r["document_number"]), title=r.get("title") or "", source=self.id,
                url=r.get("html_url"), date=r.get("publication_date"), citation=r.get("citation"),
                status=r.get("type"), snippet=(r.get("abstract") or "")[:500],
            )
            for r in rows if r.get("document_number")
        ]

    def fetch(self, native_id, *, language=None):
        num = native_id.strip()
        meta = request_with_retry(
            "GET", f"https://www.federalregister.gov/api/v1/documents/{num}.json",
            headers={"User-Agent": USER_AGENT, "Accept": "application/json"},
        ).json()
        body_url = meta.get("body_html_url") or meta.get("raw_text_url")
        text = ""
        if body_url:
            r = request_with_retry("GET", body_url, headers={"User-Agent": USER_AGENT}, timeout=_LONG)
            text = bytes_to_text(r.content, r.headers.get("content-type", ""), name_hint=body_url)
        return LegalDoc(
            ref=self.ref(num), title=meta.get("title") or num, source=self.id, text=text,
            url=meta.get("html_url"), language="en",
            metadata={k: meta.get(k) for k in ("type", "publication_date", "effective_on", "citation", "cfr_references") if meta.get(k)},
            ttl=30 * 24 * 3600,
        )


# ---------------------------------------------------------------- GovInfo

class GovinfoSource(Source):
    id = "us.govinfo"
    probe = {"query": "Montreal Convention", "search_only": True}
    name = "GovInfo (U.S. Code, Public Laws, CFR annual, Statutes at Large)"
    jurisdictions = ("US",)
    kinds = ("legislation",)
    modes = ("keyword", "fulltext")
    languages = ("en",)
    auth_env = ("GOVINFO_API_KEY",)
    auth_help = "Free key from https://api.data.gov/signup/."
    homepage = "https://www.govinfo.gov"
    coverage = (
        "Full-text search of GPO collections: USCODE, PLAW, STATUTE, CFR, FR, BILLS, CRPT. "
        "filters: collections (list, e.g. ['USCODE'])."
    )

    def search(self, query, *, kind="any", mode="keyword", limit=10, filters=None):
        filters = filters or {}
        c = filters.get("collections")
        rows = _usfed.search_govinfo(query, collections=(c if isinstance(c, list) else [c]) if c else None, page_size=limit)
        hits = []
        for r in rows:
            pkg, gran = r.get("package_id"), r.get("granule_id")
            if not pkg:
                continue
            hits.append(Hit(
                ref=self.ref(f"{pkg}/{gran}" if gran else pkg), title=r.get("title") or pkg, source=self.id,
                url=r.get("details_url"), date=r.get("date_issued"), snippet=(r.get("snippet") or "")[:400],
                extra={"collection": r.get("collection")} if r.get("collection") else {},
            ))
        return hits

    def fetch(self, native_id, *, language=None):
        pkg, _, gran = native_id.strip().partition("/")
        stem = gran or pkg
        text, used = "", ""
        for url in (
            f"https://www.govinfo.gov/content/pkg/{pkg}/html/{stem}.htm",
            f"https://www.govinfo.gov/content/pkg/{pkg}/pdf/{stem}.pdf",
        ):
            try:
                r = request_with_retry("GET", url, headers={"User-Agent": USER_AGENT}, timeout=_LONG)
            except httpx.HTTPError:
                continue
            text = bytes_to_text(r.content, r.headers.get("content-type", ""), name_hint=url)
            if text.strip():
                used = url
                break
        if not text.strip():
            ensure_keys_loaded()
            key = os.getenv("GOVINFO_API_KEY", "")
            base = f"https://api.govinfo.gov/packages/{pkg}" + (f"/granules/{gran}" if gran else "")
            for fmt in ("htm", "xml", "pdf"):
                try:
                    r = request_with_retry("GET", f"{base}/{fmt}", params={"api_key": key},
                                           headers={"User-Agent": USER_AGENT}, timeout=_LONG)
                except httpx.HTTPError:
                    continue
                text = bytes_to_text(r.content, r.headers.get("content-type", ""), name_hint=f"x.{fmt}")
                if text.strip():
                    used = f"{base}/{fmt}"
                    break
        title = text.split("\n", 1)[0][:200] if text else native_id
        return LegalDoc(
            ref=self.ref(native_id), title=title, source=self.id, text=text,
            url=f"https://www.govinfo.gov/app/details/{pkg}" + (f"/{gran}" if gran else ""),
            language="en", metadata={"content_url": used} if used else {"no_text_reason": "no htm/pdf content"},
            ttl=30 * 24 * 3600,
        )


# ----------------------------------------------------------- CourtListener

_CL = "https://www.courtlistener.com/api/rest/v4"


def _cl_headers() -> dict[str, str]:
    ensure_keys_loaded()
    h = {"User-Agent": USER_AGENT, "Accept": "application/json"}
    tok = os.getenv("COURTLISTENER_API_TOKEN")
    if tok:
        h["Authorization"] = f"Token {tok}"
    return h


_CL_WINDOW: "deque[float]" = deque()
_CL_LOCK = threading.Lock()
_CL_PER_MINUTE = 5
_CL_MAX_WAIT = 65.0
#: Search hits, by cluster id, so a fetch needs one request instead of two.
_CL_HITS: dict[str, dict[str, Any]] = {}


def _cl_slot() -> None:
    """Wait for a slot in CourtListener's 5-requests-a-minute window.

    The free tier allows 5/min, 50/h, 125/day. Pacing here costs at most a
    minute; a 429 costs the call (and retrying inside the window spends the
    quota), which is what made the agent fall back to web pages."""
    while True:
        with _CL_LOCK:
            now = time.monotonic()
            while _CL_WINDOW and now - _CL_WINDOW[0] >= 60.0:
                _CL_WINDOW.popleft()
            if len(_CL_WINDOW) < _CL_PER_MINUTE:
                _CL_WINDOW.append(now)
                return
            wait = 60.5 - (now - _CL_WINDOW[0])
        if wait > _CL_MAX_WAIT:
            raise SourceUnavailable("CourtListener is pacing requests (5/min); try again in a minute.")
        time.sleep(max(0.5, wait))


def _cl_get(url: str, params: Optional[dict[str, Any]] = None) -> httpx.Response:
    """One CourtListener request, paced to the free tier (see ``_cl_slot``);
    a 429 that still happens (the hourly or daily cap) is reported at once."""
    _cl_slot()
    try:
        return request_with_retry("GET", url, params=params, headers=_cl_headers(), timeout=_LONG, attempts=1)
    except httpx.HTTPStatusError as exc:
        code = exc.response.status_code
        if code == 429:
            wait = exc.response.headers.get("Retry-After")
            raise SourceUnavailable(
                "CourtListener quota reached (free tier: 5 requests/min, 50/h, 125/day)"
                + (f"; retry after {wait} s" if wait else "") + ". Use another source or try later."
            ) from exc
        if code in (401, 403):
            raise SourceUnavailable("CourtListener rejected the token; check COURTLISTENER_API_TOKEN.") from exc
        raise
    except httpx.TransportError:
        return request_with_retry("GET", url, params=params, headers=_cl_headers(), timeout=_LONG, attempts=2)


class CourtListenerSource(Source):
    id = "us.courtlistener"
    probe = {"query": "Montreal Convention Article 17 accident", "kind": "case", "search_only": True}
    name = "CourtListener (Free Law Project)"
    jurisdictions = ("US",)
    kinds = ("case",)
    modes = ("keyword", "fulltext", "semantic")
    languages = ("en",)
    homepage = "https://www.courtlistener.com"
    coverage = (
        "US federal and state court opinions (millions), full-text and native semantic search "
        "(free quota 5 requests/min, 125/day — search with care); fetch an "
        "opinion by cluster id. filters: court (scotus, ca2, ca9, nysd, cal, ny …; space-separated), "
        "filed_after / filed_before (YYYY-MM-DD)."
    )

    def search(self, query, *, kind="any", mode="keyword", limit=10, filters=None):
        filters = filters or {}
        params: dict[str, Any] = {"q": query, "type": "o", "highlight": "on"}
        if mode == "semantic":
            params["semantic"] = "true"
        else:
            params["order_by"] = "score desc"
        if filters.get("court"):
            params["court"] = filters["court"]
        if filters.get("filed_after"):
            params["filed_after"] = filters["filed_after"]
        if filters.get("filed_before"):
            params["filed_before"] = filters["filed_before"]
        try:
            r = _cl_get(f"{_CL}/search/", params)
        except httpx.HTTPStatusError as exc:
            if mode == "semantic" and exc.response.status_code in (400, 404):
                params.pop("semantic", None)
                params["order_by"] = "score desc"
                r = _cl_get(f"{_CL}/search/", params)
            else:
                raise
        data = r.json()
        hits = []
        for h in (data.get("results") or [])[:limit]:
            cid = h.get("cluster_id")
            if not cid:
                m = re.search(r"/opinion/(\d+)/", h.get("absolute_url") or "")
                cid = m.group(1) if m else None
            if not cid:
                continue
            snippet = ""
            for op in h.get("opinions") or []:
                if op.get("snippet"):
                    snippet = op["snippet"]
                    break
            snippet = re.sub(r"<[^>]+>", "", snippet or h.get("snippet") or "")
            cites = h.get("citation") or []
            name = re.sub(r"</?mark>", "", h.get("caseName") or "")
            _CL_HITS[str(cid)] = {
                "case_name": name, "date": h.get("dateFiled"),
                "citation": "; ".join(cites[:3]) if isinstance(cites, list) else cites,
                "absolute_url": h.get("absolute_url"), "court": h.get("court"),
            }
            hits.append(Hit(
                ref=self.ref(str(cid)), title=name, source=self.id, kind="case",
                url="https://www.courtlistener.com" + (h.get("absolute_url") or ""),
                date=h.get("dateFiled"), citation=", ".join(cites[:2]) if isinstance(cites, list) else cites,
                snippet=" ".join(snippet.split())[:500],
                extra={"court": h.get("court"), "docket": h.get("docketNumber")},
            ))
        return hits

    def fetch(self, native_id, *, language=None):
        cid = native_id.strip()
        known = _CL_HITS.get(cid)
        if known:
            cluster = {"case_name": known.get("case_name"), "date_filed": known.get("date"),
                       "absolute_url": known.get("absolute_url"), "_citation": known.get("citation")}
        else:
            cluster = _cl_get(f"{_CL}/clusters/{cid}/").json()
        ops = _cl_get(f"{_CL}/opinions/", {"cluster": cid}).json().get("results") or []
        parts = []
        for op in ops:
            body = ""
            for field in ("html_with_citations", "html", "html_lawbox", "html_columbia", "xml_harvard", "plain_text"):
                v = op.get(field) or ""
                if v.strip():
                    body = v if field == "plain_text" else html_to_text(v, xml=field == "xml_harvard")
                    break
            if body:
                kind = op.get("type") or "opinion"
                author = op.get("author_str") or ""
                parts.append(f"[{kind}{' — ' + author if author else ''}]\n{body}")
        text = "\n\n".join(parts)
        cites = [f"{c.get('volume')} {c.get('reporter')} {c.get('page')}" for c in cluster.get("citations") or []]
        if not cites and cluster.get("_citation"):
            cites = [cluster["_citation"]]
        return LegalDoc(
            ref=self.ref(cid), title=cluster.get("case_name") or cid, source=self.id, text=text,
            url="https://www.courtlistener.com" + (cluster.get("absolute_url") or ""), kind="case", language="en",
            metadata={k: v for k, v in {"date": cluster.get("date_filed"), "citation": "; ".join(cites[:3]),
                                        "judges": cluster.get("judges")}.items() if v},
        )


SOURCES = [EcfrSource(), FederalRegisterSource(), GovinfoSource(), CourtListenerSource()]
