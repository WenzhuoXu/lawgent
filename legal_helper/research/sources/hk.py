"""Hong Kong: e-Legislation, through the Department of Justice's open data.

www.elegislation.gov.hk itself disallows robots (``Disallow: /`` except the
home page) and sits behind a JavaScript client check, so it is not used. The
DoJ publishes the same verified copies on DATA.GOV.HK, weekly, as bulk zips
of HKLM XML per language (English and Traditional Chinese are both authentic;
Simplified Chinese is a reference text) plus a "list of legislation" that
names, for every Cap, its title, status, version date, zip and member file.

* **Index** — that list, English + Traditional Chinese (2.8 + 3.0 MB XML,
  about 5 s), reduced to ~1.5 MB of JSON under
  ``state/legal_docs/index/hk.elegislation/caps.json`` on the first search or
  fetch and refreshed weekly. Title and Cap-number search run over it.
* **Fetch** — one Cap without the 236–561 MB zip: the zip's central directory
  is read with two HTTP Range requests (tail, then the ~211 KB directory) and
  kept beside the index per zip and file date; then one Range request reads
  the member and it is inflated locally. Sections come from the XML
  (``section/@name`` s2A …), scheduled conventions' articles from the
  schedule's cross-headings.
"""

from __future__ import annotations

import hashlib
import json
import re
import struct
import threading
import time
import zlib
from pathlib import Path
from typing import Any, Optional

import httpx
from lxml import etree

from ...connectors.base import USER_AGENT, request_with_retry
from ..base import Hit, LegalDoc, Source, SourceUnavailable

_RES = "https://resource.data.one.gov.hk/doj/data"
_LISTS = {"en": f"{_RES}/hkel_list_c_all_en.xml", "zh": f"{_RES}/hkel_list_c_all_zh-Hant.xml"}
_NS = "http://www.xml.gov.hk/schemas/hklm/1.0"
_N = f"{{{_NS}}}"
_HEADERS = {"User-Agent": USER_AGENT}
_TTL = 7 * 24 * 3600
_LOCK = threading.Lock()
_MEM: dict[str, Any] = {}


def _ws(s: str) -> str:
    return " ".join((s or "").replace("\xa0", " ").replace("　", " ").split())


def _dir() -> Path:
    from ...config import load_settings

    d = Path(load_settings().state_dir) / "legal_docs" / "index" / "hk.elegislation"
    d.mkdir(parents=True, exist_ok=True)
    return d


def _http(url: str, *, rng: Optional[str] = None, timeout: float = 60.0) -> httpx.Response:
    headers = dict(_HEADERS)
    if rng:
        headers["Range"] = rng
    try:
        r = request_with_retry("GET", url, headers=headers, timeout=httpx.Timeout(timeout, connect=10.0))
    except httpx.HTTPStatusError as exc:
        raise SourceUnavailable(f"DATA.GOV.HK answered {exc.response.status_code} for {url.rsplit('/', 1)[-1]}") from exc
    except httpx.TransportError as exc:
        raise SourceUnavailable(f"DATA.GOV.HK unreachable ({type(exc).__name__}); try again shortly") from exc
    if rng and r.status_code != 206:
        raise SourceUnavailable("DATA.GOV.HK ignored the byte-range request; the zip cannot be read member by member")
    return r


# ------------------------------------------------------------------ index

def _index() -> dict[str, Any]:
    """Cap number → {en: {...}, zh: {...}} from the DoJ list of legislation."""
    with _LOCK:
        now = time.time()
        if _MEM.get("caps") and now - _MEM.get("at", 0) < _TTL:
            return _MEM["caps"]
        path = _dir() / "caps.json"
        if path.exists() and now - path.stat().st_mtime < _TTL:
            try:
                caps = json.loads(path.read_text(encoding="utf-8"))
                _MEM.update(caps=caps, at=path.stat().st_mtime)
                return caps
            except (OSError, ValueError):
                pass
        caps: dict[str, Any] = {}
        try:
            for lang, url in _LISTS.items():
                root = etree.fromstring(_http(url).content, parser=etree.XMLParser(huge_tree=True))
                for ch in root.iter("Chapter"):
                    cap = _ws(ch.findtext("CapNo") or "")
                    if not cap:
                        continue
                    title = _ws(ch.findtext("ChapterTitleEnglish") or ch.findtext("ChapterTitleTraditionalChinese")
                                or ch.findtext("ChapterTitleSimplifiedChinese") or "")
                    v = ch.find("Version")
                    vd = v.find("VersionDate") if v is not None else None
                    entry = {
                        "title": title, "display": _ws(ch.findtext("CapNoDisplay") or ""),
                        "type": ch.findtext("LegislationType"), "principal": ch.findtext("PrincipalOrdinance"),
                        "status": vd.get("statusCategory") if vd is not None else None,
                        "pencil": (vd.get("isPencilMarked") == "true") if vd is not None else False,
                        "date": (vd.text or "")[:10] if vd is not None else None,
                        "zip": _ws(v.findtext("DataResourceUrl") or "") if v is not None else "",
                        "member": (_ws(v.findtext("FileLocation") or "") + "\\" + _ws(v.findtext("FileName") or ""))
                        if v is not None else "",
                    }
                    caps.setdefault(cap, {})[lang] = entry
                files = {}
                for dr in root.iter("DataResource"):
                    files[dr.get("url")] = dr.get("fileDate")
                caps.setdefault("_files", {}).update(files)
        except SourceUnavailable:
            if path.exists():
                caps = json.loads(path.read_text(encoding="utf-8"))
                _MEM.update(caps=caps, at=now)
                return caps
            raise
        path.write_text(json.dumps(caps, ensure_ascii=False), encoding="utf-8")
        _MEM.update(caps=caps, at=now)
        return caps


# --------------------------------------------------------- remote zip read

def _central_directory(url: str, file_date: str, *, refresh: bool = False) -> dict[str, list[int]]:
    """member name → [local header offset, compressed size, size, method]."""
    key = hashlib.sha1(url.encode()).hexdigest()[:16]
    path = _dir() / f"cd_{key}.json"
    with _LOCK:
        if path.exists() and not refresh:
            try:
                saved = json.loads(path.read_text(encoding="utf-8"))
                if saved.get("file_date") == file_date and saved.get("members"):
                    return saved["members"]
            except (OSError, ValueError):
                pass
    tail_r = _http(url, rng="bytes=-65536")
    tail = tail_r.content
    total = int((tail_r.headers.get("content-range") or "/0").rsplit("/", 1)[-1] or 0)
    i = tail.rfind(b"PK\x05\x06")
    if i < 0:
        raise SourceUnavailable("the DoJ zip has no end-of-central-directory record in its last 64 KB")
    _, _, _, _, n, cd_size, cd_off, _ = struct.unpack("<IHHHHIIH", tail[i:i + 22])
    if cd_off == 0xFFFFFFFF or n == 0xFFFF or cd_size == 0xFFFFFFFF:
        j = tail.rfind(b"PK\x06\x06")
        cd_size = struct.unpack("<Q", tail[j + 40:j + 48])[0]
        cd_off = struct.unpack("<Q", tail[j + 48:j + 56])[0]
    if total and cd_off + cd_size > total:
        raise SourceUnavailable("the DoJ zip's central directory points past the end of the file")
    cd = _http(url, rng=f"bytes={cd_off}-{cd_off + cd_size - 1}").content
    members: dict[str, list[int]] = {}
    p = 0
    while p + 46 <= len(cd) and cd[p:p + 4] == b"PK\x01\x02":
        (method, _, _, _crc, csize, usize, fl, el, cl, _, _, _, loff) = struct.unpack("<HHHIIIHHHHHII", cd[p + 10:p + 46])
        name = cd[p + 46:p + 46 + fl].decode("utf-8", "replace")
        extra = cd[p + 46 + fl:p + 46 + fl + el]
        if 0xFFFFFFFF in (loff, csize, usize):
            q = 0
            while q + 4 <= len(extra):
                hid, hsz = struct.unpack("<HH", extra[q:q + 4])
                data = extra[q + 4:q + 4 + hsz]
                if hid == 1:
                    vals = list(struct.unpack("<" + "Q" * (hsz // 8), data[: (hsz // 8) * 8]))
                    k = 0
                    if usize == 0xFFFFFFFF and k < len(vals):
                        usize, k = vals[k], k + 1
                    if csize == 0xFFFFFFFF and k < len(vals):
                        csize, k = vals[k], k + 1
                    if loff == 0xFFFFFFFF and k < len(vals):
                        loff = vals[k]
                q += 4 + hsz
        if name.lower().endswith(".xml"):
            members[name.lower()] = [loff, csize, usize, method]
        p += 46 + fl + el + cl
    with _LOCK:
        path.write_text(json.dumps({"url": url, "file_date": file_date, "members": members}), encoding="utf-8")
    return members


def _read_member(url: str, info: list[int]) -> bytes:
    loff, csize, usize, method = info
    head = _http(url, rng=f"bytes={loff}-{loff + 30 + 512 + csize - 1}").content
    if head[:4] != b"PK\x03\x04":
        raise ValueError("stale central directory")
    fl, el = struct.unpack("<HH", head[26:30])
    start = 30 + fl + el
    data = head[start:start + csize]
    if len(data) < csize:
        data += _http(url, rng=f"bytes={loff + start + len(data)}-{loff + start + csize - 1}").content
    if method == 8:
        return zlib.decompress(data, -15)
    if method == 0:
        return data
    raise SourceUnavailable(f"zip member uses unsupported compression method {method}")


# ------------------------------------------------------------- HKLM → text

_INLINE = {"i", "b", "u", "sup", "sub", "inline", "term", "ref", "shortTitle", "date", "abbr", "span", "location",
           "em", "strong", "fnRef", "noteRef", "a"}
_NUMBERED = {"subsection", "paragraph", "subparagraph", "clause", "subclause", "subsubclause", "item", "subitem"}
_SKIP = {"meta", "num", "heading"}
_TREATY = re.compile(r"^(?:Articles?|ARTICLES?)\s+(\d+[A-Za-z]?|[IVXLC]+)(?![\w(])\s*(?:[—–-]\s*(.*))?$")
_TREATY_ZH = re.compile(r"^第\s*([0-9]+[A-Za-z]?|[〇零一二三四五六七八九十百]+)\s*[條条](?:\s*[—–-]?\s*(.*))?$")


def _local(el: Any) -> str:
    return etree.QName(el).localname if isinstance(el.tag, str) else ""


def _article_label(m: re.Match[str], line: str) -> tuple[str, str]:
    """('Article 17', '17') for 'Article 17—…'; ('第十七條', '17') for a Chinese heading,
    so '第17條', '第十七條' and 'Article 17' all find it."""
    num = m.group(1)
    if line.startswith("第"):
        from ...documents.clauses import parse_number

        v = parse_number(num)
        head = re.match(r"^第\s*\S+?\s*[條条]", line)
        return (head.group(0) if head else line[:8]), (str(v) if v is not None else num.upper())
    return f"Article {num}", num.upper()


def _inline_text(el: Any, *, sep_markers: bool = False) -> str:
    parts: list[str] = []

    def rec(e: Any) -> None:
        name = _local(e)
        if name == "br":
            parts.append(" ")
        elif name == "marker" and e.get("role") == "blank-line":
            parts.append("\x01" if sep_markers else " ")
        elif name == "sourceNote":
            parts.append(" (" + _ws("".join(e.itertext())).strip("()") + ")")
            return
        elif e.text:
            parts.append(e.text)
        for c in e:
            rec(c)
            if c.tail:
                parts.append(c.tail)

    if el.text:
        parts.append(el.text)
    for c in el:
        rec(c)
        if c.tail:
            parts.append(c.tail)
    # source whitespace (the XML is pretty-printed) is a space; a blank-line
    # marker is the only line break
    return re.sub(r"\s+", " ", "".join(parts)).replace("\x01", "\n")


class _Lines:
    def __init__(self) -> None:
        self.lines: list[str] = []
        self.pos = 0
        self.prefix = ""
        self.marks: list[tuple[int, str, str, str, str, str]] = []

    def line(self, s: str) -> None:
        s = _ws(s)
        if not s:
            return
        if self.prefix:
            s, self.prefix = f"{self.prefix} {s}", ""
        self.lines.append(s)
        self.pos += len(s) + 1

    def flush(self) -> None:
        if self.prefix:
            p, self.prefix = self.prefix, ""
            self.line(p)

    def mark(self, label: str, key: str, heading: str, level: str, caption: str = "") -> None:
        self.flush()
        self.marks.append((self.pos, label, key, heading[:160], level, caption))

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


class _Hklm:
    def __init__(self, *, qualify: bool, zh: bool) -> None:
        self.b = _Lines()
        self.qualify = qualify
        self.zh = zh
        self.schedule: Optional[str] = None
        self.in_unit = 0
        #: an instrument (the Basic Law) numbers its articles in plain paragraphs
        self.articles_anywhere = False

    def block(self, el: Any) -> None:
        name = _local(el)
        if not name or name in ("meta",):
            return
        if name == "section":
            self._section(el)
        elif name in _NUMBERED:
            num = el.find(f"{_N}num")
            n = _ws(_inline_text(num)) if num is not None else ""
            self.b.prefix = f"{self.b.prefix} {n}".strip()
            self.flow(el, skip=("num",))
            self.b.flush()
        elif name in ("part", "division", "subdivision"):
            head = _ws(" ".join(_inline_text(c) for c in el if _local(c) in ("num", "heading")))
            if head:
                if self.in_unit:
                    self.b.line(head)
                else:
                    self.b.mark(head, "", head, "container")
                    self.b.line(head)
            self.flow(el, skip=("num", "heading"))
        elif name == "schedule":
            self._schedule(el)
        elif name == "crossHeading":
            self._cross_heading(el)
        elif name == "tr":
            tds = [td for td in el if _local(td) in ("td", "th")]
            if any(_local(x) in ("p", "paragraph", "table") for td in tds for x in td):
                # a layout table (the Basic Law sets every article in one): its
                # cells are blocks, not a row
                for td in tds:
                    self.flow(td)
                return
            cells = [_ws(_inline_text(td)) for td in tds]
            row = " | ".join(c for c in cells if c)
            if row:
                self.b.line(row)
        elif name == "p" and self.articles_anywhere and not self.in_unit:
            text = _ws(_inline_text(el))
            m = (_TREATY.match(text) or _TREATY_ZH.match(text)) if len(text) <= 60 else None
            if m:
                label, key = _article_label(m, text)
                self.b.mark(label, key, text, "article")
                self.b.line(text)
            else:
                self.flow(el)
        elif name in ("editorialNote", "note"):
            t = _ws("".join(el.itertext()))
            if t:
                self.b.line(f"[{t}]")
        else:
            self.flow(el)

    def flow(self, el: Any, *, skip: tuple[str, ...] = ()) -> None:
        buf = [el.text or ""]
        for c in el:
            name = _local(c)
            if not name or name in skip:
                buf.append(c.tail or "")
                continue
            if name in _INLINE or name in ("br", "marker", "sourceNote"):
                if name == "br":
                    buf.append(" ")
                elif name == "marker":
                    buf.append(" ")
                elif name == "sourceNote":
                    buf.append(" (" + _ws("".join(c.itertext())).strip("()") + ")")
                else:
                    buf.append(_inline_text(c))
                buf.append(c.tail or "")
                continue
            self.b.line("".join(buf))
            buf = []
            self.block(c)
            buf.append(c.tail or "")
        self.b.line("".join(buf))

    def _section(self, el: Any) -> None:
        num = el.find(f"{_N}num")
        n = (num.get("value") if num is not None else None) or ""
        head_el = el.find(f"{_N}heading")
        head = _ws(_inline_text(head_el)) if head_el is not None else ""
        if not n or self.in_unit:
            if head:
                self.b.line(head)
            self.flow(el, skip=("num", "heading"))
            return
        label = f"Sch{self.schedule}.{n}" if self.schedule is not None else f"s {n}"
        heading = f"{label} {head}".strip()
        status = (el.get("status") or "").lower()
        if status and status != "operational":
            heading += f" [{status}]"
        self.b.mark(label, label.upper() if self.schedule is not None else n.upper(), heading, "article")
        self.b.line(heading)
        self.in_unit += 1
        try:
            self.flow(el, skip=("num", "heading"))
        finally:
            self.in_unit -= 1

    def _has_units(self, el: Any) -> bool:
        for s in el.iter(f"{_N}section"):
            num = s.find(f"{_N}num")
            if num is not None and num.get("value"):
                return True
        for ch in el.iter(f"{_N}crossHeading"):
            for line in _inline_text(ch, sep_markers=True).split("\n"):
                if _TREATY.match(_ws(line)) or _TREATY_ZH.match(_ws(line)):
                    return True
        return False

    def _schedule(self, el: Any) -> None:
        name = el.get("name") or ""
        n = re.sub(r"^sch", "", name) or "1"
        head = _ws(" ".join(_inline_text(c) for c in el if _local(c) in ("num", "heading")))
        saved = self.schedule
        self.schedule = n
        try:
            status = (el.get("status") or "").lower()
            if status and status != "operational":
                head += f" [{status}]"
            if not self._has_units(el):
                label = f"Sch{n}"
                self.b.mark(label, label.upper(), f"{label} {head}", "article")
                self.b.line(f"{label} {head}")
                self.in_unit += 1
                try:
                    self.flow(el, skip=("num", "heading"))
                finally:
                    self.in_unit -= 1
                return
            self.b.mark(head or f"Schedule {n}", "", head or f"Schedule {n}", "container")
            self.b.line(head or f"Schedule {n}")
            self.flow(el, skip=("num", "heading"))
        finally:
            self.schedule = saved

    def _cross_heading(self, el: Any) -> None:
        lines = [_ws(x) for x in _inline_text(el, sep_markers=True).split("\n")]
        lines = [x for x in lines if x]
        for line in lines:
            m = (_TREATY.match(line) or _TREATY_ZH.match(line)) if self.schedule is not None else None
            if m:
                label, key = _article_label(m, line)
                caption = (m.group(2) or "").strip(" —–-")
                if self.qualify:
                    # printed label, schedule-qualified key: several schedules print
                    # the same labels and a bare '17' must stay section 17
                    qid = f"Sch{self.schedule}.Art{key}"
                    self.b.mark(label, qid.upper(), line, "article", f"{caption} [{qid}]".strip())
                    self.b.line(f"{line} [{qid}]")
                else:
                    self.b.mark(label, key, line, "article", caption)
                    self.b.line(line)
                continue
            if self.in_unit:
                self.b.line(line)
            else:
                self.b.mark(line, "", line, "container")
                self.b.line(line)


def _render(root: Any, *, zh: bool, only_schedule: Optional[str] = None) -> tuple[str, list[dict[str, Any]]]:
    main = root.find(f"{_N}main")
    if main is None:
        main = root
    if only_schedule:
        sch = next((s for s in main.iter(f"{_N}schedule")
                    if re.sub(r"^sch", "", s.get("name") or "").upper() == only_schedule.upper()), None)
        if sch is None:
            return "", []
        r = _Hklm(qualify=False, zh=zh)
        r.block(sch)
    else:
        has_sections = any(
            s.find(f"{_N}num") is not None and s.find(f"{_N}num").get("value")
            for s in main.iter(f"{_N}section") if not any(_local(a) == "schedule" for a in s.iterancestors())
        )
        r = _Hklm(qualify=has_sections, zh=zh)
        r.articles_anywhere = _local(root) == "lawDoc" and not has_sections
        for c in main:
            r.block(c)
    text = r.b.text()
    return text, r.b.sections()


# ------------------------------------------------------------------ source

_CAP = re.compile(r"(?:\bCap\.?\s*|第\s*)([0-9]{1,4}[A-Z]{0,3}|A[0-9]{3})\s*章?", re.I)
_STATUS = {"InEffect": "In effect", "NoLongerInEffect": "No longer in effect", "NotYetInEffect": "Not yet in effect"}


def _norm_cap(s: str) -> str:
    return re.sub(r"\s+", "", s).upper()


class HkElegislationSource(Source):
    id = "hk.elegislation"
    probe = {"query": "Carriage by Air Ordinance", "kind": "legislation", "ref": "500", "section": "2A",
             "min_chars": 20000}
    name = "Hong Kong e-Legislation (DoJ open data on DATA.GOV.HK)"
    jurisdictions = ("HK",)
    kinds = ("legislation",)
    modes = ("keyword",)
    languages = ("en", "zh-Hant", "zh-Hans (reference)")
    homepage = "https://www.elegislation.gov.hk"
    coverage = (
        "Every Hong Kong Ordinance, subsidiary legislation and instrument (Basic Law = A101) in its current "
        "verified version, English or Traditional Chinese (both authentic; language=zh), Simplified Chinese as "
        "reference (language=zh-Hans). Ids are Cap numbers: '500' (Carriage by Air Ordinance), '448A'; one "
        "schedule on its own '500/Sch1A' (where 'Article 17' is unique; in the whole Ordinance another "
        "schedule's article is 'Sch1A.Art17'). Sections 's 2A'. Search = titles (English, or 中文 for Chinese "
        "titles) and Cap numbers ('Cap. 500', '第500章'); no full-text search. Weekly data. filters: status "
        "(in_effect|repealed), type (ordinance|subsidiary|instrument)."
    )

    def search(self, query, *, kind="any", mode="keyword", limit=10, filters=None):
        if kind not in ("any", "legislation"):
            return []
        filters = filters or {}
        limit = max(1, min(int(limit or 10), 30))
        caps = _index()
        hits: list[Hit] = []
        cjk = bool(re.search(r"[一-鿿]", query))
        lang = "zh" if cjk else "en"
        for m in _CAP.finditer(query):
            cap = _norm_cap(m.group(1))
            if cap in caps:
                hits.append(self._hit(cap, caps[cap], lang, how="cap number"))
        if not hits and re.fullmatch(r"\s*[0-9]{1,4}[A-Z]{0,3}\s*", query or "") and _norm_cap(query) in caps:
            hits.append(self._hit(_norm_cap(query), caps[_norm_cap(query)], lang, how="cap number"))
        if hits:
            return hits[:limit]
        status = str(filters.get("status") or "").lower()
        typ = {"ordinance": "O", "subsidiary": "S", "subsidiary_legislation": "S", "instrument": "I"}.get(
            str(filters.get("type") or "").lower())
        if cjk:
            needles = [x for x in re.findall(r"[一-鿿]+", query) if x not in ("條例", "规例", "規例")] or \
                re.findall(r"[一-鿿]+", query)
        else:
            needles = [w for w in re.findall(r"[a-z0-9]+", query.lower().replace("’", "'"))
                       if w not in {"the", "of", "and", "hong", "kong", "hk", "cap", "ordinance"}] or \
                re.findall(r"[a-z0-9]+", query.lower())
        nq = _ws(re.sub(r"[^\w一-鿿 ]", " ", query.lower()))
        scored = []
        for cap, entry in caps.items():
            if cap.startswith("_"):
                continue
            e = entry.get(lang) or entry.get("en") or entry.get("zh")
            if not e:
                continue
            if typ and e.get("type") != typ:
                continue
            st = e.get("status") or ""
            if status in ("in_effect", "in_force", "current") and st != "InEffect":
                continue
            if status in ("repealed", "no_longer_in_effect") and st != "NoLongerInEffect":
                continue
            t = e.get("title") or ""
            tl = t.lower()
            if cjk:
                if not all(n in t for n in needles):
                    continue
            else:
                words = set(re.findall(r"[a-z0-9]+", tl.replace("’", "'")))
                if not all(w in words or any(x.startswith(w) for x in words if len(w) >= 4) for w in needles):
                    continue
            tn = _ws(re.sub(r"[^\w一-鿿 ]", " ", tl))
            exact = 0 if tn == nq else (1 if tn.startswith(nq) else 2)
            scored.append(((exact, 0 if st == "InEffect" else 1, {"O": 0, "I": 1, "S": 2}.get(e.get("type"), 3), len(t)), cap, entry))
        scored.sort(key=lambda x: x[0])
        return [self._hit(cap, entry, lang, how="title") for _, cap, entry in scored[:limit]]

    def _hit(self, cap: str, entry: dict[str, Any], lang: str, *, how: str) -> Hit:
        e = entry.get(lang) or entry.get("en") or entry.get("zh") or {}
        other = entry.get("zh" if lang == "en" else "en") or {}
        extra = {k: v for k, v in {
            "type": {"O": "Ordinance", "S": "subsidiary legislation", "I": "instrument"}.get(e.get("type") or ""),
            "other_language_title": other.get("title"), "matched": how,
        }.items() if v}
        if lang == "zh":
            extra["language"] = "zh — fetch with language='zh' for the Chinese text"
        return Hit(
            ref=self.ref(cap), title=e.get("title") or cap, source=self.id, kind="legislation",
            url=f"https://www.elegislation.gov.hk/hk/cap{cap}!{'zh-Hant-HK' if lang == 'zh' else 'en'}",
            date=e.get("date"), status=_STATUS.get(e.get("status") or "", e.get("status")),
            citation=e.get("display") or f"Cap. {cap}", extra=extra,
        )

    def fetch(self, native_id, *, language=None):
        nid = native_id.strip().strip("/")
        sched = None
        sm = re.match(r"^(.+?)/Sch(?:edule)?\s*(\w+)$", nid, re.I)
        if sm:
            nid, sched = sm.group(1), sm.group(2)
        m = _CAP.search(nid)
        cap = _norm_cap(m.group(1) if m else nid)
        lang_req = (language or "en").lower()
        lang = "zh" if lang_req.startswith("zh") or lang_req in ("tc", "sc", "chi") else "en"
        simplified = lang_req in ("zh-hans", "sc", "zh-cn", "zh_hans")
        caps = _index()
        entry = caps.get(cap)
        if not entry:
            raise ValueError(f"no Cap. {cap} in the DoJ list of legislation (ids look like '500', '448A', 'A101')")
        e = entry.get(lang) or {}
        if not e.get("zip"):
            raise ValueError(f"Cap. {cap} has no {'Chinese' if lang == 'zh' else 'English'} file in the DoJ data")
        zip_url, member = e["zip"], e["member"]
        if simplified:
            zip_url = zip_url.replace("_zh-Hant.zip", "_zh-Hans.zip")
            member = member.replace("zh-Hant", "zh-Hans")
        file_date = (caps.get("_files") or {}).get(e["zip"]) or e.get("date") or ""
        raw = None
        for attempt in range(2):
            members = _central_directory(zip_url, file_date, refresh=attempt > 0)
            info = None
            # The list names "cap_A101_en_c\\cap_A101_…" where the instruments zip
            # holds "a101_en_c\\a101_…"; and it may name a newer version than the
            # zip carries yet — so fall back to the newest file in the Cap's folder.
            for name in (member.lower(), member.lower().replace("cap_", "")):
                info = members.get(name)
                if info is not None:
                    break
                prefix = name.split("\\", 1)[0] + "\\"
                cands = sorted(k for k in members if k.startswith(prefix))
                if cands:
                    info = members[cands[-1]]
                    break
            if info is None:
                raise ValueError(f"Cap. {cap}: {member} is not in {zip_url.rsplit('/', 1)[-1]}")
            try:
                raw = _read_member(zip_url, info)
                break
            except (ValueError, zlib.error):
                raw = None
                continue
        if raw is None:
            raise SourceUnavailable(f"Cap. {cap}: the zip changed while reading it; try again")
        root = etree.fromstring(raw, parser=etree.XMLParser(huge_tree=True, resolve_entities=False))
        text, sections = _render(root, zh=lang == "zh", only_schedule=sched)
        meta: dict[str, Any] = {
            "status": _STATUS.get(e.get("status") or "", e.get("status")),
            "version": e.get("date"),
            "doc_status": _ws(root.findtext(f".//{_N}docStatus") or ""),
            "citation": e.get("display") or f"Cap. {cap}",
            "data_file": f"{zip_url.rsplit('/', 1)[-1]} :: {member}",
            "data_date": file_date or None,
        }
        if e.get("pencil"):
            meta["pencil_marked"] = ("e-Legislation flags this version 'pencil-marked' (changes pending "
                                     "incorporation); check the website before relying on the wording.")
        if simplified:
            meta["language_note"] = "Simplified Chinese is a reference text; the authentic texts are English and Traditional Chinese."
        treaty = list(dict.fromkeys(m_.group(1) for s_ in sections
                                    for m_ in [re.match(r"^SCH(\w+)\.ART", s_.get("key") or "")] if m_))
        if treaty:
            meta["scheduled_conventions"] = (
                "The convention article labels ('Article 17' / '第17條') repeat across the schedules and section= "
                f"returns the first (Schedule {treaty[0]}); another schedule's article is asked for by its bracketed "
                "id ('Sch1A.Art17'), or read one schedule on its own: "
                + ", ".join(f"hk.elegislation:{cap}/Sch{x}" for x in treaty[:6]) + "."
            )
        meta = {k: v for k, v in meta.items() if v}
        if sched and not text.strip():
            meta["no_text_reason"] = f"Cap. {cap} has no Schedule {sched}"
        elif not text.strip():
            meta["no_text_reason"] = "the HKLM XML carried no text"
        title = e.get("title") or f"Cap. {cap}"
        if sched:
            title = f"{title} — Schedule {sched}"
        page_lang = {"en": "en", "zh": "zh-Hant-HK"}[lang] if not simplified else "zh-Hans-CN"
        return LegalDoc(
            ref=self.ref(cap + (f"/Sch{sched}" if sched else "")), title=title, source=self.id, text=text,
            url=f"https://www.elegislation.gov.hk/hk/cap{cap}!{page_lang}", kind="legislation",
            language="zh-Hans" if simplified else ("zh-Hant" if lang == "zh" else "en"), metadata=meta,
            sections=sections or None,
        )


SOURCES = [HkElegislationSource()]
