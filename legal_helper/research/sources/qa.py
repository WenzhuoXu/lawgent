"""Qatar: Al Meezan, the Ministry of Justice's legal portal (almeezan.qa).

The portal is a single-page app over the MoJ's REST API (api.moj.gov.qa/api/v1);
its public read endpoints need no token (the identity server and reCAPTCHA are
used only for e-services), robots.txt ``Allow: /``. This is an undocumented
API behind the official portal: it can change without notice.

* Legislation title search — ``/Legislation/search`` (English or Arabic
  titles; the API orders by date, so hits are re-ranked by title overlap).
* Full-text search — ``/Search/search-index`` over legislation, rulings,
  agreements and opinions; the index is Arabic, so search in Arabic.
* A law with every article — ``/Legislation/{id}``: the chapter tree with
  each article (المادة) and its own amendment / cancellation flags. Article
  bodies are Arabic (the official language) even when the titles are
  English; ``isMachineTranslated`` is reported.
* Court of Cassation and other rulings — ``/Search/search-index?entityType=
  Ruling`` and ``/Ruling/{id}``.

Added articles (مادة 1 مكرر, مادة 11 مكرر 1) are labelled "Article 1bis" /
"Article 11bis1" (المادة 1bis …): a label with a space after the number would
reduce to the number itself and read the original article.
"""

from __future__ import annotations

import re
from typing import Any, Optional

import httpx

from ...connectors.base import USER_AGENT, request_with_retry
from ..base import Hit, LegalDoc, Source, SourceUnavailable
from ..textify import html_to_text, normalize_lines

_API = "https://api.moj.gov.qa/api/v1"
_SITE = "https://almeezan.qa"
_ARABIC = re.compile(r"[؀-ۿ]")
_UUID = re.compile(r"^[0-9a-f]{8}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{12}$", re.I)
_STATUS = {
    "in force": "in force", "قيد التطبيق": "in force", "ساري": "in force", "سارية": "in force",
    "repealed": "repealed", "ملغى": "repealed", "ملغي": "repealed", "ملغاة": "repealed", "cancelled": "repealed",
    "amended": "amended", "معدل": "amended", "منشور": "published",
}


def _get(path: str, params: Optional[dict[str, Any]] = None, *, lang: str = "ar") -> dict[str, Any]:
    try:
        r = request_with_retry(
            "GET", f"{_API}{path}", params=params,
            headers={"User-Agent": USER_AGENT, "Accept": "application/json", "Accept-Language": lang},
            timeout=httpx.Timeout(60.0, connect=10.0),
        )
    except httpx.HTTPStatusError as exc:
        if exc.response.status_code == 404:
            raise ValueError(f"Al Meezan has no {path}") from exc
        raise SourceUnavailable(f"api.moj.gov.qa answered {exc.response.status_code} for {path}") from exc
    except httpx.TransportError as exc:
        raise SourceUnavailable(f"api.moj.gov.qa unreachable ({type(exc).__name__}); try again later.") from exc
    try:
        data = r.json()
    except ValueError as exc:
        raise SourceUnavailable(f"api.moj.gov.qa {path}: not JSON ({r.text[:100]!r})") from exc
    if data.get("success") is False:
        raise SourceUnavailable(f"api.moj.gov.qa {path}: {data.get('message') or data.get('errors')}")
    return data.get("data") or {}


def _status(s: Any) -> Optional[str]:
    s = str(s or "").strip()
    return _STATUS.get(s.lower(), _STATUS.get(s, s)) or None


def _date(s: Any) -> Optional[str]:
    s = str(s or "")
    return s[:10] if re.match(r"^\d{4}-\d{2}-\d{2}", s) else None


def _html(s: Any) -> str:
    s = str(s or "")
    if not s or s == "None":
        return ""
    if "<" in s:
        s = re.sub(r"<br\s*/?>", "\n", s, flags=re.I)
        return html_to_text(s)
    return normalize_lines(s)


def _words(s: str) -> set[str]:
    return {w for w in re.findall(r"[\w؀-ۿ]+", (s or "").lower()) if len(w) > 2 and w not in {"the", "and", "law", "of", "on", "for"}}


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


class QaAlmeezanSource(Source):
    id = "qa.almeezan"
    probe = {"query": "Civil Aviation", "kind": "legislation", "ref": "law/900dcab1-6eb4-4230-8948-088f9e2645a6",
             "section": "المادة 97", "min_chars": 20000}
    name = "Al Meezan — Qatar Legal Portal (Ministry of Justice)"
    jurisdictions = ("QA",)
    kinds = ("legislation", "case")
    modes = ("keyword", "fulltext")
    languages = ("ar", "en")
    homepage = _SITE
    coverage = (
        "Qatari laws, decree-laws, Amiri decisions, ministerial and cabinet resolutions, ratified agreements "
        "(with status per law and per article), and Court of Cassation and other rulings. keyword = title "
        "search (English or Arabic titles: 'Civil Aviation', 'Law No. 15 of 2002'); fulltext = the Arabic "
        "full-text index (search in Arabic: الطيران المدني, الناقل الجوي); kind=case searches rulings (Arabic). "
        "Article texts are Arabic, the official language; language=en gives English titles where they exist. "
        "Refs: 'law/<uuid>', 'ruling/<uuid>'. Provisions: 'المادة 97' / 'Article 97', added articles "
        "'Article 1bis', 'Article 11bis1'. filters: status (in_force)."
    )

    # ---------------------------------------------------------------- search
    def search(self, query, *, kind="any", mode="keyword", limit=10, filters=None):
        filters = filters or {}
        q = (query or "").strip()
        if not q:
            return []
        hits: list[Hit] = []
        if kind in ("legislation", "any"):
            if mode == "fulltext":
                hits += self._index(q, "Legislation", limit, filters)
            else:
                hits += self._titles(q, limit, filters)
                if not hits:
                    hits += self._index(q, "Legislation", limit, filters)
        if kind in ("case", "any"):
            hits += self._index(q, "Ruling", limit if kind == "case" else max(3, limit // 2), filters)
        return hits

    def _titles(self, q: str, limit: int, filters: dict[str, Any]) -> list[Hit]:
        lang = "ar" if _ARABIC.search(q) else "en"
        d = _get("/Legislation/search", {"searchTerm": q, "pageNumber": 1, "pageSize": 50}, lang=lang)
        rows = d.get("data") or []
        want = _words(q)
        nums = re.findall(r"\d+", q)
        in_force = str(filters.get("status", "")).lower() in ("in_force", "current", "in force")

        def score(r: dict[str, Any]) -> tuple:
            t = r.get("title") or ""
            overlap = len(want & _words(t))
            num_hit = all(n in re.findall(r"\d+", t) for n in nums) if nums else False
            primary = (r.get("type") or "") in ("Law", "Decree-Law", "Decree Law", "قانون", "مرسوم بقانون")
            return (-int(num_hit), -overlap, (_status(r.get("status")) != "in force"), not primary, len(t))

        rows.sort(key=score)
        hits = []
        for r in rows:
            st = _status(r.get("status"))
            if in_force and st != "in force":
                continue
            lid = r.get("id")
            if not lid:
                continue
            hits.append(Hit(
                ref=self.ref(f"law/{lid}"), title=r.get("title") or lid, source=self.id, kind="legislation",
                url=f"{_SITE}/legislations/{lid}", date=_date(r.get("publishDate")), status=st,
                citation=r.get("title"), snippet=_html(r.get("description"))[:300],
                extra={k: v for k, v in {"type": r.get("type"), "effective": _date(r.get("effectiveDate")),
                                         "cancelled": _date(r.get("cancelationDate"))}.items() if v},
            ))
        return hits[:limit]

    def _index(self, q: str, entity: str, limit: int, filters: dict[str, Any]) -> list[Hit]:
        lang = "ar" if _ARABIC.search(q) else "en"
        d = _get("/Search/search-index", {"searchTerm": q, "pageNumber": 1, "pageSize": max(5, min(limit, 30)),
                                          "entityType": entity}, lang=lang)
        hits = []
        in_force = str(filters.get("status", "")).lower() in ("in_force", "current", "in force")
        for it in d.get("items") or []:
            et = it.get("entityType")
            raw_id = str(it.get("id") or "")
            if et == "Ruling":
                rid = raw_id.split("ruling-", 1)[-1]
                if not _UUID.match(rid):
                    continue
                hits.append(Hit(
                    ref=self.ref(f"ruling/{rid}"), title=it.get("titleClear") or it.get("title") or rid, source=self.id,
                    kind="case", url=f"{_SITE}/rulings/{rid}", date=_date(it.get("rulingDate") or it.get("publishDate")),
                    citation=" - ".join(x for x in (it.get("rulingCourtName"), it.get("rulingCircuitName"), it.get("title")) if x),
                    snippet=_html(it.get("principals") or it.get("contentClear"))[:400],
                    extra={k: v for k, v in {"court": it.get("rulingCourtName"), "circuit": it.get("rulingCircuitName"),
                                             "subjects": it.get("rulingSubjectNames")}.items() if v},
                ))
                continue
            if et != "Legislation":
                continue
            lid = it.get("parentLegislationId") or raw_id.split("legislation-", 1)[-1]
            if not lid or not _UUID.match(str(lid)):
                continue
            st = _status(it.get("legislationStatusName"))
            if in_force and st != "in force":
                continue
            hits.append(Hit(
                ref=self.ref(f"law/{lid}"), title=it.get("parentLegislationTitle") or it.get("titleClear") or lid,
                source=self.id, kind="legislation", url=f"{_SITE}/legislations/{lid}",
                date=_date(it.get("publishDate")), status=st, snippet=_html(it.get("contentClear"))[:400],
                extra={k: v for k, v in {"type": it.get("legislationTypeName"),
                                         "gazette": it.get("officialGazetteName")}.items() if v},
            ))
        seen: set[str] = set()
        out = []
        for h in hits:
            if h.ref not in seen:
                seen.add(h.ref)
                out.append(h)
        return out[:limit]

    # ----------------------------------------------------------------- fetch
    def fetch(self, native_id, *, language=None):
        nid = native_id.strip()
        kind, _, uid = nid.partition("/")
        if not uid:
            kind, uid = "law", nid
        if not _UUID.match(uid):
            raise ValueError("Al Meezan refs look like law/<uuid> or ruling/<uuid>; take them from legal_search")
        lang = "en" if (language or "").lower().startswith("en") else "ar"
        if kind == "ruling":
            return self._ruling(uid, lang)
        d = _get(f"/Legislation/{uid}", lang=lang)
        if not d:
            raise ValueError(f"Al Meezan has no legislation {uid}")
        w = _Writer()
        title = d.get("titleClear") or d.get("title") or uid
        w.line(title)
        pre = _html(d.get("descriptionClear") or d.get("description"))
        if pre:
            w.line(pre)
        ar = lang == "ar"
        self._sections(w, d.get("sections") or [], ar=ar, depth=0)
        text, secs = w.result()
        gaz = d.get("officialGazette") or {}
        st = _status(d.get("status"))
        meta: dict[str, Any] = {
            "status": st, "type": d.get("type"), "number": d.get("number"), "year": d.get("year"),
            "published": _date(d.get("publishDate")), "effective": _date(d.get("effectiveDate")),
            "cancelled": _date(d.get("cancelationDate")), "citation": title,
            "articles": d.get("totalAlmawadCounts"),
            "machine_translated": str(d.get("isMachineTranslated")) == "True" or d.get("isMachineTranslated") is True or None,
            "gazette": " ".join(str(x) for x in (gaz.get("title"), f"p. {gaz.get('pageFrom')}" if gaz.get("pageFrom") else None) if x) or None,
            "gazette_pdf": gaz.get("filePath"),
            "authorities": ", ".join(a.get("relevantAuthorityTitle", "") for a in d.get("relevantAuthorities") or [] if a.get("relevantAuthorityTitle")) or None,
            "language_note": None if ar else "English titles; article bodies come back in Arabic where no English text exists.",
        }
        if not secs:
            meta["no_text_reason"] = "Al Meezan returned no articles for this legislation"
        return LegalDoc(
            ref=self.ref(f"law/{uid}"), title=title, source=self.id, text=text, url=f"{_SITE}/legislations/{uid}",
            kind="legislation", language=lang, metadata={k: v for k, v in meta.items() if v not in (None, "", [])},
            sections=secs or None,
        )

    def _sections(self, w: _Writer, sections: list[dict[str, Any]], *, ar: bool, depth: int) -> None:
        for s in sections:
            t = " ".join(str(s.get("title") or "").split())
            if t:
                start = w.line(t)
                w.mark(start, t[:120], "", t[:120], "container")
            for a in s.get("almawad") or []:
                self._article(w, a, ar=ar)
            self._sections(w, s.get("sections") or [], ar=ar, depth=depth + 1)

    def _article(self, w: _Writer, a: dict[str, Any], *, ar: bool) -> None:
        n = str(a.get("number") or "").strip()
        added = str(a.get("isAddition")) == "True"
        try:
            order = int(a.get("additionOrder") or 0)
        except ValueError:
            order = 0
        suffix = ("bis" if order == 0 else f"bis{order}") if added else ""
        label = (f"المادة {n}{suffix}" if ar else f"Article {n}{suffix}") if n else (a.get("title") or "").strip()
        start = w.line(label)
        body = _html(a.get("description"))
        if body:
            w.line(body)
        flags = []
        if str(a.get("isAmended")) == "True":
            flags.append("معدلة" if ar else "amended")
        if added:
            flags.append("مضافة" if ar else "added")
        if str(a.get("isCancel")) == "True" or _date(a.get("cancelationDate")):
            flags.append(("ملغاة" if ar else "repealed") + (f" {_date(a.get('cancelationDate'))}" if _date(a.get("cancelationDate")) else ""))
        eff = _date(a.get("effectiveDate"))
        if flags or eff:
            w.line("[" + "; ".join(flags + ([("نافذة من " if ar else "in force from ") + eff] if eff else [])) + "]")
        note = _html(a.get("footerNote"))
        if note:
            w.line(note)
        if n:
            w.mark(start, label, f"{n}{suffix}".upper(), label, "article")

    def _ruling(self, uid: str, lang: str) -> LegalDoc:
        d = _get(f"/Ruling/{uid}", lang=lang)
        if not d:
            raise ValueError(f"Al Meezan has no ruling {uid}")
        title = d.get("title") or uid
        parts = [title]
        head = " - ".join(str(x) for x in (d.get("court"), d.get("courtCircuit"), _date(d.get("rulingDate"))) if x and x != "None")
        if head:
            parts.append(head)
        principals = _html(d.get("principals"))
        if principals:
            parts += ["المبادئ" if lang == "ar" else "Principles", principals]
        body = _html(d.get("description"))
        if body:
            parts += ["الحكم" if lang == "ar" else "Judgment", body]
        text = "\n".join(p for p in parts if p)
        meta = {k: v for k, v in {
            "court": d.get("court"), "circuit": d.get("courtCircuit"), "date": _date(d.get("rulingDate")),
            "published": _date(d.get("publishDate")), "number": d.get("number"), "year": d.get("year"),
            "citation": title if d.get("court") and str(d.get("court")) in title
            else " - ".join(str(x) for x in (d.get("court"), d.get("courtCircuit"), title) if x and x != "None"),
            "machine_translated": str(d.get("isMachineTranslated")) == "True" or None,
        }.items() if v not in (None, "", "None")}
        if len(body) < 100:
            meta["no_text_reason"] = "Al Meezan returned no ruling text"
        return LegalDoc(ref=self.ref(f"ruling/{uid}"), title=title, source=self.id, text=text,
                        url=f"{_SITE}/rulings/{uid}", kind="case", language=lang, metadata=meta)


SOURCES = [QaAlmeezanSource()]
