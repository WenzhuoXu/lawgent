"""Mode-2 round-trip verification: does the source actually say what's claimed?

Mode 1 (fabricated cite) is the easy failure to detect. Mode 2 — the cite
exists, the passage exists, but the passage doesn't support the proposition
as stated — is the dominant failure in the >2,000 documented sanction
cases (Damien Charlotin database, May 2026). The Stanford RegLab 2024
study calls it "misgrounded citation."

This module supplies the offline primitives that the cite-check skill
uses once it has fetched source text via PKULaw / flk.npc.gov.cn /
CourtListener / EUR-Lex:

* ``extract_quoted_phrases``  — pull out everything between quote marks.
* ``roundtrip_quote``         — exact / near / not-found classification.
* ``extract_assertions_with_cite`` — "the court held that X (cite)" pairs.

Framework adapted from anthropics/claude-for-legal litigation-legal/CLAUDE.md
and sboghossian/master-claude-for-legal citation-verifier.md (MIT).
"""

from __future__ import annotations

import re
import unicodedata
from dataclasses import dataclass
from typing import Literal


# Capture ASCII " ", curly " " " "  " " " ", PRC 「 」 『 』, fancy CJK quotes.
_QUOTE_PAIRS = [
    ("\"", "\""),
    ("“", "”"),   # English curly quotes
    ("‘", "’"),   # English curly single quotes
    ("«", "»"),   # « »
    ("「", "」"),   # 「 」
    ("『", "』"),   # 『 』
    ("„", "“"),   # „ "
]

_MIN_QUOTE_LEN = 4
_MAX_QUOTE_LEN = 400


@dataclass(frozen=True)
class QuotedPhrase:
    text: str
    span: tuple[int, int]


def extract_quoted_phrases(text: str) -> list[QuotedPhrase]:
    """Pull every quoted phrase out of ``text``.

    Handles ASCII, English curly, French, and CJK quote pairs. The skill
    layer is responsible for tying each phrase to a nearby citation; this
    function only returns what's in quotation marks.
    """
    out: list[QuotedPhrase] = []
    for open_q, close_q in _QUOTE_PAIRS:
        if open_q == close_q:
            # symmetric quote: pair greedily with the next same-char
            pattern = re.compile(
                re.escape(open_q) + r"([^" + re.escape(open_q) + r"\n]{"
                + str(_MIN_QUOTE_LEN) + r"," + str(_MAX_QUOTE_LEN) + r"})"
                + re.escape(open_q)
            )
        else:
            pattern = re.compile(
                re.escape(open_q) + r"([^" + re.escape(close_q) + r"]{"
                + str(_MIN_QUOTE_LEN) + r"," + str(_MAX_QUOTE_LEN) + r"})"
                + re.escape(close_q)
            )
        for m in pattern.finditer(text):
            out.append(QuotedPhrase(text=m.group(1), span=m.span()))
    out.sort(key=lambda q: q.span[0])
    # Drop strictly-contained overlaps.
    kept: list[QuotedPhrase] = []
    for q in out:
        if not any(
            k.span[0] <= q.span[0] and k.span[1] >= q.span[1] and k is not q
            for k in kept
        ):
            kept.append(q)
    return kept


RoundtripStatus = Literal["exact", "near", "not_found", "could_not_check"]


@dataclass(frozen=True)
class RoundtripResult:
    quote: str
    status: RoundtripStatus
    reason: str | None = None


def _normalize(s: str) -> str:
    s = unicodedata.normalize("NFKC", s)
    # Collapse all whitespace (incl. CJK full-width space) to single space.
    s = re.sub(r"\s+", " ", s).strip()
    # Normalize ASCII smart quotes / dashes so a quote with curly quotes
    # matches a source rendered with ASCII quotes and vice versa.
    table = str.maketrans({
        "“": "\"", "”": "\"",
        "‘": "'",  "’": "'",
        "–": "-",  "—": "-",
    })
    return s.translate(table)


def _strip_punct(s: str) -> str:
    # Keep word chars, CJK chars, and whitespace; drop everything else.
    return re.sub(r"[^\w一-鿿\s]", "", s).strip()


# Elision markers inside quotes: "……" / "..." / "⋯⋯". NFKC (applied in
# ``_normalize``) folds U+2026 to "...", so post-normalization the dot form
# is the common case; ⋯ (U+22EF) survives NFKC and is matched directly.
_ELLIPSIS_SPLIT = re.compile(r"\.{3,}|…+|⋯+")


def _segments_in_order(segments: list[str], source: str) -> bool:
    pos = 0
    for seg in segments:
        idx = source.find(seg, pos)
        if idx < 0:
            return False
        pos = idx + len(seg)
    return True


def roundtrip_quote(quote: str, source_text: str | None) -> RoundtripResult:
    """Classify a quoted phrase against the cited source's full text.

    * ``exact``           — quote (after Unicode NFKC + whitespace collapse)
                            appears verbatim in ``source_text``.
    * ``near``            — quote matches the source modulo punctuation
                            (a flag, per Anthropic's "verbatim quotes from
                            the record must be verbatim" rule).
    * ``not_found``       — likely **paraphrase-as-quote**. Mode-2 hallucination.
    * ``could_not_check`` — no source_text supplied; the skill could not
                            retrieve the source.
    """
    if not quote.strip():
        return RoundtripResult(quote, "not_found", "empty quote")
    if source_text is None:
        return RoundtripResult(
            quote, "could_not_check", "no source text supplied"
        )
    q_norm = _normalize(quote)
    s_norm = _normalize(source_text)
    if q_norm in s_norm:
        return RoundtripResult(quote, "exact")
    q_nopunct = _strip_punct(q_norm)
    s_nopunct = _strip_punct(s_norm)
    # Elided quote ("A……B"): the quote as a whole never appears verbatim, so
    # verify each segment appears in the source *in order* instead of falsely
    # reporting paraphrase-as-quote.
    if _ELLIPSIS_SPLIT.search(q_norm):
        segs = [s for s in (seg.strip() for seg in _ELLIPSIS_SPLIT.split(q_norm)) if s]
        if segs and _segments_in_order(segs, s_norm):
            return RoundtripResult(
                quote, "exact", "elided quote (……) — all segments found in source, in order"
            )
        segs_np = [s for s in (_strip_punct(seg) for seg in segs) if s]
        if segs_np and _segments_in_order(segs_np, s_nopunct):
            return RoundtripResult(
                quote,
                "near",
                "elided quote matches modulo punctuation/whitespace — verify or drop quote marks",
            )
    if q_nopunct and q_nopunct in s_nopunct:
        return RoundtripResult(
            quote,
            "near",
            "matches modulo punctuation/whitespace — verify or drop quote marks",
        )
    return RoundtripResult(
        quote,
        "not_found",
        "quote does not appear in source — likely paraphrase passed as quote",
    )


_ASSERTION_VERBS_EN = (
    "held",
    "ruled",
    "stated",
    "found",
    "concluded",
    "rejected",
    "noted",
    "observed",
    "explained",
    "provides",
    "requires",
    "mandates",
    "prohibits",
)
_ASSERTION_VERBS_CN = (
    "认定", "判决", "裁定", "指出", "明确", "规定", "要求", "禁止", "确认",
)


@dataclass(frozen=True)
class Assertion:
    text: str
    verb: str
    span: tuple[int, int]


def extract_assertions(text: str) -> list[Assertion]:
    """Find sentence-shaped statements that attribute a holding to a source.

    Example matches: "The court held that X.", "法院认定 X。", "Article 6
    provides that Y." These are the propositions that need a Mode-2 check
    (per the Stanford RegLab "misgrounded citation" failure mode).
    """
    out: list[Assertion] = []
    en_pattern = re.compile(
        r"\b(?:[Tt]he\s+(?:court|tribunal|panel|board|agency)|[A-Z][\w\s]{0,40})\s+"
        r"\b(" + "|".join(_ASSERTION_VERBS_EN) + r")\b[^.]{5,300}\.",
    )
    for m in en_pattern.finditer(text):
        out.append(Assertion(text=m.group(0), verb=m.group(1), span=m.span()))
    cn_pattern = re.compile(
        r"[一-鿿、，；：]{0,40}(" + "|".join(_ASSERTION_VERBS_CN)
        + r")[^。；]{5,300}[。；]"
    )
    for m in cn_pattern.finditer(text):
        out.append(Assertion(text=m.group(0), verb=m.group(1), span=m.span()))
    out.sort(key=lambda a: a.span[0])
    return out


def nearest_citation_after(span: tuple[int, int], citations, window: int = 200):
    """Return the citation whose span starts closest after ``span[1]`` within
    ``window`` chars; ``None`` if there is no such cite. Used to tie a
    quoted phrase or assertion to the cite that supposedly supports it.
    """
    end = span[1]
    best = None
    best_d = None
    for c in citations:
        d = c.span[0] - end
        if 0 <= d <= window and (best_d is None or d < best_d):
            best = c
            best_d = d
    return best
