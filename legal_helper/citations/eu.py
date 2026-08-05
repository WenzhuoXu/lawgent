"""Regex patterns for EU legal citations (Celex, OJ, TFEU/TEU, Reg/Dir).

Confirmation requires a round-trip against EUR-Lex; this module only
extracts the structural form. See ``legal_helper/citations/groundedness.py``
for the Mode-2 round-trip layer.
"""

from __future__ import annotations

import re
from dataclasses import dataclass


# Celex number: 32016R0679 (sector + year + descriptor + number).
#   sector: 1-2 digits     year: 4 digits    descriptor: 1 letter   number: 4 digits
CELEX_RE = re.compile(r"\b\d{1,2}[12]\d{3}[A-Z]\d{4}(?:\(\d{2}\))?\b")

# Official Journal: "OJ L 119, 4.5.2016, p. 1" or "OJ C 202, 7.6.2016, p. 1"
OJ_RE = re.compile(
    r"OJ\s+[LC]\s*\d+(?:[,\s]+\d{1,2}\.\d{1,2}\.\d{4})?(?:[,\s]+p\.\s*\d+)?",
    re.IGNORECASE,
)

# Regulation/Directive form: "Regulation (EU) 2016/679 art. 6(1)",
# "Directive 2019/770", "Reg (EC) No 261/2004 art. 5".
EU_INSTRUMENT_RE = re.compile(
    r"(?:Regulation|Directive|Decision|Reg\.?|Dir\.?)\s+"
    r"\((?:EU|EC|EEC|Euratom)\)\s+(?:No\s+)?\d{1,4}/\d{2,4}"
    r"(?:\s+(?:art\.|article)\s*\d+(?:\(\d+\))?(?:\(\d+\))?)?",
    re.IGNORECASE,
)

# TFEU / TEU article: "art. 101 TFEU", "Article 267 TFEU", "art. 6 TEU"
TFEU_RE = re.compile(
    r"(?:art\.|article)\s*\d+(?:\(\d+\))?(?:\(\d+\))?\s+(?:TFEU|TEU)",
    re.IGNORECASE,
)


ALL_PATTERNS: dict[str, re.Pattern[str]] = {
    "eu_celex": CELEX_RE,
    "eu_oj": OJ_RE,
    "eu_instrument": EU_INSTRUMENT_RE,
    "eu_treaty_article": TFEU_RE,
}


@dataclass(frozen=True)
class EuCitation:
    kind: str
    text: str
    span: tuple[int, int]


def extract_eu(text: str) -> list[EuCitation]:
    """Find EU-style citations and return them with their span in the text."""
    found: list[EuCitation] = []
    for kind, pattern in ALL_PATTERNS.items():
        for m in pattern.finditer(text):
            found.append(EuCitation(kind=kind, text=m.group(0), span=m.span()))
    found.sort(key=lambda c: c.span[0])
    return found
