"""European Union: EUR-Lex through the Publications Office's Cellar.

Search: CELEX / citation resolution and title search over the Cellar SPARQL
endpoint (``_eurlex``). Fetch: Cellar content negotiation, which serves
legislation as HTML and CJEU judgments as XHTML in every official language.
Open, no key. Article-level reading comes from the shared section index
("Article 9", "[45]" for a judgment paragraph).
"""

from __future__ import annotations

import re
from typing import Any, Optional

import httpx

from ...connectors.base import USER_AGENT, request_with_retry
from ._eurlex import _candidate_celex_ids, _search_by_celex, _search_by_title, _sparql_query
from ..base import Hit, LegalDoc, Source
from ..textify import bytes_to_text

_LANG3 = {
    "en": "eng", "fr": "fra", "de": "deu", "nl": "nld", "it": "ita", "es": "spa", "pt": "por",
    "pl": "pol", "el": "ell", "hu": "hun", "cs": "ces", "sv": "swe", "da": "dan", "fi": "fin",
    "bg": "bul", "hr": "hrv", "et": "est", "ga": "gle", "lv": "lav", "lt": "lit", "mt": "mlt",
    "ro": "ron", "sk": "slk", "sl": "slv",
}
_CELEX = re.compile(r"^(?:CELEX[:_]?)?([0-9][0-9]{4}[A-Z]{1,2}[0-9]{3,5}(?:\(\d+\))?(?:-\d{8})?)$", re.I)


def _kind_of(celex: str) -> str:
    return "case" if celex[:1] == "6" else "legislation"


_PFX = "PREFIX cdm: <http://publications.europa.eu/ontology/cdm#>\nPREFIX lang: <http://publications.europa.eu/resource/authority/language/>\n"


def _v(b: dict[str, Any], k: str) -> Optional[str]:
    x = b.get(k)
    return x.get("value") if isinstance(x, dict) else None


def _title_words(query: str, limit: int) -> list[dict[str, Any]]:
    """Every word in the title (bif:contains AND), newest first."""
    words = [w for w in re.findall(r"[A-Za-z][A-Za-z'-]{2,}", query) if w.lower() not in {"the", "and", "for", "of"}]
    if not words:
        return []
    expr = " AND ".join(f'\\"{w}*\\"' if len(w) >= 4 else f'\\"{w}\\"' for w in words[:8])
    q = _PFX + f"""SELECT DISTINCT ?celex ?title ?date WHERE {{
  ?expr cdm:expression_title ?title . ?title bif:contains "{expr}" .
  ?expr cdm:expression_belongs_to_work ?work ; cdm:expression_uses_language lang:ENG .
  ?work cdm:resource_legal_id_celex ?celex .
  OPTIONAL {{ ?work cdm:work_date_document ?date }}
}} ORDER BY DESC(?date) LIMIT {limit * 2}"""
    rows = _sparql_query(q).get("results", {}).get("bindings", [])
    out, seen = [], set()
    for b in rows:
        c = _v(b, "celex")
        if c and c not in seen:
            seen.add(c)
            out.append({"celex": c, "title": _v(b, "title"), "date": _v(b, "date"),
                        "url": f"https://eur-lex.europa.eu/legal-content/EN/TXT/?uri=CELEX:{c}"})
    return out[:limit]


def _status(celex: str) -> dict[str, Any]:
    """In-force flags, amending acts and consolidated versions of one act."""
    base = celex.split("-")[0]
    if base[:1] == "0":
        base = "3" + base[1:]
    meta: dict[str, Any] = {}
    try:
        q = _PFX + f"""SELECT ?inforce ?eif ?eov ?date WHERE {{
  ?w cdm:resource_legal_id_celex ?c . FILTER(STR(?c) = "{base}")
  OPTIONAL {{ ?w cdm:resource_legal_in-force ?inforce }} OPTIONAL {{ ?w cdm:resource_legal_date_entry-into-force ?eif }}
  OPTIONAL {{ ?w cdm:resource_legal_date_end-of-validity ?eov }} OPTIONAL {{ ?w cdm:work_date_document ?date }} }} LIMIT 1"""
        rows = _sparql_query(q).get("results", {}).get("bindings", [])
        if rows:
            b = rows[0]
            if _v(b, "inforce") is not None:
                meta["in_force"] = _v(b, "inforce") in ("1", "true")
            if _v(b, "eif"):
                meta["entry_into_force"] = _v(b, "eif")
            if _v(b, "eov") and not _v(b, "eov").startswith("9999"):
                meta["end_of_validity"] = _v(b, "eov")
            if _v(b, "date"):
                meta["date"] = _v(b, "date")
        q = _PFX + f"""SELECT DISTINCT ?ac ?title ?eif WHERE {{
  ?base cdm:resource_legal_id_celex ?bc . FILTER(STR(?bc) = "{base}")
  ?a cdm:resource_legal_amends_resource_legal ?base . ?a cdm:resource_legal_id_celex ?ac .
  OPTIONAL {{ ?a cdm:resource_legal_date_entry-into-force ?eif }}
  OPTIONAL {{ ?e cdm:expression_belongs_to_work ?a ; cdm:expression_uses_language lang:ENG ; cdm:expression_title ?title }}
}} ORDER BY DESC(?eif) LIMIT 30"""
        amends: list[dict[str, Any]] = []
        by_ref: dict[str, dict[str, Any]] = {}
        for b in _sparql_query(q).get("results", {}).get("bindings", []):
            c = _v(b, "ac")
            if not c:
                continue
            entry = by_ref.get(c)
            if entry is None:
                entry = {"ref": f"eu.eurlex:{c}", "title": (_v(b, "title") or "")[:160], "dates": []}
                by_ref[c] = entry
                amends.append(entry)
            d = _v(b, "eif")
            if d and d not in entry["dates"]:
                entry["dates"].append(d)
        for a in amends:
            a["dates"].sort()
            # Cellar lists entry into force and, where it differs, the date of application.
            a["entry_into_force"] = a["dates"][0] if a["dates"] else None
            if len(a["dates"]) > 1:
                a["later_dates"] = a["dates"][1:]
            a.pop("dates")
        amends.sort(key=lambda a: a.get("entry_into_force") or "", reverse=True)
        if amends:
            meta["amended_by"] = amends
        q = _PFX + f"""SELECT DISTINCT ?cc WHERE {{
  ?base cdm:resource_legal_id_celex ?bc . FILTER(STR(?bc) = "{base}")
  ?c cdm:act_consolidated_based_on_resource_legal ?base . ?c cdm:resource_legal_id_celex ?cc }}"""
        cons = sorted({_v(b, "cc") for b in _sparql_query(q).get("results", {}).get("bindings", []) if _v(b, "cc")})
        if cons:
            latest = cons[-1]
            meta["latest_consolidated"] = f"eu.eurlex:{latest}"
            meta["consolidated_versions"] = len(cons)
            cons_date = latest.rsplit("-", 1)[-1]
            later = [a for a in amends if (a.get("entry_into_force") or "").replace("-", "") > cons_date]
            if later:
                meta["consolidation_note"] = (
                    f"The latest consolidated text ({latest}) predates {len(later)} amending act(s) "
                    f"in force later (newest: {later[0]['ref']}, {later[0].get('entry_into_force')}); "
                    "read the amending act for the current wording."
                )
    except Exception as exc:  # noqa: BLE001 — status is advisory; the text still comes back
        meta["status_error"] = f"{type(exc).__name__}: {str(exc)[:200]}"
    return meta


class EurlexSource(Source):
    id = "eu.eurlex"
    probe = {"query": "Regulation 261/2004", "ref": "62021CJ0436", "section": "[25]"}
    name = "EUR-Lex (Cellar)"
    jurisdictions = ("EU",)
    kinds = ("legislation", "case")
    modes = ("keyword",)
    languages = ("en", "fr", "de", "nl", "it", "es", "…24 official")
    homepage = "https://eur-lex.europa.eu"
    coverage = (
        "EU Treaties, regulations, directives, decisions and CJEU / General Court judgments, by CELEX "
        "(32004R0261, 62021CJ0436, consolidated 02004R0261-20050217), citation ('Regulation 261/2004', "
        "'C-436/21') or title words. A fetched act reports in_force, amended_by and latest_consolidated. "
        "Text in any official language (language=en|fr|de…). filters: document_type (REG|DIR|DEC)."
    )

    def search(self, query, *, kind="any", mode="keyword", limit=10, filters=None):
        filters = filters or {}
        results: list[dict[str, Any]] = []
        cands = _candidate_celex_ids(query)
        if cands:
            results = _search_by_celex(cands, limit)
        if not results:
            results = _search_by_title(query, filters.get("document_type"), limit)
        if not results:
            results = _title_words(query, limit)
        hits = []
        for r in results:
            celex = r.get("celex") or ""
            if not celex:
                continue
            k = _kind_of(celex)
            if kind != "any" and k != kind:
                continue
            hits.append(Hit(
                ref=self.ref(celex), title=r.get("title") or celex, source=self.id, kind=k,
                url=r.get("url"), date=r.get("date"), citation=celex,
                extra={"eli": r["eli"]} if r.get("eli") else {},
            ))
        return hits

    def fetch(self, native_id, *, language=None):
        m = _CELEX.match(native_id.strip())
        if not m:
            raise ValueError(f"not a CELEX id: {native_id!r} (e.g. 32004R0261, 62021CJ0436)")
        celex = m.group(1).upper()
        requested = (language or "en").lower()
        lang = _LANG3.get(requested[:2]) or (requested if requested in _LANG3.values() else "")
        note = ""
        if not lang:
            # EU acts exist only in the 24 official languages (no Chinese).
            lang = "eng"
            note = f"No {language!r} version exists (EU texts are in the 24 official languages); returned English."
        url = f"https://publications.europa.eu/resource/celex/{celex}"
        resp = request_with_retry(
            "GET", url,
            headers={
                "User-Agent": USER_AGENT,
                "Accept": "application/xhtml+xml, text/html;q=0.9, application/pdf;q=0.5",
                "Accept-Language": lang,
            },
            timeout=httpx.Timeout(60.0, connect=10.0),
        )
        text = bytes_to_text(resp.content, resp.headers.get("content-type", ""), name_hint=str(resp.url))
        # Cellar's HTML opens with the EUR-Lex legal notice line.
        text = re.sub(r"^.*?Avis juridique important\s*\|?\s*", "", text, count=1, flags=re.S) if "Avis juridique" in text[:300] else text
        title = ""
        try:
            found = _search_by_celex([celex], 1)
            title = (found[0].get("title") or "") if found else ""
        except Exception:  # noqa: BLE001 — the title is cosmetic
            title = ""
        if not title:
            first = text.split("\n", 1)[0][:300] if text else ""
            title = first if first and not first.endswith((".xml", ".html")) else celex
        k = _kind_of(celex)
        return LegalDoc(
            ref=self.ref(celex), title=title, source=self.id, text=text,
            url=f"https://eur-lex.europa.eu/legal-content/{lang[:2].upper()}/TXT/?uri=CELEX:{celex}",
            kind=k, language=lang,
            metadata={"celex": celex, **({"language_note": note} if note else {}), **(_status(celex) if k == "legislation" else {})},
        )


SOURCES = [EurlexSource()]
