"""United States beyond the federal registers: the U.S. Code, New York and California statutes.

us.uscode — the Office of the Law Revision Counsel's current U.S. Code
(uscode.house.gov, "temporary format" site): one section from its page
``/us/usc/t49/s41713/``; a whole chapter from the title's current release-point
USLM XML (``xml_usc49@119-136.zip``, one member per title, 0.3–30 MB). Search
is citation-first ("49 U.S.C. 41713", "49 USC ch. 417"); GovInfo covers
full-text search.

us_ny.openleg — the New York State Senate's Open Legislation API (free key):
full-text search of the Consolidated Laws and a section (or article) by law id
and location ("GBS/349").

us_ca.codes — California's 29 codes from the Legislature's official PUBINFO
bulk file (leginfo.legislature.ca.gov itself sits behind a bot challenge). The
1.2 GB zip is never downloaded: a small index is built once per weekly release
from its central directory and two tables (about 26 MB transferred, ~20 s),
kept under ``<state_dir>/legal_docs/index/us_ca.codes/`` (~4 MB), and each
section is then one HTTP Range read of its ``.lob`` member.
"""

from __future__ import annotations

import datetime as _dt
import gzip
import io
import json
import os
import re
import struct
import threading
import time
import zipfile
import zlib
from concurrent.futures import ThreadPoolExecutor
from pathlib import Path
from typing import Any, Optional

import httpx
from lxml import etree

from ...connectors.base import USER_AGENT, ensure_keys_loaded, request_with_retry
from ..base import Hit, LegalDoc, Source, SourceUnavailable
from ..textify import html_to_text, normalize_lines

_LONG = httpx.Timeout(120.0, connect=10.0)
_UA = {"User-Agent": USER_AGENT}


def _words(s: str) -> list[str]:
    return [w for w in re.findall(r"[a-z0-9]+", (s or "").lower()) if len(w) > 2 and w not in _STOP]


_STOP = {"the", "and", "for", "of", "law", "code", "section", "sec", "act", "with", "from", "that", "this"}


# ================================================================== U.S. Code

_OLRC = "https://uscode.house.gov"
_USC_CITE = re.compile(
    r"^\s*(?:title\s+)?(?P<t>\d{1,2}[aA]?)\s*(?:U\.?\s*S\.?\s*C\.?\s*(?:A\.?)?|USC|/|\s+)\s*"
    r"(?:(?P<ch>ch(?:apter|\.)?)\s*(?P<chn>\d+[A-Za-z]?)|(?:§+|sec(?:tion|\.)?)?\s*(?P<s>\d+[A-Za-z0-9-]*))\s*$",
    re.I,
)
_USC_NATIVE = re.compile(r"^(?P<t>\d{1,2}[aA]?)/(?:chapter/(?P<ch>\d+[A-Za-z]?)|(?P<s>\d+[A-Za-z0-9-]*))$")
_USLM = "{http://xml.house.gov/schemas/uslm/1.0}"
_USLM_LEVELS = {"subsection", "paragraph", "subparagraph", "clause", "subclause", "item", "subitem", "subsubitem"}
_USLM_CONTAINERS = {"title", "subtitle", "chapter", "subchapter", "part", "subpart", "division", "subdivision"}
_USLM_SKIP = {"toc", "layout", "meta", "header"}
_RELEASE_CACHE: dict[str, Any] = {}


def _usc_parse(text: str) -> Optional[tuple[str, Optional[str], Optional[str]]]:
    """'49 U.S.C. § 41713' / '49/41713' / 'title 49 section 41713' → ('49', None, '41713');
    '49 USC ch. 417' / '49/chapter/417' → ('49', '417', None)."""
    t = re.sub(r"\btitle\s+(\d+[aA]?)\s*,?\s*(?:section|sec\.|§)\s*", r"\1 U.S.C. ", text or "", flags=re.I)
    m = _USC_NATIVE.match(t.strip())
    if m:
        return m.group("t").lower(), m.group("ch"), m.group("s")
    m = _USC_CITE.match(t)
    if not m:
        return None
    if m.group("chn"):
        return m.group("t").lower(), m.group("chn"), None
    if m.group("s"):
        return m.group("t").lower(), None, m.group("s")
    return None


def _usc_title_slug(title: str) -> str:
    t = title.lower()
    num = re.match(r"\d+", t).group(0)
    return f"{int(num):02d}{t[len(num):]}"


def _uslm_inline(el: etree._Element) -> str:
    parts: list[str] = []

    def walk(e: etree._Element) -> None:
        tag = e.tag.split("}")[-1] if isinstance(e.tag, str) else ""
        if tag in ("footnote", "note") and e is not el:
            if e.tail:
                parts.append(e.tail)
            return
        if isinstance(e.tag, str) and e.text:
            parts.append(e.text)
        for ch in e:
            walk(ch)
        if e is not el and e.tail:
            parts.append(e.tail)

    walk(el)
    return " ".join("".join(parts).split())


def _uslm_render(node: etree._Element) -> tuple[str, list[dict[str, Any]]]:
    lines: list[str] = []
    pos = 0
    sections: list[dict[str, Any]] = []

    def add(line: str) -> int:
        nonlocal pos
        line = " ".join(line.split())
        start = pos + (1 if lines else 0)
        if not line:
            return start
        lines.append(line)
        pos = start + len(line)
        return start

    def level(e: etree._Element) -> None:
        current = ""
        for ch in e:
            ctag = ch.tag.split("}")[-1] if isinstance(ch.tag, str) else ""
            if ctag in ("num", "heading"):
                current += " " + _uslm_inline(ch)
            elif ctag in ("content", "chapeau", "text", "continuation", "proviso"):
                if ctag == "continuation" and current.strip():
                    add(current)
                    current = ""
                current += " " + _uslm_inline(ch)
            elif ctag in _USLM_LEVELS:
                if current.strip():
                    add(current)
                    current = ""
                level(ch)
            elif ctag == "table" or ctag == "layout":
                if current.strip():
                    add(current)
                    current = ""
                for row in ch.iter(f"{_USLM}tr", "{http://www.w3.org/1999/xhtml}tr", "tr"):
                    add(" | ".join(_uslm_inline(c) for c in row if _uslm_inline(c)))
            elif ctag == "p":
                if current.strip():
                    add(current)
                    current = ""
                add(_uslm_inline(ch))
        if current.strip():
            add(current)

    def notes(e: etree._Element) -> None:
        for ch in e:
            ctag = ch.tag.split("}")[-1] if isinstance(ch.tag, str) else ""
            if ctag in ("heading", "p", "num"):
                add(_uslm_inline(ch))
            elif len(ch):
                notes(ch)
            else:
                add(_uslm_inline(ch))

    def walk(e: etree._Element) -> None:
        tag = e.tag.split("}")[-1] if isinstance(e.tag, str) else ""
        if not tag or tag in _USLM_SKIP:
            return
        if tag in _USLM_CONTAINERS:
            num = next((_uslm_inline(c) for c in e if c.tag == f"{_USLM}num"), "")
            head = next((_uslm_inline(c) for c in e if c.tag == f"{_USLM}heading"), "")
            line = f"{num} {head}".strip()
            if lines:
                add("")
            start = add(line) if line else pos
            for ch in e:
                if ch.tag not in (f"{_USLM}num", f"{_USLM}heading"):
                    walk(ch)
            if line:
                sections.append({"label": line[:80], "key": "", "heading": line[:120], "start": start,
                                 "end": pos, "level": "container"})
            return
        if tag == "section":
            num_el = next((c for c in e if c.tag == f"{_USLM}num"), None)
            value = (num_el.get("value") if num_el is not None else "") or ""
            num = _uslm_inline(num_el) if num_el is not None else ""
            head = next((_uslm_inline(c) for c in e if c.tag == f"{_USLM}heading"), "")
            if lines:
                add("")
            start = add(f"{num} {head}".strip())
            for ch in e:
                ctag = ch.tag.split("}")[-1] if isinstance(ch.tag, str) else ""
                if ctag in ("num", "heading"):
                    continue
                if ctag in _USLM_LEVELS:
                    level(ch)
                elif ctag in ("content", "chapeau", "continuation", "text"):
                    add(_uslm_inline(ch))
                elif ctag == "sourceCredit":
                    add(_uslm_inline(ch))
                elif ctag == "notes":
                    notes(ch)
                else:
                    t = _uslm_inline(ch)
                    if t:
                        add(t)
            label = f"§ {value}" if value else (num.rstrip(".") or "§")
            sections.append({"label": label, "key": value or num.strip("§ ."), "heading": f"{num} {head}".strip()[:120],
                             "start": start, "end": pos, "level": "article"})
            return
        for ch in e:
            walk(ch)

    walk(node)
    sections.sort(key=lambda s: (s["start"], 0 if s["level"] == "container" else 1))
    return "\n".join(lines), sections


def _current_release() -> tuple[str, dict[str, str]]:
    """Current OLRC release point ('119/136') and every title's XML zip path."""
    hit = _RELEASE_CACHE.get("rp")
    if hit and time.time() - hit[0] < 6 * 3600:
        return hit[1], hit[2]
    html = request_with_retry("GET", f"{_OLRC}/download.html", headers=_UA).text
    zips = dict(re.findall(r'href="(download/releasepoints/us/pl/\d+/\d+[a-z]?/xml_usc(\w+?)@[\w-]+\.zip)"', html))
    by_title = {v.lower(): k for k, v in zips.items()}
    m = re.search(r"releasepoints/us/pl/(\d+)/(\d+[a-z]?)/", html)
    if not by_title or not m:
        raise SourceUnavailable("uscode.house.gov download page no longer lists release-point XML files")
    rp = f"{m.group(1)}-{m.group(2)}"
    _RELEASE_CACHE["rp"] = (time.time(), rp, by_title)
    return rp, by_title


class UsCodeSource(Source):
    id = "us.uscode"
    name = "United States Code (OLRC, uscode.house.gov)"
    jurisdictions = ("US",)
    kinds = ("legislation",)
    modes = ("keyword",)
    languages = ("en",)
    homepage = _OLRC
    probe = {"query": "49 U.S.C. 41713", "kind": "legislation", "ref": "49/41713", "section": "§ 41713",
             "min_chars": 1500}
    coverage = (
        "The current U.S. Code (OLRC release point, newer than GovInfo's annual edition) with subsections "
        "and notes. Fetch-first: a section ('49/41713', from '49 U.S.C. § 41713') or a whole chapter "
        "('49/chapter/417'). Search takes a citation ('49 U.S.C. 41713', '49 USC ch. 417'); with filters "
        "title=N it also matches words against that title's section headings. Full-text search: us.govinfo."
    )

    def search(self, query, *, kind="any", mode="keyword", limit=10, filters=None):
        if kind not in ("any", "legislation"):
            return []
        filters = filters or {}
        q = " ".join((query or "").split())
        parsed = _usc_parse(q)
        if parsed:
            title, chapter, section = parsed
            if section:
                doc_title, url = self._section_head(title, section)
                if not doc_title:
                    return []
                return [Hit(ref=self.ref(f"{title}/{section}"), title=doc_title, source=self.id, url=url,
                            citation=f"{title} U.S.C. § {section}", status="current (OLRC)")]
            return [Hit(ref=self.ref(f"{title}/chapter/{chapter}"), title=f"{title} U.S.C. chapter {chapter}",
                        source=self.id, url=f"{_OLRC}/us/usc/t{title}/", citation=f"{title} U.S.C. ch. {chapter}",
                        snippet="Whole chapter from the current release-point XML.")]
        title = str(filters.get("title") or "").strip().lower()
        m = re.search(r"\btitle\s+(\d{1,2}[a]?)\b", q, re.I)
        if not title and m:
            title = m.group(1).lower()
        if not title:
            return []
        words = _words(re.sub(r"\btitle\s+\d+[a]?\b", " ", q, flags=re.I))
        if not words:
            return []
        try:
            html = request_with_retry("GET", f"{_OLRC}/us/usc/t{title}/", headers=_UA, timeout=_LONG).text
        except httpx.HTTPStatusError as exc:
            if exc.response.status_code == 404:
                return []
            raise
        scored = []
        for href, label in re.findall(r'class="section-link" href="(s[^"/]+)/">([^<]+)</a>', html):
            lw = set(_words(label))
            score = sum(1 for w in words if w in lw or any(x.startswith(w[:5]) for x in lw if len(w) >= 5))
            if score:
                scored.append((score, href[1:], " ".join(label.split())))
        scored.sort(key=lambda x: (-x[0], len(x[2])))
        return [
            Hit(ref=self.ref(f"{title}/{sec}"), title=f"{title} U.S.C. {label.replace(' — ', ' ')}", source=self.id,
                url=f"{_OLRC}/us/usc/t{title}/s{sec}/", citation=f"{title} U.S.C. § {sec}",
                snippet="Section heading match (title index).")
            for _, sec, label in scored[:limit]
        ]

    def _section_head(self, title: str, section: str) -> tuple[str, str]:
        url = f"{_OLRC}/us/usc/t{title}/s{section}/"
        try:
            r = request_with_retry("GET", url, headers=_UA)
        except httpx.HTTPStatusError as exc:
            if exc.response.status_code == 404:
                return "", url
            raise
        m = re.search(r"<title>([^<]+)</title>", r.text)
        return (" ".join(m.group(1).split()) if m else f"{title} U.S.C. § {section}"), url

    def fetch(self, native_id, *, language=None):
        nid = native_id.strip()
        parsed = _usc_parse(nid)
        if not parsed:
            raise ValueError("U.S. Code refs look like '49/41713' (section) or '49/chapter/417' (chapter)")
        title, chapter, section = parsed
        if section:
            return self._fetch_section(title, section)
        return self._fetch_chapter(title, chapter or "")

    def _fetch_section(self, title: str, section: str) -> LegalDoc:
        url = f"{_OLRC}/us/usc/t{title}/s{section}/"
        try:
            r = request_with_retry("GET", url, headers=_UA, timeout=_LONG)
        except httpx.HTTPStatusError as exc:
            if exc.response.status_code == 404:
                raise ValueError(f"{title} U.S.C. § {section} is not on uscode.house.gov") from exc
            raise
        from bs4 import BeautifulSoup

        soup = BeautifulSoup(r.text, "lxml")
        page_title = " ".join((soup.title.get_text() if soup.title else f"{title} U.S.C. § {section}").split())
        currency_el = soup.select_one(".lawsInEffect")
        currency = " ".join(currency_el.get_text(" ").split()) if currency_el else ""
        context = [" ".join(x.get_text(" ").split()) for x in soup.select(".expCite span")]
        root = soup.select_one("#content-width") or soup.select_one("#content") or soup
        for sel in (".section-nav", ".contextHeader", ".jumpTo", "nav", "script", "style"):
            for x in root.select(sel):
                x.decompose()
        text = html_to_text(str(root)).replace("\u2060", "")
        meta: dict[str, Any] = {"citation": f"{title} U.S.C. § {section}", "status": "current (OLRC)"}
        if currency:
            meta["version"] = currency
        if context:
            meta["context"] = " / ".join(c for c in context if c)
        if not text.strip():
            meta["no_text_reason"] = "the OLRC page had no statutory text"
        return LegalDoc(ref=self.ref(f"{title}/{section}"), title=page_title, source=self.id, text=text, url=url,
                        language="en", metadata=meta, ttl=7 * 24 * 3600)

    def _fetch_chapter(self, title: str, chapter: str) -> LegalDoc:
        rp, zips = _current_release()
        path = zips.get(_usc_title_slug(title))
        if not path:
            raise ValueError(f"no release-point XML for title {title}")
        url = f"{_OLRC}/{path}"
        r = request_with_retry("GET", url, headers=_UA, timeout=_LONG)
        with zipfile.ZipFile(io.BytesIO(r.content)) as z:
            member = next((n for n in z.namelist() if n.lower().endswith(".xml")), None)
            if not member:
                raise SourceUnavailable(f"{path} holds no XML")
            data = z.read(member)
        root = etree.fromstring(data, parser=etree.XMLParser(huge_tree=True, recover=True, no_network=True,
                                                            resolve_entities=False))
        want = f"/ch{chapter.lower()}"
        node = next((e for e in root.iter(f"{_USLM}chapter")
                     if (e.get("identifier") or "").lower().endswith(want)), None)
        if node is None:
            raise ValueError(f"title {title} has no chapter {chapter}")
        text, sections = _uslm_render(node)
        text = text.replace("\u2060", " ")  # same length: section offsets stay valid
        head = next((ln for ln in text.split("\n") if ln.strip()), f"Chapter {chapter}")
        return LegalDoc(
            ref=self.ref(f"{title}/chapter/{chapter}"), title=f"{title} U.S.C. {head}", source=self.id, text=text,
            url=f"{_OLRC}/us/usc/t{title}/", language="en",
            metadata={"citation": f"{title} U.S.C. ch. {chapter}", "version": f"release point {rp}",
                      "status": "current (OLRC)", "sections": sum(1 for s in sections if s["level"] == "article")},
            sections=sections or None, ttl=7 * 24 * 3600,
        )


# ====================================================== New York Open Legislation

_NY = "https://legislation.nysenate.gov/api/3"
_NY_LAWS = {
    "general business": "GBS", "gen bus": "GBS", "gbl": "GBS", "civil practice": "CVP", "cplr": "CVP",
    "general obligations": "GOB", "gol": "GOB", "penal": "PEN", "insurance": "ISC", "labor": "LAB",
    "executive": "EXC", "civil rights": "CVR", "public health": "PBH", "vehicle and traffic": "VAT",
    "tax": "TAX", "uniform commercial code": "UCC", "ucc": "UCC", "real property": "RPP",
    "domestic relations": "DOM", "business corporation": "BSC", "limited liability company": "LLC",
    "transportation": "TRA", "general construction": "GCN", "estates powers and trusts": "EPT",
}
_NY_CITE = re.compile(r"(?:§+|sec(?:tion|\.)?)\s*(?P<s>\d+[A-Za-z0-9-]*)|\b(?P<n>\d+(?:-[a-z0-9]+)?)\s*$", re.I)


def _ny_key() -> str:
    ensure_keys_loaded()
    return os.getenv("NYSENATE_API_KEY", "").strip()


def _ny_get(path: str, params: Optional[dict[str, Any]] = None) -> dict[str, Any]:
    p = dict(params or {})
    p["key"] = _ny_key()
    try:
        resp = request_with_retry("GET", f"{_NY}{path}", params=p, headers={**_UA, "Accept": "application/json"},
                                  timeout=_LONG)
        data = resp.json()
    except httpx.HTTPStatusError as exc:
        body: dict[str, Any] = {}
        try:
            body = exc.response.json()
        except ValueError:
            pass
        code = exc.response.status_code
        if code in (401, 403):
            raise SourceUnavailable(
                f"NY Open Legislation refused the key (HTTP {code}: {body.get('message') or 'invalid API key'}); "
                "check NYSENATE_API_KEY."
            ) from exc
        if code == 404 or body.get("errorCode") in (601, 602, 603):
            raise ValueError(f"NY Open Legislation: not found ({path}): {body.get('message') or ''}".strip()) from exc
        if code == 429:
            raise SourceUnavailable("NY Open Legislation rate limit reached (10 req/s, 4 concurrent per key); retry.") from exc
        raise SourceUnavailable(f"NY Open Legislation answered HTTP {code}: {body.get('message') or exc.response.text[:160]}") from exc
    except (httpx.HTTPError, ValueError) as exc:
        raise SourceUnavailable(f"NY Open Legislation did not answer ({type(exc).__name__})") from exc
    if not isinstance(data, dict) or data.get("success") is False:
        msg = data.get("message") if isinstance(data, dict) else ""
        raise SourceUnavailable(f"NY Open Legislation error: {msg or 'unexpected response'}")
    return data


def _ny_law_id(text: str) -> Optional[str]:
    low = " ".join(re.sub(r"[^a-z0-9 ]", " ", (text or "").lower()).split())
    m = re.search(r"\b([A-Z]{3})\b", text or "")
    if m and m.group(1) in set(_NY_LAWS.values()):
        return m.group(1)
    for name, lid in sorted(_NY_LAWS.items(), key=lambda kv: -len(kv[0])):
        if re.search(rf"\b{re.escape(name)}\b", low):
            return lid
    return None


def _ny_tree_find(node: dict[str, Any], loc: str) -> Optional[dict[str, Any]]:
    if (node.get("locationId") or "") == loc:
        return node
    for ch in ((node.get("documents") or {}).get("items") or []):
        hit = _ny_tree_find(ch, loc)
        if hit:
            return hit
    return None


def _ny_tree_text(node: dict[str, Any], out: list[str]) -> None:
    t = (node.get("text") or "").strip()
    if t:
        out.append(normalize_lines(t.replace("\\n", "\n")))
    elif node.get("title"):
        out.append(f"{node.get('docType') or ''} {node.get('docLevelId') or ''} {node['title']}".strip())
    for ch in ((node.get("documents") or {}).get("items") or []):
        _ny_tree_text(ch, out)


class NyOpenLegSource(Source):
    id = "us_ny.openleg"
    name = "NY State Senate Open Legislation (Consolidated Laws)"
    jurisdictions = ("US-NY",)
    kinds = ("legislation",)
    modes = ("keyword", "fulltext")
    languages = ("en",)
    auth_env = ("NYSENATE_API_KEY",)
    auth_help = "Free key: sign up at https://legislation.nysenate.gov (Open Legislation API), then set NYSENATE_API_KEY."
    homepage = "https://legislation.nysenate.gov"
    probe = {"query": "deceptive acts and practices", "kind": "legislation", "ref": "GBS/349", "section": "§ 349",
             "min_chars": 300}
    coverage = (
        "New York's Consolidated and Unconsolidated Laws, current text (updated weekly): full-text search; "
        "fetch a section or article by law id and location ('GBS/349' General Business Law § 349, "
        "'GOB/5-1501', 'GBS/A22-A' an article). Citations like 'N.Y. Gen. Bus. Law § 349' resolve directly. "
        "filters: law (GBS, CVP, GOB, PEN, ISC, TRA …)."
    )

    def search(self, query, *, kind="any", mode="keyword", limit=10, filters=None):
        if kind not in ("any", "legislation"):
            return []
        self.require_keys()
        filters = filters or {}
        q = " ".join((query or "").split())
        if not q:
            return []
        hits: list[Hit] = []
        law = str(filters.get("law") or "").upper() or None
        cited = _ny_law_id(q)
        m = _NY_CITE.search(q)
        if cited and m and (m.group("s") or m.group("n")):
            loc = m.group("s") or m.group("n")
            try:
                d = _ny_get(f"/laws/{cited}/{loc}/").get("result") or {}
            except ValueError:
                d = {}
            if d.get("locationId"):
                hits.append(self._hit(d, snippet=(d.get("text") or "")[:300]))
        path = f"/laws/{law or ''}/search".replace("//", "/") if law else "/laws/search"
        data = _ny_get(path, {"term": q, "limit": max(1, min(int(limit), 50)), "offset": 1})
        items = ((data.get("result") or {}).get("items")) or []
        for it in items:
            r = it.get("result") or it
            hl = it.get("highlights") or {}
            snip = " … ".join(re.sub(r"</?em>", "", x) for v in hl.values() for x in (v if isinstance(v, list) else [v]))
            if r.get("lawId") and r.get("locationId"):
                h = self._hit(r, snippet=snip)
                if all(x.ref != h.ref for x in hits):
                    hits.append(h)
        return hits[:limit]

    def _hit(self, r: dict[str, Any], snippet: str = "") -> Hit:
        lid, loc = r.get("lawId"), r.get("locationId")
        kind = (r.get("docType") or "").upper()
        label = f"§ {r.get('docLevelId') or loc}" if kind == "SECTION" else f"{kind.title()} {r.get('docLevelId') or loc}"
        return Hit(
            ref=self.ref(f"{lid}/{loc}"), title=f"N.Y. {r.get('lawName') or lid} Law {label} {r.get('title') or ''}".strip(),
            source=self.id, url=f"https://www.nysenate.gov/legislation/laws/{lid}/{loc}", date=r.get("activeDate"),
            citation=f"N.Y. {r.get('lawName') or lid} Law {label}", snippet=" ".join((snippet or "").split())[:500],
        )

    def fetch(self, native_id, *, language=None):
        self.require_keys()
        m = re.match(r"^([A-Z]{3})/(.+)$", native_id.strip())
        if not m:
            raise ValueError("NY refs look like 'GBS/349' (law id / location id)")
        lid, loc = m.group(1), m.group(2).strip("/")
        d = _ny_get(f"/laws/{lid}/{loc}/").get("result") or {}
        text = normalize_lines((d.get("text") or "").replace("\\n", "\n"))
        if (d.get("docType") or "").upper() != "SECTION" and len(text) < 400:
            # An article / title / part: read the law's tree and keep that node.
            tree = (_ny_get(f"/laws/{lid}", {"full": "true"}).get("result") or {}).get("documents") or {}
            node = _ny_tree_find(tree, loc)
            if node:
                out: list[str] = []
                _ny_tree_text(node, out)
                text = "\n\n".join(x for x in out if x)
        kind = (d.get("docType") or "").upper()
        label = f"§ {d.get('docLevelId') or loc}" if kind == "SECTION" else f"{kind.title()} {d.get('docLevelId') or loc}"
        meta = {k: v for k, v in {
            "law": d.get("lawName"), "version": d.get("activeDate"), "status": "current (Open Legislation)",
            "doc_type": kind or None, "citation": f"N.Y. {d.get('lawName') or lid} Law {label}",
            "parents": [f"{p.get('docType')} {p.get('docLevelId')} {p.get('title') or ''}".strip()
                        for p in d.get("parents") or [] if isinstance(p, dict)] or None,
        }.items() if v}
        if not text.strip():
            meta["no_text_reason"] = "Open Legislation returned no text for this location"
        return LegalDoc(
            ref=self.ref(f"{lid}/{loc}"), title=f"N.Y. {d.get('lawName') or lid} Law {label} {d.get('title') or ''}".strip(),
            source=self.id, text=text, url=f"https://www.nysenate.gov/legislation/laws/{lid}/{loc}",
            language="en", metadata=meta, ttl=7 * 24 * 3600,
        )


# ======================================================== California (PUBINFO)

_CA_DL = "https://downloads.leginfo.legislature.ca.gov/"
_CA_SITE = "https://leginfo.legislature.ca.gov/faces/codes_displaySection.xhtml"
_CA_ABBR = {
    "civ": "CIV", "civil": "CIV", "ccp": "CCP", "code civ proc": "CCP", "civil procedure": "CCP",
    "bus prof": "BPC", "business and professions": "BPC", "bpc": "BPC", "com": "COM", "commercial": "COM",
    "corp": "CORP", "corporations": "CORP", "evid": "EVID", "evidence": "EVID", "fam": "FAM", "family": "FAM",
    "gov": "GOV", "government": "GOV", "health saf": "HSC", "health and safety": "HSC", "ins": "INS",
    "insurance": "INS", "lab": "LAB", "labor": "LAB", "pen": "PEN", "penal": "PEN", "prob": "PROB",
    "probate": "PROB", "pub util": "PUC", "public utilities": "PUC", "rev tax": "RTC",
    "revenue and taxation": "RTC", "veh": "VEH", "vehicle": "VEH", "fin": "FIN", "financial": "FIN",
    "elec": "ELEC", "elections": "ELEC", "edc": "EDC", "education": "EDC", "pub res": "PRC",
    "public resources": "PRC", "wat": "WAT", "water": "WAT", "unemp ins": "UIC", "harb nav": "HNC",
    "harbors and navigation": "HNC", "food agric": "FAC", "fish game": "FGC", "mil vet": "MVC",
    "pub cont": "PCC", "public contract": "PCC", "sts hwy": "SHC", "streets and highways": "SHC",
    "welf inst": "WIC", "welfare and institutions": "WIC", "const": "CONS", "constitution": "CONS",
}
#: Bumped when the index layout changes, so an old index is rebuilt.
_CA_FORMAT = 2
#: How the codes are cited (Cal. Civ. Code § 1714); others use their full name.
_CA_CITE = {
    "CIV": "Civ. Code", "CCP": "Civ. Proc. Code", "BPC": "Bus. & Prof. Code", "PUC": "Pub. Util. Code",
    "GOV": "Gov. Code", "PEN": "Penal Code", "LAB": "Lab. Code", "INS": "Ins. Code", "HSC": "Health & Saf. Code",
    "VEH": "Veh. Code", "EVID": "Evid. Code", "COM": "Com. Code", "CORP": "Corp. Code", "FAM": "Fam. Code",
    "PROB": "Prob. Code", "RTC": "Rev. & Tax. Code", "PRC": "Pub. Resources Code",
    "FIN": "Fin. Code", "ELEC": "Elec. Code", "EDC": "Educ. Code", "CONS": "Const.",
}
_CA_COLS = ("num", "raw", "off", "csize", "method", "lob", "effective", "div", "title", "part", "chap", "art", "history")


def _ca_dir() -> Path:
    from ...config import load_settings

    d = Path(load_settings().state_dir) / "legal_docs" / "index" / "us_ca.codes"
    d.mkdir(parents=True, exist_ok=True)
    return d


def _ca_num(raw: str) -> str:
    return raw.strip().strip("[]").rstrip(".").strip("[]").rstrip(".")


def _natural(num: str) -> tuple:
    return tuple(int(p) if p.isdigit() else p for p in re.split(r"(\d+)", num) if p != "")


def _field(v: str) -> str:
    v = v.strip()
    if v == "NULL":
        return ""
    if v.startswith("`") and v.endswith("`"):
        v = v[1:-1]
    return v.replace("\t", " ").replace("\n", " ").strip()


_CA_DROP = {"cal", "california", "code", "codes", "section", "sec", "of", "and", "the"}


def _ca_norm(text: str) -> str:
    low = re.sub(r"[^a-z ]", " ", (text or "").lower().replace("&", " "))
    return " ".join(w for w in low.split() if w not in _CA_DROP)


_CA_ALIASES = sorted(((_ca_norm(k), v) for k, v in _CA_ABBR.items()), key=lambda kv: -len(kv[0]))


def _ca_code(text: str) -> Optional[str]:
    """'Cal. Civ. Code § 2175' → CIV; 'Bus. & Prof. Code 17200' → BPC; 'CIV 1714' → CIV."""
    m = re.match(r"^\s*([A-Z]{3,4})\b", text or "")
    if m and m.group(1) in set(_CA_ABBR.values()):
        return m.group(1)
    low = _ca_norm(text)
    for name, code in _CA_ALIASES:
        if name and re.search(rf"\b{re.escape(name)}\b", low):
            return code
    return None


class _Pubinfo:
    """The index over the current pubinfo_YYYY.zip; rebuilt when the zip changes."""

    def __init__(self) -> None:
        self._lock = threading.Lock()
        self._checked = 0.0
        self._meta: dict[str, Any] = {}
        self._codes: dict[str, dict[str, list[str]]] = {}
        self._toc: Optional[list[list[str]]] = None

    # -------------------------------------------------------------- http
    @staticmethod
    def _range(url: str, start: int, end: int) -> bytes:
        r = request_with_retry("GET", url, headers={**_UA, "Range": f"bytes={start}-{end}"}, timeout=_LONG)
        if r.status_code != 206:
            raise SourceUnavailable("downloads.leginfo.legislature.ca.gov ignored the Range request; "
                                    "refusing to download the 1.2 GB file")
        return r.content

    @staticmethod
    def _latest_zip() -> str:
        html = request_with_retry("GET", _CA_DL, headers=_UA).text
        years = sorted({int(y) for y in re.findall(r'href="pubinfo_(\d{4})\.zip"', html)})
        if not years:
            raise SourceUnavailable("downloads.leginfo.legislature.ca.gov lists no pubinfo_YYYY.zip")
        return f"pubinfo_{years[-1]}.zip"

    def _head(self, name: str) -> dict[str, Any]:
        r = request_with_retry("HEAD", _CA_DL + name, headers=_UA)
        return {"zip": name, "size": int(r.headers.get("content-length") or 0),
                "etag": r.headers.get("etag") or "", "last_modified": r.headers.get("last-modified") or ""}

    # ------------------------------------------------------------- index
    def ensure(self) -> dict[str, Any]:
        with self._lock:
            if self._meta and time.time() - self._checked < 900:
                return self._meta
            path = _ca_dir() / "meta.json"
            meta = json.loads(path.read_text()) if path.exists() else {}
            name = meta.get("zip") or self._latest_zip()
            try:
                live = self._head(name)
            except httpx.HTTPStatusError:
                name = self._latest_zip()
                live = self._head(name)
            if not meta or meta.get("format") != _CA_FORMAT or any(meta.get(k) != live[k] for k in ("zip", "size", "etag")):
                # A new session's zip may have appeared; the old one keeps being served.
                if not meta:
                    newest = self._latest_zip()
                    if newest != name:
                        live = self._head(newest)
                meta = self._build(live)
                self._codes.clear()
                self._toc = None
            self._meta = meta
            self._checked = time.time()
            return meta

    def _build(self, live: dict[str, Any]) -> dict[str, Any]:
        t0 = time.time()
        url = _CA_DL + live["zip"]
        size = live["size"]
        tail = self._range(url, max(0, size - 66000), size - 1)
        i = tail.rfind(b"PK\x05\x06")
        if i < 0:
            raise SourceUnavailable("pubinfo zip: no end-of-central-directory record")
        _, _, _, _, n, cd_size, cd_off, _ = struct.unpack("<IHHHHIIH", tail[i:i + 22])
        if cd_off == 0xFFFFFFFF or cd_size == 0xFFFFFFFF:
            j = tail.rfind(b"PK\x06\x06")
            if j < 0:
                raise SourceUnavailable("pubinfo zip: zip64 directory not found")
            cd_size, cd_off = struct.unpack("<QQ", tail[j + 40:j + 56])
        cd = self._range(url, cd_off, cd_off + cd_size - 1)
        members: dict[str, tuple[int, int, int]] = {}
        p = 0
        while p + 46 <= len(cd) and cd[p:p + 4] == b"PK\x01\x02":
            (_, _, _, _, meth, _, _, _, cs, _, nl, el, cl, _, _, _, off) = struct.unpack("<IHHHHHHIIIHHHHHII", cd[p:p + 46])
            name = cd[p + 46:p + 46 + nl].decode("utf-8", "replace")
            if name.startswith("LAW_") or name == "CODES_TBL.dat":
                members[name] = (off, cs, meth)
            p += 46 + nl + el + cl
        transferred = len(tail) + len(cd)

        def member(name: str) -> bytes:
            nonlocal transferred
            if name not in members:
                raise SourceUnavailable(f"pubinfo zip has no {name}")
            data = self._read_member(url, name, *members[name])
            transferred += members[name][1]
            return data

        tmp = _ca_dir() / f".build-{os.getpid()}-{threading.get_ident()}"
        (tmp / "sections").mkdir(parents=True, exist_ok=True)
        codes = {}
        for line in member("CODES_TBL.dat").decode("utf-8", "replace").split("\n"):
            f = line.split("\t")
            if len(f) >= 2 and _field(f[0]):
                codes[_field(f[0])] = _field(f[1]).rsplit(" - ", 1)[0]
        by_code: dict[str, list[str]] = {}
        for line in member("LAW_SECTION_TBL.dat").decode("utf-8", "replace").split("\n"):
            f = line.split("\t")
            if len(f) < 18:
                continue
            code, raw = _field(f[1]), _field(f[2])
            lob = f[14].strip().strip("`")
            if not code or not raw or lob not in members or _field(f[15]) not in ("Y", ""):
                continue
            off, cs, meth = members[lob]
            row = [_ca_num(raw), raw, str(off), str(cs), str(meth), lob, _field(f[6])[:10],
                   _field(f[8]), _field(f[9]), _field(f[10]), _field(f[11]), _field(f[12]), _field(f[13])[:400]]
            by_code.setdefault(code, []).append("\t".join(row))
        for code, rows in by_code.items():
            with gzip.open(tmp / "sections" / f"{code}.tsv.gz", "wt", encoding="utf-8") as fh:
                fh.write("\n".join(rows))
        toc_rows = []
        for line in member("LAW_TOC_TBL.dat").decode("utf-8", "replace").split("\n"):
            f = line.split("\t")
            if len(f) < 15 or _field(f[7]) not in ("Y", ""):
                continue
            toc_rows.append("\t".join([_field(f[0]), _field(f[13]), _field(f[1]), _field(f[2]), _field(f[3]),
                                       _field(f[4]), _field(f[5]), _field(f[6])[:300], _field(f[14]),
                                       _field(f[15])[:300] if len(f) > 15 else ""]))
        with gzip.open(tmp / "toc.tsv.gz", "wt", encoding="utf-8") as fh:
            fh.write("\n".join(toc_rows))
        (tmp / "codes.json").write_text(json.dumps(codes, ensure_ascii=False))
        final = _ca_dir()
        old_sections = final / "sections"
        old_sections.mkdir(exist_ok=True)
        for f in (tmp / "sections").iterdir():
            os.replace(f, old_sections / f.name)
        os.replace(tmp / "toc.tsv.gz", final / "toc.tsv.gz")
        os.replace(tmp / "codes.json", final / "codes.json")
        for leftover in (tmp / "sections",):
            try:
                leftover.rmdir()
            except OSError:
                pass
        try:
            tmp.rmdir()
        except OSError:
            pass
        meta = {**live, "format": _CA_FORMAT, "built_at": _dt.datetime.utcnow().strftime("%Y-%m-%dT%H:%M:%SZ"),
                "build_seconds": round(time.time() - t0, 1), "bytes_transferred": transferred,
                "sections": sum(len(v) for v in by_code.values()), "codes": len(by_code)}
        (final / "meta.json").write_text(json.dumps(meta, indent=1))
        return meta

    def _read_member(self, url: str, name: str, off: int, csize: int, method: int) -> bytes:
        guess = 30 + len(name.encode()) + 64
        blob = self._range(url, off, off + guess + csize - 1)
        sig, _, _, _, _, _, _, _, _, nl, el = struct.unpack("<IHHHHHIIIHH", blob[:30])
        if sig != 0x04034B50 or blob[30:30 + nl].decode("utf-8", "replace") != name:
            raise _Stale(name)
        start = 30 + nl + el
        data = blob[start:start + csize]
        if len(data) < csize:
            data += self._range(url, off + start + len(data), off + start + csize - 1)
        if method == 8:
            return zlib.decompress(data, -15)
        if method == 0:
            return data
        raise SourceUnavailable(f"pubinfo member {name} uses unsupported compression {method}")

    # ------------------------------------------------------------ lookups
    def rows(self, code: str) -> dict[str, list[str]]:
        if code not in self._codes:
            path = _ca_dir() / "sections" / f"{code}.tsv.gz"
            out: dict[str, list[str]] = {}
            if path.exists():
                with gzip.open(path, "rt", encoding="utf-8") as fh:
                    for line in fh.read().split("\n"):
                        f = line.split("\t")
                        if len(f) == len(_CA_COLS):
                            out.setdefault(f[0], f)
            self._codes[code] = out
        return self._codes[code]

    def toc(self) -> list[list[str]]:
        if self._toc is None:
            path = _ca_dir() / "toc.tsv.gz"
            with gzip.open(path, "rt", encoding="utf-8") as fh:
                self._toc = [ln.split("\t") for ln in fh.read().split("\n") if ln]
        return self._toc

    def codes(self) -> dict[str, str]:
        path = _ca_dir() / "codes.json"
        return json.loads(path.read_text()) if path.exists() else {}

    def body(self, code: str, row: list[str]) -> str:
        meta = self.ensure()
        rec = dict(zip(_CA_COLS, row))
        try:
            raw = self._read_member(_CA_DL + meta["zip"], rec["lob"], int(rec["off"]), int(rec["csize"]), int(rec["method"]))
        except _Stale:
            # The zip was replaced since the index was built: rebuild once and read again.
            with self._lock:
                self._checked, self._meta = 0.0, {}
            (_ca_dir() / "meta.json").unlink(missing_ok=True)
            meta = self.ensure()
            fresh = self.rows(code).get(row[0])
            if not fresh:
                raise ValueError(f"{code} {row[0]} is no longer in the current PUBINFO release")
            rec = dict(zip(_CA_COLS, fresh))
            raw = self._read_member(_CA_DL + meta["zip"], rec["lob"], int(rec["off"]), int(rec["csize"]), int(rec["method"]))
        return raw.decode("utf-8", "replace")


class _Stale(Exception):
    pass


_PUB = _Pubinfo()


def _caml_text(xml: str) -> str:
    xml = re.sub(r'<span class="EnSpace"\s*/>', " ", xml)
    return html_to_text(xml)


def _ca_section_lines(code: str, rec: dict[str, str], body: str) -> list[str]:
    head = f"§ {rec['num']}."
    lines = [head]
    lines += [ln for ln in _caml_text(body).split("\n") if ln.strip()]
    if rec.get("history"):
        lines.append(f"({rec['history']})")
    return lines


class CaliforniaCodesSource(Source):
    id = "us_ca.codes"
    name = "California Codes (Legislative Counsel PUBINFO)"
    jurisdictions = ("US-CA",)
    kinds = ("legislation",)
    modes = ("keyword",)
    languages = ("en",)
    homepage = "https://leginfo.legislature.ca.gov"
    probe = {"query": "Cal. Civ. Code § 2175", "kind": "legislation", "ref": "CIV/2175", "section": "§ 2175",
             "min_chars": 150}
    coverage = (
        "California's 29 codes and the Constitution, current text from the Legislature's official weekly "
        "PUBINFO release. Search by citation ('Cal. Civ. Code § 2175', 'CIV 1714', 'Bus. & Prof. Code 17200') "
        "or by words in the codes' division/part/chapter/article headings ('common carriers', 'carriage of "
        "persons'); no full-text search. Fetch a section ('CIV/2175') or a whole heading's sections "
        "('CIV/toc/8.4.80.5.1', up to 60 sections). filters: code (CIV, CCP, BPC, PUC …)."
    )

    def search(self, query, *, kind="any", mode="keyword", limit=10, filters=None):
        if kind not in ("any", "legislation"):
            return []
        filters = filters or {}
        q = " ".join((query or "").split())
        if not q:
            return []
        _PUB.ensure()
        code = str(filters.get("code") or "").upper() or _ca_code(q)
        hits: list[Hit] = []
        m = re.search(r"(?:§+|sec(?:tion|\.)?)?\s*(\d+(?:\.\d+)*)\s*\.?\s*$", q, re.I)
        if code and m:
            num = m.group(1)
            row = _PUB.rows(code).get(num)
            if row:
                rec = dict(zip(_CA_COLS, row))
                body = ""
                try:
                    body = " ".join(_caml_text(_PUB.body(code, row)).split())
                except SourceUnavailable:
                    body = ""
                hits.append(self._section_hit(code, rec, body[:400]))
        if code and m:
            return hits  # a citation: the section or nothing
        words = _words(q)
        codes_named = _PUB.codes()
        if words and len(hits) < limit:
            scored = []
            for r in _PUB.toc():
                if len(r) < 10 or (code and r[0] != code):
                    continue
                hw = set(_words(r[7]))
                score = sum(1 for w in words if w in hw or any(x.startswith(w[:5]) for x in hw if len(w) >= 5))
                if score:
                    scored.append((score / max(1, len(words)), r[8] == "Y", -len(r[7]), r))
            scored.sort(key=lambda x: (-x[0], not x[1], -x[2]))
            for score, _, _, r in scored:
                if score < 0.5 or len(hits) >= limit:
                    break
                hits.append(Hit(
                    ref=self.ref(f"{r[0]}/toc/{r[1]}"), title=f"{codes_named.get(r[0], r[0])} — {r[7]}",
                    source=self.id, kind="legislation",
                    url=f"https://leginfo.legislature.ca.gov/faces/codes_displayText.xhtml?lawCode={r[0]}"
                        + "".join(f"&{k}={v}" for k, v in zip(("division", "title", "part", "chapter", "article"), r[2:7]) if v),
                    citation=f"Cal. {_CA_CITE.get(r[0], codes_named.get(r[0], r[0]))}, {r[7].split(' [')[0]}",
                    snippet="Heading match; fetch reads the sections under it.",
                ))
        return hits[:limit]

    def _section_hit(self, code: str, rec: dict[str, str], snippet: str) -> Hit:
        name = _PUB.codes().get(code, code)
        return Hit(
            ref=self.ref(f"{code}/{rec['num']}"), title=f"{name} § {rec['num']}", source=self.id,
            url=f"{_CA_SITE}?lawCode={code}&sectionNum={rec['num']}.", date=rec.get("effective") or None,
            citation=f"Cal. {_CA_CITE.get(code, name)} § {rec['num']}", snippet=snippet, status="current (PUBINFO)",
            extra={"history": rec["history"]} if rec.get("history") else {},
        )

    def fetch(self, native_id, *, language=None):
        nid = native_id.strip()
        meta_idx = _PUB.ensure()
        m = re.match(r"^([A-Z]{3,4})/toc/([\d.]+)$", nid)
        if m:
            return self._fetch_toc(m.group(1), m.group(2), meta_idx)
        m = re.match(r"^([A-Z]{3,4})/(\d+(?:\.\d+)*)\.?$", nid)
        if not m:
            code = _ca_code(nid)
            mm = re.search(r"(\d+(?:\.\d+)*)\s*\.?\s*$", nid)
            if not (code and mm):
                raise ValueError("California refs look like 'CIV/2175' (section) or 'CIV/toc/8.4.80.5.1' (heading)")
            m = re.match(r"^([A-Z]{3,4})/(.+)$", f"{code}/{mm.group(1)}")
        code, num = m.group(1), m.group(2)
        row = _PUB.rows(code).get(num)
        if not row:
            raise ValueError(f"no section {code} {num} in the current PUBINFO release")
        rec = dict(zip(_CA_COLS, row))
        text = "\n".join(_ca_section_lines(code, rec, _PUB.body(code, row)))
        name = _PUB.codes().get(code, code)
        place = [f"{k} {rec[c]}".strip() for k, c in (("Division", "div"), ("Title", "title"), ("Part", "part"),
                                                        ("Chapter", "chap"), ("Article", "art")) if rec.get(c)]
        return LegalDoc(
            ref=self.ref(f"{code}/{num}"), title=f"{name} § {num}", source=self.id, text=text,
            url=f"{_CA_SITE}?lawCode={code}&sectionNum={num}.", language="en",
            metadata={k: v for k, v in {
                "citation": f"Cal. {_CA_CITE.get(code, name)} § {num}", "status": "current (PUBINFO)", "effective": rec.get("effective"),
                "history": rec.get("history"), "location": ", ".join(place) or None,
                "version": f"{meta_idx.get('zip')} of {meta_idx.get('last_modified')}",
            }.items() if v},
            ttl=7 * 24 * 3600,
        )

    def _fetch_toc(self, code: str, path: str, meta_idx: dict[str, Any]) -> LegalDoc:
        node = next((r for r in _PUB.toc() if r[0] == code and r[1] == path), None)
        if node is None:
            raise ValueError(f"no heading {path} in {code}")
        want = dict(zip(("div", "title", "part", "chap", "art"), node[2:7]))
        rows = [r for r in _PUB.rows(code).values()
                if all(not v or dict(zip(_CA_COLS, r))[k] == v for k, v in want.items())]
        rows.sort(key=lambda r: _natural(r[0]))
        total = len(rows)
        rows = rows[:60]
        with ThreadPoolExecutor(max_workers=4) as pool:
            bodies = list(pool.map(lambda r: _PUB.body(code, r), rows))
        lines = [node[7], ""]
        sections = []
        pos = len(node[7]) + 1
        for r, body in zip(rows, bodies):
            rec = dict(zip(_CA_COLS, r))
            sec_lines = _ca_section_lines(code, rec, body)
            start = pos + 1
            chunk = "\n".join(sec_lines)
            lines.append(chunk)
            lines.append("")
            sections.append({"label": f"§ {rec['num']}", "key": rec["num"], "heading": sec_lines[0],
                             "start": start, "end": start + len(chunk), "level": "article"})
            pos = start + len(chunk) + 1
        text = "\n".join(lines).rstrip()
        name = _PUB.codes().get(code, code)
        meta: dict[str, Any] = {"citation": f"Cal. {_CA_CITE.get(code, name)}, {node[7].split(' [')[0]}", "status": "current (PUBINFO)",
                                "sections": total, "version": f"{meta_idx.get('zip')} of {meta_idx.get('last_modified')}"}
        if node[9]:
            meta["note"] = node[9]
        if total > len(rows):
            meta["truncated"] = f"first {len(rows)} of {total} sections; fetch a narrower heading or single sections"
        return LegalDoc(
            ref=self.ref(f"{code}/toc/{path}"), title=f"{name} — {node[7]}", source=self.id, text=text,
            url=f"https://leginfo.legislature.ca.gov/faces/codes_displayText.xhtml?lawCode={code}", language="en",
            metadata=meta, sections=[{"label": node[7][:80], "key": "", "heading": node[7][:120], "start": 0,
                                      "end": len(text), "level": "container"}] + sections,
            ttl=7 * 24 * 3600,
        )


# NyOpenLegSource needs NYSENATE_API_KEY; the user decided not to get one
# (2026-11-04). Re-add it here if a key is ever obtained.
SOURCES = [UsCodeSource(), CaliforniaCodesSource()]
