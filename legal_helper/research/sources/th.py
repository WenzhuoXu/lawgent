"""Thailand: the Office of the Council of State's law database (OCS / Krisdika).

Search: ``www.ocs.go.th/searchlaw`` — title or full-text search over Thai
legislation (``law`` tab) and over the OCS's English translations (``law_en``
tab, about 135 Acts). That host sends only its leaf certificate, so requests
to it verify against certifi plus the GlobalSign GCC R46 OV TLS CA 2025
intermediate shipped in ``_certs/`` (never ``verify=False``).

Fetch: ``searchlaw.ocs.go.th/ocs-api/public/doc/getLawDoc`` — the consolidated
text of one version, provision by provision (มาตรา N, keys "7", "7/1",
"29ทวิ"), with the footnotes that record each amendment, and the version
timeline (original act, every amending act, current consolidation; each is
fetchable). English: the OCS translation PDF (unofficial, carries the OCS
disclaimer), found through the ``law_en`` search. Open, no key.
"""

from __future__ import annotations

import datetime as _dt
import functools
import re
import ssl
import time
import uuid
from pathlib import Path
from typing import Any, Optional

import httpx

from ...connectors.base import USER_AGENT, request_with_retry
from ..base import Hit, LegalDoc, Source, SourceUnavailable
from ..textify import bytes_to_text, html_to_text, normalize_lines

_WWW = "https://www.ocs.go.th"
_SEARCH = f"{_WWW}/searchlaw/indexs/list_table_search"
_API = "https://searchlaw.ocs.go.th/ocs-api/public/doc/getLawDoc"
_DOC_URL = "https://searchlaw.ocs.go.th/council-of-state/#/public/doc/{}"
_CERT = Path(__file__).with_name("_certs") / "globalsign_gcc_r46_ov_tls_ca_2025.pem"
_LONG = httpx.Timeout(90.0, connect=10.0)
_MARK = re.compile(r"</?mark>")
_TAG = re.compile(r"<[^>]+>")
_THAI = re.compile(r"[฀-๿]")
_THAI_DIGITS = str.maketrans("๐๑๒๓๔๕๖๗๘๙", "0123456789")
_TIMELINE_KIND = {"1": "original act", "2": "amending act", "3": "consolidated as from this date",
                  "4": "consolidated (current)"}


@functools.lru_cache(maxsize=1)
def _www_context() -> ssl.SSLContext:
    """certifi + the intermediate www.ocs.go.th omits; used for that host only."""
    import certifi

    ctx = ssl.create_default_context(cafile=certifi.where())
    if _CERT.exists():
        ctx.load_verify_locations(cafile=str(_CERT))
    return ctx


def _www_post(data: dict[str, str]) -> httpx.Response:
    last: Optional[Exception] = None
    for attempt in range(3):
        try:
            with httpx.Client(verify=_www_context(), timeout=httpx.Timeout(30.0, connect=10.0),
                              follow_redirects=True) as client:
                resp = client.post(_SEARCH, data=data, headers={"User-Agent": USER_AGENT})
            resp.raise_for_status()
            return resp
        except httpx.HTTPStatusError as exc:
            if exc.response.status_code not in (429, 500, 502, 503, 504):
                raise SourceUnavailable(f"ocs.go.th search answered HTTP {exc.response.status_code}") from exc
            last = exc
        except httpx.TransportError as exc:
            if "CERTIFICATE_VERIFY_FAILED" in str(exc):
                raise SourceUnavailable(
                    "www.ocs.go.th certificate chain did not verify even with the shipped GlobalSign "
                    f"intermediate ({_CERT.name}); its issuer may have changed — refresh the PEM from the "
                    "leaf certificate's AIA URL."
                ) from exc
            last = exc
        time.sleep(0.6 * (2 ** attempt))
    raise SourceUnavailable(f"ocs.go.th search unreachable: {type(last).__name__}: {last}")


def _www_get(url: str) -> httpx.Response:
    with httpx.Client(verify=_www_context(), timeout=_LONG, follow_redirects=True) as client:
        resp = client.get(url, headers={"User-Agent": USER_AGENT})
    resp.raise_for_status()
    return resp


def _search_rows(query: str, *, english: bool, content: bool, limit: int) -> list[dict[str, Any]]:
    tab = "law_en" if english else "law"
    data = {
        "query[tab_type]": tab, "query[type_view]": tab, "query[q]": query,
        "query[topic]": "1", "query[content]": "1" if content else "0", "query[synonyms]": "0",
        "query[stateName]": "01,02", "query[onlyMatchChild]": "0",
        "pagination[page]": "1", "pagination[perpage]": str(max(1, min(limit, 30))),
    }
    resp = _www_post(data)
    try:
        payload = resp.json()
    except ValueError as exc:
        raise SourceUnavailable(f"ocs.go.th search returned non-JSON ({resp.text[:120]!r})") from exc
    return payload.get("data") or []


def _get_law_doc(timeline_id: str, *, english: bool = False) -> dict[str, Any]:
    now = _dt.datetime.now()
    body = {
        "reqHeader": {
            "reqId": str(int(time.time() * 1000)), "reqChannel": "WEB",
            "reqDtm": now.strftime("%Y-%m-%d %H:%M:%S.000"), "reqBy": "unknow",
            "serviceName": "getPublicLawDoc", "uuid": str(uuid.uuid4()), "sessionId": str(uuid.uuid4()),
        },
        "reqBody": {"isTransEng": english, "timelineId": timeline_id},
    }
    resp = request_with_retry("POST", _API, json_body=body, headers={"User-Agent": USER_AGENT}, timeout=_LONG)
    data = resp.json()
    head = data.get("respHeader") or {}
    if head.get("errorCode") != "SUCCESS":
        raise ValueError(f"OCS getLawDoc {timeline_id!r}: {head.get('errorCode')} {head.get('errorDesc')}")
    return data.get("respBody") or {}


def _be_to_iso(d: str) -> Optional[str]:
    """'14/9/2497' (B.E.) → '1954-09-14'."""
    m = re.match(r"^(\d{1,2})/(\d{1,2})/(\d{4})$", (d or "").strip())
    if not m:
        return None
    day, month, year = int(m.group(1)), int(m.group(2)), int(m.group(3))
    if year > 2400:
        year -= 543
    return f"{year:04d}-{month:02d}-{day:02d}"


def _clean(s: Any) -> str:
    return " ".join(_TAG.sub("", _MARK.sub("", str(s or ""))).split())


def _no_key(no: str) -> str:
    """'5/1' → '5/1', '29 ทวิ' → '29ทวิ' (a request reduces to the same key)."""
    return re.sub(r"\s+", "", (no or "").translate(_THAI_DIGITS))


class OcsSource(Source):
    id = "th.ocs"
    name = "Office of the Council of State — Thai law database (searchlaw.ocs.go.th)"
    jurisdictions = ("TH",)
    kinds = ("legislation",)
    modes = ("keyword", "fulltext")
    languages = ("th", "en")
    homepage = "https://searchlaw.ocs.go.th"
    coverage = (
        "Thai Acts, Codes, Royal Decrees, Emergency Decrees and their subordinate legislation (กฎกระทรวง …) "
        "as consolidated by the Council of State: section-level text (มาตรา N; '7/1', '29ทวิ'), amendment "
        "footnotes, every version (original, amending acts, current). Search in Thai (titles; mode=fulltext "
        "for the text); a Latin-script query searches the ~135 OCS English translations (unofficial; "
        "fetch with language='en'). No case law. filters: language (th|en)."
    )
    probe = {"query": "การเดินอากาศ", "kind": "legislation", "ref": "b2dCdXhqS1hlT2MyQzBHUUs2TGRLZz09",
             "section": "มาตรา 4", "min_chars": 5000}

    # ------------------------------------------------------------------ search
    def search(self, query, *, kind="any", mode="keyword", limit=10, filters=None):
        if kind == "case":
            return []
        filters = filters or {}
        lang = str(filters.get("language") or "").lower()[:2]
        english = lang == "en" or (lang != "th" and not _THAI.search(query) and re.search(r"[A-Za-z]", query))
        english = bool(english)
        fulltext = mode == "fulltext"
        rows = _search_rows(query, english=english, content=fulltext, limit=limit)
        if not rows and not fulltext:
            rows = _search_rows(query, english=english, content=True, limit=limit)
            fulltext = True
        hits: list[Hit] = []
        children: list[Hit] = []
        for r in rows:
            tid = r.get("encTimelineID")
            if not tid:
                continue
            title = _clean(r.get("lawName") if english else r.get("lawNameTh"))
            repealed = "(ยกเลิก)" in title
            if english:
                snippet = ("Unofficial OCS English translation (no legal authority); the official Thai "
                           f"text is {self.ref(tid)}.")
            else:
                snippet = _clean(r.get("contentlaw"))[:500] if fulltext else ""
            hits.append(Hit(
                ref=self.ref(f"en/{tid}" if english else tid), title=title, source=self.id,
                kind="legislation", url=_DOC_URL.format(tid), date=_be_to_iso(r.get("publishDate") or ""),
                status="repealed (ยกเลิก)" if repealed else None, snippet=snippet,
                extra={k: v for k, v in {"law_code": r.get("lawCode"), "language": "en" if english else "th",
                                         "english_translation": bool(r.get("lawEn")) or None}.items() if v},
            ))
            for group in _child_groups(r.get("childrens")):
                for item in group.get("items") or []:
                    if "<mark>" not in str(item.get("title") or "") or not item.get("encTimelineID"):
                        continue
                    children.append(Hit(
                        ref=self.ref(item["encTimelineID"]), title=_clean(item.get("title")), source=self.id,
                        kind="legislation", url=_DOC_URL.format(item["encTimelineID"]),
                        date=_be_to_iso(item.get("date") or ""),
                        status="repealed (ยกเลิก)" if "ยกเลิก" in str(item.get("title")) else None,
                        snippet=f"{group.get('name') or 'subordinate legislation'} under {title}",
                    ))
        return (hits + children)[:limit]

    # ------------------------------------------------------------------- fetch
    def fetch(self, native_id, *, language=None):
        native = native_id.strip()
        english = native.startswith("en/") or (language or "").lower().startswith("en")
        tid = native[3:] if native.startswith("en/") else native
        if english:
            doc = self._fetch_english(tid)
            if doc is not None:
                return doc
        body = _get_law_doc(tid)
        info = body.get("lawInfo") or {}
        text, sections = _assemble(body.get("lawSections") or [], body.get("footnoteList") or [])
        meta = _metadata(self, tid, info, body.get("timelines") or [])
        if _has_tagged(sections):
            meta["amending_acts"] = ("The amending acts' own provisions (transitional clauses) follow the main text "
                                     "under a heading per act tagged [@N]; read them as section='มาตรา 76@14' "
                                     "(มาตรา 76 of the act tagged @14) or find them with query=.")
        if english:
            meta["note"] = ("The OCS has no English translation of this law; this is the official Thai text. "
                            "English translations exist for about 135 Acts (legal_search with an English query).")
        if not text.strip():
            meta["no_text_reason"] = (
                "the OCS holds no consolidated text for this version"
                + (" (repealed law: the OCS points to the Royal Gazette, ratchakitcha.soc.go.th)"
                   if "ยกเลิก" in str(info.get("lawNameTh")) else "")
            )
        return LegalDoc(
            ref=self.ref(native), title=_clean(info.get("lawNameTh")) or tid, source=self.id, text=text,
            url=_DOC_URL.format(tid), kind="legislation", language="th", metadata=meta,
            sections=sections or None,
        )

    def _fetch_english(self, tid: str) -> Optional[LegalDoc]:
        """The OCS translation PDF, or None when there is none."""
        body = _get_law_doc(tid, english=True)
        info = body.get("lawInfo") or {}
        timelines = body.get("timelines") or []
        if not any(t.get("hasTranslation") for t in timelines):
            return None
        name_en = _clean(info.get("lawNameEn"))
        rows: list[dict[str, Any]] = []
        for q in [name_en, re.sub(r",?\s*B\.E\..*$", "", name_en)]:
            if q:
                rows = _search_rows(q, english=True, content=False, limit=10)
                if rows:
                    break
        pick = next((r for r in rows if r.get("encTimelineID") == tid), None) or next(
            (r for r in rows if r.get("lawCode") == info.get("lawCode")), None)
        if not pick or not pick.get("fileUUID"):
            return None
        resp = _www_get(pick["fileUUID"])
        text = _clean_english(bytes_to_text(resp.content, resp.headers.get("content-type", ""), name_hint="x.pdf"))
        meta = _metadata(self, tid, info, timelines)
        meta["authority"] = ("Unofficial translation by the Office of the Council of State: it carries no legal "
                             "authority; the Thai text is the sole authoritative version.")
        meta["text_format"] = "pdf"
        return LegalDoc(
            ref=self.ref(f"en/{tid}"), title=name_en or _clean(info.get("lawNameTh")), source=self.id, text=text,
            url=_DOC_URL.format(tid), kind="legislation", language="en", metadata=meta,
            sections=_english_sections(text) or None, ttl=30 * 24 * 3600,
        )


def _has_tagged(sections: list[dict[str, Any]]) -> bool:
    return any("@" in str(x.get("key")) for x in sections or [])


def _child_groups(raw: Any) -> list[dict[str, Any]]:
    """``childrens`` arrives as a Python-literal string; parse it safely."""
    if isinstance(raw, list):
        return raw
    if not raw or not isinstance(raw, str):
        return []
    import ast

    try:
        val = ast.literal_eval(raw)
    except (ValueError, SyntaxError):
        return []
    return val if isinstance(val, list) else []


def _metadata(src: Source, tid: str, info: dict[str, Any], timelines: list[dict[str, Any]]) -> dict[str, Any]:
    current = next((t for t in timelines if str(t.get("timelineTypeId")) == "4"), None)
    name = str(info.get("lawNameTh") or "")
    meta: dict[str, Any] = {
        "title_en": _clean(info.get("lawNameEn")) or None,
        "law_code": info.get("lawCode"),
        "version": info.get("timelineLawCode"),
        "version_kind": _TIMELINE_KIND.get(str(info.get("timelineTypeId")), info.get("timelineTypeId")),
        "published": info.get("publishDateAd"),
        "effective": info.get("effectiveDateStartAd"),
        "status": "repealed (ยกเลิก)" if "ยกเลิก" in name else (
            "in force" if str(info.get("stateId")) == "01" else info.get("stateId")),
    }
    if timelines:
        seen: set[tuple[str, str]] = set()
        versions = []
        for t in timelines:
            key = (str(t.get("timelineId")), str(t.get("timelineTypeId")))
            if key in seen:
                continue
            seen.add(key)
            if not t.get("timelineId"):
                continue
            kind = _TIMELINE_KIND.get(str(t.get("timelineTypeId")), str(t.get("timelineTypeId")))
            versions.append(f"{t.get('timelineDateAd')} {kind}: {src.ref(t['timelineId'])}")
        meta["version_count"] = len(versions)
        meta["versions"] = versions[-40:]
        if current and current.get("timelineId"):
            meta["in_force_version"] = src.ref(current["timelineId"])
            if current.get("timelineId") != tid:
                meta["version_note"] = (
                    "This is not the current consolidation; the in-force text is " + src.ref(current["timelineId"]))
        if any(t.get("hasTranslation") for t in timelines):
            meta["english_translation"] = "available: legal_fetch with language='en' (unofficial)"
    return {k: v for k, v in meta.items() if v not in (None, "", [])}


def _assemble(raw: list[dict[str, Any]], footnotes: list[dict[str, Any]]) -> tuple[str, list[dict[str, Any]]]:
    """OCS ``lawSections`` → text plus section offsets.

    Main-act provisions are keyed by their number ("4", "5/1", "29ทวิ"); the
    transitional provisions of amending acts printed after the main text sit
    under a container per amending act and are keyed "<n>@<amendment>"
    ("76@14" = มาตรา 76 of amending Act No. 14), so they never shadow the
    main act's มาตรา 76.
    """
    notes = {f.get("footnoteUuid"): _clean(f.get("footnoteContent")) for f in footnotes if f.get("footnoteUuid")}
    blocks: list[str] = []
    secs: list[dict[str, Any]] = []
    pos = 0
    seen_article = False
    tag: Optional[str] = None
    tags_used: set[str] = set()
    pending_head: list[str] = []
    n_note = 0

    def add(text: str, label: str, key: str, level: str, caption: str = "") -> None:
        nonlocal pos
        text = text.strip()
        if not text:
            return
        start = pos + (2 if blocks else 0)
        blocks.append(text)
        pos = start + len(text)
        # Thai provisions carry no captions; the heading is the label so the
        # outline does not mistake a provision's first sentence for its title.
        heading = text.split("\n", 1)[0][:120] if level == "container" else label
        entry = {"label": label, "key": key, "heading": heading, "start": start, "end": pos, "level": level}
        if caption:
            entry["caption"] = caption
        secs.append(entry)

    for s in raw:
        t = str(s.get("sectionTypeId") or "")
        html = s.get("sectionContent") or ""
        body = html_to_text(html) if html else ""
        refs = list(dict.fromkeys(re.findall(r'href="#foot-([0-9a-fA-F-]+)"', html)))
        foot = [notes[u] for u in refs if notes.get(u)]
        foot_text = "\n".join(f"[เชิงอรรถ: {n}]" for n in foot)
        plain = normalize_lines(body)
        body = normalize_lines(body + ("\n" + foot_text if foot_text else ""))
        label = _clean(s.get("sectionLabel"))
        if t in ("1", "2", "3") and not seen_article:
            pending_head.append(body)
            continue
        if pending_head:
            add("\n".join(pending_head), "ชื่อกฎหมาย / คำปรารภ", "preamble", "para")
            pending_head = []
        if t in ("4", "10"):
            seen_article = True
            no = _no_key(s.get("sectionNo") or "")
            if not no or not re.match(r"^\d", no):
                n_note += 1
                add(body, label or "มาตรา", f"x{n_note}", "para")
                continue
            word = "ข้อ" if t == "10" else "มาตรา"
            caption = _clean(s.get("sectionName"))
            if tag:
                add(body, f"{word} {no}@{tag}", f"{no}@{tag}", "para", caption)
            else:
                add(body, f"{word} {no}", no, "article", caption)
        elif t == "1":
            heading = " ".join(plain.split())
            m = re.search(r"ฉบับที่\s*([0-9๐-๙]+)", heading)
            year = re.findall(r"(?:พ\.ศ\.|พุทธศักราช)\s*([0-9๐-๙]{4})", heading)
            base = (m.group(1) if m else (year[-1] if year else str(len(tags_used) + 1))).translate(_THAI_DIGITS)
            tag = base
            k = 2
            while tag in tags_used:
                tag = f"{base}-{k}"
                k += 1
            tags_used.add(tag)
            add(f"{heading} [@{tag}]" + ("\n" + foot_text if foot_text else ""), heading[:120], "", "container")
        elif t in ("5", "7", "8", "9") or (t == "16" and len(plain) < 160):
            heading = " ".join(plain.split())
            add(heading + ("\n" + foot_text if foot_text else ""), heading[:120], "", "container")
        else:
            n_note += 1
            add(body, label or "หมายเหตุ", f"x{n_note}", "para")
    if pending_head:
        add("\n".join(pending_head), "ชื่อกฎหมาย / คำปรารภ", "preamble", "para")
    return "\n\n".join(blocks), secs


_DISCLAIMER = re.compile(r"^(DISCLAIMER:|NO LEGAL AUTHORITY|LIABILITIES ARISING|ADOPTED AND PUBLISHED SHALL)")


def _clean_english(text: str) -> str:
    """Drop the per-page disclaimer repeats and page numbers; unglue footnote marks
    from section numbers ("Section 2.1  This Act" → "Section 2.  This Act")."""
    out: list[str] = []
    seen_disclaimer = False
    for line in text.split("\n"):
        s = line.strip()
        if _DISCLAIMER.match(s):
            if seen_disclaimer and s.startswith("DISCLAIMER:"):
                continue
            if seen_disclaimer:
                continue
            if s.startswith("ADOPTED AND PUBLISHED"):
                seen_disclaimer = True
            out.append(s)
            continue
        if re.fullmatch(r"\d{1,3}", s):
            continue
        s = re.sub(r"^(Section\s+\d+(?:/\d+)?(?:\s?(?:bis|ter|quater|quinquies))?)\.(\d{1,2})(?=\s|$)", r"\1.", s)
        out.append(s)
    return normalize_lines("\n".join(out))


_EN_SECTION = re.compile(r"^Section\s+(\d+(?:/\d+)?)(?:\s?(bis|ter|quater|quinquies|sexies|septies))?\.?(?=\s|$)")
_EN_CONTAINER = re.compile(r"^(?:CHAPTER|Chapter|PART|Part|DIVISION|Division|TITLE|BOOK)\s+[IVXLC\d]+\b")


def _english_sections(text: str) -> list[dict[str, Any]]:
    marks: list[tuple[int, str, str, str, str]] = []
    pos = 0
    for raw in text.split("\n"):
        line = raw.strip()
        start = pos
        pos += len(raw) + 1
        m = _EN_SECTION.match(line)
        if m:
            key = m.group(1) + (m.group(2) or "")
            marks.append((start, f"Section {key}", key, f"Section {key}", "article"))
        elif _EN_CONTAINER.match(line) and len(line) <= 120:
            marks.append((start, line, "", line, "container"))
    secs = []
    for i, (start, label, key, heading, level) in enumerate(marks):
        end = marks[i + 1][0] if i + 1 < len(marks) else len(text)
        secs.append({"label": label, "key": key, "heading": heading, "start": start, "end": end, "level": level})
    return secs


SOURCES = [OcsSource()]
