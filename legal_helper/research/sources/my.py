"""Malaysia: the Judiciary's eJudgment portal (grounds of judgment).

Search: the portal's documented ASMX JSON service
(``ejudgment.kehakiman.gov.my/EJudgmentWeb/eJudgmentService.asmx``,
``GetEJudgmentPortalSearchList``) — it matches the catchwords ("KeyWord"),
parties and case number, not the judgment text, and returns 20 per page in
date order (``Ordering`` is required; the service has no relevance order).

Fetch: the grounds-of-judgment PDF from the e-Filing downloader
(``efs.kehakiman.gov.my/EFSWeb/DocDownloader.aspx``). That path is disallowed
for crawlers in efs.kehakiman.gov.my/robots.txt, so it is read only for one
document per explicit legal_fetch, never during search or the health probe.
Pinpoints are the judgment's own paragraph numbers ("[12]", "12."). Many PDFs
are scans whose only text layer is the e-Filing serial-number stamp; those
pages are read by OCR.

Not covered: legislation. Laws of Malaysia Online (lom.agc.gov.my) signs every
link and encrypts its search and listing responses in the browser, a
deliberate anti-automation design, so there is no Malaysian legislation
source here. No key.
"""

from __future__ import annotations

import html as _html
import re
import threading
from datetime import datetime, timezone
from typing import Any, Optional

import httpx

from ...connectors.base import USER_AGENT, request_with_retry
from ..base import Hit, LegalDoc, Source, SourceUnavailable
from ..textify import bytes_to_text, normalize_lines

_SVC = "https://ejudgment.kehakiman.gov.my/EJudgmentWeb/eJudgmentService.asmx/GetEJudgmentPortalSearchList"
_DOC = "https://efs.kehakiman.gov.my/EFSWeb/DocDownloader.aspx"
_PORTAL = "https://ejudgment.kehakiman.gov.my/ejudgmentweb/PortalJudgmentList.aspx"
_UUID = re.compile(r"^[0-9a-fA-F]{8}-[0-9a-fA-F]{4}-[0-9a-fA-F]{4}-[0-9a-fA-F]{4}-[0-9a-fA-F]{12}$")
_COURTS = {"fc": "FC", "federal": "FC", "federal court": "FC", "mahkamah persekutuan": "FC",
           "coa": "COA", "ca": "COA", "court of appeal": "COA", "mahkamah rayuan": "COA",
           "hc": "HC", "high": "HC", "high court": "HC", "mahkamah tinggi": "HC"}
_COURT_EN = {"Mahkamah Persekutuan": "Federal Court", "Mahkamah Rayuan": "Court of Appeal",
             "Mahkamah Tinggi": "High Court", "Mahkamah Sesyen": "Sessions Court",
             "Mahkamah Majistret": "Magistrates' Court"}
_HITS: dict[str, dict[str, Any]] = {}
_HITS_LOCK = threading.Lock()
_STAMP = re.compile(
    r"S/N\s+\S+\s*\*\*Note\s*:\s*Serial number will be used to verify the originality of this document "
    r"via\s+eFILING portal", re.I)
_STAMP_HEAD = re.compile(r"^\d{2}/\d{2}/\d{4}\s+\d{2}:\d{2}:\d{2}(?:\s+\S+\s+Kand\.\s*\d+)?\s*$")


def _plain(s: Any) -> str:
    s = re.sub(r"<br\s*/?>", "\n", str(s or ""), flags=re.I)
    s = re.sub(r"<[^>]+>", "", s)
    return _html.unescape(s)


def _ms_date(v: Any) -> Optional[str]:
    m = re.search(r"/Date\((-?\d+)\)/", str(v or ""))
    if not m:
        return None
    return datetime.fromtimestamp(int(m.group(1)) / 1000, tz=timezone.utc).date().isoformat()


def _parties(raw: Any) -> tuple[str, list[str], list[str]]:
    text = _plain(raw)
    parts = re.split(r"\n\s*(?:RESPONDEN|RESPONDENT|DEFENDAN|DEFENDANT)\s*\n", text, maxsplit=1)
    def names(block: str) -> list[str]:
        out = []
        for ln in block.split("\n"):
            ln = re.sub(r"^\s*\d+\s*\.\s*\)\s*", "", ln).strip()
            if ln and not re.fullmatch(r"(PERAYU|APPELLANT|PLAINTIF|PLAINTIFF|PEMOHON|APPLICANT)", ln, re.I):
                out.append(ln)
        return out
    first = names(parts[0])
    second = names(parts[1]) if len(parts) > 1 else []
    def short(ns: list[str]) -> str:
        if not ns:
            return ""
        n0 = re.sub(r"\s*\(.*$", "", ns[0]).strip()
        return n0 + (" & Ors" if len(ns) > 2 else " & Anor" if len(ns) == 2 else "")
    title = " v ".join(x for x in (short(first), short(second)) if x)
    return title, first, second


def _case_no(raw: Any) -> tuple[str, str]:
    text = _plain(raw)
    m = re.match(r"^\s*(.*?)\s*(?:\n\s*\((.*?)\))?\s*$", text, re.S)
    num = (m.group(1) if m else text).strip()
    court = (m.group(2) if m and m.group(2) else "").strip()
    return num, court


def _search_page(query: str, *, court: str, judge: str, page: int = 1) -> dict[str, Any]:
    body = {"Param": {
        "CourtCategory": "", "Court": "", "JurisdictionType": court, "DateOfAPFrom": None, "DateOfAPTo": None,
        "DateOfResultFrom": None, "DateOfResultTo": None, "Search": query, "JudgeName": judge, "CaseType": "",
        "CurrPage": page, "Ordering": "DATE_OF_AP_DESC",
    }}
    try:
        # The service often takes 7-30 s to answer; the default 15 s read timeout retried it.
        resp = request_with_retry("POST", _SVC, json_body=body, headers={
            "User-Agent": USER_AGENT, "Content-Type": "application/json; charset=utf-8"},
            timeout=httpx.Timeout(75.0, connect=10.0), attempts=2)
        return (resp.json() or {}).get("d") or {}
    except (httpx.HTTPError, ValueError) as exc:
        raise SourceUnavailable(f"eJudgment search service unreachable: {type(exc).__name__}: {exc}") from exc


class EJudgmentSource(Source):
    id = "my.ejudgment"
    name = "eJudgment — Judiciary of Malaysia (ejudgment.kehakiman.gov.my)"
    jurisdictions = ("MY",)
    kinds = ("case",)
    modes = ("keyword",)
    languages = ("en", "ms")
    homepage = _PORTAL
    coverage = (
        "Grounds of judgment of the Federal Court, Court of Appeal and High Court (mostly 2018 on), in "
        "English or Bahasa Malaysia. Search matches catchwords, parties and case numbers (not the judgment "
        "text), newest first: use legal terms ('Montreal Convention', 'carriage by air', 'AirAsia'). Fetch "
        "reads the judgment PDF (one per request; scans are OCR'd), cut at its paragraph numbers. No "
        "legislation (Laws of Malaysia Online is deliberately not automatable). filters: court (FC|COA|HC), judge."
    )
    probe = {"query": "Montreal Convention", "kind": "case", "search_only": True}

    def search(self, query, *, kind="any", mode="keyword", limit=10, filters=None):
        if kind == "legislation":
            return []
        filters = filters or {}
        court = _COURTS.get(str(filters.get("court") or "").strip().lower(), "ALL")
        judge = str(filters.get("judge") or "")
        q = query.strip()
        items = _search_page(q, court=court, judge=judge).get("ListOfSearchItem") or []
        words = [w for w in re.findall(r"[\w'.-]+", q) if len(w) > 2]
        if not items and len(words) > 1:
            # The service matches the phrase as typed; widen to the longest word and keep
            # items whose catchwords/parties contain every word.
            anchor = max(words, key=len)
            pool: list[dict[str, Any]] = []
            for page in (1, 2):
                d = _search_page(anchor, court=court, judge=judge, page=page)
                pool += d.get("ListOfSearchItem") or []
                if page >= int(d.get("TOTAL_PAGE") or 1):
                    break
            low = [w.lower() for w in words]
            items = [it for it in pool if all(
                w in (_plain(it.get("KeyWord")) + " " + _plain(it.get("Parties")) + " " + _plain(it.get("CaseNo"))).lower()
                for w in low)]
        hits: list[Hit] = []
        for it in items:
            docs = [d for d in it.get("ListOfAPDoc") or [] if d.get("DocumentID") and not d.get("IsExpunged")]
            if not docs:
                continue
            title, first, second = _parties(it.get("Parties"))
            num, court_ms = _case_no(it.get("CaseNo"))
            court_name = _COURT_EN.get(court_ms, court_ms)
            date = _ms_date(it.get("DateOfResult"))
            meta = {
                "case_number": num, "court": court_name, "decision_date": date,
                "judgment_published": _ms_date(it.get("DateOfAP")), "judge": it.get("Judge"),
                "coram": [x for x in _plain(it.get("CorumJudge")).split("\n") if x.strip()] or None,
                "parties": {"first": first, "second": second}, "catchwords": _plain(it.get("KeyWord")).strip(),
                "title": title,
            }
            for d in docs:
                with _HITS_LOCK:
                    _HITS[d["DocumentID"].lower()] = {**meta, "document": d.get("APDocName"),
                                                      "prepared_by": d.get("APPreparedBy"),
                                                      "decision": d.get("DecisionCategory")}
            main = docs[0]
            hits.append(Hit(
                ref=self.ref(main["DocumentID"].lower()), title=title or num, source=self.id, kind="case",
                url=_PORTAL, date=date, citation=f"{num} ({court_name})" if court_name else num,
                snippet=" ".join(_plain(it.get("KeyWord")).split())[:500],
                extra={k: v for k, v in {
                    "court": court_name, "judge": it.get("Judge"), "document": main.get("APDocName"),
                    "decision": main.get("DecisionCategory") or None,
                    "other_judgments": [self.ref(d["DocumentID"].lower()) for d in docs[1:]] or None,
                }.items() if v},
            ))
            if len(hits) >= limit:
                break
        return hits

    def fetch(self, native_id, *, language=None):
        doc_id = native_id.strip().lower()
        if not _UUID.match(doc_id):
            raise ValueError("eJudgment refs are document ids (my.ejudgment:<uuid>); take them from legal_search")
        resp = request_with_retry("GET", _DOC, params={"DocumentID": doc_id, "Inline": "true"},
                                  headers={"User-Agent": USER_AGENT}, timeout=httpx.Timeout(120.0, connect=10.0))
        ctype = resp.headers.get("content-type", "")
        if "pdf" not in ctype.lower() and not resp.content.startswith(b"%PDF"):
            raise ValueError(f"eJudgment document {doc_id} did not come back as a PDF ({ctype or 'no type'})")
        text, how = _judgment_text(resp.content)
        with _HITS_LOCK:
            known = dict(_HITS.get(doc_id) or {})
        title = known.pop("title", "") or _title_from_text(text) or doc_id
        meta: dict[str, Any] = {k: v for k, v in known.items() if v}
        meta["date"] = known.get("decision_date")
        meta["citation"] = (f"{known.get('case_number')} ({known.get('court')})"
                            if known.get("case_number") else None)
        meta["read_from"] = how
        meta = {k: v for k, v in meta.items() if v}
        if not text.strip():
            meta["no_text_reason"] = "the judgment PDF has no readable text (scan; OCR found nothing)"
        lang = "en" if len(re.findall(r"\b(the|of|and)\b", text[:20000], re.I)) > 30 else "ms"
        return LegalDoc(
            ref=self.ref(doc_id), title=title, source=self.id, text=text,
            url=f"{_DOC}?DocumentID={doc_id}&Inline=true", kind="case", language=lang, metadata=meta,
        )


def _strip_stamps(text: str) -> str:
    out = []
    for ln in text.split("\n"):
        s = _STAMP.sub("", ln).strip()
        if _STAMP_HEAD.match(s) or re.fullmatch(r"S/N\s+[A-Za-z0-9+/=]{12,}", s) or re.match(
                r"^\*{0,2}\s*Note\s*:\s*Serial number will be used to verify", s, re.I):
            continue
        s = re.sub(r"^\d{2}/\d{2}/\d{4}\s+\d{2}:\d{2}:\d{2}\s+\S+\s+Kand\.\s*\d+\s*", "", s)
        if re.fullmatch(r"(?:Page|Muka surat)\s+\d+\s+(?:of|daripada)\s+\d+", s, re.I):
            continue
        out.append(s)
    return normalize_lines("\n".join(out))


def _body_chars(text: str) -> int:
    return sum(1 for ch in re.sub(r"^\[page [^\]]*\]$", "", text, flags=re.M) if not ch.isspace())


def _judgment_text(data: bytes) -> tuple[str, str]:
    """PDF → text; pages whose only text is the e-Filing stamp are OCR'd.

    The shared reader treats a full-page scan carrying a >100-character stamp
    as a text page, so it would return 30 pages of serial numbers.
    """
    text = _strip_stamps(bytes_to_text(data, "application/pdf", name_hint="x.pdf"))
    pages = max(1, len(re.findall(r"^\[page \d+", text, flags=re.M)))
    if _body_chars(text) >= 400 * pages:
        return text, "PDF text layer"
    try:
        return _ocr_scan(data), "PDF scan read by OCR (RapidOCR); check names, amounts and dates"
    except Exception as exc:  # noqa: BLE001 — return what the text layer had
        return text, f"PDF text layer (OCR failed: {type(exc).__name__}: {exc})"


def _ocr_scan(data: bytes) -> str:
    import hashlib

    import pypdfium2 as pdfium

    from ...documents.ocr import ocr_pdf_page, ocr_unavailable_reason
    from ...documents.pdf import page_layers
    from ..textify import raw_dir

    reason = ocr_unavailable_reason()
    if reason:
        raise RuntimeError(reason)
    sha = hashlib.sha256(data).hexdigest()
    path = raw_dir() / f"{sha[:32]}.pdf"
    if not path.exists():
        path.write_bytes(data)
    pdf = pdfium.PdfDocument(str(path))
    blocks: list[str] = []
    ocr_pages = 0
    try:
        layers = page_layers(pdf, list(range(len(pdf))))
        for layer in layers:
            own = _strip_stamps(layer.text)
            if sum(1 for ch in own if not ch.isspace()) >= 100 or layer.image_share < 0.25:
                blocks.append(f"[page {layer.number}]\n{own}")
                continue
            result = ocr_pdf_page(pdf, layer.number - 1, sha)
            ocr_pages += 1
            blocks.append(f"[page {layer.number} · OCR]\n{_strip_stamps(result.text)}")
    finally:
        pdf.close()
    head = (f"[OCR: {ocr_pages} of {len(blocks)} pages are scans read by OCR; recognition errors are possible — "
            "check names, amounts, dates and paragraph numbers against the PDF]")
    return normalize_lines(head + "\n\n" + "\n\n".join(blocks))


def _title_from_text(text: str) -> str:
    m = re.search(r"\bBETWEEN\b\s*\n(.{3,120}?)\s*(?:\.{2,}|…)?\s*(?:APPELLANT|PLAINTIFF|APPLICANT|PERAYU|PLAINTIF|PEMOHON)", text, re.S)
    n = re.search(r"\bAND\b\s*\n(.{3,120}?)\s*(?:\.{2,}|…)?\s*(?:RESPONDENT|DEFENDANT|RESPONDEN|DEFENDAN)", text, re.S)
    if m and n:
        a = " ".join(m.group(1).split()).strip(". ")
        b = " ".join(n.group(1).split()).strip(". ")
        return f"{a} v {b}"
    return ""


SOURCES = [EJudgmentSource()]
