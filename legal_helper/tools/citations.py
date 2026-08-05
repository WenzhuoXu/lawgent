"""Expose citation extract / validate / Mode-2 verification as model-callable tools.

Tool surface (all ``@beta_tool``):

* ``extract_citations_tool`` — Mode-1 extract (US eyecite + PRC regex + EU regex).
* ``validate_citations_tool`` — structural validation with three-valued status.
* ``quote_roundtrip_tool`` — Mode-2 check: does the quote appear verbatim in
  the source text the skill just fetched?
* ``provenance_audit_tool`` — flag citations missing a ``[PKULaw]`` /
  ``[CourtListener]`` / ``[model knowledge — verify]`` provenance tag.
* ``cite_check_report_tool`` — render the severity-tiered
  ``verification-report.md``.
* ``verification_log_append_tool`` — record a confirmed cite so the next
  matter doesn't re-Shepardize it.
"""

from __future__ import annotations

import contextvars
import json
from typing import Any

from anthropic import beta_tool

from ..citations import (
    CoverageStats,
    ReportItem,
    VerificationReport,
    extract_assertions,
    extract_citations,
    extract_quoted_phrases,
    find_untagged_citations,
    log as cite_log,
    nearest_citation_after,
    roundtrip_quote,
    validate_citations,
)


# Side-channel for the structured cite-check payload. ``cite_check_report_tool``
# returns JSON to the model (truncated to 1200 chars in the streamed
# ``output_preview``), so the workflow cannot recover the full ``items[]`` /
# ``coverage`` from the event stream. The tool runs INLINE in the same thread
# and context as ``_run_cite_check_after_summary`` (the cite-check SkillAgent is
# not forked onto a worker thread), so a ContextVar reliably carries the last
# rendered report's structured payload back to the workflow without truncation.
_last_cite_check_payload: contextvars.ContextVar[dict[str, Any] | None] = (
    contextvars.ContextVar("_last_cite_check_payload", default=None)
)


def take_last_cite_check_payload() -> dict[str, Any] | None:
    """Return (and clear) the most recent ``cite_check_report_tool`` structured
    payload captured in the current context, or ``None`` if the tool did not run.
    """
    payload = _last_cite_check_payload.get()
    _last_cite_check_payload.set(None)
    return payload


@beta_tool
def extract_citations_tool(text: str) -> str:
    """Extract every legal citation found in ``text`` (US via eyecite + PRC + EU regex).

    Returns JSON: ``{"count": N, "citations": [{text, jurisdiction, kind, span}]}``.
    """
    cites = extract_citations(text)
    return json.dumps(
        {
            "count": len(cites),
            "citations": [
                {
                    "text": c.text,
                    "jurisdiction": c.jurisdiction,
                    "kind": c.kind,
                    "span": list(c.span),
                }
                for c in cites
            ],
        },
        ensure_ascii=False,
    )


@beta_tool
def validate_citations_tool(text: str) -> str:
    """Extract + structurally validate every citation in ``text``.

    Returns JSON with three-valued status per cite (``verified`` /
    ``flagged`` / ``could_not_check``). ``ok`` is ``True`` only when
    *every* cite is ``verified`` — a ``could_not_check`` cite never
    passes silently (per the Anthropic claude-for-legal discipline:
    *never* "confirmed" without consulting a primary source).
    """
    cites = extract_citations(text)
    results = validate_citations(cites)
    ok = all(r.status == "verified" for r in results)
    return json.dumps(
        {
            "ok": ok,
            "count": len(results),
            "results": [
                {
                    "citation": r.citation.text,
                    "jurisdiction": r.citation.jurisdiction,
                    "kind": r.citation.kind,
                    "ok": r.ok,
                    "status": r.status,
                    "reason": r.reason,
                }
                for r in results
            ],
        },
        ensure_ascii=False,
    )


@beta_tool
def quote_roundtrip_tool(quote: str, source_text: str | None = None) -> str:
    """Mode-2 check: confirm the quoted phrase appears verbatim in the source.

    Returns JSON with ``status`` in ``exact`` / ``near`` / ``not_found`` /
    ``could_not_check``. ``not_found`` is the **paraphrase-as-quote**
    failure mode — the dominant pattern in the 2,000+ documented
    hallucinated-citation court filings.

    Pass ``source_text=None`` only when the skill genuinely could not
    fetch the source; the tool will return ``could_not_check`` so the
    reviewer sees an explicit gap, not a silent pass.
    """
    result = roundtrip_quote(quote, source_text)
    return json.dumps(
        {
            "quote": result.quote,
            "status": result.status,
            "reason": result.reason,
        },
        ensure_ascii=False,
    )


@beta_tool
def provenance_audit_tool(text: str) -> str:
    """Flag citations missing a provenance tag.

    Per anthropics/claude-for-legal: every cite carries a provenance tag
    describing where it came from — ``[PKULaw]``, ``[CourtListener]``,
    ``[eCFR]``, ``[EUR-Lex]``, ``[user provided]`` (retrieval) or
    ``[model knowledge — verify]``, ``[web search — verify]`` (verify).
    Untagged cites default to ``[model knowledge — verify]`` and need
    a primary-source check.
    """
    cites = extract_citations(text)
    untagged = find_untagged_citations(text, cites)
    return json.dumps(
        {
            "total_citations": len(cites),
            "untagged_count": len(untagged),
            "untagged": [
                {
                    "text": c.text,
                    "jurisdiction": c.jurisdiction,
                    "kind": c.kind,
                    "span": list(c.span),
                }
                for c in untagged
            ],
            "recommendation": (
                "Tag each untagged citation with `[model knowledge — verify]` "
                "as default, or with the actual retrieval source if the cite "
                "appeared in a connector tool result this session."
            ),
        },
        ensure_ascii=False,
    )


@beta_tool
def cite_check_report_tool(
    text: str,
    document_name: str = "draft",
    stakes: str = "internal",
    sources_consulted: list[str] | None = None,
) -> str:
    """Render the severity-tiered ``verification-report.md`` for ``text``.

    Combines extract + validate + provenance audit + quote round-trip
    discovery (without a primary-source body — quote items are emitted
    as ``could_not_check`` unless the skill separately calls
    ``quote_roundtrip_tool`` with a fetched source).

    Args:
        text: The draft to audit.
        document_name: Filename to record in the report header.
        stakes: ``filed`` (court / regulatory submission — strictest pass,
            do-not-file threshold applies) or ``internal``.
        sources_consulted: Display list for the report header, e.g.
            ``["PKULaw", "CourtListener", "EUR-Lex"]``.

    Returns JSON: ``{"report_markdown": "…", "coverage": {...},
    "do_not_file": bool, "items": [...]}``.
    """
    stakes_lit = "filed" if str(stakes).lower() == "filed" else "internal"
    report = VerificationReport(
        document=document_name,
        stakes=stakes_lit,
        sources_consulted=list(sources_consulted or []),
    )

    cites = extract_citations(text)
    results = validate_citations(cites)
    cs = CoverageStats(total_cites=len(cites))

    by_span: dict[tuple[int, int], Any] = {(c.span[0], c.span[1]): c for c in cites}

    for r in results:
        cspan = (r.citation.span[0], r.citation.span[1])
        loc = f"char {cspan[0]}-{cspan[1]}"
        if r.status == "verified":
            cs.confirmed += 1
            continue
        if r.status == "could_not_check":
            cs.could_not_check += 1
            report.add(
                ReportItem(
                    location=loc,
                    issue_type="could_not_check",
                    severity="model_only",
                    claimed=r.citation.text,
                    source_says="(not retrieved)",
                    fix=(
                        r.reason
                        or "Round-trip against the primary-source connector."
                    ),
                    jurisdiction=r.citation.jurisdiction,
                )
            )
            continue
        cs.miscited += 1
        report.add(
            ReportItem(
                location=loc,
                issue_type="flagged_miscited",
                severity="critical",
                claimed=r.citation.text,
                source_says="(structural validation failed)",
                fix=r.reason or "Re-pull canonical form from a primary source.",
                jurisdiction=r.citation.jurisdiction,
            )
        )
        _ = by_span

    untagged = find_untagged_citations(text, cites)
    for c in untagged:
        loc = f"char {c.span[0]}-{c.span[1]}"
        report.add(
            ReportItem(
                location=loc,
                issue_type="untagged",
                severity="model_only",
                claimed=c.text,
                source_says="(no provenance tag within 160 chars)",
                fix=(
                    "Tag with `[model knowledge — verify]` (default) or with "
                    "the actual retrieval source."
                ),
                jurisdiction=c.jurisdiction,
            )
        )

    # Mode-2 stubs — quote phrases with no fetched source come in as could-not-check.
    for q in extract_quoted_phrases(text):
        owner = nearest_citation_after(q.span, cites)
        report.add(
            ReportItem(
                location=f"char {q.span[0]}-{q.span[1]}",
                issue_type="could_not_check",
                severity="model_only",
                claimed=q.text,
                source_says="(quote round-trip not yet performed)",
                fix=(
                    "Call quote_roundtrip_tool(quote, source_text) with the "
                    "primary source for "
                    + (owner.text if owner else "the cited authority")
                    + "."
                ),
                jurisdiction=(owner.jurisdiction if owner else ""),
            )
        )

    # Assertion stubs — propositions attributed to a court / 法院 without a quote.
    for a in extract_assertions(text):
        owner = nearest_citation_after(a.span, cites)
        if owner is None:
            continue
        report.add(
            ReportItem(
                location=f"char {a.span[0]}-{a.span[1]}",
                issue_type="could_not_check",
                severity="nuanced",
                claimed=a.text,
                source_says="(assertion-to-source check not yet performed)",
                fix=(
                    "Confirm the cited authority actually supports the whole "
                    "proposition (Stanford RegLab \"misgrounded citation\")."
                ),
                jurisdiction=owner.jurisdiction,
            )
        )

    report.coverage = cs
    report.evaluate_do_not_file()
    md = report.render_markdown()
    payload = {
        "report_markdown": md,
        "coverage": {
            "total_cites": cs.total_cites,
            "confirmed": cs.confirmed,
            "could_not_check": cs.could_not_check,
            "miscited": cs.miscited,
            "misgrounded": cs.misgrounded,
        },
        "do_not_file": report.do_not_file,
        "do_not_file_reason": report.do_not_file_reason,
        "stakes": report.stakes,
        "items": [
            {
                "location": i.location,
                "issue_type": i.issue_type,
                "severity": i.severity,
                "claimed": i.claimed,
                "source_says": i.source_says,
                "fix": i.fix,
                "jurisdiction": i.jurisdiction,
            }
            for i in report.items
        ],
    }
    # Stash the full structured payload for the workflow to recover without the
    # streamed output_preview truncation (see take_last_cite_check_payload).
    _last_cite_check_payload.set(payload)
    return json.dumps(payload, ensure_ascii=False)


@beta_tool
def verification_log_append_tool(
    cite: str,
    source: str,
    verdict: str,
    who: str = "cite-check",
) -> str:
    """Append a confirmed-cite entry to ``~/.legal_helper/verification-log.md``.

    Adapted from anthropics/claude-for-legal: a cite verified for one
    matter doesn't need re-verification for the next.

    Args:
        cite: The citation string (e.g. ``410 U.S. 113`` or ``(2023)京01民终12345号``).
        source: Where it was verified — ``CourtListener``, ``PKULaw``,
            ``EUR-Lex``, ``flk.npc.gov.cn``, etc.
        verdict: ``confirmed`` / ``corrected to X`` / ``could not verify``.
        who: The verifier (default ``cite-check``).

    Returns JSON: ``{"path": "<absolute path>"}``.
    """
    path = cite_log.append(cite=cite, source=source, verdict=verdict, who=who)
    return json.dumps({"path": str(path)}, ensure_ascii=False)


@beta_tool
def ground_answer_tool(
    text: str, max_sources: int = 12, annotate_sources: bool = False
) -> str:
    """Automated source-grounding pass over a drafted legal answer.

    Citation-grounded legal research (Harvey's Westlaw grounding,
    Claude-for-Legal's verifier) hinges on confirming the **quoted language**
    actually appears in the **cited source**. This tool does that end-to-end:
    it finds every citation in ``text`` — URLs *and* PRC statute pinpoints
    (《民法典》第一千零八十七条 resolves via the PKULaw ``pkulaw_fatiao``
    connector, the primary PRC path) — plus every quoted phrase, fetches each
    cited source once, and round-trips each quote against the fetched body —
    so paraphrase-passed-as-quote (Mode-2 hallucination) and dead/unfetchable
    citations surface automatically instead of relying on the model to check
    each one by hand.

    Returns JSON with per-quote verdicts (``exact`` / ``near`` / ``not_found``
    / ``could_not_check`` / ``no_citation``), per-source fetch status, summary
    counts, a 0-1 ``grounding_score``, per-ref ``trust`` tiers
    (connector-verified vs model-only ``[verify]``), and
    ``temporality_flags`` for cites to repealed/revised (已废止/已修改)
    authorities. Run it on the finished answer (or a quote-heavy section)
    before relying on the quotations.

    Args:
        text: The drafted answer (or section) with inline citations.
        max_sources: Maximum distinct sources to fetch (default 12).
        annotate_sources: When ``True``, also return ``annotated_text`` with
            each Sources / 资料来源 entry tagged ``[connector-verified: …]``
            or ``[verify]``.
    """
    from ..citations.grounding import ground_answer, prc_aware_fetch

    result = ground_answer(text, fetch=prc_aware_fetch, max_sources=max_sources)
    if annotate_sources:
        from ..citations.report import TrustTag, annotate_sources_with_trust

        tags = [
            TrustTag(t["ref"], t["tier"], t.get("connector") or "")
            for t in result.get("trust", [])
        ]
        result["annotated_text"] = annotate_sources_with_trust(text, tags)
    return json.dumps(result, ensure_ascii=False)
