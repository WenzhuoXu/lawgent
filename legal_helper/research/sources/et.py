"""Ethiopia: federal proclamations from the Federal Negarit Gazeta mirror.

There is no official search API. Search matches the curated catalogue in
``_ethiopia_catalog`` (aviation-weighted) and accepts any proclamation
number; fetch reads metaappz's typed English transcript and, where there is
none, the gazette PDF — through the shared reader, so an image-only scan is
OCR'd (RapidOCR reads the English column; the Amharic column is not read).
"""

from __future__ import annotations

import re

import httpx

from ...connectors.base import USER_AGENT, request_with_retry
from ._ethiopia_catalog import _METAAPPZ, _extract_transcript, _normalize_proclamation, search_catalogue
from ..base import Hit, LegalDoc, Source
from ..textify import bytes_to_text, normalize_lines


class EthiopiaSource(Source):
    id = "et.negarit"
    probe = {"query": "civil aviation", "ref": "616/2008", "section": "Article 2"}
    name = "Federal Negarit Gazeta (metaappz mirror)"
    jurisdictions = ("ET",)
    kinds = ("legislation",)
    modes = ("keyword",)
    languages = ("en", "am")
    homepage = _METAAPPZ
    coverage = (
        "Federal proclamations by number ('616/2008', '1179/2020') or by topic from a small curated "
        "catalogue. Typed English transcript where one exists, otherwise the bilingual gazette scan "
        "read by OCR (English column)."
    )

    def search(self, query, *, kind="any", mode="keyword", limit=10, filters=None):
        hits = []
        parsed = _normalize_proclamation(query)
        for r in search_catalogue(query, limit):
            hits.append(Hit(
                ref=self.ref(r["proclamation"]), title=r.get("title") or r["proclamation"], source=self.id,
                url=r.get("pdf"), snippet=(r.get("summary") or "")[:400],
                citation=f"Proclamation No. {r['proclamation']}",
            ))
        if parsed and not any(h.ref.endswith(f"{parsed[0]}/{parsed[1]}") for h in hits):
            num, year, _slug = parsed
            hits.insert(0, Hit(
                ref=self.ref(f"{num}/{year}"), title=f"Proclamation No. {num}/{year}", source=self.id,
                citation=f"Proclamation No. {num}/{year}",
                snippet="Not in the curated catalogue; fetch reads it from the gazette mirror by number.",
            ))
        return hits[:limit]

    def fetch(self, native_id, *, language=None):
        parsed = _normalize_proclamation(native_id)
        if not parsed:
            raise ValueError("Ethiopian refs are proclamation numbers like '616/2008'")
        num, year, slug = parsed
        txt_url = f"{_METAAPPZ}/References/ethiopian_laws/federal/{slug}/en/txt"
        pdf_url = f"{_METAAPPZ}/pdf/ethiopian_laws/federal/{year}/{slug}.pdf"
        text, how = "", ""
        try:
            r = request_with_retry("GET", txt_url, headers={"User-Agent": USER_AGENT})
            text = normalize_lines(_extract_transcript(r.text).replace(". ", ".\n"))
            how = "typed transcript"
        except httpx.HTTPError:
            text = ""
        if len(text) < 500:
            r = request_with_retry("GET", pdf_url, headers={"User-Agent": USER_AGENT},
                                   timeout=httpx.Timeout(120.0, connect=10.0))
            text = bytes_to_text(r.content, r.headers.get("content-type", ""), name_hint=pdf_url)
            how = "gazette PDF (OCR where the page is a scan)"
        # Ge'ez-script lines carry no English; keep them out of keyword matching noise.
        lines = [ln for ln in text.split("\n") if not re.fullmatch(r"[ሀ-፿\s\d.,;:()/-]+", ln or "x")]
        text = "\n".join(lines)
        # Proclamations number their articles bare ("2. Definitions").
        meta = {"read_from": how, "pdf": pdf_url, "bare_section_numbers": True}
        if not text.strip():
            meta["no_text_reason"] = "neither a transcript nor readable gazette text"
        return LegalDoc(
            ref=self.ref(f"{num}/{year}"), title=f"Proclamation No. {num}/{year}", source=self.id, text=text,
            url=txt_url if how == "typed transcript" else pdf_url, language="en", metadata=meta,
            ttl=90 * 24 * 3600,
        )


SOURCES = [EthiopiaSource()]
