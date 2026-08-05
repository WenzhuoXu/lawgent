"""Unified citation extraction: eyecite (US/EN) + PRC regex (CN)."""

from __future__ import annotations

from dataclasses import dataclass
from typing import Literal

from .eu import extract_eu
from .prc import extract_prc


Jurisdiction = Literal["US", "CN", "EU", "UK", "UNKNOWN"]


@dataclass(frozen=True)
class Citation:
    text: str
    jurisdiction: Jurisdiction
    kind: str
    span: tuple[int, int]
    raw: object | None = None  # original eyecite/PrcCitation object


def _eyecite_extract(text: str) -> list[Citation]:
    try:
        from eyecite import get_citations
    except ImportError:
        return []

    out: list[Citation] = []
    for cite in get_citations(text):
        # eyecite returns CitationBase subclasses with `.token.start/.end` or `.span()`.
        try:
            span = cite.span()  # newer eyecite
        except Exception:
            span = (getattr(cite.token, "start", -1), getattr(cite.token, "end", -1))
        kind = type(cite).__name__
        out.append(
            Citation(
                text=cite.matched_text() if hasattr(cite, "matched_text") else str(cite),
                jurisdiction="US",
                kind=kind,
                span=tuple(span),
                raw=cite,
            )
        )
    return out


def extract_citations(text: str) -> list[Citation]:
    """Return all citations found in `text`, sorted by span."""
    out: list[Citation] = []
    out.extend(_eyecite_extract(text))
    for p in extract_prc(text):
        out.append(
            Citation(
                text=p.text,
                jurisdiction="CN",
                kind=f"prc_{p.kind}",
                span=p.span,
                raw=p,
            )
        )
    for e in extract_eu(text):
        out.append(
            Citation(
                text=e.text,
                jurisdiction="EU",
                kind=e.kind,
                span=e.span,
                raw=e,
            )
        )
    out.sort(key=lambda c: c.span[0])
    return _dedupe_overlapping(out)


def _dedupe_overlapping(cites: list[Citation]) -> list[Citation]:
    """Drop strictly-contained overlaps (regex picks up sub-patterns of eyecite hits)."""
    kept: list[Citation] = []
    for c in cites:
        contained = any(
            (k.span[0] <= c.span[0] and k.span[1] >= c.span[1] and k is not c)
            for k in kept
        )
        if not contained:
            kept.append(c)
    return kept
