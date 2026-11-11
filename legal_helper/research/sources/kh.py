"""Cambodia: Open Development Cambodia's law library (CKAN).

There is no reachable official database: the Council of Ministers, the
Ministry of Justice and the Senate refuse requests from this host (WAF
"Request Rejected"), and the Royal Gazette is not online as a database. Open
Development Cambodia (data.opendevelopmentcambodia.net), an open-data project,
curates Cambodian laws, royal decrees, sub-decrees and prakas with metadata
(document number, promulgation date, status, Royal Gazette reference) and the
PDFs — through the standard CKAN Action API (``package_search`` filtered to
``type:laws_record`` and Cambodia, ``package_show``). Curated, not official:
cite the instrument by its number and say the text was read from ODC.

Text: English PDFs are unofficial translations, read through the shared PDF
reader (OCR for English scans). Many PDFs are bilingual and the Khmer pages
either are scans or carry a legacy-font text layer that decodes as Latin
garbage; such pages are replaced by a marker, never passed off as text. Khmer
scans cannot be read here — RapidOCR has no Khmer model and Tesseract (khm)
is not installed — so a Khmer-only scan comes back with ``no_text_reason``.
No key.
"""

from __future__ import annotations

import re
from typing import Any

import httpx

from ...connectors.base import USER_AGENT, request_with_retry
from ..base import Hit, LegalDoc, Source, SourceUnavailable
from ..textify import bytes_to_text, normalize_lines

_CKAN = "https://data.opendevelopmentcambodia.net/api/3/action"
_SITE = "https://data.opendevelopmentcambodia.net"
_UA = {"User-Agent": USER_AGENT}
_FQ = 'type:laws_record AND odm_spatial_range:"[\\"kh\\"]"'
_STATUS = {"signed_and_in_effect": "signed and in effect", "in_effect": "in effect", "adopted": "adopted",
           "validated": "validated", "under_negotiation": "under negotiation", "revoked": "revoked",
           "repealed": "repealed", "amended": "amended", "draft": "draft"}
_SW = set("the of and to in a shall be by for or this on with as any that is are which an its from at such "
          "article law parties party agreement royal government ministry kingdom cambodia".split())
_KHMER = re.compile(r"[ក-៿]")


def _ckan(action: str, params: dict[str, Any]) -> dict[str, Any]:
    try:
        resp = request_with_retry("GET", f"{_CKAN}/{action}", params=params, headers=_UA,
                                  timeout=httpx.Timeout(45.0, connect=10.0))
        data = resp.json()
    except (httpx.HTTPError, ValueError) as exc:
        raise SourceUnavailable(f"Open Development Cambodia API unreachable: {type(exc).__name__}: {exc}") from exc
    if not data.get("success"):
        raise SourceUnavailable(f"ODC CKAN {action} failed: {str(data.get('error'))[:200]}")
    return data.get("result") or {}


def _tr(v: Any, lang: str = "en") -> str:
    if isinstance(v, dict):
        return (v.get(lang) or v.get("en") or v.get("km") or "").strip()
    return str(v or "").strip()


def _langs(pkg: dict[str, Any]) -> list[str]:
    langs: list[str] = []
    for r in pkg.get("resources") or []:
        for lg in r.get("odm_language") or []:
            if lg not in langs:
                langs.append(lg)
    return langs or list(pkg.get("odm_language") or [])


class OdcSource(Source):
    id = "kh.odc"
    name = "Open Development Cambodia — law library (curated, not official)"
    jurisdictions = ("KH",)
    kinds = ("legislation",)
    modes = ("keyword",)
    languages = ("en", "km")
    homepage = f"{_SITE}/en/laws_record"
    coverage = (
        "Cambodian laws, royal decrees (Kram/Reach Kret), sub-decrees, prakas, curated by Open Development "
        "Cambodia (NOT an official source; no official Cambodian database is reachable). Metadata: number "
        "(NS/RKM/0108/003), promulgation date, status, Royal Gazette reference. Text: English (unofficial "
        "translations) where ODC has it; Khmer scans cannot be read here (no Khmer OCR). Search in English. "
        "filters: document_type (law|royal_decree|sub-decree|prakas…), status."
    )
    probe = {"query": "civil aviation", "kind": "legislation",
             "ref": "law-on-enactment-of-the-approval-of-asean-multilateral-agreement-on-the-full-liberalisation-of-air-",
             "section": "Article 3", "min_chars": 5000}

    def search(self, query, *, kind="any", mode="keyword", limit=10, filters=None):
        if kind == "case":
            return []
        filters = filters or {}
        fq = _FQ
        if filters.get("document_type"):
            fq += f' AND odm_document_type:"{filters["document_type"]}"'
        if filters.get("status"):
            fq += f' AND odm_laws_status:"{filters["status"]}"'
        res = _ckan("package_search", {"q": query, "fq": fq, "rows": max(1, min(limit, 30))})
        hits = []
        for p in res.get("results") or []:
            name = p.get("name")
            if not name:
                continue
            langs = _langs(p)
            num = _tr(p.get("odm_document_number"))
            status = p.get("odm_laws_status")
            hits.append(Hit(
                ref=self.ref(name), title=_tr(p.get("title_translated")) or p.get("title") or name, source=self.id,
                kind="legislation", url=f"{_SITE}/en/laws_record/{name}",
                date=p.get("odm_promulgation_date") or None, status=_STATUS.get(status, status) if status else None,
                citation=num or None, snippet=_tr(p.get("notes_translated"))[:400],
                extra={k: v for k, v in {
                    "type": p.get("odm_document_type"), "languages": langs or None,
                    "english_text": "en" in langs or None,
                    "gazette": p.get("odm_laws_official_publication_reference") or None,
                }.items() if v},
            ))
        return hits

    def fetch(self, native_id, *, language=None):
        name = native_id.strip()
        if not re.fullmatch(r"[a-z0-9_\-]{2,120}", name):
            raise ValueError("ODC refs are dataset names like 'law-on-civil-aviation'; take them from legal_search")
        pkg = _ckan("package_show", {"id": name})
        want = (language or "").lower()[:2]
        pdfs = [r for r in pkg.get("resources") or [] if (r.get("format") or "").upper() == "PDF" or
                str(r.get("url") or "").lower().endswith(".pdf")]
        def lang_of(r: dict[str, Any]) -> list[str]:
            return list(r.get("odm_language") or [])
        order = sorted(pdfs, key=lambda r: (0 if (want and want in lang_of(r)) else
                                            1 if "en" in lang_of(r) else 2))
        meta: dict[str, Any] = {k: v for k, v in {
            "citation": _tr(pkg.get("odm_document_number")) or None,
            "citation_km": _tr(pkg.get("odm_document_number"), "km") or None,
            "title_km": _tr(pkg.get("title_translated"), "km") or None,
            "type": pkg.get("odm_document_type"),
            "date": pkg.get("odm_promulgation_date") or None,
            "effective": pkg.get("odm_effective_date") or None,
            "status": _STATUS.get(pkg.get("odm_laws_status"), pkg.get("odm_laws_status")),
            "gazette": pkg.get("odm_laws_official_publication_reference") or None,
            "authority": ("Curated copy from Open Development Cambodia, not an official publication; English "
                          "texts are unofficial translations. Khmer is the only official language."),
            "languages": _langs(pkg) or None,
        }.items() if v}
        text, lang, reason = "", "en", ""
        for r in order[:2]:
            langs = lang_of(r)
            try:
                resp = request_with_retry("GET", r["url"], headers=_UA, timeout=httpx.Timeout(180.0, connect=10.0))
            except httpx.HTTPError as exc:
                reason = f"PDF download failed ({type(exc).__name__}: {exc})"
                continue
            if not resp.content.startswith(b"%PDF"):
                reason = "the resource is not a PDF"
                continue
            if "en" in langs:
                text, note = _english_pages(bytes_to_text(resp.content, "application/pdf", name_hint="x.pdf"))
                lang = "en"
                if note:
                    meta["text_note"] = note
            else:
                text, reason = _khmer_text(resp.content)
                lang = "km"
            if text.strip():
                meta["file"] = r["url"]
                break
        if not text.strip():
            meta["no_text_reason"] = reason or "ODC has no PDF for this instrument"
        if want == "km" and lang != "km":
            meta["note"] = "No readable Khmer text; the English (unofficial) text is returned."
        return LegalDoc(
            ref=self.ref(name), title=_tr(pkg.get("title_translated")) or pkg.get("title") or name, source=self.id,
            text=text, url=f"{_SITE}/en/laws_record/{name}", kind="legislation", language=lang, metadata=meta,
            sections=_article_sections(text) or None, ttl=30 * 24 * 3600,
        )


def _english_pages(text: str) -> tuple[str, str]:
    """Keep English pages; replace pages that are Khmer in a legacy font (Latin garbage) or OCR noise."""
    parts = re.split(r"^(\[page \d+[^\]]*\])$", text, flags=re.M)
    out = [parts[0]] if parts and not parts[0].startswith("[page") else []
    dropped: list[str] = []
    for i in range(1, len(parts) - 1, 2):
        marker, body = parts[i], parts[i + 1]
        if _KHMER.search(body):
            out.append(f"{marker}\n{body}")
            continue
        toks = re.findall(r"[A-Za-z]+", body)
        ratio = sum(1 for t in toks if t.lower() in _SW) / max(1, len(toks))
        if len(toks) >= 20 and ratio < 0.08:
            num = re.search(r"\d+", marker)
            dropped.append(num.group(0) if num else "?")
            out.append(f"{marker}\n[Khmer page in a legacy font encoding or scan — not machine-readable here; see the PDF]\n")
            continue
        out.append(f"{marker}\n{body}")
    note = ""
    if dropped:
        note = (f"{len(dropped)} page(s) are Khmer text this reader cannot decode (legacy font or scan) and were "
                f"replaced by a marker: {', '.join(dropped[:30])}{'…' if len(dropped) > 30 else ''}.")
    return normalize_lines("".join(out)), note


def _khmer_text(data: bytes) -> tuple[str, str]:
    """A Khmer PDF's own Unicode text layer, or why there is none (no Khmer OCR here)."""
    import hashlib
    from pathlib import Path

    from ...documents.pdf import read_pdf_text
    from ..textify import raw_dir

    path = raw_dir() / f"{hashlib.sha256(data).hexdigest()[:32]}.pdf"
    if not path.exists():
        path.write_bytes(data)
    text = read_pdf_text(Path(path), ocr=False)
    khmer = len(_KHMER.findall(text))
    if khmer >= 200:
        return normalize_lines(text), ""
    return "", ("the Khmer PDF is a scan (or has no Unicode text layer) and no Khmer OCR is available here "
                "(RapidOCR has no Khmer model; Tesseract with 'khm' is not installed); open the PDF")


_ART = re.compile(r"^(?:Article|ARTICLE|Art\.)\s*(\d{1,4}(?:\s?(?:bis|ter|new|-\d+))?)\b\s*[.:\-–—]?")
_ART_KM = re.compile(r"^មាត្រា\s*([0-9០-៩]{1,4})")
_CONT = re.compile(r"^(?:CHAPTER|Chapter|PART|Part|SECTION|Section|BOOK|Book|TITLE|Title)\s+[IVXLC\d]+\b|^ជំពូក")
_KM_DIGITS = str.maketrans("០១២៣៤៥៦៧៨៩", "0123456789")


def _article_sections(text: str) -> list[dict[str, Any]]:
    if not text:
        return []
    marks: list[tuple[int, str, str, str]] = []
    pos = 0
    prev = ""
    for raw in text.split("\n"):
        line = raw.strip()
        start = pos
        pos += len(raw) + 1
        before, prev = prev, (line or prev)
        m = _ART.match(line) or _ART_KM.match(line)
        # A heading follows a finished sentence, a heading or a page break; "Article 3 (…) of
        # this" after "…referred to in" is a wrapped cross-reference.
        starts_block = (not before or before.startswith("[page") or before[-1] in ".:;)]" or before.isupper()
                        or len(before) < 40)
        if m and len(line) <= 200 and starts_block:
            key = re.sub(r"\s+", "", m.group(1)).translate(_KM_DIGITS)
            word = "មាត្រា" if line.startswith("មាត្រា") else "Article"
            marks.append((start, f"{word} {key}", key, "article"))
        elif _CONT.match(line) and len(line) <= 120:
            marks.append((start, line, "", "container"))
    secs = []
    for i, (start, label, key, level) in enumerate(marks):
        end = marks[i + 1][0] if i + 1 < len(marks) else len(text)
        secs.append({"label": label, "key": key, "heading": label, "start": start, "end": end, "level": level})
    return secs


SOURCES = [OdcSource()]
