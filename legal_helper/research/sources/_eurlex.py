"""EUR-Lex citation resolution and Cellar SPARQL title search.

Helpers for ``eu.py`` (moved from the retired ``connectors/eu.py``): CELEX
extraction from citations ("Regulation (EC) No 2027/97", "C-436/21",
consolidated "02004R0261-20050217"), SPARQL lookups by CELEX and title.
A module whose name starts with ``_`` is never loaded as a source.
"""

from __future__ import annotations

import re
from typing import Any, Optional

from ...connectors.base import SEARCH_TTL, http_get, truncate


_SPARQL_ENDPOINT = "https://publications.europa.eu/webapi/rdf/sparql"

# CELEX expressions like 32016R0679, 32001L0051, 62019CJ0311.
# Sector digit (1-9), 4-digit year, document-type letter(s), 3-5 digit number.
_CELEX_RE = re.compile(r"\b([0-9]\d{4}[A-Z]{1,2}\d{3,5}(?:-\d{8})?)\b")

# Legislation: "Regulation (EU) 2016/679" / "Regulation (EC) No 1907/2006" /
# "Regulation 261/2004" / "Directive 2004/82/EC" / "Decision 2008/615/JHA".
# Two numbers separated by "/", in either order — _decode_legis() figures out
# which is the year. The "No" is optional (handled by `[^/]{0,60}?`).
_REG_RE = re.compile(r"\bregulation\b[^/]{0,60}?\b(\d{1,4})\s*/\s*(\d{2,4})\b", re.I)
_DIR_RE = re.compile(r"\bdirective\b[^/]{0,60}?\b(\d{1,4})\s*/\s*(\d{2,4})\b", re.I)
_DEC_RE = re.compile(r"\bdecision\b[^/]{0,60}?\b(\d{1,4})\s*/\s*(\d{2,4})\b", re.I)

# CJEU cases. "C-402/07" or "Case C-402/07" or "Joined Cases C-402/07 and
# C-432/07". Court of Justice judgments use sector 6, doc-type CJ; General
# Court (T-…) uses TJ. The year may be 2- or 4-digit.
_CASE_CJ_RE = re.compile(r"\bC[-‑]\s*(\d{1,4})\s*/\s*(\d{2,4})\b")
_CASE_TJ_RE = re.compile(r"\bT[-‑]\s*(\d{1,4})\s*/\s*(\d{2,4})\b")

_DOC_TYPE_LETTER = {
    "REG": "R",
    "REGULATION": "R",
    "DIR": "L",
    "DIRECTIVE": "L",
    "DEC": "D",
    "DECISION": "D",
}


def _expand_year(raw: str) -> int:
    """Expand a 2- or 4-digit year string. 2-digit years split at 70: < 70
    becomes 20XX, >= 70 becomes 19XX (CJEU goes back to 1954)."""
    y = int(raw)
    if y < 100:
        return 2000 + y if y < 70 else 1900 + y
    return y


def _decode_legis(a: str, b: str, kind: str = "R") -> Optional[tuple[int, int]]:
    """Decode an 'X/Y' legislative reference into (year, number), or None.

    EU numbering until 2014: regulations are NUMBER/YEAR (261/2004, and with a
    two-digit year before 1999: 2027/97, 95/93); directives and decisions are
    YEAR/NUMBER (2004/38/EC, 95/46/EC). Since 2015 everything is YEAR/NUMBER
    (2016/679). A two-digit component is always a year in its slot — reading
    "2027/97" as year 2027 produced a CELEX that does not exist.
    """
    ai, bi = int(a), int(b)
    if kind == "R":
        if len(b) == 2:
            return _expand_year(b), ai
        if 1900 <= bi <= 2099 and 1 <= ai <= 99999:
            return bi, ai
        if 1900 <= ai <= 2099 and 1 <= bi <= 99999:
            return ai, bi
        return None
    if len(a) == 2:
        return _expand_year(a), bi
    if 1900 <= ai <= 2099 and 1 <= bi <= 99999:
        return ai, bi
    if 1900 <= bi <= 2099 and 1 <= ai <= 99999:
        return bi, ai
    return None


def _candidate_celex_ids(query: str) -> list[str]:
    """Best-effort extraction of CELEX IDs implied by the natural-language query."""
    out: list[str] = []
    for m in _CELEX_RE.findall(query):
        out.append(m.upper())

    def _push(sector: str, letter: str, year: int, number: int) -> None:
        if not (1900 <= year <= 2099):
            return
        if not (1 <= number <= 99999):
            return
        out.append(f"{sector}{year:04d}{letter}{number:04d}")

    for a, b in _REG_RE.findall(query):
        decoded = _decode_legis(a, b, "R")
        if decoded:
            _push("3", "R", *decoded)
    for a, b in _DIR_RE.findall(query):
        decoded = _decode_legis(a, b, "L")
        if decoded:
            _push("3", "L", *decoded)
    for a, b in _DEC_RE.findall(query):
        decoded = _decode_legis(a, b, "D")
        if decoded:
            _push("3", "D", *decoded)

    # CJEU cases: number/year order, year is always the second component.
    for number, year in _CASE_CJ_RE.findall(query):
        _push("6", "CJ", _expand_year(year), int(number))
    for number, year in _CASE_TJ_RE.findall(query):
        _push("6", "TJ", _expand_year(year), int(number))

    # Dedupe preserving order.
    seen: set[str] = set()
    uniq: list[str] = []
    for c in out:
        if c not in seen:
            seen.add(c)
            uniq.append(c)
    return uniq


def _sparql_query(sparql: str) -> dict[str, Any]:
    # http_get gives retry-with-backoff, the 15-min search cache, and raises
    # ConnectorHTTPError (an httpx.HTTPError) on a non-JSON 200 — Virtuoso
    # answers some malformed queries with an HTML error page at status 200.
    return http_get(
        _SPARQL_ENDPOINT,
        params={"query": sparql, "format": "application/sparql-results+json"},
        headers={"Accept": "application/sparql-results+json"},
        cache_ttl=SEARCH_TTL,
    )


def _row(b: dict[str, Any], key: str) -> Optional[str]:
    v = b.get(key)
    return v.get("value") if isinstance(v, dict) else None


def _format_results(bindings: list[dict[str, Any]], per_page: int) -> list[dict[str, Any]]:
    seen: set[str] = set()
    out: list[dict[str, Any]] = []
    for b in bindings:
        celex = _row(b, "celex") or ""
        if celex in seen:
            continue
        seen.add(celex)
        title = _row(b, "title") or ""
        eli = _row(b, "eli")
        date = _row(b, "date")
        out.append(
            {
                "celex": celex,
                "title": truncate(title, 400),
                "date": date,
                "eli": eli,
                "url": (
                    f"https://eur-lex.europa.eu/legal-content/EN/TXT/?uri=CELEX:{celex}"
                    if celex
                    else None
                ),
            }
        )
        if len(out) >= per_page:
            break
    return out


def _doc_type_filter(document_type: Optional[str]) -> str:
    if not document_type:
        return ""
    letter = _DOC_TYPE_LETTER.get(document_type.strip().upper())
    if not letter:
        return ""
    # CELEX letter is at offset 5 (sector digit + 4-digit year + 1 letter).
    return f'FILTER (SUBSTR(STR(?celex), 5, 1) = "{letter}")'


def _search_by_celex(celex_ids: list[str], per_page: int) -> list[dict[str, Any]]:
    # Issue one SPARQL call per CELEX in parallel. Virtuoso handles a batched
    # ``FILTER (STR(?celex) IN (…))`` very unevenly (4-30 s with title join);
    # single-CELEX queries are consistently ~1 s and parallel httpx gives us
    # near-constant total latency regardless of how many CELEX IDs we pass.
    import concurrent.futures

    def _one(celex: str) -> list[dict[str, Any]]:
        sparql = f"""
PREFIX cdm: <http://publications.europa.eu/ontology/cdm#>
PREFIX lang: <http://publications.europa.eu/resource/authority/language/>
SELECT ?celex ?title ?date ?eli WHERE {{
  ?work cdm:resource_legal_id_celex ?celex .
  FILTER (STR(?celex) = "{celex}")
  OPTIONAL {{ ?work cdm:resource_legal_eli ?eli }}
  OPTIONAL {{ ?work cdm:resource_legal_date_signature ?date }}
  OPTIONAL {{
    ?expr cdm:expression_belongs_to_work ?work ;
          cdm:expression_uses_language lang:ENG ;
          cdm:expression_title ?title .
  }}
}} LIMIT 1
"""
        data = _sparql_query(sparql)
        return data.get("results", {}).get("bindings", [])

    ids = celex_ids[: per_page * 2]
    bindings: list[dict[str, Any]] = []
    with concurrent.futures.ThreadPoolExecutor(max_workers=min(len(ids), 6)) as pool:
        for batch in pool.map(_one, ids):
            bindings.extend(batch)
    return _format_results(bindings, per_page)


def _search_by_title(query: str, document_type: Optional[str], per_page: int) -> list[dict[str, Any]]:
    # Strip noise tokens and quote escapes; bif:contains expects a Lucene-ish
    # phrase / boolean expression, so we wrap the whole query as a phrase.
    needle = query.strip().replace('"', "").replace("'", "")
    if len(needle) < 3:
        return []
    filt = _doc_type_filter(document_type)
    sparql = f"""
PREFIX cdm: <http://publications.europa.eu/ontology/cdm#>
PREFIX lang: <http://publications.europa.eu/resource/authority/language/>
SELECT DISTINCT ?celex ?title ?date ?eli WHERE {{
  ?expr cdm:expression_title ?title .
  ?title bif:contains '"{needle}"' .
  ?expr cdm:expression_belongs_to_work ?work ;
        cdm:expression_uses_language lang:ENG .
  ?work cdm:resource_legal_id_celex ?celex .
  OPTIONAL {{ ?work cdm:resource_legal_eli ?eli }}
  OPTIONAL {{ ?work cdm:resource_legal_date_signature ?date }}
  {filt}
}} LIMIT {per_page * 2}
"""
    data = _sparql_query(sparql)
    return _format_results(data.get("results", {}).get("bindings", []), per_page)
