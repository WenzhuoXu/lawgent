"""EU law via EUR-Lex.

The legacy ``EURLexWebService/search.htm`` endpoint that this connector used
to call returns HTTP 404 (the public Quick-Search HTML endpoint that used to
respond with machine-readable JSON has been retired). Free-text and CELEX
lookups now go through the Publications Office Cellar SPARQL endpoint at
``https://publications.europa.eu/webapi/rdf/sparql``, which is open and
unauthenticated.

The connector still emits the same JSON shape (``source``, ``query``,
``count``, ``results[...]``) so calling skills do not need to change.
"""

import json
import re
from typing import Any, Optional

import httpx
from anthropic import beta_tool

from ._body_fetch import fetch_body_text

from .base import SEARCH_TTL, http_get, truncate


_SPARQL_ENDPOINT = "https://publications.europa.eu/webapi/rdf/sparql"

# CELEX expressions like 32016R0679, 32001L0051, 62019CJ0311.
# Sector digit (1-9), 4-digit year, document-type letter(s), 3-5 digit number.
_CELEX_RE = re.compile(r"\b([1-9]\d{3,4}[A-Z]{1,2}\d{3,5})\b")

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


def _decode_legis(a: str, b: str) -> Optional[tuple[int, int]]:
    """Decode a 'X/Y' legislative reference. Returns (year, number) or None.

    Pre-2015 style is ``NUMBER/YEAR`` (e.g. 261/2004); post-2015 is
    ``YEAR/NUMBER`` (e.g. 2016/679). The year-looking number wins.
    """
    ai, bi = int(a), int(b)
    if 1900 <= bi <= 2099 and 1 <= ai <= 99999:
        return bi, ai
    if 1900 <= ai <= 2099 and 1 <= bi <= 99999:
        return ai, bi
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
        decoded = _decode_legis(a, b)
        if decoded:
            _push("3", "R", *decoded)
    for a, b in _DIR_RE.findall(query):
        decoded = _decode_legis(a, b)
        if decoded:
            _push("3", "L", *decoded)
    for a, b in _DEC_RE.findall(query):
        decoded = _decode_legis(a, b)
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


@beta_tool
def eurlex_search(
    query: str,
    document_type: Optional[str] = None,
    per_page: int = 8,
) -> str:
    """Search EUR-Lex. Returns CELEX + title + ELI + URL only — no body
    text. Call ``eurlex_fetch(celex)`` on a hit to get the verbatim
    article body.

    Backed by the Publications Office Cellar SPARQL endpoint. The connector
    first scans the query for CELEX IDs and citation patterns it can convert
    deterministically to a CELEX; only when no CELEX candidate matches does
    it fall back to an English-title full-text search.

    **Pass the citation form, not a sentence.** Recognised inputs:

    - CELEX:                ``32016R0679``, ``62007CJ0549``
    - Regulation:           ``Regulation (EU) 2016/679`` / ``Regulation 261/2004``
    - Directive:            ``Directive 2004/82/EC``
    - Decision:             ``Decision 2008/615/JHA``
    - CJEU judgment:        ``C-402/07``, ``Joined Cases C-402/07 and C-432/07``
    - General Court:        ``T-201/04``
    - Free-text title:      ``GDPR data minimisation`` / ``Schengen Borders Code``

    Extra descriptive words after the citation (party names, topic keywords)
    don't help and can hurt — they push the connector into the slower
    title-text fallback. Issue one tight call per CELEX rather than one big
    multi-citation prose query.

    Args:
        query: Citation, CELEX, or short title phrase.
        document_type: Optional EUR-Lex document type filter — ``REG``,
            ``DIR``, ``DEC``. Omit to search all.
        per_page: Results per page (1-20).
    """
    per_page = max(1, min(int(per_page), 20))
    results: list[dict[str, Any]] = []
    errors: list[str] = []

    candidates = _candidate_celex_ids(query)
    if candidates:
        try:
            results = _search_by_celex(candidates, per_page)
        except httpx.HTTPError as e:
            errors.append(f"CELEX lookup failed: {e!s}")

    if not results:
        try:
            results = _search_by_title(query, document_type, per_page)
        except httpx.HTTPError as e:
            errors.append(f"title search failed: {e!s}")

    if not results and errors:
        return json.dumps(
            {
                "error": "; ".join(errors),
                "source": "EUR-Lex",
                "query": query,
            },
            ensure_ascii=False,
        )

    return json.dumps(
        {
            "source": "EUR-Lex",
            "query": query,
            "document_type_filter": document_type or "all",
            "candidate_celex": candidates,
            "count": len(results),
            "results": results,
        },
        ensure_ascii=False,
    )


_CELEX_RE = re.compile(r"^\s*(?:CELEX[:_]?)?(\d{1}\d{4}[A-Z]{1,2}\d{4})\b", re.IGNORECASE)


@beta_tool
def eurlex_fetch(celex: str, language: str = "eng", max_chars: int = 16000) -> str:
    """Return the verbatim body of one EU legal act by CELEX id.

    Calls the Cellar content-negotiation endpoint with ``Accept: text/html``
    and extracts plain text. Use ``language="eng"`` (default), ``"fra"``,
    ``"deu"`` etc. for the authentic language version you want.

    Args:
        celex: CELEX id (e.g. ``32004R0261`` for Reg (EC) 261/2004) — the
            ``celex`` field from an ``eurlex_search`` result.
        language: ISO-639-2/B three-letter language code (default ``eng``).
        max_chars: Maximum body characters returned (default 16000).
    """
    m = _CELEX_RE.match(celex or "")
    if not m:
        return json.dumps(
            {"error": f"celex must look like e.g. 32004R0261; got {celex!r}"},
            ensure_ascii=False,
        )
    celex_id = m.group(1).upper()
    url = f"https://publications.europa.eu/resource/celex/{celex_id}"
    result = fetch_body_text(
        url,
        max_chars=max_chars,
        accept="text/html",
        headers={"Accept-Language": language},
        timeout=httpx.Timeout(60.0, connect=10.0),
    )
    out = {
        "source": "EUR-Lex Cellar",
        "celex": celex_id,
        "language": language,
        "body_text": result.get("body_text") or "",
        "body_extractor": result.get("extractor"),
        "body_truncated": result.get("truncated"),
        "body_source_url": result.get("url"),
        "url": f"https://eur-lex.europa.eu/legal-content/EN/TXT/?uri=CELEX:{celex_id}",
    }
    if result.get("error"):
        out["error"] = result["error"]
    if result.get("note"):
        out["note"] = result["note"]
    return json.dumps(out, ensure_ascii=False)
