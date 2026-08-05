"""Provenance-tag taxonomy for legal citations.

Adapted (with attribution) from anthropics/claude-for-legal
``litigation-legal/CLAUDE.md``. The reviewer can tell at a glance which
cites to spot-check first:

* Retrieval tags (e.g. ``[PKULaw]``, ``[CourtListener]``, ``[eCFR]``,
  ``[EUR-Lex]``) describe **provenance, not confidence**. Use only when
  the citation literally appeared in that source's tool result in this
  session.
* Verify tags (``[model knowledge — verify]``, ``[web search — verify]``,
  ``[retrieved but verify support]``) mark a citation the reader must
  confirm against a primary source before relying on it.
* ``[settled — last confirmed YYYY-MM-DD]`` marks a stable statutory or
  regulatory reference that has been checked against a primary source
  on the stated date. Without the date, fall back to ``[model knowledge
  — verify]`` — an unconfirmed "settled" is the confident-overclaim the
  whole tag system exists to prevent.
* ``[VERIFY: …]`` / ``[UNCERTAIN: …]`` / ``[CITE NEEDED: …]`` are the
  drafting-skill counterparts of ``[verify]`` with the specific claim
  spelled out.
"""

from __future__ import annotations

import re
from dataclasses import dataclass


RETRIEVAL_TAGS: tuple[str, ...] = (
    # PRC
    "PKULaw",
    "flk.npc.gov.cn",
    # US
    "CourtListener",
    "Westlaw",
    "Lexis",
    "PACER",
    "eCFR",
    "FederalRegister",
    "GovInfo",
    "USPTO",
    # EU
    "EUR-Lex",
    # third-party legal research
    "Trellis",
    "Descrybe",
    "Harvey",
    "CoCounsel",
    "Solve Intelligence",
    # generic primary-source label
    "statute / regulator site",
    "statute/regulator site",
    "user provided",
)

VERIFY_TAGS: tuple[str, ...] = (
    "model knowledge — verify",
    "web search — verify",
    "retrieved but verify support",
    "verify",
    "verify against record",
    "verify against primary source",
    "verify exact quote — record cite pending",
    "premise flagged — verify",
    "statute unretrieved — verify",
    "US framework — verify against [jurisdiction] law",
)

# "settled — last confirmed YYYY-MM-DD"
SETTLED_RE = re.compile(
    r"^settled\s+—\s+last confirmed\s+\d{4}-\d{2}-\d{2}$",
    re.IGNORECASE,
)
SETTLED_RE_DASH = re.compile(
    r"^settled\s+-\s+last confirmed\s+\d{4}-\d{2}-\d{2}$",
    re.IGNORECASE,
)

TAG_RE = re.compile(r"\[(?P<inner>[^\[\]\n]{1,120})\]")

# "[connector-verified: PKULaw]" — trust tier rendered by report.TrustTag.
CONNECTOR_VERIFIED_RE = re.compile(r"^connector[- ]verified:\s*\S", re.IGNORECASE)

_RETRIEVAL_LOWER = {t.lower() for t in RETRIEVAL_TAGS}
_VERIFY_LOWER = {t.lower() for t in VERIFY_TAGS}


@dataclass(frozen=True)
class TagInspection:
    raw: str
    kind: str  # "retrieval" | "verify" | "settled" | "marker" | "unknown"


def inspect_tag(inner: str) -> TagInspection:
    s = inner.strip()
    low = s.lower()
    if low in _RETRIEVAL_LOWER:
        return TagInspection(inner, "retrieval")
    if CONNECTOR_VERIFIED_RE.match(s):
        return TagInspection(inner, "retrieval")
    if low in _VERIFY_LOWER:
        return TagInspection(inner, "verify")
    if SETTLED_RE.match(s) or SETTLED_RE_DASH.match(s):
        return TagInspection(inner, "settled")
    for prefix in ("VERIFY:", "UNCERTAIN:", "CITE NEEDED:"):
        if s.startswith(prefix):
            return TagInspection(inner, "marker")
    return TagInspection(inner, "unknown")


def is_provenance_tag(inner: str) -> bool:
    return inspect_tag(inner).kind != "unknown"


def find_untagged_citations(text: str, citations, window: int = 160):
    """Return citations that have no provenance tag within ``window`` chars
    of their end position. The skill should escalate every untagged cite
    — per Anthropic's discipline, the default is ``[model knowledge —
    verify]``, not "no tag."
    """
    untagged = []
    for c in citations:
        end = c.span[1]
        snippet = text[end : end + window]
        tags = TAG_RE.findall(snippet)
        if not any(is_provenance_tag(t) for t in tags):
            untagged.append(c)
    return untagged
