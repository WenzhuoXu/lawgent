"""Australia: the Federal Register of Legislation and Victorian legislation.

au.frl — the Commonwealth's official register (legislation.gov.au) through its
public OData API (``api.prod.legislation.gov.au/v1``, keyless, CC BY 4.0):
title search (``$filter=contains(name,…)``), full-text search (the
undocumented ``titles/search(criteria='and(text("…",nameAndText,all),
pointintime(Latest))')`` form the site's own front end uses), the in-force
status of every title, and the latest (or an as-at) compilation as the
register's EPUB, whose XHTML marks every heading by class (``ActHead5`` +
``CharSectno`` = a section), so sections are cut from structure.

au.vic — legislation.vic.gov.au. Title search and version/status come from the
public site's own search and page APIs (www host, which robots.txt allows).
The authorised PDF/DOCX files live on ``content.legislation.vic.gov.au``,
whose robots.txt is ``User-agent: * / Disallow: /`` (and the www host
disallows ``/sites/default/files/*.pdf|*.doc``), so the text is not
downloaded: fetch returns the version, status and the file links.
"""

from __future__ import annotations

import base64
import io
import json
import re
import zipfile
from typing import Any, Optional
from urllib.parse import quote

import httpx

from ...connectors.base import USER_AGENT, request_with_retry
from ..base import Hit, LegalDoc, Source, SourceUnavailable
from ..textify import bytes_to_text

_FRL_API = "https://api.prod.legislation.gov.au/v1"
_FRL_SITE = "https://www.legislation.gov.au"
_LONG = httpx.Timeout(90.0, connect=10.0)
_HEADERS = {"User-Agent": USER_AGENT, "Accept": "application/json"}
_FRL_ID = re.compile(r"^[CF]\d{4}[A-Z]\d{5}$", re.I)
_STATUS = {"InForce": "In force", "Repealed": "Repealed", "Ceased": "Ceased", "NotInForce": "Not in force",
           "Revoked": "Revoked", "Lapsed": "Lapsed", "Spent": "Spent", "NeverEffective": "Never effective"}
_STOP = {"the", "of", "and", "for", "to", "in", "a", "an", "on", "au", "australia", "australian", "cth", "commonwealth"}
_SELECT = "id,name,status,collection,isPrincipal,isInForce,year,number,makingDate,hasCommencedUnincorporatedAmendments"


def _ws(s: str) -> str:
    return " ".join((s or "").replace("\xa0", " ").split())


def _odata_str(s: str) -> str:
    return s.replace("'", "''")


def _frl_get(path: str, *, timeout: Optional[httpx.Timeout] = None, accept: str = "application/json") -> httpx.Response:
    # documents/find answers JSON metadata to "Accept: application/json" and
    # the file itself to "*/*".
    try:
        return request_with_retry("GET", f"{_FRL_API}{path}", headers={**_HEADERS, "Accept": accept},
                                  timeout=timeout or httpx.Timeout(45.0, connect=10.0))
    except httpx.HTTPStatusError as exc:
        code = exc.response.status_code
        if code in (400, 404):
            raise
        raise SourceUnavailable(f"Federal Register of Legislation API answered {code}; try again shortly") from exc
    except httpx.TransportError as exc:
        raise SourceUnavailable(f"Federal Register of Legislation API unreachable ({type(exc).__name__})") from exc


def _frl_json(path: str) -> dict[str, Any]:
    return _frl_get(path).json()


def _title_hit(src: Source, t: dict[str, Any], *, how: str = "") -> Hit:
    tid = t.get("id") or ""
    status = _STATUS.get(t.get("status") or "", t.get("status"))
    extra = {k: v for k, v in {
        "collection": t.get("collection"),
        "principal": t.get("isPrincipal"),
        "unincorporated_amendments": True if t.get("hasCommencedUnincorporatedAmendments") else None,
        "matched": how or None,
    }.items() if v is not None}
    num = f"No. {t['number']}, {t['year']}" if t.get("number") and t.get("year") else None
    return Hit(
        ref=src.ref(tid), title=t.get("name") or tid, source=src.id, kind="legislation",
        url=f"{_FRL_SITE}/{tid}/latest/text", date=(t.get("makingDate") or "")[:10] or None,
        status=status, citation=num, extra=extra,
    )


def _norm(s: str) -> str:
    return re.sub(r"[^a-z0-9 ]", "", _ws(s.lower().replace("’", "'"))).strip()


# --------------------------------------------------------------- EPUB → text

class _Lines:
    def __init__(self) -> None:
        self.lines: list[str] = []
        self.pos = 0
        self.marks: list[tuple[int, str, str, str, str, str]] = []
        self.units: list[tuple[str, Optional[str]]] = []

    def line(self, s: str) -> None:
        s = _ws(s)
        if s:
            self.lines.append(s)
            self.pos += len(s) + 1

    def mark(self, label: str, key: str, heading: str, level: str, schedule: Optional[str] = None,
             caption: str = "") -> None:
        self.marks.append((self.pos, label, key, heading[:160], level, caption))
        if level != "container":
            self.units.append((key, schedule))

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


_ARTICLE_LINE = re.compile(r"^Articles?\s+(\d+[A-Za-z]{0,2})(?:\s*[—–-]\s*(.{1,140}))?$")


def _epub_html_files(data: bytes) -> list[str]:
    z = zipfile.ZipFile(io.BytesIO(data))
    names = z.namelist()
    order: list[str] = []
    try:
        opf_name = next(n for n in names if n.endswith(".opf"))
        opf = z.read(opf_name).decode("utf-8", "replace")
        base = opf_name.rsplit("/", 1)[0] + "/" if "/" in opf_name else ""
        hrefs = dict(re.findall(r'<item\s+id="([^"]+)"\s+href="([^"]+)"', opf))
        for idref in re.findall(r'<itemref\s+idref="([^"]+)"', opf):
            href = hrefs.get(idref)
            if href and (base + href) in names:
                order.append(base + href)
    except StopIteration:
        pass
    if not order:
        order = sorted(n for n in names if n.endswith((".html", ".xhtml")))
    return [z.read(n).decode("utf-8", "replace") for n in order]


def _render_frl(htmls: list[str], *, prefix: str = "s", only_schedule: Optional[str] = None
                ) -> tuple[str, list[dict[str, Any]], list[str]]:
    """FRL compilation XHTML → text, sections, schedules whose labels repeat.

    Convention articles scheduled to an Act keep their printed label
    ('Article 17', matched as written) but carry the schedule in their key
    ('SCH1A.ART17', shown as [Sch1A.Art17]): several schedules print the same
    labels, and a bare '17' must stay section 17. ``only_schedule`` renders
    one schedule alone, where 'Article 17' is unique."""
    import warnings

    from bs4 import BeautifulSoup

    try:
        from bs4 import XMLParsedAsHTMLWarning
    except ImportError:  # pragma: no cover
        XMLParsedAsHTMLWarning = Warning  # type: ignore[assignment,misc]
    out = _Lines()
    state: dict[str, Any] = {"schedule": None, "endnotes": False}
    qualify = only_schedule is None
    want = (only_schedule or "").upper()

    def keep() -> bool:
        return only_schedule is None or (state["schedule"] or "").upper() == want

    def heading_level(tag: Any) -> tuple[Optional[int], str]:
        cls = " ".join(tag.get("class") or [])
        m = re.search(r"\bActHead(\d)\b", cls)
        if m:
            return int(m.group(1)), cls
        m = re.match(r"^h([1-6])$", tag.name or "")
        if m and not cls:
            return int(m.group(1)), cls
        return None, cls

    def block(tag: Any) -> None:
        name = tag.name
        if name == "table":
            for tr in tag.find_all("tr"):
                cells = [_ws(td.get_text("")) for td in tr.find_all(["td", "th"], recursive=False)]
                row = " | ".join(c for c in cells if c)
                if row:
                    out.line(row)
            return
        if name not in ("p", "h1", "h2", "h3", "h4", "h5", "h6"):
            for c in tag.children:
                if getattr(c, "name", None):
                    block(c)
            return
        level, cls = heading_level(tag)
        if re.search(r"\b(TOC\d|TofSects\w*|Header|Footer)\b", cls):
            return
        for img in tag.find_all("img"):
            if img.get("alt"):
                img.replace_with(f" {img['alt']} ")
        text = _ws(tag.get_text(""))
        if not text:
            return
        if "ENotesHeading1" in cls:
            state["schedule"], state["endnotes"] = None, True
            if keep():
                out.mark("Endnotes", "ENDNOTES", text, "article")
                out.line(text)
            return
        if state["endnotes"]:
            if keep():
                out.line(text)
            return
        if level is not None and level != 5:
            m = re.match(r"^Schedule\s+(\w+)", text)
            if m and level in (1, 6):
                state["schedule"] = m.group(1)
            elif level == 1 and not text.lower().startswith("schedules"):
                state["schedule"] = None
        if not keep():
            return
        if level == 5:
            nums = [_ws(s.get_text("")) for s in tag.find_all("span", class_="CharSectno")]
            num = "".join(nums).replace("\u2011", "-").strip()
            if num:
                rest = _ws(text[text.find(nums[-1]) + len(nums[-1]):] if nums[-1] in text else text)
                sch = state["schedule"]
                if sch is not None:
                    label, key = f"Sch{sch}.{num}", f"SCH{sch}.{num}".upper()
                else:
                    label, key = f"{prefix} {num}", num.upper()
                out.mark(label, key, f"{label} {rest}".strip(), "article", sch)
                out.line(f"{label} {rest}".strip())
                return
        if level is not None and level != 5:
            out.mark(text, "", text, "container")
            out.line(text)
            return
        if state["schedule"] is not None:
            m = _ARTICLE_LINE.match(text)
            if m:
                heading = f"Article {m.group(1)} {m.group(2) or ''}".strip()
                caption = (m.group(2) or "").strip()
                if qualify:
                    # printed label, schedule-qualified key: see _render_frl
                    qid = f"Sch{state['schedule']}.Art{m.group(1)}"
                    out.mark(f"Article {m.group(1)}", qid.upper(), heading, "article", state["schedule"],
                             f"{caption} [{qid}]".strip())
                    out.line(f"{heading} [{qid}]")
                else:
                    out.mark(f"Article {m.group(1)}", m.group(1).upper(), heading, "article", state["schedule"], caption)
                    out.line(heading)
                return
        out.line(text)

    for html in htmls:
        with warnings.catch_warnings():
            warnings.simplefilter("ignore", XMLParsedAsHTMLWarning)
            soup = BeautifulSoup(html, "lxml")
        state["endnotes"] = False
        for t in soup.find_all(["script", "style"]):
            t.decompose()
        body = soup.body or soup
        for c in body.children:
            if getattr(c, "name", None):
                block(c)
    text = out.text()
    first: dict[str, Optional[str]] = {}
    repeats: list[str] = []
    for key, sch in out.units:
        if key not in first:
            first[key] = sch
        elif first[key] != sch and sch is not None and sch not in repeats:
            repeats.append(sch)
    return text, out.sections(), repeats


class FrlSource(Source):
    id = "au.frl"
    probe = {"query": "Civil Aviation (Carriers' Liability) Act 1959", "kind": "legislation", "ref": "C1959A00002",
             "section": "12", "min_chars": 20000}
    name = "Federal Register of Legislation (Commonwealth of Australia)"
    jurisdictions = ("AU",)
    kinds = ("legislation",)
    modes = ("keyword", "fulltext")
    languages = ("en",)
    homepage = _FRL_SITE
    coverage = (
        "Every Commonwealth Act, legislative instrument (regulations, CASA instruments, Civil Aviation Safety "
        "Regulations), notifiable instrument and gazette, with in-force status. Ids are FRL title ids "
        "('C1959A00002' = Civil Aviation (Carriers' Liability) Act 1959, 'F2019L01172'); an as-at version "
        "'C1959A00002/2015-01-01'. Fetch reads the latest compilation (or as-at) with its compilation number; "
        "sections 's 12', '12A', schedule clauses 'Sch1.3', scheduled convention articles 'Article 17' (another "
        "schedule's: 'Sch1.Art17'; or one schedule alone 'C1959A00002/Sch1A'), "
        "'Endnotes' (legislation history). keyword = title words; fulltext = inside the text (quote a phrase). "
        "filters: collection (Act|LegislativeInstrument|NotifiableInstrument), status (in_force|repealed), year."
    )

    # ---------------------------------------------------------- search
    def search(self, query, *, kind="any", mode="keyword", limit=10, filters=None):
        if kind not in ("any", "legislation"):
            return []
        filters = filters or {}
        limit = max(1, min(int(limit or 10), 30))
        q = query.strip()
        if _FRL_ID.match(q):
            try:
                t = _frl_json(f"/titles('{q.upper()}')?$select={_SELECT}")
                return [_title_hit(self, t, how="id")]
            except httpx.HTTPStatusError:
                pass
        if mode == "fulltext":
            return self._fulltext(q, limit, filters)
        words = [w for w in re.findall(r"[A-Za-z0-9]+", q.replace("’", "'")) if w.lower() not in _STOP]
        words = [w for w in words if not (len(w) == 1 and w.lower() == "s")][:7]
        if not words:
            return []
        clauses = [f"contains(name,'{_odata_str(w)}')" for w in words]
        coll = filters.get("collection")
        if coll:
            clauses.append(f"collection eq '{_odata_str(str(coll))}'")
        status = str(filters.get("status") or "").lower()
        if status in ("in_force", "inforce", "current"):
            clauses.append("isInForce eq true")
        elif status in ("repealed", "not_in_force"):
            clauses.append("isInForce eq false")
        if filters.get("year"):
            clauses.append(f"year eq {int(filters['year'])}")
        flt = " and ".join(clauses)
        path = f"/titles?$top=60&$count=true&$select={_SELECT}&$filter={quote(flt, safe='')}"
        try:
            rows = _frl_json(path).get("value") or []
        except httpx.HTTPStatusError as exc:
            raise SourceUnavailable(f"FRL title search rejected the query ({exc.response.status_code})") from exc
        if not rows:
            return self._fulltext(q, limit, filters)
        nq = _norm(" ".join(re.findall(r"[A-Za-z0-9']+", q)))
        coll_rank = {"Act": 0, "LegislativeInstrument": 1, "NotifiableInstrument": 2}

        def rank(t: dict[str, Any]) -> tuple:
            n = _norm(t.get("name") or "")
            exact = 0 if n == nq else (1 if n.startswith(nq) else 2)
            return (exact, 0 if t.get("isPrincipal") else 1, coll_rank.get(t.get("collection") or "", 3),
                    0 if t.get("isInForce") else 1, len(n))

        rows.sort(key=rank)
        return [_title_hit(self, t, how="title") for t in rows[:limit]]

    def _fulltext(self, q: str, limit: int, filters: dict[str, Any]) -> list[Hit]:
        phrase = q.startswith('"') and q.endswith('"') and len(q) > 2
        text = q.strip('"').replace('"', "")
        match = "contains" if phrase or len(text.split()) == 1 else "all"
        crit = f'and(text("{text}",nameAndText,{match}),pointintime(Latest))'
        path = (f"/titles/search(criteria='{quote(_odata_str(crit), safe='')}')?$top={min(60, limit * 3)}"
                f"&$count=true&$select={_SELECT}")
        try:
            rows = _frl_json(path).get("value") or []
        except httpx.HTTPStatusError as exc:
            raise SourceUnavailable(f"FRL full-text search rejected the query ({exc.response.status_code}: "
                                    f"{exc.response.text[:120]})") from exc
        status = str(filters.get("status") or "").lower()
        coll = filters.get("collection")
        out = []
        for t in rows:
            if status in ("in_force", "inforce", "current") and not t.get("isInForce"):
                continue
            if status in ("repealed", "not_in_force") and t.get("isInForce"):
                continue
            if coll and t.get("collection") != coll:
                continue
            out.append(_title_hit(self, t, how="text"))
        return out[:limit]

    # ----------------------------------------------------------- fetch
    def fetch(self, native_id, *, language=None):
        raw = native_id.strip().strip("/")
        parts = raw.split("/")
        tid = parts[0].upper()
        asat = next((p for p in parts[1:] if re.fullmatch(r"\d{4}-\d{2}-\d{2}", p)), "")
        sched = next((re.sub(r"^(?:Sch(?:edule)?)\s*", "", p, flags=re.I) for p in parts[1:]
                      if re.match(r"^Sch(?:edule)?\s*\w+$", p, re.I)), None)
        if not _FRL_ID.match(tid) or len(parts) > 1 + bool(asat) + bool(sched):
            raise ValueError("FRL ids look like 'C1959A00002' (an Act) or 'F2019L01172' (an instrument), "
                             "optionally '/YYYY-MM-DD' (as at) and '/Sch1A' (one schedule)")
        try:
            title = _frl_json(f"/titles('{tid}')?$select={_SELECT}")
        except httpx.HTTPStatusError as exc:
            raise ValueError(f"no FRL title {tid!r}") from exc
        sel = f"asAt={asat}T00:00:00" if asat else "asAtSpecification='Latest'"
        version: dict[str, Any] = {}
        try:
            version = _frl_json(f"/versions/find(titleId='{tid}',{sel})")
        except httpx.HTTPStatusError:
            version = {}
        dsel = f"asat={asat}" if asat else "asatspecification='Latest'"
        text, sections, repeats, fmt = "", [], [], ""
        errors = []
        for f in ("Epub", "Word", "Pdf"):
            path = (f"/documents/find(titleid='{tid}',{dsel},type='Primary',format='{f}',"
                    "uniqueTypeNumber=0,volumeNumber=0,rectificationVersionNumber=0)")
            try:
                r = _frl_get(path, timeout=_LONG, accept="*/*")
            except httpx.HTTPStatusError as exc:
                errors.append(f"{f}: {exc.response.status_code}")
                continue
            data = r.content
            if "json" in (r.headers.get("content-type") or ""):
                # The API often answers with the document's record, the file
                # itself base64-encoded in "bytes".
                try:
                    data = base64.b64decode((r.json() or {}).get("bytes") or "")
                except (ValueError, TypeError):
                    data = b""
                if not data:
                    errors.append(f"{f}: metadata without the file")
                    continue
            if f == "Epub":
                try:
                    htmls = _epub_html_files(data)
                    pre = "s"
                    if title.get("collection") != "Act":
                        joined = " ".join(htmls)[:400000]
                        if len(re.findall(r"\bregulation \d", joined)) > len(re.findall(r"\bsection \d", joined)):
                            pre = "reg"
                    text, sections, repeats = _render_frl(htmls, prefix=pre, only_schedule=sched)
                    if sched:
                        # a schedule is cut from the EPUB's structure; Word/PDF cannot be cut
                        fmt = f if text.strip() else ""
                        if not text.strip():
                            errors.append(f"no Schedule {sched} in this document")
                        break
                except (zipfile.BadZipFile, ValueError) as exc:
                    errors.append(f"Epub: {exc}")
                    continue
            else:
                try:
                    text = bytes_to_text(data, "",
                                         name_hint=f"x.{'docx' if f == 'Word' else 'pdf'}")
                except Exception as exc:  # noqa: BLE001 — try the next format
                    errors.append(f"{f}: {type(exc).__name__}")
                    continue
                sections = []
            if text.strip():
                fmt = f
                break
        status = _STATUS.get(title.get("status") or "", title.get("status"))
        meta: dict[str, Any] = {
            "status": status,
            "collection": title.get("collection"),
            "made": (title.get("makingDate") or "")[:10] or None,
            "version": (f"compilation No. {version['compilationNumber']}" if version.get("compilationNumber")
                        else ("as made" if version.get("registerId") == tid
                              else (f"version registered {(version.get('registeredAt') or '')[:10]}" if version else None))),
            "version_end": (version.get("end") or "")[:10] or None,
            "register_id": version.get("registerId"),
            "effective": (version.get("start") or "")[:10] or None,
            "is_latest": version.get("isLatest"),
            "text_format": fmt or None,
        }
        if version.get("hasUnincorporatedAmendments") or title.get("hasCommencedUnincorporatedAmendments"):
            meta["unincorporated_amendments"] = (
                "The register flags amendments that have commenced but are not yet incorporated into this "
                "compilation; check the amending Acts before relying on the wording."
            )
        reasons = [_ws(re.sub(r"\]\(/[^)]+\)", "]", x.get("markdown") or "")).replace("[", "").replace("]", "")
                   for x in (version.get("reasons") or []) if x.get("markdown")]
        if reasons:
            meta["this_version_because"] = reasons[:8]
        treaty = list(dict.fromkeys(m.group(1) for s_ in sections
                                    for m in [re.match(r"^SCH(\w+)\.ART", s_.get("key") or "")] if m))
        if treaty:
            meta["scheduled_conventions"] = (
                "The convention article labels ('Article 17') repeat across the schedules and section= returns the "
                f"first (Schedule {treaty[0]}); another schedule's article is asked for by its bracketed id "
                "('Sch1.Art17'), or read one schedule on its own: "
                + ", ".join(f"au.frl:{tid}{'/' + asat if asat else ''}/Sch{x}" for x in treaty[:6]) + "."
            )
        if repeats:
            meta["repeated_labels"] = (
                f"Some labels in Schedule(s) {', '.join(repeats)} repeat labels used earlier; section= returns the "
                "first. Use query= or offset to reach the later one."
            )
        meta = {k: v for k, v in meta.items() if v not in (None, "", [])}
        if not text.strip():
            meta["no_text_reason"] = f"the register served no readable document ({'; '.join(errors) or 'empty'})"
        url = f"{_FRL_SITE}/{tid}/" + (asat if asat else "latest") + "/text"
        name = title.get("name") or tid
        if sched:
            name = f"{name} — Schedule {sched}"
        return LegalDoc(
            ref=self.ref("/".join([tid] + ([asat] if asat else []) + ([f"Sch{sched}"] if sched else []))), title=name, source=self.id, text=text,
            url=url, kind="legislation", language="en", metadata=meta, sections=sections or None,
        )


# ================================================================= Victoria

_VIC = "https://www.legislation.vic.gov.au"
_VIC_ES = f"{_VIC}/api/tide/elasticsearch/content-legislation-vic-gov-au__production__sapi_node/_search"
_VIC_TYPES = {
    "act": ["act_in_force"], "acts": ["act_in_force"], "sr": ["statutory_rule_in_force"],
    "regulation": ["statutory_rule_in_force"], "statutory_rule": ["statutory_rule_in_force"],
}
_VIC_SOURCE = ["title", "url", "type", "field_legislation_status", "field_act_sr_year", "field_act_sr_number",
               "field_in_force_version_number", "field_in_force_effective_date", "field_legislation_type",
               "field_act_sr_status", "legislation_type"]


class VicLegislationSource(Source):
    id = "au.vic"
    probe = {"query": "Transport Integration Act", "kind": "legislation", "search_only": True}
    name = "Victorian Legislation (legislation.vic.gov.au)"
    jurisdictions = ("AU-VIC",)
    kinds = ("legislation",)
    modes = ("keyword",)
    languages = ("en",)
    homepage = _VIC
    coverage = (
        "Victorian Acts and statutory rules in force (and repealed), title search with version number, "
        "effective date and status. Ids are the site path ('in-force/acts/transport-integration-act-2010'). "
        "TEXT IS NOT DOWNLOADED: the authorised PDF/DOCX are on content.legislation.vic.gov.au, whose "
        "robots.txt disallows all agents; fetch returns the version list and the file links for a person to "
        "open. filters: type (act|statutory_rule), status (in_force|repealed)."
    )

    def search(self, query, *, kind="any", mode="keyword", limit=10, filters=None):
        if kind not in ("any", "legislation"):
            return []
        filters = filters or {}
        limit = max(1, min(int(limit or 10), 30))
        types = _VIC_TYPES.get(str(filters.get("type") or "").lower())
        status = str(filters.get("status") or "").lower()
        if not types:
            if status in ("repealed", "revoked"):
                types = ["act_repealed", "statutory_rule_repealed", "act_revoked", "statutory_rule_revoked"]
            else:
                types = ["act_in_force", "statutory_rule_in_force"]
        body = {
            "size": limit,
            "query": {"bool": {
                "must": [{"match": {"title": {"query": query, "operator": "and"}}}],
                "should": [{"match_phrase": {"title": {"query": query, "boost": 5}}}],
                "filter": [{"terms": {"type": types}}],
            }},
            "_source": _VIC_SOURCE,
        }
        rows = self._es(body)
        if not rows:
            body["query"]["bool"]["must"] = [{"match": {"title": {"query": query}}}]
            rows = self._es(body)
        hits: list[Hit] = []
        for h in rows:
            s = h.get("_source") or {}
            title = (s.get("title") or [""])[0]
            url = (s.get("url") or [""])[0].replace("/site-6", "")
            if not url:
                continue
            native = url.strip("/")
            ver = (s.get("field_in_force_version_number") or [None])[0]
            eff = (s.get("field_in_force_effective_date") or [None])[0]
            typ = (s.get("type") or [""])[0]
            if "/repealed-revoked/" in url or "repealed" in typ or "revoked" in typ:
                st = "Repealed or revoked"
            elif typ.endswith("in_force"):
                st = "In force"
            else:
                st = typ or None
            year = (s.get("field_act_sr_year") or [None])[0]
            num = (s.get("field_act_sr_number") or [None])[0]
            hits.append(Hit(
                ref=self.ref(native), title=title, source=self.id, kind="legislation", url=f"{_VIC}{url}",
                date=(eff or "")[:10] or None, status=st,
                citation=(f"No. {num} of {year}" if num and year else None),
                extra={k: v for k, v in {"version": ver, "type": typ}.items() if v},
            ))
        # in-force titles first (the index keeps repealed Acts under the same type)
        hits.sort(key=lambda h: 0 if h.status == "In force" else 1)
        return hits

    def _es(self, body: dict[str, Any]) -> list[dict[str, Any]]:
        try:
            r = request_with_retry("POST", _VIC_ES, json_body=body,
                                   headers={"User-Agent": USER_AGENT, "Accept": "application/json"},
                                   timeout=httpx.Timeout(30.0, connect=10.0))
        except httpx.HTTPError as exc:
            raise SourceUnavailable(f"legislation.vic.gov.au search failed ({type(exc).__name__}: {str(exc)[:120]})") from exc
        return ((r.json() or {}).get("hits") or {}).get("hits") or []

    def fetch(self, native_id, *, language=None):
        path = "/" + native_id.strip().strip("/")
        if path.startswith("/http"):
            path = "/" + re.sub(r"^https?://[^/]+/", "", native_id.strip()).strip("/")
        try:
            r = request_with_retry("GET", f"{_VIC}/api/tide/page", params={"path": path, "site": 6},
                                   headers={"User-Agent": USER_AGENT, "Accept": "application/json"})
        except httpx.HTTPStatusError as exc:
            if exc.response.status_code == 404:
                raise ValueError(f"legislation.vic.gov.au has no page {path!r}") from exc
            raise SourceUnavailable(f"legislation.vic.gov.au answered {exc.response.status_code}") from exc
        except httpx.TransportError as exc:
            raise SourceUnavailable(f"legislation.vic.gov.au unreachable ({type(exc).__name__})") from exc
        page = r.json() or {}
        title = page.get("title") or path
        versions = []
        files: dict[str, str] = {}
        for v in page.get("versions") or []:
            versions.append({k: v.get(k) for k in ("version", "date", "status") if v.get(k)})
        blob = json.dumps(page)
        for m in re.finditer(r'https?://content\.legislation\.vic\.gov\.au/sites/default/files/[^"\\]+?\.(pdf|docx?)', blob):
            kind = "authorised_pdf" if "authorised" in m.group(0) else m.group(1)
            files.setdefault(kind, m.group(0))
        current = next((v for v in versions if (v.get("status") or "").lower() == "in force"), versions[0] if versions else {})
        meta: dict[str, Any] = {
            "status": current.get("status") or (page.get("header") or {}).get("tag"),
            "version": current.get("version"),
            "effective": current.get("date"),
            "versions": versions[:12],
            "files": files or None,
            "no_text_reason": (
                "Not downloaded: the authorised PDF/DOCX are served only from content.legislation.vic.gov.au, whose "
                "robots.txt disallows all automated agents. Open the file link (or the page URL) in a browser, or "
                "ask the user to attach the PDF."
            ),
        }
        meta = {k: v for k, v in meta.items() if v not in (None, "", [], {})}
        return LegalDoc(
            ref=self.ref(path.strip("/")), title=title, source=self.id, text="", url=f"{_VIC}{path}",
            kind="legislation", language="en", metadata=meta, ttl=24 * 3600,
        )


SOURCES = [FrlSource(), VicLegislationSource()]
