"""Automated source-grounding verification for a finished legal answer.

The repo already ships the Mode-2 *primitives* (``groundedness.py``: quote
extraction + round-trip classification). What was missing — and what
distinguishes citation-grounded legal harnesses (Harvey's Westlaw grounding,
Claude-for-Legal's verifier) — is an **automated** pass that, given a drafted
answer, fetches each cited source online and checks that the quoted language
actually appears in it.

``ground_answer`` ties the pieces together:

1. find every URL in the answer (markdown ``[text](url)`` and bare),
2. find every quoted phrase,
3. attach each quote to the nearest following citation URL,
4. fetch each unique source once (``_body_fetch.fetch_body_text`` by default),
5. round-trip each quote against the fetched body, and
6. report a per-quote verdict + an overall grounding score.

It is deliberately conservative: an un-fetchable source or an un-cited quote
is flagged, never silently passed. ``fetch`` is injectable so the verifier is
unit-testable offline and so callers can route through PKULaw / connectors.
"""

from __future__ import annotations

import json
import re
from dataclasses import asdict, dataclass
from typing import Any, Callable, Optional

from .extract import extract_citations
from .groundedness import extract_quoted_phrases, roundtrip_quote
from .report import assign_trust_tiers

# markdown [text](url) — capture the url; and bare http(s) urls.
_MD_LINK = re.compile(r"\[[^\]]*\]\((https?://[^)\s]+)\)")
_BARE_URL = re.compile(r"(?<!\()\bhttps?://[^\s)\]]+")

FetchFn = Callable[[str], dict[str, Any]]

# PRC citation kinds that can anchor a quote without a URL — a PKULaw-aware
# fetch resolves them (URLs are not the only citation form in a PRC-first
# product).
_PRC_ANCHOR_KINDS = {
    "prc_statute_pinpoint",
    "prc_case_number",
    "prc_judicial_interpretation",
    "prc_state_council",
    "prc_ministry_order",
}


@dataclass(frozen=True)
class _Cite:
    url: str  # http(s) URL, or a structured citation ref (e.g. 《民法典》第…条)
    span: tuple[int, int]


@dataclass(frozen=True)
class QuoteVerdict:
    quote: str
    url: Optional[str]  # URL or structured citation ref the quote was checked against
    status: str  # exact | near | not_found | could_not_check | no_citation
    reason: Optional[str] = None


def _find_citations(text: str) -> list[_Cite]:
    cites: list[_Cite] = []
    for m in _MD_LINK.finditer(text):
        cites.append(_Cite(url=m.group(1), span=m.span()))
    for m in _BARE_URL.finditer(text):
        url = m.group(0).rstrip(".,;)")
        if not any(c.url == url and c.span[0] <= m.start() <= c.span[1] for c in cites):
            cites.append(_Cite(url=url, span=m.span()))
    for c in extract_citations(text):
        if c.kind in _PRC_ANCHOR_KINDS:
            cites.append(_Cite(url=c.text, span=c.span))
    cites.sort(key=lambda c: c.span[0])
    return cites


def _nearest_url_after(span: tuple[int, int], cites: list[_Cite], window: int = 400) -> Optional[str]:
    end = span[1]
    best: Optional[_Cite] = None
    best_d: Optional[int] = None
    for c in cites:
        d = c.span[0] - end
        if -40 <= d <= window and (best_d is None or abs(d) < best_d):
            best, best_d = c, abs(d)
    return best.url if best else None


def _default_fetch(url: str) -> dict[str, Any]:
    from ..connectors._body_fetch import fetch_body_text

    return fetch_body_text(url, max_chars=60000)


# ----- PKULaw-aware fetch ----------------------------------------------------

_CN_STATUTE_REF_RE = re.compile(
    r"《\s*([^《》\n]+?)\s*》\s*"
    r"(第[\d一二三四五六七八九十百千万零]+条(?:之[一二三四五六七八九十]+)?)"
)

_REPEALED_MARKERS = ("已废止", "已失效", "失效")
_REVISED_MARKERS = ("已修改", "已修订", "已被修订", "已被修正")


def _parse_statute_ref(ref: str) -> tuple[str, str] | None:
    """Split 《法律名》第N条… into (law_name, 条号); None if not statute-shaped."""
    m = _CN_STATUTE_REF_RE.search(ref)
    return (m.group(1), m.group(2)) if m else None


def _in_force_status(status_text: str | None) -> str | None:
    """Map PKULaw 效力 / flk 状态 wording onto a temporality verdict."""
    if not status_text:
        return None
    if any(m in status_text for m in _REPEALED_MARKERS):
        return "repealed"
    if any(m in status_text for m in _REVISED_MARKERS):
        return "revised"
    if "尚未生效" in status_text:
        return "not_yet_effective"
    if "有效" in status_text:  # 有效 / 现行有效
        return "in_force"
    return None


def _pkulaw_fetch_statute(law_name: str, article: str) -> dict[str, Any]:
    """Resolve (law title, 条号) via the ``pkulaw_fatiao`` MCP (primary PRC path)."""
    try:
        from ..mcp.registry import load_registry
        from ..mcp.sync_client import http_call_tool

        spec = next((s for s in load_registry() if s.name == "pkulaw_fatiao"), None)
    except Exception as exc:  # noqa: BLE001 — fetch failures are reported, not raised
        return {"body_text": "", "error": f"MCP registry unavailable: {exc}",
                "connector": "pkulaw_fatiao"}
    if spec is None:
        return {"body_text": "", "error": "pkulaw_fatiao MCP not configured in .mcp.json",
                "connector": "pkulaw_fatiao"}
    raw = http_call_tool(
        spec, "get_law_item_content", {"law_name": law_name, "article_number": article}
    )
    body, error, status = raw or "", None, None
    try:
        obj = json.loads(raw)
    except (json.JSONDecodeError, TypeError):
        obj = None  # plain-text 条文 body
    if isinstance(obj, dict):
        if obj.get("error"):
            body, error = "", str(obj["error"])
        else:
            body = str(
                obj.get("content") or obj.get("text") or obj.get("body_text")
                or json.dumps(obj, ensure_ascii=False)
            )
            status = str(obj.get("timeliness") or obj.get("效力") or obj.get("status") or "") or None
    # PKULaw prepends 效力 metadata to the article text; scan the head as a
    # fallback when no structured field came back.
    return {
        "body_text": body,
        "error": error,
        "extractor": "pkulaw_fatiao",
        "connector": "pkulaw_fatiao",
        "in_force_status": _in_force_status(status or body[:400]),
    }


def _flk_status_fallback(law_name: str) -> dict[str, Any] | None:
    """flk.npc.gov.cn fallback — status metadata only, never body text."""
    try:
        from ..connectors.prc import flk_npc_search

        data = json.loads(flk_npc_search.call({"query": law_name, "per_page": 3}))
    except Exception:  # noqa: BLE001
        return None
    for row in data.get("results") or []:
        title = row.get("title") or ""
        if law_name in title or title in law_name:
            return row
    return None


def prc_aware_fetch(ref: str) -> dict[str, Any]:
    """Resolve a citation ref to source body text.

    http(s) URLs go through the web body-fetch; 《法律名》第N条 refs resolve
    via ``pkulaw_fatiao`` (the primary PRC path per CLAUDE.md) with
    ``flk_npc_search`` as the status-only fallback. Other structured refs
    (案号, 法释…) return an explicit error so they surface as
    ``could_not_check`` — never a silent pass.
    """
    if ref.startswith(("http://", "https://")):
        out = _default_fetch(ref)
        out.setdefault("connector", "web")
        return out
    parsed = _parse_statute_ref(ref)
    if parsed is None:
        return {
            "body_text": "",
            "error": (
                f"no connector round-trip for {ref!r} — resolve 案号 via "
                "pkulaw_case_search / pkulaw_anhao, 法释 via pkulaw_law_search"
            ),
            "connector": None,
        }
    law_name, article = parsed
    result = _pkulaw_fetch_statute(law_name, article)
    if not result.get("body_text"):
        row = _flk_status_fallback(law_name)
        if row:
            result["in_force_status"] = (
                _in_force_status(str(row.get("status") or "")) or result.get("in_force_status")
            )
            result["error"] = (
                (result.get("error") or "pkulaw_fatiao returned no body")
                + f"; flk.npc.gov.cn status for 《{row.get('title') or law_name}》: {row.get('status')}"
            )
    return result


def ground_answer(text: str, *, fetch: Optional[FetchFn] = None, max_sources: int = 12) -> dict[str, Any]:
    """Verify that an answer's quoted language is grounded in its cited sources.

    Returns a dict with per-quote verdicts, per-source fetch status, summary
    counts, a 0-1 ``grounding_score`` (exact+near over checkable quotes),
    per-ref ``trust`` tiers (connector-verified vs model-only "[verify]"),
    and ``temporality_flags`` for cites to repealed/revised authorities.
    """
    fetch = fetch or prc_aware_fetch
    cites = _find_citations(text)
    quotes = extract_quoted_phrases(text)

    # Which URLs are actually referenced by a quote → fetch only those.
    quote_urls: list[tuple[Any, Optional[str]]] = [
        (q, _nearest_url_after(q.span, cites)) for q in quotes
    ]
    wanted = []
    for _q, url in quote_urls:
        if url and url not in wanted:
            wanted.append(url)
    wanted = wanted[:max_sources]

    bodies: dict[str, dict[str, Any]] = {}
    for url in wanted:
        try:
            result = fetch(url)
        except Exception as exc:  # noqa: BLE001 — fetch failures are reported, not raised
            result = {"error": str(exc), "body_text": ""}
        bodies[url] = result

    verdicts: list[QuoteVerdict] = []
    for q, url in quote_urls:
        if not url:
            verdicts.append(QuoteVerdict(q.text, None, "no_citation",
                                         "quoted phrase has no nearby source link"))
            continue
        body = bodies.get(url, {})
        source_text = body.get("body_text") or None
        if not source_text:
            verdicts.append(QuoteVerdict(q.text, url, "could_not_check",
                                         body.get("error") or "source body unavailable"))
            continue
        rt = roundtrip_quote(q.text, source_text)
        verdicts.append(QuoteVerdict(q.text, url, rt.status, rt.reason))

    checkable = [v for v in verdicts if v.status in {"exact", "near", "not_found"}]
    grounded = [v for v in checkable if v.status in {"exact", "near"}]
    score = (len(grounded) / len(checkable)) if checkable else None

    # Trust tiers: only a ref whose body was actually retrieved this run is
    # connector-verified; everything else stays model-only "[verify]".
    verified_by: dict[str, str] = {
        ref: str(b.get("connector") or b.get("extractor") or "web")
        for ref, b in bodies.items()
        if b.get("body_text")
    }
    refs: list[str] = []
    for c in cites:
        if c.url not in refs:
            refs.append(c.url)
    trust_tags = assign_trust_tiers(refs, verified_by)

    temporality = [
        {
            "ref": ref,
            "status": b["in_force_status"],
            "note": (
                "cited authority is not current (修正/修订 renumbering or 废止 risk) "
                "— re-pin against the in-force text"
            ),
        }
        for ref, b in bodies.items()
        if b.get("in_force_status") in {"repealed", "revised", "not_yet_effective"}
    ]

    return {
        "quote_count": len(quotes),
        "citation_count": len(cites),
        "sources_fetched": len(wanted),
        "verdicts": [asdict(v) for v in verdicts],
        "summary": {
            "exact": sum(1 for v in verdicts if v.status == "exact"),
            "near": sum(1 for v in verdicts if v.status == "near"),
            "not_found": sum(1 for v in verdicts if v.status == "not_found"),
            "could_not_check": sum(1 for v in verdicts if v.status == "could_not_check"),
            "no_citation": sum(1 for v in verdicts if v.status == "no_citation"),
        },
        "grounding_score": score,
        "trust": [
            {"ref": t.ref, "tier": t.tier, "connector": t.connector, "tag": t.render()}
            for t in trust_tags
        ],
        "temporality_flags": temporality,
        "sources": {
            url: {
                "fetched": bool(b.get("body_text")),
                "extractor": b.get("extractor"),
                "connector": b.get("connector"),
                "in_force_status": b.get("in_force_status"),
                "error": b.get("error"),
                "url": b.get("url", url),
            }
            for url, b in bodies.items()
        },
        "advice": (
            "not_found quotes are likely paraphrase-as-quote (Mode-2 hallucination) — "
            "fix the quote marks or correct the citation. could_not_check sources should "
            "be re-fetched via a connector or web_fetch before relying on the quote."
        ),
    }


__all__ = ["ground_answer", "prc_aware_fetch", "QuoteVerdict"]
