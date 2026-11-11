"""Indonesia: JDIH of the Ministry of State Secretariat (jdih.setneg.go.id).

The State Secretariat promulgates Undang-Undang, Perppu, Peraturan
Pemerintah, Peraturan Presiden, Keputusan / Instruksi Presiden; its JDIH
(legal documentation network) serves them through a JSON API behind the
site (robots.txt allows everything): title search (``/api/hukumproduk/
produkhukum`` with ``status_hukum``), the record (``detaildata``: State
Gazette numbers, the repeal / amendment relations as JDIH ids, the file), and
the PDF of the official copy (``/api/hukumproduk/pdf``).

The PDFs are scans of the signed salinan with a text layer made by OCR: it is
noisy ("2OO9", "batrwa", "Pasal TT" for Pasal 77) and each page ends with a
catchword repeating the start of the next page ("Pasal 73 . . ."). The text is
kept as the PDF has it; provisions are cut at "Pasal N" lines (catchwords and
OCR-mangled numbers resolved by their sequence), and the Penjelasan
(elucidation) is indexed separately as E<N>. Quote with care and check the PDF.

Not covered: ministerial regulations (peraturan menteri) — the Kemenhub
aviation regulations belong to the aviation pack — and case law (Putusan MA
blocks AI agents by robots.txt and sits behind Cloudflare). No English text.
"""

from __future__ import annotations

import re
import threading
from typing import Any, Optional

import httpx

from ...connectors.base import USER_AGENT, request_with_retry
from ..base import Hit, LegalDoc, Source, SourceUnavailable
from ..textify import bytes_to_text

_BASE = "https://jdih.setneg.go.id"
_API = f"{_BASE}/api/hukumproduk"
_UA = {"User-Agent": USER_AGENT}
_STATUS = {"berlaku": "berlaku (in force)", "sebagian": "berlaku sebagian (partly amended or revoked)",
           "dicabut": "dicabut (repealed)", "diubah": "diubah (amended)"}
_JNS_WORDS = [
    (r"peraturan\s+pemerintah\s+pengganti\s+undang[-\s]*undang|perp?pu", "PERPU"),
    (r"undang[-\s]*undang|\buu\b", "UU"),
    (r"peraturan\s+pemerintah|\bpp\b", "PP"),
    (r"peraturan\s+presiden|perpres", "PERPRES"),
    (r"keputusan\s+presiden|keppres", "KEPPRES"),
    (r"instruksi\s+presiden|inpres", "INPRES"),
]
_NUMBER = re.compile(r"(?:no(?:mor)?\.?\s*)?(\d{1,4})\s*(?:/|\s+tahun\s+|\s+th\.?\s+)\s*(\d{4})", re.I)
_IDS: dict[str, str] = {}
_IDS_LOCK = threading.Lock()
_OCR_DIGITS = str.maketrans({"O": "0", "o": "0", "D": "0", "I": "1", "l": "1", "i": "1", "|": "1", "L": "1",
                             "t": "1", "T": "7", "S": "5", "s": "5", "B": "8", "Z": "2", "z": "2", "g": "9"})


def _post(path: str, body: dict[str, Any], *, timeout: float = 45.0) -> dict[str, Any]:
    try:
        resp = request_with_retry("POST", f"{_API}/{path}", json_body=body, headers=_UA,
                                  timeout=httpx.Timeout(timeout, connect=10.0))
        return resp.json() or {}
    except (httpx.HTTPError, ValueError) as exc:
        raise SourceUnavailable(f"JDIH Setneg API ({path}) unreachable: {type(exc).__name__}: {exc}") from exc


def _parse_citation(query: str) -> Optional[tuple[str, str, str]]:
    q = query.strip()
    jns = None
    for pat, code in _JNS_WORDS:
        if re.search(pat, q, re.I):
            jns = code
            break
    m = _NUMBER.search(q)
    if jns and m:
        return jns, str(int(m.group(1))), m.group(2)
    return None


def _native(jns: str, no: str, thn: str) -> str:
    return f"{jns.upper()}/{no}/{thn}"


def _title(jns_name: str, no: str, thn: str, tentang: str) -> str:
    return f"{jns_name} Nomor {no} Tahun {thn} tentang {(tentang or '').strip().title()}"


class JdihSetnegSource(Source):
    id = "idn.jdih"
    name = "JDIH Kementerian Sekretariat Negara (jdih.setneg.go.id)"
    jurisdictions = ("ID",)
    kinds = ("legislation",)
    modes = ("keyword",)
    languages = ("id",)
    homepage = _BASE
    coverage = (
        "Indonesian Undang-Undang, Perppu, Peraturan Pemerintah, Perpres, Keppres, Inpres (1945 on) with "
        "status_hukum (berlaku / berlaku sebagian / dicabut) and repeal-amendment links. Search titles in "
        "Bahasa Indonesia ('penerbangan', 'perlindungan konsumen') or by citation ('UU 1/2009', "
        "'Undang-Undang Nomor 1 Tahun 2009'). Text is the official scanned copy's OCR layer (noisy; check "
        "the PDF): Pasal N, Penjelasan as E<N>. filters: type (UU|PERPU|PP|PERPRES|KEPPRES|INPRES), year."
    )
    probe = {"query": "penerbangan", "kind": "legislation", "ref": "UU/1/2009", "section": "Pasal 1",
             "min_chars": 50000}

    def search(self, query, *, kind="any", mode="keyword", limit=10, filters=None):
        if kind == "case":
            return []
        filters = filters or {}
        hits: list[Hit] = []
        cit = _parse_citation(query)
        if cit:
            jns, no, thn = cit
            row = (_post("detaildata", {"jns": jns, "no": no, "thn": thn, "k": ""}).get("row") or [None])[0]
            if row:
                hits.append(self._hit(row))
        jns_f = filters.get("type")
        body = {
            "tentang": "" if cit else query.strip(), "p_lihan": "semua",
            "jns": [str(j).upper() for j in (jns_f if isinstance(jns_f, list) else [jns_f])] if jns_f else (
                [cit[0]] if cit else []),
            "thn": [str(filters["year"])] if filters.get("year") else ([cit[2]] if cit else []),
            "status": "", "terx": "All", "sortOrder": "desc", "length": max(1, min(limit, 30)), "start": 0,
        }
        if not cit or not hits:
            for row in _post("produkhukum", body).get("data") or []:
                if cit and str(row.get("no_peraturan")) != cit[1]:
                    continue
                hits.append(self._hit(row))
        seen: set[str] = set()
        out = []
        for h in hits:
            if h.ref not in seen:
                seen.add(h.ref)
                out.append(h)
        return out[:limit]

    def _hit(self, row: dict[str, Any]) -> Hit:
        jns, no, thn = row.get("jns") or "", str(row.get("no_peraturan") or ""), str(row.get("tahun") or "")
        native = _native(jns, no, thn)
        if row.get("idperaturan"):
            with _IDS_LOCK:
                _IDS[row["idperaturan"]] = native
        status = row.get("status_hukum")
        return Hit(
            ref=self.ref(native), title=_title(row.get("nama_jenis") or jns, no, thn, row.get("tentang") or ""),
            source=self.id, kind="legislation", url=f"{_BASE}/detail/{jns.lower()}/{no}/{thn}" if jns else _BASE,
            date=(row.get("diundangkan") or row.get("tgl_di") or "")[:10] or None,
            status=_STATUS.get(status, status) if status else None, citation=f"{jns} {no}/{thn}",
        )

    def fetch(self, native_id, *, language=None):
        m = re.fullmatch(r"([A-Za-z]+)/(\d{1,4})/(\d{4})", native_id.strip())
        if not m:
            cit = _parse_citation(native_id)
            if not cit:
                raise ValueError("JDIH refs look like 'UU/1/2009' (type/number/year); take them from legal_search")
            jns, no, thn = cit
        else:
            jns, no, thn = m.group(1).upper(), str(int(m.group(2))), m.group(3)
        d = _post("detaildata", {"jns": jns, "no": no, "thn": thn, "k": ""})
        row = (d.get("row") or [None])[0]
        if not row:
            raise ValueError(f"JDIH Setneg has no {jns} {no}/{thn}")
        files = [f for f in d.get("file") or [] if str(f.get("ext") or f.get("realName") or "").lower().endswith("pdf")]
        rel_names = {"mencabut": "repeals", "mengubah": "amends", "dicabut": "repealed_by", "diubah": "amended_by",
                     "diubah_sebagian": "partly_amended_by", "dicabut_sebagian": "partly_repealed_by",
                     "mencabutsebagian": "partly_repeals", "mengubahsebagian": "partly_amends"}
        relations: dict[str, list[str]] = {}
        with _IDS_LOCK:
            known = dict(_IDS)
        for k, label in rel_names.items():
            ids = [x.strip() for x in str(row.get(k) or "").split(",") if x.strip()]
            if ids:
                relations[label] = [f"{self.id}:{known[i]}" if i in known else f"JDIH id {i}" for i in ids]
        terkait = [x for x in str(row.get("terkait") or "").split(",") if x.strip()]
        status_raw = None
        try:
            for r in _post("produkhukum", {"tentang": row.get("tentang") or "", "p_lihan": "semua", "jns": [jns],
                                           "thn": [thn], "status": "", "terx": "All", "sortOrder": "desc",
                                           "length": 10, "start": 0}).get("data") or []:
                if str(r.get("no_peraturan")) == no:
                    status_raw = r.get("status_hukum")
                    break
        except SourceUnavailable:
            status_raw = None
        text, used, err = "", "", ""
        for f in files[:2]:
            name = f.get("realName") or f.get("name")
            try:
                resp = request_with_retry("GET", f"{_API}/pdf", params={"l": "uploads", "f": name,
                                                                         "fl": row.get("idperaturan")},
                                          headers=_UA, timeout=httpx.Timeout(180.0, connect=10.0))
            except httpx.HTTPError as exc:
                err = f"{type(exc).__name__}: {exc}"
                continue
            text = bytes_to_text(resp.content, resp.headers.get("content-type", ""), name_hint="x.pdf")
            if text.strip():
                used = name
                break
        meta: dict[str, Any] = {k: v for k, v in {
            "citation": f"{row.get('nama_jenis') or jns} Nomor {no} Tahun {thn}",
            "date": (row.get("diundangkan") or row.get("tgl_di") or "")[:10] or None,
            "gazette": (f"LN {thn} No. {row.get('ln')}" if row.get("ln") else None),
            "supplement": (f"TLN No. {row.get('tln')}" if row.get("tln") else None),
            "status": _STATUS.get(status_raw, status_raw) if status_raw else row.get("status"),
            "relations": relations or None,
            "implementing_or_related": f"{len(terkait)} instruments listed by JDIH" if terkait else None,
            "file": used or None,
            "text_quality": ("OCR layer of the scanned official copy: expect recognition errors (O for 0, "
                             "'batrwa' for 'bahwa'); check quotations against the PDF.") if used else None,
        }.items() if v}
        secs = _pasal_sections(text) if text else []
        if any(s.get("key", "").startswith("E") for s in secs):
            meta["penjelasan"] = "The elucidation of Pasal N reads as section='E<N>' (e.g. 'E5')."
        if not text.strip():
            meta["no_text_reason"] = f"the official PDF could not be read ({err or 'no file listed'})"
        return LegalDoc(
            ref=self.ref(_native(jns, no, thn)), title=_title(row.get("nama_jenis") or jns, no, thn, row.get("tentang") or ""),
            source=self.id, text=text, url=f"{_BASE}/detail/{jns.lower()}/{no}/{thn}", kind="legislation",
            language="id", metadata=meta, sections=secs or None, ttl=14 * 24 * 3600,
        )


_D = "0-9OoDIli|LtTSsBZzg"
_PASAL = re.compile(rf"^Pasa[l1Ii|]\s*([{_D}](?:\s?[{_D}]){{0,3}})\s*([A-Z])?\s*\.?\s*$")
_CATCH = re.compile(r"^Pasa[l1Ii|]\s*\S{1,6}\s*(?:[.,]\s*){2,}$|^Pasa[l1Ii|]\s*\S{1,6}\s*…$")
_CONTAINER = re.compile(r"^(?:BAB\s+[IVXLC]+[A-Z]?|Bagian\s+(?:Ke\w+|Pertama)|Paragraf\s+\d+)\s*$")


def _increasing(cands: list[tuple[int, int, str]]) -> set[int]:
    """Indices (into cands) of the longest run of non-decreasing article numbers.

    OCR drops or mangles some "Pasal N" lines and a stray line can look like
    one; the statute's own order is the evidence for which ones are real.
    """
    n = len(cands)
    if not n:
        return set()
    best = [1] * n
    prev = [-1] * n
    for i in range(n):
        for j in range(max(0, i - 60), i):
            a, b = cands[j], cands[i]
            ok = b[1] > a[1] or (b[1] == a[1] and b[2] > a[2])
            if ok and best[j] + 1 > best[i]:
                best[i], prev[i] = best[j] + 1, j
    k = max(range(n), key=lambda i: best[i])
    keep = set()
    while k >= 0:
        keep.add(k)
        k = prev[k]
    return keep


def _pasal_sections(text: str) -> list[dict[str, Any]]:
    """Cut at "Pasal N" lines of the OCR text; the Penjelasan's Pasal are keyed E<N>."""
    lines = text.split("\n")
    offsets, pos = [], 0
    for ln in lines:
        offsets.append(pos)
        pos += len(ln) + 1
    cands: list[tuple[int, int, str]] = []  # (line index, number, letter)
    split = None
    for i, raw in enumerate(lines):
        line = raw.strip()
        if split is None and line == "PENJELASAN" and len(cands) > 3:
            split = i
            continue
        if not line or _CATCH.match(line):
            continue
        m = _PASAL.match(line)
        if m:
            fixed = re.sub(r"\s", "", m.group(1)).translate(_OCR_DIGITS)
            if fixed.isdigit():
                cands.append((i, int(fixed), m.group(2) or ""))
    body = [c for c in cands if split is None or c[0] < split]
    expl = [c for c in cands if split is not None and c[0] > split]
    marks: list[tuple[int, str, str, str, str]] = []
    for group, is_expl in ((body, False), (expl, True)):
        keep = _increasing(group)
        for idx, (i, num, letter) in enumerate(group):
            if idx not in keep:
                continue
            key = f"{num}{letter}"
            if is_expl:
                marks.append((offsets[i], f"Penjelasan Pasal {key}", f"E{key}", f"Penjelasan Pasal {key}", "para"))
            else:
                marks.append((offsets[i], f"Pasal {key}", key, f"Pasal {key}", "article"))
    if split is not None:
        marks.append((offsets[split], "PENJELASAN", "", "PENJELASAN", "container"))
    last_raw = ""
    for i, raw in enumerate(lines):
        line = raw.strip()
        if (split is not None and i >= split) or not _CONTAINER.match(line or "x"):
            continue
        heading = line
        nxt = next((lines[j].strip() for j in range(i + 1, min(i + 4, len(lines))) if lines[j].strip()), "")
        if nxt and nxt.isupper() and len(nxt) <= 100 and not nxt.startswith("[page"):
            heading = f"{line} {nxt}"
        if line == last_raw and marks and marks[-1][4] == "container":
            marks.pop()  # the earlier one was the page-end catchword
        marks.append((offsets[i], heading, "", heading, "container"))
        last_raw = line
    marks.sort(key=lambda x: x[0])
    # A container followed directly by the same container (catchword then heading): keep the later.
    cleaned: list[tuple[int, str, str, str, str]] = []
    for mk in marks:
        if cleaned and cleaned[-1][4] == "container" and mk[4] == "container" and \
                cleaned[-1][1].split(" ")[:2] == mk[1].split(" ")[:2]:
            cleaned.pop()
        cleaned.append(mk)
    secs = []
    for j, (start_, label, key, heading, level) in enumerate(cleaned):
        end_ = cleaned[j + 1][0] if j + 1 < len(cleaned) else len(text)
        secs.append({"label": label, "key": key, "heading": heading, "start": start_, "end": end_, "level": level})
    return secs


SOURCES = [JdihSetnegSource()]
