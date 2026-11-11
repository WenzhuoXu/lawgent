"""Vietnam: Công báo (the Official Gazette) and vbpl.vn (the national legal database).

vn.congbao — the Government Office's Công báo search API
(``api-searchcongbao.chinhphu.vn``): every normative document published in the
Gazette since ~2004, consolidated texts (Văn bản hợp nhất, VBHN) and treaties,
matched inside the text with the passage that matched; effective / expiry
dates; the published Word file (or PDF) as the text (Điều N sections). Its
ranking is a relevance score over the text, but it only returns documents
whose wording matches (a paraphrase or an English query finds nothing), so it
is listed as keyword / fulltext, not semantic.

vn.vbpl — the Ministry of Justice's vbpl.vn document pages (server-rendered):
the text, the in-force status (Còn hiệu lực / Hết hiệu lực một phần / …),
dates and, where the page has one, the English text. vbpl.vn's own search runs
in the browser against an API that robots.txt disallows (``/api/``), so search
here reads a local title index built from vbpl.vn's sitemap (allowed): the 12
central-government shards (~58,000 documents), ~47 MB downloaded once in about
30 s, stored as ~8 MB under ``state/legal_docs/index/vn.vbpl/`` and rebuilt
weekly. Both are open, no key.
"""

from __future__ import annotations

import datetime as _dt
import json
import re
import threading
import time
import unicodedata
from pathlib import Path
from typing import Any, Optional

import httpx

from ...connectors.base import USER_AGENT, request_with_retry
from ..base import Hit, LegalDoc, Source, SourceUnavailable
from ..textify import bytes_to_text, html_to_text, normalize_lines

_LONG = httpx.Timeout(90.0, connect=10.0)
_UA = {"User-Agent": USER_AGENT}
_NUM = re.compile(
    r"(\d{1,4}/\d{4}/[A-ZĐ][A-ZĐ0-9\-]*|\d{1,4}/VBHN-[A-ZĐ0-9\-]+|\d{1,4}/[A-ZĐ]{1,6}-[A-ZĐ0-9\-]+)",
    re.I,
)


def _fold(s: str) -> str:
    """Vietnamese → ASCII the way vbpl.vn slugs are made ('Hàng không' → 'hang khong')."""
    s = (s or "").replace("đ", "d").replace("Đ", "D")
    s = unicodedata.normalize("NFD", s)
    s = "".join(ch for ch in s if unicodedata.category(ch) != "Mn")
    return re.sub(r"[^a-z0-9]+", " ", s.lower()).strip()


def _number_slug(num: str) -> str:
    return _fold(num).replace(" ", "-")


def _status_from_dates(effective: Optional[str], expires: Optional[str]) -> Optional[str]:
    today = _dt.date.today().isoformat()
    if expires and expires[:10] <= today:
        return f"hết hiệu lực (expired {expires[:10]})"
    if effective and effective[:10] > today:
        return f"chưa có hiệu lực (in force from {effective[:10]})"
    if effective:
        return f"có hiệu lực từ {effective[:10]} (Công báo dates; check vn.vbpl for amendments)"
    return None


def _clean_word_text(text: str) -> str:
    """The shared Word reader marks bold/italic as Markdown and prints running
    headers; statute headings ("**Điều 1. Phạm vi**") must start their line."""
    out = []
    for line in text.split("\n"):
        s = line.strip()
        if s.startswith(("[Header", "[Footer")):
            continue
        s = s.replace("**", "")
        if s.startswith("*") and s.endswith("*") and len(s) > 2:
            s = s[1:-1].strip()
        s = re.sub(r"^\*(?=\S)", "", s)
        out.append(s)
    return normalize_lines("\n".join(out))


# ===================================================================== Công báo

_CB_API = "https://api-searchcongbao.chinhphu.vn/search"
_CB_SITE = "https://congbao.chinhphu.vn"
_CB_GROUPS = {"vbqppl": "/van-ban/nhom/vbqpp", "normative": "/van-ban/nhom/vbqpp",
              "treaty": "/van-ban/nhom/dieu-uoc-quoc-te", "dieu-uoc": "/van-ban/nhom/dieu-uoc-quoc-te",
              "other": "/van-ban/nhom/khac"}


def _cb_search(query: str, *, limit: int, filters: dict[str, Any], path: str = "/van-ban") -> list[dict[str, Any]]:
    body = {"filters": filters, "page": 1, "page_size": max(1, min(limit, 30)), "query": query}
    try:
        resp = request_with_retry("POST", _CB_API + path, json_body=body, headers=_UA)
    except httpx.HTTPError as exc:
        raise SourceUnavailable(f"Công báo search API unreachable: {type(exc).__name__}: {exc}") from exc
    data = resp.json()
    if not data.get("success", True) and data.get("code") not in (200, None):
        raise SourceUnavailable(f"Công báo search failed: {data.get('message')}")
    return data.get("data") or []


def _cb_title(r: dict[str, Any]) -> str:
    title = (r.get("tieu_de") or "").strip()
    num = r.get("so_ky_hieu") or ""
    if not title or title == num:
        title = " ".join(x for x in (r.get("loai_van_ban"), f"số {num}" if num else "", r.get("trich_yeu")) if x)
    return " ".join(title.split())


def _cb_files(r: dict[str, Any]) -> list[str]:
    urls = [f.get("duong_dan") for f in sorted(r.get("danh_sach_tep_van_ban") or [], key=lambda f: f.get("thu_tu") or 0)
            if f.get("duong_dan")]

    def rank(u: str) -> int:
        low = u.lower()
        return 0 if low.endswith(".docx") else 1 if low.endswith(".doc") else 2
    return sorted(urls, key=rank)


class CongbaoSource(Source):
    id = "vn.congbao"
    name = "Công báo nước CHXHCN Việt Nam (congbao.chinhphu.vn)"
    jurisdictions = ("VN",)
    kinds = ("legislation",)
    modes = ("keyword", "fulltext")
    languages = ("vi",)
    homepage = _CB_SITE
    coverage = (
        "Official Gazette: laws (Luật), ordinances, decrees (Nghị định), decisions, circulars (Thông tư), "
        "consolidated texts (Văn bản hợp nhất, e.g. 102/VBHN-VPQH = Luật Hàng không dân dụng as amended) "
        "and treaties, as published (~2004 on). Search in Vietnamese (text-matched, with the matching "
        "passage; by number '130/2025/QH15' too); effective/expiry dates. No amendment status: use "
        "vn.vbpl. filters: type (Luật|Nghị định|Thông tư|Văn bản hợp nhất…), agency (QUỐC HỘI|CHÍNH PHỦ|"
        "BỘ GIAO THÔNG VẬN TẢI…), date_from/date_to (YYYY-MM-DD, issue date), group (vbqppl|treaty|other)."
    )
    probe = {"query": "hàng không dân dụng", "kind": "legislation", "ref": "468690", "section": "Điều 1",
             "min_chars": 20000}

    def search(self, query, *, kind="any", mode="keyword", limit=10, filters=None):
        if kind == "case":
            return []
        filters = filters or {}
        api_filters: dict[str, Any] = {}
        for k_in, k_out in (("type", "ten_loai_van_ban"), ("agency", "ten_co_quan_ban_hanh")):
            v = filters.get(k_in)
            if v:
                api_filters[k_out] = v if isinstance(v, list) else [v]
        if filters.get("date_from"):
            api_filters["ngay_ban_hanh_tu"] = str(filters["date_from"])[:10]
        if filters.get("date_to"):
            api_filters["ngay_ban_hanh_den"] = str(filters["date_to"])[:10]
        path = _CB_GROUPS.get(str(filters.get("group") or "").lower(), "/van-ban")
        rows = _cb_search(query, limit=limit, filters=api_filters, path=path)
        hits = []
        for r in rows[:limit]:
            if not r.get("id_van_ban"):
                continue
            eff, exp = r.get("ngay_co_hieu_luc"), r.get("ngay_het_hieu_luc")
            extra = {k: v for k, v in {
                "type": r.get("loai_van_ban"), "agency": ", ".join(r.get("ten_co_quan") or []) or None,
                "effective": (eff or "")[:10] or None, "expires": (exp or "")[:10] or None,
                "gazette": r.get("danh_sach_dang_trong_cong_bao"), "summary": r.get("trich_yeu"),
            }.items() if v}
            vb = _vbpl_ref_for(r.get("so_ky_hieu") or "")
            if vb:
                extra["vbpl"] = vb
            hits.append(Hit(
                ref=self.ref(str(r["id_van_ban"])), title=_cb_title(r), source=self.id, kind="legislation",
                url=f"{_CB_SITE}/van-ban/van-ban-{r['id_van_ban']}.htm", date=(r.get("ngay_ban_hanh") or "")[:10] or None,
                status=_status_from_dates(eff, exp), citation=r.get("so_ky_hieu"),
                snippet=" ".join((r.get("noi_dung_lien_quan_tim_thay") or "").split())[:500], extra=extra,
                score=None,
            ))
        return hits

    def fetch(self, native_id, *, language=None):
        doc_id = native_id.strip()
        if not doc_id.isdigit():
            raise ValueError("Công báo refs are numeric document ids (vn.congbao:468690); take them from legal_search")
        page = request_with_retry("GET", f"{_CB_SITE}/van-ban/van-ban-{doc_id}.htm", headers=_UA)
        human_url = str(page.url)
        from bs4 import BeautifulSoup

        soup = BeautifulSoup(page.text, "lxml")
        h1 = soup.select_one("h1")
        page_title = " ".join(h1.get_text(" ").split()) if h1 else ""
        row: Optional[dict[str, Any]] = None
        m = _NUM.search(page_title)
        if m:
            try:
                for r in _cb_search(m.group(1), limit=10, filters={}):
                    if str(r.get("id_van_ban")) == doc_id:
                        row = r
                        break
            except SourceUnavailable:
                row = None
        files = _cb_files(row) if row else []
        if not files:
            links = []
            for a in soup.select('a[data-atc="download"]'):
                href, name = a.get("href") or "", (a.get("data-file") or "").lower()
                if href:
                    links.append((0 if name.endswith(".docx") else 1 if name.endswith(".doc") else 2, href))
            files = [h for _, h in sorted(links)]
        text, used, last_err = "", "", ""
        for url in files[:3]:
            try:
                r = request_with_retry("GET", url, headers=_UA, timeout=_LONG)
            except httpx.HTTPError as exc:
                last_err = f"{type(exc).__name__}: {exc}"
                continue
            hint = url.split("?")[0]
            if re.search(r"pdf$", hint, re.I) and not hint.lower().endswith(".pdf"):
                hint += ".pdf"
            text = bytes_to_text(r.content, r.headers.get("content-type", ""), name_hint=hint)
            if "word" in (r.headers.get("content-type") or "") or hint.lower().endswith((".docx", ".doc")):
                text = _clean_word_text(text)
            if text.strip():
                used = url
                break
        eff, exp = (row or {}).get("ngay_co_hieu_luc"), (row or {}).get("ngay_het_hieu_luc")
        meta: dict[str, Any] = {k: v for k, v in {
            "citation": (row or {}).get("so_ky_hieu") or (m.group(1) if m else None),
            "type": (row or {}).get("loai_van_ban"),
            "agency": ", ".join((row or {}).get("ten_co_quan") or []) or None,
            "date": ((row or {}).get("ngay_ban_hanh") or "")[:10] or None,
            "effective": (eff or "")[:10] or None,
            "expires": (exp or "")[:10] or None,
            "status": _status_from_dates(eff, exp),
            "gazette": (row or {}).get("danh_sach_dang_trong_cong_bao"),
            "signed_by": (row or {}).get("nguoi_ky"),
            "file": used or None,
            "note": ("Text as published in the Gazette (the original, not consolidated). For the in-force "
                     "wording search a Văn bản hợp nhất (type='Văn bản hợp nhất') or vn.vbpl."),
        }.items() if v}
        vb = _vbpl_ref_for(meta.get("citation") or "")
        if vb:
            meta["vbpl"] = vb
        if not text.strip():
            meta["no_text_reason"] = f"the Gazette file could not be read ({last_err or 'no file listed'})"
        return LegalDoc(
            ref=self.ref(doc_id), title=_cb_title(row) if row else (page_title or doc_id), source=self.id,
            text=text, url=human_url, kind="legislation", language="vi", metadata=meta,
        )


# ======================================================================= vbpl.vn

_VBPL = "https://vbpl.vn"
_INDEX_TTL = 7 * 24 * 3600
_INDEX: Optional[list[tuple[str, str, frozenset[str]]]] = None
_INDEX_AT = 0.0
_INDEX_LOCK = threading.Lock()


def _index_file() -> Path:
    from ...config import load_settings

    d = Path(load_settings().state_dir) / "legal_docs" / "index" / "vn.vbpl"
    d.mkdir(parents=True, exist_ok=True)
    return d / "central.tsv"


def _build_index(path: Path) -> None:
    root = request_with_retry("GET", f"{_VBPL}/sitemap.xml", headers=_UA).text
    central = root.split("Địa phương")[0]
    shards = [u for u in re.findall(r"<loc>([^<]+)</loc>", central) if not u.rstrip("/").endswith("/0.xml")]
    if not shards:
        raise SourceUnavailable("vbpl.vn sitemap index lists no central-government shards")
    rows: dict[str, str] = {}
    for url in shards:
        xml = request_with_retry("GET", url, headers=_UA, timeout=_LONG).text
        for slug, doc_id in re.findall(r"<loc>https://vbpl\.vn/van-ban/chi-tiet/([^<]+?)--([^<]+?)</loc>", xml):
            if doc_id.startswith("vbpqta_"):
                continue  # English-translation entries: their pages answer 404
            rows[doc_id] = slug
    tmp = path.with_suffix(".tmp")
    tmp.write_text("".join(f"{i}\t{s}\n" for i, s in rows.items()), encoding="utf-8")
    tmp.replace(path)


def _index(build: bool = True) -> Optional[list[tuple[str, str, frozenset[str]]]]:
    global _INDEX, _INDEX_AT
    with _INDEX_LOCK:
        path = _index_file()
        fresh = path.exists() and time.time() - path.stat().st_mtime < _INDEX_TTL
        if _INDEX is not None and fresh and _INDEX_AT >= path.stat().st_mtime:
            return _INDEX
        if not fresh:
            if not build and not path.exists():
                return None
            if build:
                try:
                    _build_index(path)
                except (httpx.HTTPError, SourceUnavailable) as exc:
                    if not path.exists():
                        raise SourceUnavailable(f"vbpl.vn title index could not be built: {exc}") from exc
        rows = []
        for line in path.read_text(encoding="utf-8").splitlines():
            doc_id, _, slug = line.partition("\t")
            if slug:
                rows.append((doc_id, slug, frozenset(slug.split("-"))))
        _INDEX, _INDEX_AT = rows, time.time()
        return rows


def _vbpl_ref_for(number: str) -> Optional[str]:
    """vn.vbpl ref of a document number, from the index if it is already built."""
    if not number:
        return None
    try:
        rows = _index(build=False)
    except Exception:  # noqa: BLE001 — a cross-link is optional
        return None
    if not rows:
        return None
    key = "so-" + _number_slug(number)
    for doc_id, slug, _ in rows:
        if slug.endswith(key) or f"{key}-" in slug:
            return f"vn.vbpl:{doc_id}"
    return None


def _flight(html: str) -> str:
    parts = re.findall(r'self\.__next_f\.push\(\[1,"((?:[^"\\]|\\.)*)"\]\)', html)
    out = []
    for p in parts:
        try:
            out.append(json.loads('"' + p + '"'))
        except ValueError:
            continue
    return "".join(out)


def _flight_text(flight: str, ref: str) -> str:
    """Resolve an RSC text reference ('$2b') — the row length is in UTF-8 bytes."""
    rid = ref[1:] if ref.startswith("$") else ref
    m = re.search(rf"(?:^|\n){re.escape(rid)}:T([0-9a-f]+),", flight)
    if not m:
        return ""
    n = int(m.group(1), 16)
    return flight[m.end():].encode("utf-8")[:n].decode("utf-8", errors="ignore")


def _initial_doc(flight: str) -> Optional[dict[str, Any]]:
    i = flight.find('"initialDoc"')
    if i < 0:
        return None
    m = re.compile(r'"initialDoc":\s*\{').search(flight, i)
    if not m:
        return None
    try:
        obj, _ = json.JSONDecoder().raw_decode(flight[m.end() - 1:])
    except ValueError:
        return None
    return obj if isinstance(obj, dict) else None


def _content_html(flight: str, content: Any) -> str:
    if isinstance(content, dict):
        content = content.get("content")
    if not isinstance(content, str) or not content:
        return ""
    if re.fullmatch(r"\$[0-9a-f]+", content):
        return _flight_text(flight, content)
    return content


def _deslug(slug: str) -> str:
    words = slug.split("-")
    s = " ".join(words)
    m = re.search(r"\bso (\d+) (\d{4}) ([a-z0-9]+(?: [a-z0-9]+)?)$", s)
    if m:
        s = s[: m.start()] + f"số {m.group(1)}/{m.group(2)}/{m.group(3).replace(' ', '-').upper()}"
    return s[:1].upper() + s[1:]


class VbplSource(Source):
    id = "vn.vbpl"
    name = "Cơ sở dữ liệu quốc gia về văn bản pháp luật (vbpl.vn)"
    jurisdictions = ("VN",)
    kinds = ("legislation",)
    modes = ("keyword",)
    languages = ("vi", "en")
    homepage = _VBPL
    coverage = (
        "Ministry of Justice national database: central-government laws, decrees, circulars with in-force "
        "status (Còn hiệu lực / Hết hiệu lực một phần / Hết hiệu lực toàn bộ / Chưa có hiệu lực), dates and "
        "full text; English where vbpl.vn has it (language='en'). Search is a local title index of vbpl.vn's "
        "sitemap (titles without diacritics; numbers like '66/2006/QH11' match best; built once, ~30 s). "
        "Provincial documents are not indexed. ids: '15068' (numeric) or a UUID."
    )
    probe = {"ref": "15068", "section": "Điều 145", "min_chars": 20000}

    def search(self, query, *, kind="any", mode="keyword", limit=10, filters=None):
        if kind == "case":
            return []
        rows = _index(build=True) or []
        numbers = [_number_slug(n) for n in _NUM.findall(query)]
        rest = _NUM.sub(" ", query)
        tokens = [t for t in _fold(rest).split() if t not in {"so", "va", "cua", "ve", "the", "nam"}]
        phrase = "-".join(tokens)
        scored: list[tuple[float, str, str]] = []
        for doc_id, slug, toks in rows:
            score = 0.0
            if numbers:
                if not any(slug.endswith("so-" + n) or f"so-{n}-" in slug for n in numbers):
                    continue
                score += 10
            if tokens:
                have = sum(1 for t in tokens if t in toks)
                if not numbers and have < max(1, len(tokens) - (1 if len(tokens) >= 4 else 0)):
                    continue
                score += have / len(tokens) * 5
                if phrase and phrase in slug:
                    score += 3
                    if slug.startswith(phrase) or slug.startswith(("luat-" + phrase, "bo-luat-" + phrase)):
                        score += 1
            score -= len(toks) * 0.01
            scored.append((score, doc_id, slug))
        scored.sort(key=lambda x: -x[0])
        hits = []
        for score, doc_id, slug in scored[:limit]:
            hits.append(Hit(
                ref=self.ref(doc_id), title=_deslug(slug), source=self.id, kind="legislation",
                url=f"{_VBPL}/van-ban/chi-tiet/{slug}--{doc_id}",
                snippet="Title from the vbpl.vn index (no diacritics); fetch for the official title, in-force status and text.",
            ))
        return hits

    def fetch(self, native_id, *, language=None):
        doc_id = native_id.strip()
        if not re.fullmatch(r"[0-9]+|[0-9a-f]{8}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{12}", doc_id):
            raise ValueError("vbpl.vn refs are a numeric id or a UUID (vn.vbpl:15068); take them from legal_search")
        english = (language or "").lower().startswith("en")
        url = f"{_VBPL}{'/en' if english else ''}/van-ban/chi-tiet/van-ban--{doc_id}"
        d: Optional[dict[str, Any]] = None
        for _attempt in range(3):
            try:
                resp = request_with_retry("GET", url, headers=_UA, timeout=_LONG)
            except httpx.HTTPStatusError as exc:
                if exc.response.status_code == 404:
                    raise ValueError(f"vbpl.vn has no document {doc_id!r}") from exc
                raise
            flight = _flight(resp.text)
            d = _initial_doc(flight)
            if d and (d.get("docNum") or d.get("title")):
                break
            time.sleep(1.5)
        if not d or not (d.get("docNum") or d.get("title")):
            raise SourceUnavailable(
                f"vbpl.vn rendered 'page not found' for {doc_id!r} three times (it does so intermittently for "
                "existing documents); try again shortly, or read the document from vn.congbao by its number."
            )
        lang = "vi"
        html = ""
        note = None
        if english:
            html = _content_html(flight, d.get("documentContentEn"))
            if html:
                lang = "en"
            else:
                note = "vbpl.vn has no English text for this document; this is the official Vietnamese text."
        if not html:
            html = _content_html(flight, d.get("documentContent"))
        text = html_to_text(html) if html else ""
        canon = re.search(r'<link rel="canonical" href="([^"]+)"', resp.text)
        eff = d.get("effStatus") or {}
        dtype = d.get("docType") or {}
        meta: dict[str, Any] = {k: v for k, v in {
            "citation": d.get("docNum"),
            "type": dtype.get("name"),
            "agency": d.get("agencyName"),
            "date": (d.get("issueDate") or "")[:10] or None,
            "effective": (d.get("effFrom") or "")[:10] or None,
            "expires": (d.get("effTo") or "")[:10] or None,
            "status": eff.get("name"),
            "consolidated": True if d.get("isConsolidatedDocument") else None,
            "translation": True if d.get("isTranslationDoc") else None,
            "english": "available (language='en')" if d.get("documentContentEn") and lang != "en" else None,
            "note": note,
        }.items() if v}
        if not text.strip():
            meta["no_text_reason"] = (
                "vbpl.vn has no HTML text for this document"
                + (f"; its file ({d.get('documentContentFileName')}) is served only through vbpl.vn's API, "
                   "which robots.txt disallows" if d.get("documentContentFileName") else "")
                + (f". Read it from the Gazette: legal_search(jurisdiction='VN', query='{d.get('docNum')}', "
                   "source='vn.congbao')." if d.get("docNum") else ".")
            )
        return LegalDoc(
            ref=self.ref(doc_id), title=d.get("title") or d.get("docNum") or doc_id, source=self.id, text=text,
            url=canon.group(1) if canon else url, kind="legislation", language=lang, metadata=meta,
        )


SOURCES = [CongbaoSource(), VbplSource()]
