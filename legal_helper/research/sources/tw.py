"""Taiwan: 全國法規資料庫 (law.moj.gov.tw, Ministry of Justice).

Fetch reads the live statute page ``LawClass/LawAll.aspx?pcode=…`` — the one
page robots.txt allows besides news (``Disallow: /`` except
``/News/NewsDetail.aspx``, ``/LawClass/LawAll.aspx``, the sitemap). Search
and English text come from the MOJ's official bulk OpenAPI
(``/api/{Ch|En}/{Law|Order}/JSON``, zips refreshed by the MOJ every few
weeks), because the site's own search, single-article and English pages are
disallowed. The zips are downloaded lazily into a small local index under
``<state>/legal_docs/index/tw.moj/`` (gzip JSONL, one law per line, ≈30 MB
for Chinese, ≈15 MB for English; built in ~5–20 s for 法律, the 26 MB 命令
zip arrives in the background) and refreshed weekly.

Judicial Yuan judgments (judgment.judicial.gov.tw / opendata) are not here:
those hosts drop TCP connections from outside Taiwan (tested from this
server), so a judgment source needs a Taiwan egress.

Articles: 第 97 條 → key 97; 第 99-1 條 (the MOJ's display of 第九十九條之一)
→ key 99.1 with the label 第99條之1, the form the section index reads.
"""

from __future__ import annotations

import gzip
import io
import json
import re
import threading
import time
import zipfile
from pathlib import Path
from typing import Any, Iterator, Optional

import httpx

from ...connectors.base import USER_AGENT, request_with_retry
from ..base import Hit, LegalDoc, Source, SourceUnavailable
from ..textify import normalize_lines

_SITE = "https://law.moj.gov.tw"
_BULK = {
    ("ch", "law"): f"{_SITE}/api/Ch/Law/JSON",
    ("ch", "order"): f"{_SITE}/api/Ch/Order/JSON",
    ("en", "law"): f"{_SITE}/api/En/Law/JSON",
    ("en", "order"): f"{_SITE}/api/En/Order/JSON",
}
_REFRESH_S = 7 * 24 * 3600
_LOCK = threading.Lock()
_BUILDING: set[str] = set()
_TITLES: dict[str, Any] = {"loaded_at": 0.0, "rows": []}
_PCODE = re.compile(r"pcode=([A-Z]\d{7})", re.I)
_LATIN = re.compile(r"[A-Za-z]{3}")
_TW_NO = re.compile(r"^第\s*(\d+)(?:-(\d+))?\s*條")
_EN_NO = re.compile(r"^Article\s+(\d+(?:-\d+)*)")


def _index_dir() -> Path:
    from ...config import load_settings

    d = Path(load_settings().state_dir) / "legal_docs" / "index" / "tw.moj"
    d.mkdir(parents=True, exist_ok=True)
    return d


def _roc_date(s: str) -> Optional[str]:
    """'民國 112 年 06 月 28 日' / '20230628' → '2023-06-28'."""
    s = (s or "").strip()
    m = re.search(r"民國\s*(\d+)\s*年\s*(\d+)\s*月\s*(\d+)\s*日", s)
    if m:
        return f"{int(m.group(1)) + 1911}-{int(m.group(2)):02d}-{int(m.group(3)):02d}"
    m = re.match(r"^(\d{4})(\d{2})(\d{2})$", s)
    if m:
        return f"{m.group(1)}-{m.group(2)}-{m.group(3)}"
    return s or None


# ------------------------------------------------------------------ bulk index

def _download(lang: str, part: str) -> bytes:
    url = _BULK[(lang, part)]
    last: Optional[Exception] = None
    for attempt in range(4):
        try:
            r = request_with_retry("GET", url, headers={"User-Agent": USER_AGENT},
                                   timeout=httpx.Timeout(300.0, connect=15.0))
            if r.content[:2] == b"PK":
                return r.content
            # The MOJ server answers 500 "file in use" while it rewrites a zip.
            last = RuntimeError(r.text[:120])
        except httpx.HTTPError as exc:
            last = exc
        time.sleep(5 * (attempt + 1))
    raise SourceUnavailable(f"law.moj.gov.tw bulk {lang}/{part} download failed ({last}); retry later.")


def _records(zbytes: bytes) -> tuple[str, list[dict[str, Any]]]:
    z = zipfile.ZipFile(io.BytesIO(zbytes))
    name = next(n for n in z.namelist() if n.lower().endswith(".json"))
    data = json.loads(z.read(name).decode("utf-8-sig"))
    return str(data.get("UpdateDate") or ""), list(data.get("Laws") or [])


def _build(lang: str, part: str) -> None:
    """Download one bulk zip and write ``<lang>_<part>.jsonl.gz`` (pcode TAB json per law)."""
    key = f"{lang}_{part}"
    with _LOCK:
        busy = key in _BUILDING
        if not busy:
            _BUILDING.add(key)
    if busy:  # another thread is downloading it: wait for that build instead of starting a second
        deadline = time.time() + 600
        while key in _BUILDING and time.time() < deadline:
            time.sleep(0.5)
        return
    try:
        t0 = time.time()
        update, laws = _records(_download(lang, part))
        d = _index_dir()
        tmp = d / f"{key}.jsonl.gz.tmp"
        with gzip.open(tmp, "wt", encoding="utf-8", compresslevel=3) as fh:
            for law in laws:
                url = law.get("LawURL") or law.get("EngLawURL") or ""
                m = _PCODE.search(url)
                if not m:
                    continue
                fh.write(m.group(1).upper() + "\t" + json.dumps(law, ensure_ascii=False) + "\n")
        tmp.replace(d / f"{key}.jsonl.gz")
        meta_p = d / "meta.json"
        meta = json.loads(meta_p.read_text("utf-8")) if meta_p.exists() else {}
        meta[key] = {"update": update, "built": time.time(), "laws": len(laws), "seconds": round(time.time() - t0, 1)}
        meta_p.write_text(json.dumps(meta, ensure_ascii=False), "utf-8")
        if lang == "ch":
            _TITLES["loaded_at"] = 0.0  # reload titles
    finally:
        with _LOCK:
            _BUILDING.discard(key)


def _ensure(lang: str, part: str, *, wait: bool) -> bool:
    """Make sure the index file exists; refresh it in the background when stale."""
    p = _index_dir() / f"{lang}_{part}.jsonl.gz"
    if p.exists():
        if time.time() - p.stat().st_mtime > _REFRESH_S:
            threading.Thread(target=_safe_build, args=(lang, part), daemon=True, name=f"tw-index-{lang}-{part}").start()
        return True
    if wait:
        _build(lang, part)
        return p.exists()
    threading.Thread(target=_safe_build, args=(lang, part), daemon=True, name=f"tw-index-{lang}-{part}").start()
    return False


def _safe_build(lang: str, part: str) -> None:
    try:
        _build(lang, part)
    except Exception:  # noqa: BLE001 — a background refresh never raises
        pass


def _iter_index(lang: str, parts: tuple[str, ...]) -> Iterator[tuple[str, str]]:
    for part in parts:
        p = _index_dir() / f"{lang}_{part}.jsonl.gz"
        if not p.exists():
            continue
        with gzip.open(p, "rt", encoding="utf-8") as fh:
            for line in fh:
                pcode, _, rest = line.partition("\t")
                yield pcode, rest


def _titles() -> list[dict[str, Any]]:
    """Compact title list of every Chinese law and order (kept in memory, ~12k rows)."""
    if _TITLES["rows"] and time.time() - _TITLES["loaded_at"] < 3600:
        return _TITLES["rows"]
    rows = []
    for pcode, raw in _iter_index("ch", ("law", "order")):
        try:
            law = json.loads(raw)
        except ValueError:
            continue
        rows.append({
            "pcode": pcode, "name": law.get("LawName") or "", "eng": law.get("EngLawName") or "",
            "level": law.get("LawLevel") or "", "category": law.get("LawCategory") or "",
            "modified": law.get("LawModifiedDate") or "", "effective": law.get("LawEffectiveDate") or "",
            "abandoned": law.get("LawAbandonNote") or "", "has_eng": law.get("LawHasEngVersion") == "Y",
        })
    _TITLES["rows"] = rows
    _TITLES["loaded_at"] = time.time()
    return rows


def _record(lang: str, pcode: str) -> Optional[dict[str, Any]]:
    prefix = pcode.upper()
    for pc, raw in _iter_index(lang, ("law", "order")):
        if pc == prefix:
            return json.loads(raw)
    return None


def _index_note() -> Optional[str]:
    p = _index_dir() / "meta.json"
    meta = json.loads(p.read_text("utf-8")) if p.exists() else {}
    if "ch_order" not in meta:
        return "命令 (regulations) are still being indexed; this search covered 法律 only — repeat it in a minute."
    return None


# ------------------------------------------------------------------ text

class _Writer:
    def __init__(self) -> None:
        self.parts: list[str] = []
        self.pos = 0
        self.marks: list[tuple[int, str, str, str, str]] = []

    def line(self, s: str) -> Optional[int]:
        s = normalize_lines(s or "")
        if not s:
            return None
        start = self.pos
        self.parts.append(s + "\n")
        self.pos += len(s) + 1
        return start

    def mark(self, start: Optional[int], label: str, key: str, heading: str, level: str) -> None:
        if start is not None:
            self.marks.append((start, label, key, heading, level))

    def result(self) -> tuple[str, list[dict[str, Any]]]:
        text = "".join(self.parts).rstrip("\n")
        marks = sorted(self.marks, key=lambda m: m[0])
        out = []
        for i, (start, label, key, heading, level) in enumerate(marks):
            end = marks[i + 1][0] if i + 1 < len(marks) else len(text)
            out.append({"label": label, "key": key, "heading": heading, "start": start,
                        "end": max(start, min(end, len(text))), "level": level})
        return text, out


def _chapter_label(s: str) -> str:
    s = " ".join((s or "").split())
    # "第 一 章 總則" → "第一章 總則"
    return re.sub(r"^第\s*(\S+?)\s*(編|章|節|款|目)(\s*之\s*\S+)?\s*", lambda m: f"第{m.group(1)}{m.group(2)}{(m.group(3) or '').replace(' ', '')} ", s).strip()


def _tw_mark(w: _Writer, start: Optional[int], number_line: str) -> None:
    m = _TW_NO.match(number_line.strip())
    if not m:
        return
    n, sub = m.group(1), m.group(2)
    label = f"第{n}條之{sub}" if sub else f"第{n}條"
    w.mark(start, label, f"{n}.{sub}" if sub else n, number_line.strip(), "article")


def _lawall_text(html: str) -> tuple[str, list[dict[str, Any]], dict[str, Any]]:
    from bs4 import BeautifulSoup

    soup = BeautifulSoup(html, "lxml")
    meta: dict[str, Any] = {}
    table = soup.select_one("table.table")
    title = ""
    if table is not None:
        for tr in table.find_all("tr"):
            th, td = tr.find("th"), tr.find("td")
            if not th or not td:
                continue
            k = th.get_text(strip=True).rstrip("：:")
            if k == "法規名稱":
                a = td.find("a", id="hlLawName") or td.find("a")
                title = (a.get_text(strip=True) if a else td.get_text(" ", strip=True)).strip()
                if td.find(class_="label-eng"):
                    meta["has_english"] = True
            else:
                v = " ".join(td.get_text(" ", strip=True).split())
                if "日期" in k:
                    meta[{"修正日期": "amended", "公發布日": "published", "公布日期": "published",
                          "生效日期": "effective", "廢止日期": "repealed", "發布日期": "published"}.get(k, k)] = _roc_date(v)
                elif k == "法規類別":
                    meta["category"] = v
                else:
                    meta[k] = v
    body = soup.select_one("#pnLawFla .law-reg-content") or soup.select_one(".law-reg-content")
    w = _Writer()
    if title:
        w.line(title)
    if body is None:
        return w.result()[0], [], {**meta, "title": title}
    for el in body.find_all(recursive=False):
        cls = el.get("class") or []
        if "h3" in cls:
            label = _chapter_label(el.get_text(" ", strip=True))
            start = w.line(label)
            w.mark(start, label, "", label, "container")
            continue
        if "row" not in cls:
            t = " ".join(el.get_text(" ", strip=True).split())
            if t:
                w.line(t)
            continue
        no = el.select_one(".col-no")
        number = " ".join(no.get_text(" ", strip=True).split()) if no else ""
        paras = [" ".join(d.get_text(" ", strip=True).split()) for d in el.select(".law-article > div")]
        if not paras:
            data = el.select_one(".col-data")
            paras = [ln for ln in (data.get_text("\n", strip=True).split("\n") if data else []) if ln.strip()]
        start = w.line(number)
        for p in paras:
            w.line(p)
        _tw_mark(w, start, number)
    text, secs = w.result()
    meta["title"] = title
    return text, secs, meta


def _bulk_text(law: dict[str, Any], *, english: bool) -> tuple[str, list[dict[str, Any]]]:
    w = _Writer()
    p = "Eng" if english else ""
    w.line(law.get("EngLawName") if english else law.get("LawName"))
    if law.get(f"{p}LawForeword"):
        w.line(str(law[f"{p}LawForeword"]))
    for a in law.get(f"{p}LawArticles") or []:
        typ = a.get(f"{p}ArticleType")
        no = (a.get(f"{p}ArticleNo") or "").strip()
        content = str(a.get(f"{p}ArticleContent") or "")
        if typ == "C":
            label = " ".join(content.split()) if english else _chapter_label(content)
            start = w.line(label)
            w.mark(start, label, "", label, "container")
            continue
        start = w.line(no)
        w.line(content)
        if english:
            m = _EN_NO.match(no)
            if m:
                w.mark(start, f"Article {m.group(1)}", m.group(1), no, "article")
        else:
            _tw_mark(w, start, no)
    return w.result()


# ------------------------------------------------------------------ source

class TwMojSource(Source):
    id = "tw.moj"
    probe = {"query": "民用航空法", "kind": "legislation", "ref": "K0090001", "section": "第97條", "min_chars": 20000}
    name = "全國法規資料庫 (law.moj.gov.tw, Ministry of Justice)"
    jurisdictions = ("TW",)
    kinds = ("legislation",)
    modes = ("keyword", "fulltext")
    languages = ("zh", "en")
    homepage = _SITE
    coverage = (
        "Every Taiwan 憲法・法律 (~1,350) and 命令 (~10,450 regulations, incl. repealed), current text. Search in "
        "Chinese (繁體 or 简体 words that appear in the title); keyword = title, fulltext = article text; English "
        "words search the English titles. Refs are pcodes (K0090001 = 民用航空法; 'en/K0090001' = the MOJ's English "
        "translation, unofficial). Provisions: 第97條, 第99條之1 (shown on the site as 第 99-1 條). "
        "filters: level (法律|命令), status (repealed to include 廢止 laws). No judgments (Judicial Yuan is "
        "unreachable from outside Taiwan)."
    )

    def search(self, query, *, kind="any", mode="keyword", limit=10, filters=None):
        if kind == "case":
            return []
        filters = filters or {}
        q = (query or "").strip()
        if not q:
            return []
        if not _ensure("ch", "law", wait=True):
            raise SourceUnavailable("the law.moj.gov.tw bulk index could not be built; retry later.")
        _ensure("ch", "order", wait=False)
        terms = [t for t in re.split(r"[\s　,，、]+", q) if t]
        terms = [_to_traditional(t) for t in terms]
        english = bool(_LATIN.search(q)) and not re.search(r"[一-鿿]", q)
        level = str(filters.get("level") or "")
        level = {"law": "法律", "act": "法律", "regulation": "命令", "order": "命令"}.get(level.lower(), level)
        with_repealed = str(filters.get("status", "")).lower() in ("repealed", "all", "廢止", "abandoned")
        hits: list[Hit] = []
        if mode != "fulltext":
            hits = self._titles(terms, english, level, with_repealed, limit)
        if not hits and not english:
            hits = self._fulltext(terms, level, with_repealed, limit)
        note = _index_note()
        if note and hits:
            hits[0].extra["index_note"] = note
        return hits

    def _hit(self, row: dict[str, Any], *, english: bool = False, snippet: str = "", provision: str = "") -> Hit:
        pc = row["pcode"]
        status = "repealed (廢止)" if row.get("abandoned") else "in force"
        title = (row.get("eng") or row["name"]) if english else row["name"]
        return Hit(
            ref=self.ref(f"en/{pc}" if english else pc), title=title, source=self.id, kind="legislation",
            url=f"{_SITE}/LawClass/LawAll.aspx?pcode={pc}", date=_roc_date(row.get("modified") or ""), status=status,
            snippet=snippet,
            extra={k: v for k, v in {"level": row.get("level"), "category": row.get("category"),
                                     "english": row.get("eng") if row.get("has_eng") and not english else None,
                                     "chinese": row["name"] if english else None,
                                     "provision": provision or None}.items() if v},
        )

    def _titles(self, terms: list[str], english: bool, level: str, with_repealed: bool, limit: int) -> list[Hit]:
        rows = []
        q = " ".join(terms)
        for r in _titles():
            if level and r["level"] != level:
                continue
            if r["abandoned"] and not with_repealed:
                continue
            hay = r["eng"].lower() if english else r["name"]
            if english:
                if not r["eng"] or not all(t.lower() in hay for t in terms):
                    continue
            elif not all(t in hay for t in terms):
                continue
            rows.append(r)
        key = (lambda r: (r["eng"].lower() != q.lower(), not r["eng"].lower().startswith(q.lower()), len(r["eng"]))) if english \
            else (lambda r: (r["name"] != q, not r["name"].startswith(terms[0]), r["level"] != "法律", len(r["name"])))
        rows.sort(key=key)
        return [self._hit(r, english=english) for r in rows[:limit]]

    def _fulltext(self, terms: list[str], level: str, with_repealed: bool, limit: int) -> list[Hit]:
        found: list[tuple[int, dict[str, Any], str, str]] = []
        for pcode, raw in _iter_index("ch", ("law", "order")):
            if not all(t in raw for t in terms):
                continue
            law = json.loads(raw)
            if level and law.get("LawLevel") != level:
                continue
            if law.get("LawAbandonNote") and not with_repealed:
                continue
            arts = [a for a in law.get("LawArticles") or [] if a.get("ArticleType") == "A"
                    and all(t in (a.get("ArticleContent") or "") for t in terms)]
            if not arts and not all(t in (law.get("LawName") or "") for t in terms):
                continue
            best = arts[0] if arts else None
            snippet = ""
            prov = ""
            if best:
                content = " ".join(str(best.get("ArticleContent") or "").split())
                i = content.find(terms[0])
                snippet = (best.get("ArticleNo", "") + " " + content[max(0, i - 60): i + 160]).strip()
                m = _TW_NO.match(best.get("ArticleNo") or "")
                if m:
                    prov = f"第{m.group(1)}條之{m.group(2)}" if m.group(2) else f"第{m.group(1)}條"
            row = {"pcode": pcode, "name": law.get("LawName") or "", "eng": law.get("EngLawName") or "",
                   "level": law.get("LawLevel") or "", "category": law.get("LawCategory") or "",
                   "modified": law.get("LawModifiedDate") or "", "abandoned": law.get("LawAbandonNote") or "",
                   "has_eng": law.get("LawHasEngVersion") == "Y"}
            score = len(arts) + (5 if row["level"] == "法律" else 0)
            found.append((score, row, snippet, prov))
        found.sort(key=lambda x: -x[0])
        return [self._hit(r, snippet=s, provision=p) for _, r, s, p in found[:limit]]

    def fetch(self, native_id, *, language=None):
        nid = native_id.strip()
        english = nid.lower().startswith("en/") or (language or "").lower().startswith("en")
        pcode = nid.split("/")[-1].upper()
        if not re.fullmatch(r"[A-Z]\d{7}", pcode):
            raise ValueError("Taiwan refs are pcodes like K0090001 (or en/K0090001 for English)")
        if english:
            return self._fetch_english(pcode, native_id=nid)
        url = f"{_SITE}/LawClass/LawAll.aspx?pcode={pcode}"
        text, secs, meta = "", [], {}
        try:
            r = request_with_retry("GET", url, headers={"User-Agent": USER_AGENT},
                                   timeout=httpx.Timeout(60.0, connect=10.0))
            text, secs, meta = _lawall_text(r.text)
            meta["read_from"] = "LawAll.aspx (live)"
        except httpx.HTTPError as exc:
            meta["live_error"] = f"{type(exc).__name__}: {str(exc)[:120]}"
        title = meta.pop("title", "") or ""
        rec = None
        if _ensure("ch", "law", wait=False):
            rec = _record("ch", pcode)
        if (not secs) and rec:
            text, secs = _bulk_text(rec, english=False)
            meta["read_from"] = "MOJ bulk data snapshot (the live page could not be read)"
        if rec:
            title = title or rec.get("LawName") or pcode
            meta.update({k: v for k, v in {
                "level": rec.get("LawLevel"), "category": meta.get("category") or rec.get("LawCategory"),
                "amended": meta.get("amended") or _roc_date(rec.get("LawModifiedDate") or ""),
                "effective_note": rec.get("LawEffectiveNote") or None,
                "repealed": rec.get("LawAbandonNote") or None,
                "history": (rec.get("LawHistories") or "").strip()[:3000] or None,
            }.items() if v})
            if rec.get("LawHasEngVersion") == "Y":
                meta["english_translation"] = self.ref(f"en/{pcode}")
        meta["status"] = "repealed (廢止)" if meta.get("repealed") else "in force"
        meta["citation"] = title
        if not text.strip():
            meta["no_text_reason"] = "neither LawAll.aspx nor the bulk data has text for this pcode"
        meta.pop("has_english", None)
        return LegalDoc(
            ref=self.ref(nid), title=title or pcode, source=self.id, text=text, url=url, kind="legislation",
            language="zh", metadata={k: v for k, v in meta.items() if v not in (None, "", [], {})}, sections=secs or None,
        )

    def _fetch_english(self, pcode: str, *, native_id: str) -> LegalDoc:
        if not _ensure("en", "law", wait=True):
            raise SourceUnavailable("the MOJ English bulk data could not be downloaded; retry later.")
        orders_ready = _ensure("en", "order", wait=False)
        rec = _record("en", pcode)
        if rec is None:
            if not orders_ready:
                raise SourceUnavailable("English regulations (命令) are still being indexed; retry in a minute.")
            raise ValueError(f"the MOJ has no English translation of {pcode}")
        text, secs = _bulk_text(rec, english=True)
        meta = {k: v for k, v in {
            "official": False,
            "note": "English translation published by the MOJ for reference; only the Chinese text is authoritative.",
            "amended": _roc_date(rec.get("EngLawModifiedDate") or ""),
            "chinese_title": rec.get("LawName"), "chinese": self.ref(pcode),
            "repealed": rec.get("EngLawAbandonNote") or None,
            "history": (rec.get("EngLawHistories") or "").strip()[:2000] or None,
            "read_from": "MOJ bulk English data (the site's English pages are disallowed by robots.txt)",
        }.items() if v not in (None, "")}
        return LegalDoc(
            ref=self.ref(native_id), title=rec.get("EngLawName") or pcode, source=self.id, text=text,
            url=f"{_SITE}/ENG/LawClass/LawAll.aspx?pcode={pcode}", kind="legislation", language="en",
            metadata=meta, sections=secs or None, ttl=7 * 24 * 3600,
        )


# Simplified → Traditional for the characters common in statute titles, so a
# 简体 query (民用航空法 is the same; 航空器飞航 is not) still matches.
_S2T = str.maketrans(
    "飞机场运输货条规则办务处罚国际关税险责损赔偿员业营证书让转权价费检验设备资产护经济劳动医疗卫药币银汇电讯网络个数据执补监环边领导军迟误订约签认质灾难伤残众进仓储邮递于与为应须该时间门开发达实细纲组织团体会议选举宪诉讼录档报纸无驾驶车铁湾",
    "飛機場運輸貨條規則辦務處罰國際關稅險責損賠償員業營證書讓轉權價費檢驗設備資產護經濟勞動醫療衛藥幣銀匯電訊網絡個數據執補監環邊領導軍遲誤訂約簽認質災難傷殘眾進倉儲郵遞於與為應須該時間門開發達實細綱組織團體會議選舉憲訴訟錄檔報紙無駕駛車鐵灣",
)


def _to_traditional(s: str) -> str:
    return s.translate(_S2T)


SOURCES = [TwMojSource()]
