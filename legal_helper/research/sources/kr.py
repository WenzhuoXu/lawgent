"""South Korea: 국가법령정보센터 (law.go.kr, Ministry of Government Legislation).

Two sources over the same database, sharing native ids (a statute version is
its 법령일련번호 = ``lsiSeq`` = the DRF ``MST``):

* ``kr.law`` — the 국가법령정보 공동활용 Open API (DRF: ``lawSearch.do`` /
  ``lawService.do``). Needs ``KOREA_LAW_OC``: the user ID registered at
  open.law.go.kr, *bound to the server's IP address or domain* (no DRF call
  works without it). Statute search (title or 본문), the statute by article
  (조문 / 항 / 호 / 목 from the JSON), 지능형 법령검색 (``aiSearch``, native
  semantic search over articles), 판례 search and full text, English.
  Quirks handled as chrisryugj/korean-law-mcp (MIT) documents them: bursts
  of spurious 404s (retried), and a miss that comes back as 200 with an empty
  body or an HTML notice page.
* ``kr.law_web`` — the site's own public pages (robots.txt ``Allow: /``),
  keyless: statute search by title or article text (``lsScListR.do``), the
  whole statute of a version (``lsInfoR.do`` with ``lsiSeq`` + ``efYd``), 판례
  search (``precScListR.do``) and the judgment (``precInfoP.do``), a 판례 by
  사건번호 (``/판례/(2017다257067)``), English translations
  (``eng/engLsScListR.do``, ``eng/engLsInfoR.do``). Markup, not an API: it
  can change without notice. Searched by default only while ``KOREA_LAW_OC``
  is not set, so the two never return the same statute twice.

Articles are 제N조 / 제N조의M; the section index reads them directly
("제97조" → 97, "제6조의2" → 6.2).
"""

from __future__ import annotations

import json
import os
import re
import time
from typing import Any, Optional
from urllib.parse import quote

import httpx

from ...connectors.base import USER_AGENT, request_with_retry
from ..base import Hit, LegalDoc, Source, SourceUnavailable, _load_keys
from ..textify import html_to_text, normalize_lines

_SITE = "https://www.law.go.kr"
_LONG = httpx.Timeout(90.0, connect=10.0)
_HEADERS = {"User-Agent": USER_AGENT}
_CASE_NO = re.compile(r"\d{2,4}\s*[가-힣]{1,4}\s*\d+")
_HANGUL = re.compile(r"[가-힣]")
_JO = re.compile(r"^제\s*(\d+)\s*조(?:\s*의\s*(\d+))?")


def _dot_date(s: str) -> Optional[str]:
    """'2026. 9. 17.' / '20260917' → '2026-09-17'."""
    s = (s or "").strip()
    m = re.match(r"^(\d{4})\.?\s*(\d{1,2})\.?\s*(\d{1,2})", s)
    if m:
        return f"{m.group(1)}-{int(m.group(2)):02d}-{int(m.group(3)):02d}"
    m = re.match(r"^(\d{4})(\d{2})(\d{2})$", s)
    if m:
        return f"{m.group(1)}-{m.group(2)}-{m.group(3)}"
    return s or None


def _ymd(s: str) -> str:
    d = _dot_date(s) or ""
    return d.replace("-", "")


def _status_for(ef_yd: str) -> str:
    today = time.strftime("%Y%m%d")
    return "not yet in force" if ef_yd and ef_yd > today else "in force"


def _clean(s: Any) -> str:
    if isinstance(s, list):
        return "\n".join(_clean(x) for x in s if x)
    s = str(s or "")
    s = re.sub(r"<br\s*/?>", "\n", s, flags=re.I)
    s = re.sub(r"<[^>]+>", "", s)
    s = s.replace("&nbsp;", " ").replace("&lt;", "<").replace("&gt;", ">").replace("&amp;", "&")
    return normalize_lines(s)


def _listify(v: Any) -> list[Any]:
    if v is None:
        return []
    return v if isinstance(v, list) else [v]


class _Writer:
    def __init__(self) -> None:
        self.parts: list[str] = []
        self.pos = 0
        self.marks: list[tuple[int, str, str, str, str]] = []

    def block(self, text: str) -> Optional[int]:
        text = normalize_lines(text or "")
        if not text:
            return None
        start = self.pos
        self.parts.append(text + "\n")
        self.pos += len(text) + 1
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


def _jo_mark(w: _Writer, start: Optional[int], first_line: str, num: str = "", branch: str = "") -> bool:
    m = _JO.match(first_line.strip())
    if not m and not num:
        return False
    n = m.group(1) if m else str(int(num))
    b = (m.group(2) if m else None) or (str(int(branch)) if branch and branch not in ("0", "00") else None)
    label = f"제{n}조의{b}" if b else f"제{n}조"
    w.mark(start, label, n + (f".{b}" if b else ""), first_line[:120], "article")
    return True


_EN_CONTAINER = re.compile(r"^(?:CHAPTER|Chapter|SECTION|Section|SUB-SECTION|Sub-Section|SUBSECTION|PART|Part)\s+[\dIVXLC]+(?:-\d+)?\b")
_EN_ARTICLE = re.compile(r"^Article\s+(\d+(?:-\d+)*)(?=\s*\(|\s*$)")
_EN_ADDENDA = re.compile(r"^(?:ADDENDA|ADDENDUM|Addenda|Addendum)\b")


def _english_text(html: str) -> tuple[str, list[dict[str, Any]]]:
    """MOLEG English HTML → text and provisions.

    The section index alone misreads it: "SECTION 1 Aircraft Rental Services"
    is a 절 (a container), and "Article 7 (Title) (1) …" on one long line is
    rejected as a sentence. Each heading span is put on its own line and the
    provisions are marked here; articles inside the ADDENDA (which restart
    at Article 1) stay inside their addendum.
    """
    from bs4 import BeautifulSoup

    soup = BeautifulSoup(html, "lxml")
    for junk in soup.select("script, style, input, img, ul.lawico01"):
        junk.decompose()
    for span in soup.select("span.bl"):
        span.insert_after("\n")
    text = html_to_text(str(soup))
    marks: list[tuple[int, str, str, str, str]] = []
    pos = 0
    addenda = 0
    for line in text.split("\n"):
        start = pos
        pos += len(line) + 1
        ln = line.strip()
        if not ln:
            continue
        if _EN_ADDENDA.match(ln) and len(ln) <= 160:
            addenda += 1
            if addenda == 1:
                marks.append((start, "ADDENDA", "", "ADDENDA", "container"))
                marks.append((start, ln[:80], "ADDENDA", ln[:120], "article"))
            else:
                marks.append((start, ln[:80], ln[:80].replace(" ", ""), ln[:120], "para"))
            continue
        if addenda:
            continue
        m = _EN_ARTICLE.match(ln)
        if m and len(ln) <= 200:
            marks.append((start, f"Article {m.group(1)}", m.group(1), ln[:120], "article"))
        elif _EN_CONTAINER.match(ln) and len(ln) <= 120:
            marks.append((start, ln, "", ln, "container"))
    secs = []
    for i, (start, label, key, heading, level) in enumerate(marks):
        end = marks[i + 1][0] if i + 1 < len(marks) else len(text)
        secs.append({"label": label, "key": key, "heading": heading, "start": start, "end": end, "level": level})
    return text, secs


# ===================================================================== web

def _web(method: str, path: str, *, data: Optional[dict[str, Any]] = None, timeout=None) -> httpx.Response:
    try:
        resp = request_with_retry(method, f"{_SITE}{path}", data=data, headers=_HEADERS, timeout=timeout or _LONG)
    except httpx.HTTPStatusError as exc:
        raise SourceUnavailable(f"law.go.kr answered {exc.response.status_code} for {path.split('?')[0]}") from exc
    except httpx.TransportError as exc:
        raise SourceUnavailable(f"law.go.kr unreachable ({type(exc).__name__}); try again later.") from exc
    return resp


def _web_statute_text(html: str) -> tuple[str, list[dict[str, Any]], dict[str, Any]]:
    """lsInfoR.do HTML → text, provisions, metadata."""
    from bs4 import BeautifulSoup

    soup = BeautifulSoup(html, "lxml")
    meta: dict[str, Any] = {}
    top = soup.select_one("#conTop")
    title = ""
    if top is not None:
        h2 = top.find("h2")
        title = h2.get_text(" ", strip=True) if h2 else ""
        sub = top.select_one(".ct_sub")
        if sub:
            meta["version_line"] = sub.get_text(" ", strip=True)
    body = soup.select_one("#conScroll")
    w = _Writer()
    if title:
        w.block(title + ("\n" + meta["version_line"] if meta.get("version_line") else ""))
    if body is None:
        return w.result()[0], [], {**meta, "title": title}
    for li in body.select("ul.pconfile > li"):
        pdf = next((a.get("href") for a in li.find_all("a") if a.find("img", alt=re.compile("PDF"))), None)
        if pdf:
            li["data-pdf"] = pdf
    for junk in body.select("ul.lawico01, div.rule_area, input, img, script, style, .btn_rule, .byl_pop, .d_cont_bx"):
        junk.decompose()
    sub = body.select_one(".cont_subtit")
    if sub is not None:
        meta["contact"] = " ".join(sub.get_text(" ", strip=True).split())[:600]
        sub.decompose()
    pending: Optional[str] = None  # "jo" | "pjuk"
    suppl_seen = 0
    for el in body.find_all(recursive=False):
        name = el.name
        if name == "a":
            aid = el.get("id") or el.get("name") or ""
            pending = "jo" if re.match(r"^J\d+:\d+$", aid) else ("pjuk" if aid.startswith("pjuk") else pending)
            continue
        if name == "div" and el.get("id") == "arDivArea":
            for grp in el.select("div.pgroup"):
                text = re.sub(r"^부\s*칙", "부칙", html_to_text(str(grp)))
                start = w.block(text)
                first = text.split("\n", 1)[0] if text else ""
                label = re.sub(r"^부\s*칙", "부칙", first).strip()[:80]
                if start is None:
                    continue
                suppl_seen += 1
                if suppl_seen == 1:
                    n = len(el.select("div.pgroup"))
                    w.mark(start, f"부칙 ({n}건)", "", f"부칙 ({n}건)", "container")
                    w.mark(start, label, "부칙", label, "article")
                else:
                    # Addressable by query/offset; kept out of the outline (some laws carry 60+).
                    w.mark(start, label, label.replace(" ", ""), label, "para")
            continue
        if name == "ul" and "pconfile" in (el.get("class") or []):
            lines = []
            for li in el.find_all("li", recursive=False):
                t = " ".join(li.get_text(" ", strip=True).split())
                pdf = li.get("data-pdf")
                if t:
                    lines.append(t + (f" (PDF: {_SITE}/LSW/{pdf})" if pdf else ""))
            if lines:
                start = w.block("\n".join(lines))
                w.mark(start, "별표·서식", "", "별표·서식", "container")
            continue
        if name != "div":
            continue
        text = html_to_text(str(el))
        if not text:
            continue
        start = w.block(text)
        first = text.split("\n", 1)[0]
        if el.select_one("p.gtit") is not None or (pending == "pjuk" and not _JO.match(first)):
            w.mark(start, first[:120], "", first[:120], "container")
        else:
            _jo_mark(w, start, first)
        pending = None
    text, secs = w.result()
    meta["title"] = title
    return text, secs, meta


def _version_meta(line: str) -> dict[str, Any]:
    """'[시행 2026. 9. 17.] [법률 제21822호, 2026. 6. 16., 일부개정]' → fields."""
    out: dict[str, Any] = {}
    m = re.search(r"\[시행\s*([\d.\s]+)\]", line or "")
    if m:
        out["effective"] = _dot_date(m.group(1))
    m = re.search(r"\[([^\[\]]*?제\s*[\d-]+호)\s*,\s*([\d.\s]+?)\s*,\s*([^\]]+)\]", line or "")
    if m:
        out["citation"] = m.group(1).strip()
        out["published"] = _dot_date(m.group(2))
        out["amendment"] = m.group(3).strip()
    return out


class KrLawWebSource(Source):
    id = "kr.law_web"
    probe = {"query": "항공안전법", "kind": "legislation", "ref": "law/항공안전법", "section": "제97조", "min_chars": 20000}
    name = "국가법령정보센터 law.go.kr (public site)"
    jurisdictions = ("KR",)
    kinds = ("legislation", "case")
    modes = ("keyword", "fulltext")
    languages = ("ko", "en")
    homepage = _SITE
    coverage = (
        "Korean statutes in force (법률·대통령령·부령) with every version, 판례 (대법원 and lower courts, "
        "plus 국세청 tax cases), and MOLEG's unofficial English translations — from the law.go.kr site "
        "itself (keyless). Search in Korean: keyword = statute title (항공안전법) / 사건명; fulltext = article "
        "text (statute hits name the matching 조문) / judgment text; a 사건번호 (2017다257067) finds that "
        "judgment; English words search the translations. Refs: '<lsiSeq>/<efYd>' (one version), "
        "'law/<법령명>' (current version), 'prec/<판례일련번호>', 'eng/<lsiSeq>'; language=en on a "
        "statute gives its English translation. Provisions: 제97조, 제6조의2, 부칙; English 'Article 97'. "
        "filters: status (current|all versions → 'history'), language (en)."
    )

    @property
    def default(self) -> bool:  # type: ignore[override]
        _load_keys()
        return not os.getenv("KOREA_LAW_OC")

    # --------------------------------------------------------------- search
    def search(self, query, *, kind="any", mode="keyword", limit=10, filters=None):
        filters = filters or {}
        q = (query or "").strip()
        if not q:
            return []
        english = str(filters.get("language", "")).lower().startswith("en") or not _HANGUL.search(q) and bool(re.search(r"[A-Za-z]{3}", q))
        hits: list[Hit] = []
        if kind in ("legislation", "any"):
            if english:
                hits += self._search_english(q, limit)
            else:
                hits += self._search_statutes(q, mode, limit, filters)
        if kind in ("case", "any") and not english:
            hits += self._search_cases(q, mode, limit if kind == "case" else max(3, limit // 2))
        return hits[: max(limit, 1) if kind != "any" else limit + max(3, limit // 2)]

    def _search_statutes(self, q: str, mode: str, limit: int, filters: dict[str, Any]) -> list[Hit]:
        from bs4 import BeautifulSoup

        history = str(filters.get("status", "")).lower() in ("history", "all", "연혁")
        section = "joBody" if mode == "fulltext" else "lawNm"
        data = {"q": q, "outmax": str(max(10, min(limit * 2, 50))), "pg": "1", "fsort": "21,10,31",
                "section": section, "p2": "" if history else "3"}
        html = _web("POST", "/LSW/lsScListR.do?menuId=1&subMenuId=15&tabMenuId=81", data=data).text
        soup = BeautifulSoup(html, "lxml")
        hits: list[Hit] = []
        if section == "joBody":
            for a in soup.find_all("a", onclick=re.compile(r"fJoView\(")):
                args = re.findall(r"'([^']*)'", a.get("onclick", ""))
                # fJoView('jo', id, joSeq, lsiSeq, joNo, joBrNo, lawName, ?, '제150조', '', efYd, '')
                if len(args) < 11:
                    continue
                lsi, name, label, ef = args[3], args[6], args[8], args[10]
                text = " ".join(a.get_text(" ", strip=True).split())
                text = re.sub(r"^\d+\.\s*", "", text)
                hits.append(Hit(
                    ref=self.ref(f"{lsi}/{ef}"), title=f"{name} {label}", source=self.id, kind="legislation",
                    url=f"{_SITE}/법령/{quote(name)}/{quote(label)}", date=_dot_date(ef), status=_status_for(ef),
                    snippet=text[:300], extra={"provision": label},
                ))
            return hits[:limit]
        for tr in soup.select("table tr"):
            a = tr.find("a", onclick=re.compile(r"lsViewWideAll\("))
            if a is None:
                continue
            args = re.findall(r"'([^']*)'", a.get("onclick", ""))
            if len(args) < 2:
                continue
            lsi, ef = args[0], args[1]
            cells = [" ".join(td.get_text(" ", strip=True).split()) for td in tr.find_all("td")]
            # cells: no, 법령명, 공포일자, 법령종류, 공포번호, 시행일자, 제정·개정구분, 소관부처
            kind_ = cells[3] if len(cells) > 3 else ""
            num = cells[4] if len(cells) > 4 else ""
            hits.append(Hit(
                ref=self.ref(f"{lsi}/{ef}"), title=self._clean_name(a), source=self.id, kind="legislation",
                url=f"{_SITE}/LSW/lsInfoP.do?lsiSeq={lsi}&efYd={ef}", date=_dot_date(cells[2]) if len(cells) > 2 else None,
                status=_status_for(ef) if not history else f"version in force from {_dot_date(ef)}",
                citation=f"{kind_} {num}".strip() or None,
                extra={k: v for k, v in {"effective": _dot_date(ef), "amendment": cells[6] if len(cells) > 6 else None,
                                         "ministry": cells[7] if len(cells) > 7 else None}.items() if v},
            ))
        exact = [h for h in hits if h.title == q]
        rest = [h for h in hits if h.title != q]
        rest.sort(key=lambda h: (not h.title.startswith(q), len(h.title)))
        return (exact + rest)[:limit]

    @staticmethod
    def _clean_name(a) -> str:
        # The result list splits highlighted words into <strong> runs ("<strong>항공</strong>안전법").
        parts = []
        for node in a.descendants:
            if isinstance(node, str):
                parts.append(node)
        name = "".join(parts)
        return " ".join(name.split())

    def _search_cases(self, q: str, mode: str, limit: int) -> list[Hit]:
        from bs4 import BeautifulSoup

        if _CASE_NO.fullmatch(q.replace(" ", "")):
            seq = self._prec_by_number(q.replace(" ", ""))
            if seq:
                return [Hit(ref=self.ref(f"prec/{seq}"), title=q, source=self.id, kind="case",
                            url=f"{_SITE}/LSW/precInfoP.do?precSeq={seq}", citation=q)]
        data = {"q": q, "section": "bdyText" if mode == "fulltext" else "evtNm", "outmax": str(max(10, min(limit, 50))),
                "pg": "1", "precSeq": "0", "dtlYn": "N"}
        html = _web("POST", "/LSW/precScListR.do?menuId=7&subMenuId=47&tabMenuId=213", data=data).text
        if "검색결과" in html and "가 없습니다" in html and mode != "fulltext":
            data["section"] = "bdyText"
            html = _web("POST", "/LSW/precScListR.do?menuId=7&subMenuId=47&tabMenuId=213", data=data).text
        soup = BeautifulSoup(html, "lxml")
        hits: list[Hit] = []
        seen: set[str] = set()
        for td in soup.select("td.s_tit"):
            link = td.find("a")
            if link is None:
                continue
            on = link.get("onclick", "")
            m = (re.search(r"(?:lsEmpViewWideAll|precView)\('(\d+)'", on)
                 or re.search(r"showExternalLink\('\d+',\s*'(\d+)'", on))
            if not m or m.group(1) in seen:
                continue
            seq = m.group(1)
            seen.add(seq)
            span = link.find("span")
            court_line = " ".join(span.get_text(" ", strip=True).split()).strip("[] ") if span else ""
            if span:
                span.extract()
            title = "".join(x for x in link.strings).strip()
            title = " ".join(title.split())
            tr = td.find_parent("tr")
            nxt = tr.find_next_sibling("tr") if tr is not None else None
            summary = ""
            if nxt is not None and nxt.select_one("td.tl"):
                summary = " ".join(nxt.select_one("td.tl").get_text(" ", strip=True).split())
            cm = re.match(r"^(.+?)\s(\d{4}\.\s*\d{1,2}\.\s*\d{1,2}\.)\s*(?:선고\s*)?([^\s:]+)", court_line)
            hits.append(Hit(
                ref=self.ref(f"prec/{seq}"), title=title, source=self.id, kind="case",
                url=f"{_SITE}/LSW/precInfoP.do?precSeq={seq}", date=_dot_date(cm.group(2)) if cm else None,
                citation=court_line or None, snippet=summary[:400],
                extra={k: v for k, v in {"court": cm.group(1) if cm else None,
                                         "case_number": cm.group(3) if cm else None,
                                         "external": "국세청 (NTS) decision; full text on taxlaw.nts.go.kr" if "showExternalLink" in on else None}.items() if v},
            ))
            if len(hits) >= limit:
                break
        return hits

    def _prec_by_number(self, number: str) -> Optional[str]:
        try:
            html = _web("GET", f"/판례/({quote(number)})").text
        except SourceUnavailable:
            return None
        m = re.search(r"precSeq=(\d+)", html)
        return m.group(1) if m else None

    def _search_english(self, q: str, limit: int) -> list[Hit]:
        from bs4 import BeautifulSoup

        html = _web("POST", "/LSW/eng/engLsScListR.do", data={"q": q, "outmax": str(max(10, min(limit, 50))), "pg": "1"}).text
        soup = BeautifulSoup(html, "lxml")
        hits: list[Hit] = []
        seen: set[str] = set()
        for tr in soup.select("table tr"):
            a = tr.find("a", onclick=re.compile(r"sideInfoView\w*\('engLsInfoR'"))
            if a is None:
                continue
            m = re.search(r"sideInfoView\w*\('engLsInfoR',\s*'(\d+)'", a.get("onclick", ""))
            if not m or m.group(1) in seen:
                continue
            seen.add(m.group(1))
            cells = [" ".join(td.get_text(" ", strip=True).split()) for td in tr.find_all("td")]
            hits.append(Hit(
                ref=self.ref(f"eng/{m.group(1)}"), title=" ".join(a.get_text(" ", strip=True).split()), source=self.id,
                kind="legislation", url=f"{_SITE}/LSW/eng/engLsInfoR.do?lsiSeq={m.group(1)}&chrClsCd=010203",
                date=_dot_date(cells[2]) if len(cells) > 2 else None, status="unofficial English translation",
                citation=" ".join(x for x in (cells[3] if len(cells) > 3 else "", cells[4] if len(cells) > 4 else "") if x) or None,
                extra={k: v for k, v in {"effective": _dot_date(cells[5]) if len(cells) > 5 else None}.items() if v},
            ))
        return hits[:limit]

    # ---------------------------------------------------------------- fetch
    def fetch(self, native_id, *, language=None):
        nid = native_id.strip()
        en = (language or "").lower().startswith("en")
        if nid.startswith("prec/"):
            return self._fetch_case(nid[5:])
        if nid.startswith("eng/"):
            return self._fetch_english(nid[4:], native_id=nid)
        if nid.startswith("law/"):
            lsi, ef = self._resolve_name(nid[4:])
        else:
            lsi, _, ef = nid.partition("/")
            if not lsi.isdigit():
                raise ValueError("law.go.kr refs: '<lsiSeq>/<efYd>', 'law/<법령명>', 'prec/<id>', 'eng/<lsiSeq>'")
            if not ef:
                raise ValueError("a statute ref needs its version date: '<lsiSeq>/<efYd>' (take it from legal_search), or use 'law/<법령명>'")
        if en:
            name = self._name_of(lsi)
            eng = self._english_for(name) if name else None
            if not eng:
                raise ValueError(f"no English translation found for {name or lsi} on law.go.kr")
            return self._fetch_english(eng, native_id=nid)
        html = _web("POST", "/LSW/lsInfoR.do", data={"lsiSeq": lsi, "efYd": ef, "chrClsCd": "010202", "ancYnChk": "0"}).text
        text, secs, meta = _web_statute_text(html)
        title = meta.pop("title", "") or lsi
        meta.update(_version_meta(meta.get("version_line", "")))
        meta.update({"lsiSeq": lsi, "efYd": ef, "status": _status_for(ef)})
        if meta.get("citation"):
            meta["citation"] = f"{title} [{meta['citation']}]"
        try:
            meta.update(self._versions(title, lsi))
        except Exception:  # noqa: BLE001 — versions are advisory
            pass
        try:
            eng = self._english_for(title)
            if eng:
                meta["english_translation"] = self.ref(f"eng/{eng}")
        except Exception:  # noqa: BLE001
            pass
        if not text.strip() or not secs:
            meta["no_text_reason"] = "law.go.kr returned no statute body for this lsiSeq/efYd"
        return LegalDoc(
            ref=self.ref(nid), title=title, source=self.id, text=text if secs else text,
            url=f"{_SITE}/LSW/lsInfoP.do?lsiSeq={lsi}&efYd={ef}", kind="legislation", language="ko",
            metadata={k: v for k, v in meta.items() if v not in (None, "", [], {})}, sections=secs or None,
        )

    def _resolve_name(self, name: str) -> tuple[str, str]:
        html = _web("GET", f"/법령/{quote(name.strip())}").text
        m = re.search(r"lsiSeq=(\d+)[^\"']*?efYd=(\d{8})", html) or re.search(r"efYd=(\d{8})[^\"']*?lsiSeq=(\d+)", html)
        if not m:
            raise ValueError(f"law.go.kr has no statute named {name!r}; search for it with legal_search")
        a, b = m.group(1), m.group(2)
        return (a, b) if len(b) == 8 and not len(a) == 8 else (b, a)

    def _name_of(self, lsi: str) -> str:
        html = _web("GET", f"/LSW/lsSideInfoP.do?lsiSeq={lsi}&joNo=0001&joBrNo=00&docCls=jo&urlMode=lsScJoRltInfoR",
                    timeout=httpx.Timeout(30.0, connect=10.0)).text
        text = html_to_text(html)
        m = re.search(r"^(.+?)\s*\[시행", text, re.M)
        return m.group(1).replace("법령 화면내검색", "").strip() if m else ""

    def _english_for(self, name: str) -> Optional[str]:
        hits = self._search_english(name, 3)
        return hits[0].ref.split("eng/", 1)[1] if hits else None

    def _versions(self, title: str, lsi: str) -> dict[str, Any]:
        hits = self._search_statutes(title, "keyword", 30, {"status": "history"})
        same = [h for h in hits if h.title == title]
        if not same:
            return {}
        same.sort(key=lambda h: (h.extra.get("effective") or ""), reverse=True)
        today = time.strftime("%Y-%m-%d")
        out: dict[str, Any] = {"versions": [
            {"ref": h.ref, "effective": h.extra.get("effective"), "citation": h.citation,
             "this": True if h.ref.split(":", 1)[1].startswith(lsi + "/") else None}
            for h in same[:10]
        ]}
        out["versions"] = [{k: v for k, v in x.items() if v} for x in out["versions"]]
        future = [h for h in same if (h.extra.get("effective") or "") > today]
        if future:
            out["not_yet_in_force"] = [f"{h.ref} (from {h.extra.get('effective')})" for h in future[:3]]
        return out

    def _fetch_case(self, seq: str) -> LegalDoc:
        if not seq.isdigit():
            num = seq.replace(" ", "")
            found = self._prec_by_number(num)
            if not found:
                raise ValueError(f"no 판례 {seq!r} on law.go.kr")
            seq = found
        html = _web("GET", f"/LSW/precInfoP.do?precSeq={seq}").text
        from bs4 import BeautifulSoup

        soup = BeautifulSoup(html, "lxml")
        h2 = soup.select_one("#contentBody h2")
        sub = soup.select_one("#contentBody .subtit1")
        head = [x.get_text(" ", strip=True) for x in (h2, sub) if x is not None]
        body = html_to_text(html, root_selector="#conScroll")
        text = "\n".join(head + [body]) if body else ""
        lines = text.split("\n")
        title = lines[0] if lines else seq
        meta: dict[str, Any] = {"precSeq": seq}
        m = re.search(r"^\[(.+?)\s(\d{4}\.\s*\d{1,2}\.\s*\d{1,2}\.)\s*(선고\s*)?(.+?)\]$", lines[1] if len(lines) > 1 else "")
        if m:
            meta.update({"court": m.group(1), "date": _dot_date(m.group(2)), "citation": lines[1].strip("[]")})
        if len(text) < 200:
            meta["no_text_reason"] = "law.go.kr shows no judgment text for this id (an external NTS decision opens on taxlaw.nts.go.kr)"
        return LegalDoc(
            ref=self.ref(f"prec/{seq}"), title=title, source=self.id, text=text,
            url=f"{_SITE}/LSW/precInfoP.do?precSeq={seq}", kind="case", language="ko",
            metadata={k: v for k, v in meta.items() if v},
        )

    def _fetch_english(self, lsi: str, *, native_id: str) -> LegalDoc:
        from bs4 import BeautifulSoup

        html = _web("GET", f"/LSW/eng/engLsInfoR.do?lsiSeq={lsi}&chrClsCd=010203").text
        soup = BeautifulSoup(html, "lxml")
        root = soup.select_one("#sideCenter") or soup.body or soup
        for junk in root.select("script, style, input, img, ul.lawico01"):
            junk.decompose()
        notice = root.select_one(".topinfoTxt")
        note = " ".join(notice.get_text(" ", strip=True).split()) if notice else ""
        if notice:
            notice.decompose()
        h2 = root.find("h2")
        title = h2.get_text(" ", strip=True) if h2 else lsi
        sub = root.select_one(".subtit1")
        version = sub.get_text(" ", strip=True) if sub else ""
        text, secs = _english_text(str(root))
        meta: dict[str, Any] = {"lsiSeq": lsi, "version_line": version, "official": False,
                                "note": note[:400] or "Unofficial English translation; only the Korean text has legal effect."}
        m = re.search(r"Enforcement Date\s*([\d]{1,2})\.\s*([A-Za-z]{3}),?\s*(\d{4})", version)
        if m:
            meta["effective"] = time.strftime("%Y-%m-%d", time.strptime(f"{m.group(1)} {m.group(2)} {m.group(3)}", "%d %b %Y"))
        if len(text) < 200:
            meta["no_text_reason"] = "no English text on law.go.kr for this id"
        return LegalDoc(
            ref=self.ref(native_id), title=title, source=self.id, text=text,
            url=f"{_SITE}/LSW/eng/engLsInfoR.do?lsiSeq={lsi}&chrClsCd=010203", kind="legislation", language="en",
            metadata={k: v for k, v in meta.items() if v not in (None, "")}, sections=secs or None,
        )


# ===================================================================== DRF

_DRF = "https://www.law.go.kr/DRF"


def _drf(path: str, params: dict[str, Any], *, want: str = "JSON") -> Any:
    """One DRF call. Retries the bursts of spurious 404s; a 200 that is an
    empty body or an HTML notice is a miss, an auth refusal is SourceUnavailable."""
    oc = os.getenv("KOREA_LAW_OC", "").strip()
    if not oc:
        raise SourceUnavailable("kr.law needs KOREA_LAW_OC (open.law.go.kr OPEN API 신청, with this server's IP).")
    p = {"OC": oc, "type": want, **{k: v for k, v in params.items() if v not in (None, "")}}
    last: Optional[Exception] = None
    resp = None
    for attempt in range(4):
        try:
            resp = request_with_retry("GET", f"{_DRF}/{path}", params=p, headers=_HEADERS, timeout=_LONG, attempts=2)
            break
        except httpx.HTTPStatusError as exc:
            last = exc
            if exc.response.status_code == 404 and attempt < 3:
                time.sleep(0.6 * (attempt + 1))
                continue
            raise SourceUnavailable(f"law.go.kr DRF answered {exc.response.status_code} for {path}") from exc
        except httpx.TransportError as exc:
            raise SourceUnavailable(f"law.go.kr DRF unreachable ({type(exc).__name__})") from exc
    if resp is None:
        raise SourceUnavailable(f"law.go.kr DRF kept answering 404 for {path} ({last})")
    body = resp.text.strip()
    if "검증에 실패" in body or "사용자 정보" in body[:400]:
        msg = body[:300]
        try:
            d = json.loads(body)
            msg = f"{d.get('result', '')} {d.get('msg', '')}".strip()
        except ValueError:
            pass
        raise SourceUnavailable(
            f"law.go.kr DRF refused KOREA_LAW_OC: {msg[:240]} — register this server's IP/domain at "
            "open.law.go.kr → 마이페이지 → API인증키관리."
        )
    if want == "HTML":
        return body
    if not body or body.lstrip().startswith("<!DOCTYPE") or body.lstrip().lower().startswith("<html"):
        return None
    try:
        return json.loads(body)
    except ValueError as exc:
        raise SourceUnavailable(f"law.go.kr DRF {path}: unreadable reply ({body[:120]!r})") from exc


def _first_list(d: Any, *keys: str) -> list[dict[str, Any]]:
    """The result list under a search envelope ({"LawSearch": {"law": [...]}})."""
    if not isinstance(d, dict):
        return []
    env = next(iter(d.values())) if len(d) == 1 and isinstance(next(iter(d.values())), dict) else d
    for k in keys:
        if k in env:
            return [x for x in _listify(env[k]) if isinstance(x, dict)]
    return []


class KrLawSource(Source):
    id = "kr.law"
    probe = {"query": "항공안전법", "kind": "legislation", "ref": "286927", "section": "제97조", "min_chars": 20000}
    name = "국가법령정보 공동활용 Open API (law.go.kr DRF)"
    jurisdictions = ("KR",)
    kinds = ("legislation", "case")
    modes = ("keyword", "fulltext", "semantic")
    languages = ("ko", "en")
    auth_env = ("KOREA_LAW_OC",)
    auth_help = ("Free: register at open.law.go.kr (회원가입 → OPEN API 신청) with this server's public IP or domain; "
                 "KOREA_LAW_OC is the ID (e-mail local part). Until then kr.law_web serves the same database keyless.")
    homepage = "https://open.law.go.kr"
    coverage = (
        "Official MOLEG Open API over 국가법령정보센터: statutes in force (법률·대통령령·부령) by title or 본문, "
        "the statute by 조문/항/호/목, native semantic article search (지능형 법령검색, mode=semantic), 판례 "
        "(대법원·하급심·국세청; 사건명 or 본문; 사건번호 filter), English translations (language=en). Search in "
        "Korean. Refs: '<MST>' (법령일련번호 = one version), 'prec/<판례일련번호>', 'eng/<MST>'. Provisions: "
        "제97조, 제6조의2, 부칙. filters: court (대법원|…), case_number, date_from/date_to (YYYYMMDD), org."
    )

    def search(self, query, *, kind="any", mode="keyword", limit=10, filters=None):
        filters = filters or {}
        q = (query or "").strip()
        english = str(filters.get("language", "")).lower().startswith("en")
        hits: list[Hit] = []
        if kind in ("legislation", "any"):
            if mode == "semantic" and not english:
                hits += self._ai(q, limit)
            else:
                target = "elaw" if english else "law"
                d = _drf("lawSearch.do", {"target": target, "query": q, "search": 2 if mode == "fulltext" else 1,
                                          "display": max(10, min(limit * 2, 100)), "org": filters.get("org")})
                for r in _first_list(d, "law")[: limit * 2]:
                    mst = str(r.get("법령일련번호") or "")
                    if not mst:
                        continue
                    ef = str(r.get("시행일자") or "")
                    title = r.get("법령명영문") if english else r.get("법령명한글")
                    hits.append(Hit(
                        ref=self.ref(f"eng/{mst}" if english else mst), title=str(title or r.get("법령명한글") or mst),
                        source=self.id, kind="legislation",
                        url=f"{_SITE}/LSW/{'eng/engLsInfoR.do' if english else 'lsInfoP.do'}?lsiSeq={mst}" + ("" if english else f"&efYd={ef}"),
                        date=_dot_date(str(r.get("공포일자") or "")), status=str(r.get("현행연혁코드") or _status_for(ef)),
                        citation=f"{r.get('법령구분명') or ''} 제{r.get('공포번호')}호".strip() if r.get("공포번호") else None,
                        extra={k: v for k, v in {"effective": _dot_date(ef), "law_id": r.get("법령ID"),
                                                 "ministry": r.get("소관부처명"), "amendment": r.get("제개정구분명")}.items() if v},
                    ))
                exact = [h for h in hits if h.title == q]
                hits = exact + [h for h in hits if h.title != q]
        if kind in ("case", "any") and not english:
            params = {"target": "prec", "query": q, "search": 2 if mode in ("fulltext", "semantic") else 1,
                      "display": max(10, min(limit, 100)), "curt": filters.get("court"),
                      "nb": filters.get("case_number"),
                      "prncYd": f"{filters.get('date_from', '')}~{filters.get('date_to', '')}" if filters.get("date_from") or filters.get("date_to") else None}
            if _CASE_NO.fullmatch(q.replace(" ", "")) and not filters.get("case_number"):
                params["nb"] = q.replace(" ", "")
                params.pop("query")
            d = _drf("lawSearch.do", params)
            for r in _first_list(d, "prec")[:limit]:
                seq = str(r.get("판례일련번호") or "")
                if not seq:
                    continue
                hits.append(Hit(
                    ref=self.ref(f"prec/{seq}"), title=str(r.get("사건명") or seq), source=self.id, kind="case",
                    url=f"{_SITE}/LSW/precInfoP.do?precSeq={seq}", date=_dot_date(str(r.get("선고일자") or "")),
                    citation=" ".join(str(x) for x in (r.get("법원명"), r.get("선고일자"), r.get("선고"), r.get("사건번호"), r.get("판결유형")) if x),
                    extra={k: v for k, v in {"court": r.get("법원명"), "case_number": r.get("사건번호"),
                                             "source": r.get("데이터출처명")}.items() if v},
                ))
        return hits[: limit if kind != "any" else limit * 2]

    def _ai(self, q: str, limit: int) -> list[Hit]:
        d = _drf("lawSearch.do", {"target": "aiSearch", "search": 0, "query": q, "display": max(5, min(limit, 50))})
        rows = _first_list(d, "법령조문", "aiSearch", "law", "items")
        if not rows and isinstance(d, dict):
            env = next(iter(d.values())) if len(d) == 1 else d
            rows = next((v for v in (env.values() if isinstance(env, dict) else []) if isinstance(v, list)), [])
        hits = []
        for r in rows[:limit]:
            mst = str(r.get("법령일련번호") or "")
            if not mst:
                continue
            jo = str(r.get("조문번호") or "").lstrip("0")
            br = str(r.get("조문가지번호") or "").lstrip("0")
            label = f"제{jo}조" + (f"의{br}" if br else "") if jo else ""
            hits.append(Hit(
                ref=self.ref(mst), title=f"{r.get('법령명') or mst} {label}".strip(), source=self.id, kind="legislation",
                url=f"{_SITE}/LSW/lsInfoP.do?lsiSeq={mst}", date=_dot_date(str(r.get("공포일자") or "")),
                snippet=_clean(r.get("조문내용"))[:500],
                extra={k: v for k, v in {"provision": label, "title": r.get("조문제목"),
                                         "effective": _dot_date(str(r.get("시행일자") or ""))}.items() if v},
            ))
        return hits

    def fetch(self, native_id, *, language=None):
        nid = native_id.strip()
        if nid.startswith("prec/"):
            return self._case(nid[5:])
        if nid.startswith("eng/") or (language or "").lower().startswith("en"):
            return self._english(nid.split("/", 1)[1] if nid.startswith("eng/") else nid, native_id=nid)
        mst = nid.split("/")[0]
        d = _drf("lawService.do", {"target": "law", "MST": mst})
        law = (d or {}).get("법령") if isinstance(d, dict) else None
        if not law:
            raise ValueError(f"law.go.kr DRF has no statute with MST {mst}")
        info = law.get("기본정보") or {}
        w = _Writer()
        title = str(info.get("법령명_한글") or info.get("법령명한글") or mst)
        ef = str(info.get("시행일자") or "")
        w.block(f"{title}\n[시행 {_dot_date(ef) or ''}] [{info.get('법종구분', '') if isinstance(info.get('법종구분'), str) else (info.get('법종구분') or {}).get('content', '')} 제{info.get('공포번호', '')}호, {_dot_date(str(info.get('공포일자') or ''))}]")
        for jo in _listify((law.get("조문") or {}).get("조문단위")):
            if not isinstance(jo, dict):
                continue
            head = _clean(jo.get("조문내용"))
            if jo.get("조문여부") == "전문":
                start = w.block(head)
                w.mark(start, head.split("\n")[0][:120], "", head.split("\n")[0][:120], "container")
                continue
            lines = [head] if head else []
            for hang in _listify(jo.get("항")):
                if not isinstance(hang, dict):
                    continue
                if hang.get("항내용"):
                    lines.append(_clean(hang.get("항내용")))
                for ho in _listify(hang.get("호")):
                    if isinstance(ho, dict):
                        if ho.get("호내용"):
                            lines.append(_clean(ho.get("호내용")))
                        for mok in _listify(ho.get("목")):
                            if isinstance(mok, dict) and mok.get("목내용"):
                                lines.append(_clean(mok.get("목내용")))
            start = w.block("\n".join(lines))
            first = lines[0].split("\n")[0] if lines else ""
            _jo_mark(w, start, first, str(jo.get("조문번호") or ""), str(jo.get("조문가지번호") or ""))
        suppl = [b for b in _listify((law.get("부칙") or {}).get("부칙단위")) if isinstance(b, dict)]
        for i, b in enumerate(suppl):
            label = f"부칙 <제{b.get('부칙공포번호', '')}호, {_dot_date(str(b.get('부칙공포일자') or ''))}>"
            start = w.block(label + "\n" + _clean(b.get("부칙내용")))
            if i == 0:
                w.mark(start, f"부칙 ({len(suppl)}건)", "", "부칙", "container")
                w.mark(start, label, "부칙", label, "article")
            else:
                w.mark(start, label, label.replace(" ", ""), label, "para")
        annexes = [b for b in _listify((law.get("별표") or {}).get("별표단위")) if isinstance(b, dict)]
        for b in annexes:
            t = _clean(b.get("별표제목"))
            body = _clean(b.get("별표내용"))
            link = b.get("별표서식PDF파일링크") or b.get("별표서식파일링크")
            start = w.block("\n".join(x for x in (t, body, f"(file: {_SITE}{link})" if link and str(link).startswith("/") else (link or "")) if x))
            if b is annexes[0]:
                w.mark(start, "별표·서식", "", "별표·서식", "container")
        text, secs = w.result()
        meta = {k: v for k, v in {
            "mst": mst, "law_id": info.get("법령ID"), "effective": _dot_date(ef), "published": _dot_date(str(info.get("공포일자") or "")),
            "status": _status_for(ef), "amendment": info.get("제개정구분") if isinstance(info.get("제개정구분"), str) else None,
            "ministry": (info.get("소관부처") or {}).get("content") if isinstance(info.get("소관부처"), dict) else info.get("소관부처"),
            "citation": f"{title} [제{info.get('공포번호')}호, {_dot_date(str(info.get('공포일자') or ''))}]" if info.get("공포번호") else None,
        }.items() if v}
        if not secs:
            meta["no_text_reason"] = "DRF returned the statute without articles"
        return LegalDoc(ref=self.ref(nid), title=title, source=self.id, text=text,
                        url=f"{_SITE}/LSW/lsInfoP.do?lsiSeq={mst}&efYd={ef}", kind="legislation", language="ko",
                        metadata=meta, sections=secs or None)

    def _case(self, seq: str) -> LegalDoc:
        d = _drf("lawService.do", {"target": "prec", "ID": seq})
        c = None
        if isinstance(d, dict):
            c = d.get("PrecService") or (next(iter(d.values())) if len(d) == 1 else d)
        if not isinstance(c, dict) or not (c.get("판례내용") or c.get("판결요지")):
            # 국세청 cases are served as HTML only.
            html = _drf("lawService.do", {"target": "prec", "ID": seq}, want="HTML")
            text = html_to_text(html)
            if len(text) < 200:
                raise ValueError(f"law.go.kr DRF has no 판례 {seq}")
            return LegalDoc(ref=self.ref(f"prec/{seq}"), title=text.split("\n", 1)[0][:200], source=self.id, text=text,
                            url=f"{_SITE}/LSW/precInfoP.do?precSeq={seq}", kind="case", language="ko")
        parts = [str(c.get("사건명") or ""),
                 f"[{c.get('법원명') or ''} {_dot_date(str(c.get('선고일자') or '')) or ''} {c.get('선고') or ''} {c.get('사건번호') or ''} {c.get('판결유형') or ''}]"]
        for label, key in (("판시사항", "판시사항"), ("판결요지", "판결요지"), ("참조조문", "참조조문"),
                           ("참조판례", "참조판례"), ("전문", "판례내용")):
            v = _clean(c.get(key))
            if v:
                parts += [f"【{label}】", v]
        text = "\n".join(p for p in parts if p.strip())
        return LegalDoc(
            ref=self.ref(f"prec/{seq}"), title=str(c.get("사건명") or seq), source=self.id, text=text,
            url=f"{_SITE}/LSW/precInfoP.do?precSeq={seq}", kind="case", language="ko",
            metadata={k: v for k, v in {"court": c.get("법원명"), "date": _dot_date(str(c.get("선고일자") or "")),
                                        "case_number": c.get("사건번호"),
                                        "citation": f"{c.get('법원명') or ''} {c.get('선고일자') or ''} {c.get('사건번호') or ''}".strip()}.items() if v},
        )

    def _english(self, mst: str, *, native_id: str) -> LegalDoc:
        html = _drf("lawService.do", {"target": "elaw", "MST": mst}, want="HTML")
        text, secs = _english_text(html)
        title = next((ln for ln in text.split("\n") if ln.isupper() and len(ln) > 5), text.split("\n", 1)[0][:200])
        meta: dict[str, Any] = {"official": False, "note": "Unofficial English translation; only the Korean text has legal effect."}
        if len(text) < 200:
            meta["no_text_reason"] = "no English text for this MST"
        return LegalDoc(ref=self.ref(native_id), title=title, source=self.id, text=text,
                        url=f"{_SITE}/LSW/eng/engLsInfoR.do?lsiSeq={mst}&chrClsCd=010203", kind="legislation",
                        language="en", metadata=meta, sections=secs or None)


# The DRF source (KrLawSource) needs an OC id bound to this server's IP; the
# user decided not to register (2026-11-04), so only the keyless web source is
# live. Re-add KrLawSource() here if an OC id is ever obtained.
SOURCES = [KrLawWebSource()]
