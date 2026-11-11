"""Macau: court judgments (court.gov.mo) and legislation (Boletim Oficial / 搜法易, DSAJ).

``mo.court`` — 澳門特別行政區法院 (www.court.gov.mo; robots.txt has no
Disallow). Full-text judgment search through the site's own Symfony form:
GET the form (session cookie + CSRF ``_token``), POST it back; up to four
keywords joined by AND, optional court (終審法院 tui, 中級法院 tsi, 初級法院
tjb, 行政法院 ta) and 案件編號. The Chinese form lists judgments written in
Chinese, the Portuguese form those written in Portuguese (each judgment is
in the language of its proceedings). Fetch reads ``/sentence/{zh|pt}/{id}``.

``mo.legis`` — Macau's official legislation, from the Direcção dos Serviços
de Assuntos de Justiça: the 搜法易 search (search.bo.dsaj.gov.mo, robots.txt
``Disallow:`` empty — its JSON ``/_/search`` endpoint, the one its own page
calls) finds 法律, 行政法規, 行政命令, 法令, 批示 … in Chinese or Portuguese
with their state (in force / amended / repealed), and the text is the
Boletim Oficial page of the act on bo.dsaj.gov.mo (``/bo/i/2025/27/lei04_cn.asp``,
Chinese in Big5, Portuguese in cp1252) as published — the act as enacted,
not a consolidation; later amendments are listed from the act's LegisMac
record. (The older io.gov.mo / legismac.safp.gov.mo hosts no longer resolve;
the Assembleia Legislativa's law list at al.gov.mo covers only AL laws with
bill PDFs, so it is not used.)
"""

from __future__ import annotations

import re
from typing import Any, Optional

import httpx

from ...connectors.base import USER_AGENT, request_with_retry
from ..base import Hit, LegalDoc, Source, SourceUnavailable
from ..textify import html_to_text, normalize_lines

_LATIN = re.compile(r"[A-Za-zÀ-ÿ]{3}")
_CJK = re.compile(r"[一-鿿]")


def _lang_of(query: str, filters: dict[str, Any], language: Optional[str] = None) -> str:
    want = str(language or filters.get("language") or "").lower()
    if want.startswith("pt"):
        return "pt"
    if want.startswith("zh") or want.startswith("cn"):
        return "zh"
    return "pt" if _LATIN.search(query or "") and not _CJK.search(query or "") else "zh"


def _decode_html(data: bytes, content_type: str = "") -> str:
    m = re.search(rb"charset=[\"']?([\w-]+)", data[:2000], re.I) or re.search(r"charset=([\w-]+)", content_type or "", re.I)
    enc = (m.group(1).decode() if isinstance(m.group(1), bytes) else m.group(1)).lower() if m else "utf-8"
    enc = {"big5": "big5hkscs", "windows-1252": "cp1252", "iso-8859-1": "cp1252"}.get(enc, enc)
    try:
        return data.decode(enc, errors="replace")
    except LookupError:
        return data.decode("utf-8", errors="replace")


# ===================================================================== courts

_COURT = "https://www.court.gov.mo"
_COURTS = {"tui": "終審法院", "tsi": "中級法院", "tjb": "初級法院", "ta": "行政法院", "all": "所有"}
_COURT_ALIASES = {
    "終審法院": "tui", "终审法院": "tui", "tribunal de última instância": "tui", "tui": "tui",
    "中級法院": "tsi", "中级法院": "tsi", "tribunal de segunda instância": "tsi", "tsi": "tsi",
    "初級法院": "tjb", "初级法院": "tjb", "tribunal judicial de base": "tjb", "tjb": "tjb",
    "行政法院": "ta", "tribunal administrativo": "ta", "ta": "ta",
}
_COURT_NAMES = [
    ("終審法院", "終審法院"), ("中級法院", "中級法院"), ("初級法院", "初級法院"), ("行政法院", "行政法院"),
    ("Tribunal de Última Instância", "終審法院"), ("Tribunal de Segunda Instância", "中級法院"),
    ("Tribunal Judicial de Base", "初級法院"), ("Tribunal Administrativo", "行政法院"),
]
_F = "wizcasesearch_sentence_filter_type"
_PROC_NO = re.compile(r"^\d{1,5}/\d{4}(?:/[A-Z]+)?$")


def _dmy(s: str) -> Optional[str]:
    m = re.match(r"^(\d{1,2})/(\d{1,2})/(\d{4})$", (s or "").strip())
    return f"{m.group(3)}-{int(m.group(2)):02d}-{int(m.group(1)):02d}" if m else (s or None)


class MoCourtSource(Source):
    id = "mo.court"
    probe = {"query": "航空公司", "kind": "case", "ref": "zh/41437", "min_chars": 2000}
    name = "澳門特別行政區法院 Tribunais da RAEM (court.gov.mo)"
    jurisdictions = ("MO",)
    kinds = ("case",)
    modes = ("keyword", "fulltext")
    languages = ("zh", "pt")
    homepage = _COURT
    coverage = (
        "Judgments and decisions of the 終審法院 (TUI), 中級法院 (TSI), 初級法院 (TJB) and 行政法院 (TA) that the "
        "courts publish, full text. Search is full-text (up to 4 words, all must occur); a 案件編號 like 447/2025 "
        "finds that case. Chinese queries list judgments written in Chinese, Portuguese queries those written in "
        "Portuguese (filters.language = zh|pt overrides). Refs: 'zh/41437' or 'pt/16941'. filters: court "
        "(tui|tsi|tjb|ta), language."
    )

    def search(self, query, *, kind="any", mode="keyword", limit=10, filters=None):
        if kind == "legislation":
            return []
        filters = filters or {}
        q = (query or "").strip()
        if not q:
            return []
        lang = _lang_of(q, filters)
        court = _COURT_ALIASES.get(str(filters.get("court") or "").strip().lower(),
                                   _COURT_ALIASES.get(str(filters.get("court") or "").strip(), "all"))
        form: dict[str, Any] = {f"{_F}[court]": court, f"{_F}[recContent][logic]": "AND"}
        if _PROC_NO.match(q):
            form[f"{_F}[procNo]"] = q
            keys: list[str] = []
        else:
            keys = [k for k in re.split(r"[\s　]+", q) if k][:4]
        page = f"{_COURT}/{lang}/subpage/researchjudgments?court={court if court != 'all' else 'tui'}"
        hits: list[Hit] = []
        try:
            with httpx.Client(timeout=httpx.Timeout(40.0, connect=10.0), follow_redirects=True,
                              headers={"User-Agent": USER_AGENT}) as client:
                r = client.get(page)
                r.raise_for_status()
                m = re.search(rf'name="{re.escape(_F)}\[_token\]"[^>]*value="([^"]+)"', r.text)
                if not m:
                    raise SourceUnavailable("court.gov.mo: the judgment search form has no CSRF token (layout changed?)")
                data: dict[str, Any] = dict(form)
                if keys:
                    data[f"{_F}[recContent][key][]"] = keys
                data[f"{_F}[_token]"] = m.group(1)
                r = client.post(page, data=data)
                r.raise_for_status()
                hits = self._parse(r.text, lang, court)
                p = 2
                while len(hits) < limit and p <= 3 and f"page={p}" in r.text:
                    r = client.get(f"{page}&page={p}")
                    r.raise_for_status()
                    more = self._parse(r.text, lang, court)
                    if not more:
                        break
                    hits += [h for h in more if h.ref not in {x.ref for x in hits}]
                    p += 1
        except httpx.HTTPError as exc:
            raise SourceUnavailable(f"court.gov.mo unreachable ({type(exc).__name__}: {str(exc)[:120]})") from exc
        return hits[:limit]

    def _parse(self, html: str, lang: str, court: str) -> list[Hit]:
        from bs4 import BeautifulSoup

        soup = BeautifulSoup(html, "lxml")
        hits: list[Hit] = []
        for block in soup.select("div.case_list"):
            row = next((li for li in block.find_all("li", recursive=False) if "seperate" not in (li.get("class") or [])), None)
            if row is None:
                continue
            date = row.select_one("span.date")
            num = row.select_one("span.num")
            typ = row.select_one("span.type")
            links = [a.get("href") or "" for a in row.select("span.download a")]
            txts = [h for h in links if re.match(r"^/sentence/(zh|pt)/\d+$", h)]
            txt = next((h for h in txts if h.startswith(f"/sentence/{lang}/")), txts[0] if txts else None)
            if not txt:
                continue
            native = txt.split("/sentence/", 1)[1]
            fields: dict[str, str] = {}
            for box in block.select("div.more-block"):
                tit = box.select_one(".case_tit")
                body = box.select_one(".case_summary") or box
                k = tit.get_text(" ", strip=True) if tit else ""
                v = " ".join(body.get_text(" ", strip=True).split())
                if k and k not in fields:
                    fields[k] = v
            subject = fields.get("主題") or fields.get("Assunto") or ""
            summary = fields.get("摘要") or fields.get("Sumário") or ""
            number = num.get_text(" ", strip=True) if num else ""
            kind_ = " ".join(typ.get_text(" ", strip=True).split()) if typ else ""
            pdf = next((h for h in links if h.endswith(".pdf")), None)
            hits.append(Hit(
                ref=self.ref(native),
                title=f"{'案件編號' if lang == 'zh' else 'Processo n.º'} {number} — {kind_}" + (f" — {subject[:80]}" if subject else ""),
                source=self.id, kind="case", url=f"{_COURT}{txt}", date=_dmy(date.get_text(strip=True) if date else ""),
                citation=number or None, snippet=summary[:400],
                extra={k: v for k, v in {"court": _COURTS.get(court) if court != "all" else None, "subject": subject[:200] or None,
                                         "pdf": f"{_COURT}{pdf}" if pdf else None}.items() if v},
            ))
        return hits

    def fetch(self, native_id, *, language=None):
        nid = native_id.strip().strip("/")
        m = re.match(r"^(zh|pt)/(\d+)$", nid)
        if not m:
            raise ValueError("court.gov.mo refs look like 'zh/41437' or 'pt/16941' (take them from legal_search)")
        lang, sid = m.group(1), m.group(2)
        if language and language[:2] in ("zh", "pt") and language[:2] != lang:
            lang = language[:2]
        url = f"{_COURT}/sentence/{lang}/{sid}"
        try:
            r = request_with_retry("GET", url, headers={"User-Agent": USER_AGENT}, timeout=httpx.Timeout(60.0, connect=10.0))
        except httpx.HTTPStatusError as exc:
            if exc.response.status_code == 404:
                raise ValueError(f"court.gov.mo has no {lang} text for judgment {sid}") from exc
            raise
        text = html_to_text(_decode_html(r.content, r.headers.get("content-type", "")))
        # The page opens with the subject lines and a print button before the judgment itself.
        head = re.search(r"^(?:打印全文|Imprimir(?: texto integral)?)\s*$", text[:3000], re.M)
        if head:
            text = text[head.end():]
        text = text.lstrip("\ufeff \n").strip()
        meta: dict[str, Any] = {}
        for name, zh in _COURT_NAMES:
            if name in text[:6000]:
                meta["court"] = zh if lang == "zh" else name
                break
        mm = (re.search(r"(?:編號[：:]\s*|上訴案|案件)?第\s*([\d]+/\d{4})\s*號", text[:800])
              or re.search(r"Processo\s+n\.?\s*[º°o]\s*([\d]+/\d{4})", text[:1500]))
        if mm:
            meta["case_number"] = mm.group(1)
        md = re.search(r"日期[：:]\s*(\d{4})年(\d{1,2})月(\d{1,2})日", text)
        if md:
            meta["date"] = f"{md.group(1)}-{int(md.group(2)):02d}-{int(md.group(3)):02d}"
        if meta.get("court") and meta.get("case_number"):
            meta["citation"] = f"{meta['court']} 第{meta['case_number']}號" if lang == "zh" else f"{meta['court']}, Processo n.º {meta['case_number']}"
        title = meta.get("citation") or text.split("\n", 1)[0][:200] or sid
        if len(text) < 200:
            meta["no_text_reason"] = "court.gov.mo returned no judgment text (try the other language or the PDF)"
        return LegalDoc(ref=self.ref(f"{lang}/{sid}"), title=title, source=self.id, text=text, url=url, kind="case",
                        language=lang, metadata=meta)


# ================================================================ legislation

_SEARCH = "https://search.bo.dsaj.gov.mo"
_ID = re.compile(r"^L[EG]\d{10}$")


def _state(states: list[str]) -> str:
    s = set(states or [])
    if "abolished" in s:
        return "repealed"
    if "inactive" in s:
        return "not in force"
    if "altered" in s:
        return "in force (amended)"
    return "in force" if "active" in s else ", ".join(sorted(s)) or "unknown"


class MoLegisSource(Source):
    id = "mo.legis"
    probe = {"query": "民航活動法", "kind": "legislation", "ref": "LE0000045864", "section": "第三十條", "min_chars": 5000}
    name = "澳門特別行政區公報 / 搜法易 (DSAJ, bo.dsaj.gov.mo)"
    jurisdictions = ("MO",)
    kinds = ("legislation",)
    modes = ("keyword", "fulltext")
    languages = ("zh", "pt")
    homepage = "https://www.bo.dsaj.gov.mo"
    coverage = (
        "Macau legislation published in the Boletim Oficial — 法律, 行政法規, 行政命令, 法令, 訓令, 批示 — found "
        "through DSAJ's 搜法易 search (title and text, Chinese or Portuguese) with each act's state (in force / "
        "amended / repealed). Text is the official gazette publication (as enacted, not consolidated); the "
        "fetch lists later amendments and repeals from LegisMac. Refs 'LE0000045864' (第4/2025號法律 民航活動法). "
        "language=zh (default) or pt. Provisions: 第三十條 / Artigo 30 (ask without the .º). filters: status (in_force), language."
    )

    def search(self, query, *, kind="any", mode="keyword", limit=10, filters=None):
        if kind == "case":
            return []
        filters = filters or {}
        q = (query or "").strip()
        if not q:
            return []
        lang = _lang_of(q, filters)
        try:
            r = request_with_retry(
                "GET", f"{_SEARCH}/_/search",
                params={"lang": f"{lang}-mo", "keyword": q, "type": "laws", "page": 1},
                headers={"User-Agent": USER_AGENT, "Accept": "application/json", "X-Requested-With": "XMLHttpRequest"},
                timeout=httpx.Timeout(40.0, connect=10.0),
            )
            data = r.json()
        except (httpx.HTTPError, ValueError) as exc:
            raise SourceUnavailable(f"搜法易 search.bo.dsaj.gov.mo unavailable ({type(exc).__name__}: {str(exc)[:120]})") from exc
        key = "cn" if lang == "zh" else "pt"
        in_force = str(filters.get("status", "")).lower() in ("in_force", "current", "有效", "生效", "em vigor")
        hits: list[Hit] = []
        for rec in data.get("data") or []:
            ident = rec.get("identifier") or ""
            if not _ID.match(ident):
                continue
            states = rec.get("states") or []
            status = _state(states)
            if in_force and status in ("repealed", "not in force"):
                continue
            name = (rec.get("name") or {}).get(key) or (rec.get("name") or {}).get("cn") or ""
            desc = (rec.get("description") or {}).get(key) or (rec.get("description") or {}).get("cn") or ""
            di = rec.get("document_info") or {}
            pi = rec.get("publication_info") or {}
            content = (rec.get("content") or {}).get(key) or ""
            hl = rec.get("highlight") or {}
            marks = (hl.get("content") or []) if isinstance(hl.get("content"), list) else []
            snippet = " … ".join(re.sub(r"</?mark>", "**", m) for m in marks[:2]) if marks else content[:300]
            hits.append(Hit(
                ref=self.ref(ident), title=f"{name} {desc}".strip(), source=self.id, kind="legislation",
                url=f"{_SEARCH}/_/documents/{ident}/page?lang={lang}-mo", date=(pi.get("published_at") or "")[:10] or None,
                status=status, citation=name or None, snippet=snippet,
                extra={k: v for k, v in {
                    "type": ((di.get("type") or {}).get("name") or {}).get(key), "type_code": (di.get("type") or {}).get("code"),
                    "bulletin": f"BO {pi.get('number')}/{pi.get('year')} série {pi.get('series')}" if pi.get("number") else None,
                }.items() if v},
            ))
        return hits[:limit]

    def fetch(self, native_id, *, language=None):
        from bs4 import BeautifulSoup

        ident = native_id.strip()
        if not _ID.match(ident):
            raise ValueError("Macau legislation refs look like LE0000045864 (take them from legal_search)")
        lang = "pt" if (language or "").lower().startswith("pt") else "zh"
        try:
            r = request_with_retry("GET", f"{_SEARCH}/_/documents/{ident}/page", params={"lang": f"{lang}-mo"},
                                   headers={"User-Agent": USER_AGENT}, timeout=httpx.Timeout(60.0, connect=10.0))
        except httpx.HTTPStatusError as exc:
            if exc.response.status_code == 404:
                raise ValueError(f"no Macau act {ident}") from exc
            raise SourceUnavailable(f"bo.dsaj.gov.mo answered {exc.response.status_code}") from exc
        except httpx.TransportError as exc:
            raise SourceUnavailable(f"bo.dsaj.gov.mo unreachable ({type(exc).__name__})") from exc
        url = str(r.url)
        html = _decode_html(r.content, r.headers.get("content-type", ""))
        if "alicdn.com" in html[:3000] and "content" not in html:
            raise SourceUnavailable("bo.dsaj.gov.mo served a bot-check page instead of the act; not bypassed.")
        soup = BeautifulSoup(html, "lxml")
        legismac = None
        pdf = None
        for a in soup.find_all("a"):
            t = a.get_text(" ", strip=True)
            href = a.get("href") or ""
            if "LegisMac" in t and href.startswith("http"):
                legismac = href
            if ("PDF" in t) and href.endswith(".pdf"):
                pdf = href.replace("//bo/", "/bo/")
        text = html_to_text(html, root_selector="#content") if soup.select_one("#content") else html_to_text(html)
        meta: dict[str, Any] = {"identifier": ident}
        # The page opens with the bulletin record (法規 / 公報編號 / 刊登日期 / 廢止 / 相關法規 …), then the act.
        cut = re.search(r"^(?:《公報》原始 PDF 版本|Versão original em formato PDF)\s*$", text, re.M)
        header = text[: cut.start()] if cut else ""
        if cut:
            text = text[cut.end():].strip()
        for line in header.split("\n"):
            if " : | " in line or "： | " in line:
                k, _, v = line.partition("|")
                k = k.strip(" :：")
                if k in ("廢止", "Diplomas revogados"):
                    meta["repeals"] = v.strip()[:1500]
                elif k in ("相關法規", "Diplomas relacionados"):
                    meta["related"] = v.strip()[:1500]
                elif k in ("相關類別", "Categorias relacionadas"):
                    meta["categories"] = v.strip()[:300]
            m = re.search(r"(?:法規|Diploma)[：:]\s*\|\s*(.+?)\s+(?:公報編號|BO N\.º)[：:]\s*\|\s*([\d/]+)\s+(?:刊登日期|Publicado em)[：:]\s*\|\s*([\d.]+)", line)
            if m:
                meta["citation"] = m.group(1).strip()
                meta["bulletin"] = m.group(2)
                d = m.group(3).split(".")
                if len(d) == 3:
                    meta["published"] = f"{d[0]}-{int(d[1]):02d}-{int(d[2]):02d}"
                tail = line.split("|")[-1].strip()
                if tail:
                    meta["summary"] = tail
        title = " ".join(x for x in (meta.get("citation"), meta.get("summary")) if x) or text.split("\n", 1)[0][:200]
        if legismac:
            rec = self._legismac(legismac, lang)
            if rec.get("repeals") and not meta.get("repeals"):
                meta["repeals"] = rec["repeals"]
            rec.pop("repeals", None)
            meta.update(rec)
            if meta.get("ended_by") and meta.get("status") in (None, ""):
                meta["status"] = "not in force"
        if pdf:
            meta["gazette_pdf"] = pdf
        meta["note"] = ("Text as published in the Boletim Oficial (as enacted, not consolidated); 'status', "
                        "'amended_by' and 'ended_by' (acts that ended its force) come from the act's LegisMac record.")
        if len(text) < 200:
            meta["no_text_reason"] = "the gazette page carries no text for this act (see gazette_pdf)"
        return LegalDoc(ref=self.ref(ident), title=title, source=self.id, text=normalize_lines(text), url=url,
                        kind="legislation", language=lang, metadata={k: v for k, v in meta.items() if v})

    def _legismac(self, url: str, lang: str) -> dict[str, Any]:
        """State, amendments and repeals from the act's LegisMac record."""
        from bs4 import BeautifulSoup

        try:
            r = request_with_retry("GET", url, headers={"User-Agent": USER_AGENT}, timeout=httpx.Timeout(30.0, connect=10.0))
        except httpx.HTTPError:
            return {}
        lines = [ln for ln in BeautifulSoup(r.text, "lxml").get_text("\n", strip=True).split("\n") if ln.strip()]
        # On a LegisMac record 已更改 lists the acts that amended this one, 廢止 the acts this one
        # repealed, 不生效 / 部份不生效 the acts that ended (part of) its force.
        labels = {"生效狀況": "status", "Situação": "status", "已更改": "amended_by", "Alterações": "amended_by",
                  "廢止": "repeals", "Diplomas revogados": "repeals", "不生效": "ended_by", "Não vigência": "ended_by",
                  "註記": "notes", "Notas": "notes", "部份不生效": "partly_ended_by", "Revogação parcial": "partly_ended_by"}
        out: dict[str, Any] = {"legismac": url}
        cur: Optional[str] = None
        buf: list[str] = []
        stop = set(labels) | {"相關法規", "Diplomas relacionados", "敘述詞", "Descritores", "摘要", "Sumário", "頁數",
                              "Página", "Páginas", "經適應化處理", "Adaptação"}

        def flush() -> None:
            if cur and buf:
                v = " ".join(x for x in buf if x not in ("-", "/"))
                if v:
                    out[labels[cur]] = v[:1200]

        for ln in lines:
            if cur in ("生效狀況", "Situação") and not buf:
                buf.append(ln)  # the value may itself read 廢止 / 不生效, which are also labels
                continue
            if ln in stop:
                flush()
                cur = ln if ln in labels else None
                buf = []
                continue
            if cur:
                buf.append(ln)
        flush()
        st = out.get("status", "")
        if st:
            out["status"] = {"生效": "in force", "不生效": "not in force", "廢止": "repealed", "已廢止": "repealed",
                             "部份生效": "partly in force", "Em vigor": "in force", "Revogado": "repealed",
                             "Parcialmente em vigor": "partly in force", "Não vigente": "not in force"}.get(st, st)
        return out


SOURCES = [MoCourtSource(), MoLegisSource()]
