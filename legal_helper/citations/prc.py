"""Regex patterns for PRC legal citations (案号, 法释, 国函, 部令, 等)."""

from __future__ import annotations

import re
from dataclasses import dataclass
from typing import Literal


# GB/T 9704 canonical typography uses full-width （） for 案号 years and
# 〔〕 for issuance numbers. NFKC folds （）［］ to ASCII but does NOT map
# 〔〕 (verified), so every bracket class below must list the full-width
# forms explicitly rather than relying on normalization.
_OPEN_BRACKET = r"[\[\(〔（［]"
_CLOSE_BRACKET = r"[\]\)〕）］]"

# 案号: （2023）京01民终12345号 — judgment case number
# Allows 2-4 digit year, court code (1-6 chars incl. digits/letters), case-type marker, serial number.
CASE_NUMBER_RE = re.compile(
    r"[\(（]\s*[\d]{2,4}\s*[\)）]\s*"  # year in ASCII or full-width parens
    r"[一-鿿\d]{1,6}"     # court code (Chinese + digits)
    r"[一-鿿]{1,4}"       # case-type marker (民/刑/行/执/赔, plus 初/终/再/抗/监)
    r"\s*\d{1,8}\s*号"            # serial + 号
)

# 法释: 法释〔2024〕5号 — Supreme People's Court judicial interpretation
JUDICIAL_INTERPRETATION_RE = re.compile(
    r"法释" + _OPEN_BRACKET + r"?\s*\d{4}\s*" + _CLOSE_BRACKET + r"?\s*第?\s*\d+\s*号?"
)

# 国函 / 国办发 / 国发: State Council issuances
STATE_COUNCIL_RE = re.compile(
    r"(?:国函|国办发|国发|国办函)"
    + _OPEN_BRACKET + r"?\s*\d{4}\s*" + _CLOSE_BRACKET + r"?\s*\d+\s*号"
)

# 部令 / 部发 / 部规 — ministry orders/circulars (e.g. CAAC, MOT).
# Lookbehinds keep State Council prefixes (国发/国办发) out of the greedy
# issuer window so they classify as state_council, not ministry_order.
MINISTRY_ORDER_RE = re.compile(
    r"(?:[一-鿿]{2,8}(?:令|(?<!国)(?<!国办)发|规|公告|通告|通知))"
    + _OPEN_BRACKET + r"?\s*\d{4}\s*" + _CLOSE_BRACKET + r"?\s*第?\s*\d+\s*号"
)

# Statute pinpoint: 《刑法》第233条 / 《民法典》第一千零八十七条 / 《合同法》第52条第3款
# Numeral class includes 千/万 (民法典 runs to 第一千二百六十条); title class
# admits any non-《》 char (real titles carry （2021修正） suffixes etc.).
STATUTE_PINPOINT_RE = re.compile(
    r"《[^《》\n]+》"
    r"(?:\s*第[\d一二三四五六七八九十百千万零]+(?:条|章|节|款|项|目)"
    r"(?:之[一二三四五六七八九十]+)?)+"
)

ALL_PATTERNS: dict[str, re.Pattern[str]] = {
    "case_number": CASE_NUMBER_RE,
    "judicial_interpretation": JUDICIAL_INTERPRETATION_RE,
    "state_council": STATE_COUNCIL_RE,
    "ministry_order": MINISTRY_ORDER_RE,
    "statute_pinpoint": STATUTE_PINPOINT_RE,
}


@dataclass(frozen=True)
class PrcCitation:
    kind: str
    text: str
    span: tuple[int, int]


def extract_prc(text: str) -> list[PrcCitation]:
    """Find PRC-style citations and return them with their span in the text."""
    found: list[PrcCitation] = []
    for kind, pattern in ALL_PATTERNS.items():
        for m in pattern.finditer(text):
            found.append(PrcCitation(kind=kind, text=m.group(0), span=m.span()))
    found.sort(key=lambda c: c.span[0])
    return found


_YEAR_RE = re.compile(r"\d{4}")

# Official case-type 代字 per 《人民法院案件类型及其代字标准》(法〔2015〕287号):
# 刑/民/行/赔/执 plus 破(破产) 财(财产保全) 强清(强制清算) 认(认可与执行)
# 请/协(司法协助) 督(督促程序) 罚(司法处罚) 催(公示催告) 救(司法救助)
# 委(委托执行) 保(保全) 商(商事) — and the specialist-court markers 知/海.
CASE_TYPE_MARKERS = "民刑行执赔知海破财商请认协督罚强清催救委保"

PrcStatus = Literal["verified", "flagged", "could_not_check"]


def validate_prc(cite: PrcCitation) -> tuple[PrcStatus, str | None]:
    """Structural validation of a PRC cite (sanity, not authority).

    Structural sanity only — authority confirmation (是否现行有效 / does the
    pinpoint actually support the proposition) requires a round-trip via
    ``pkulaw_fatiao.get_law_item_content``. See
    ``legal_helper/citations/groundedness.py``.

    Returns ``(status, reason)`` where status is three-valued: an unknown
    案号 case-type marker downgrades to ``could_not_check`` — it is outside
    the official taxonomy we can vouch for, but must never silently pass.
    """
    if cite.kind == "case_number":
        if "年" in cite.text or "月" in cite.text:
            return "flagged", "case number contains date markers; expected court-code form"
        if not any(c in cite.text for c in CASE_TYPE_MARKERS):
            return (
                "could_not_check",
                "case-type marker not in the official 案号 taxonomy "
                "(法〔2015〕287号) — verify against PKULaw before relying on it",
            )
    if cite.kind == "statute_pinpoint" and "第" not in cite.text:
        return "flagged", "statute reference missing pinpoint marker (第…条/款/项)"
    if cite.kind == "judicial_interpretation":
        m = _YEAR_RE.search(cite.text)
        if m:
            y = int(m.group(0))
            if y < 1979 or y > 2030:
                return "flagged", f"法释 year {y} implausible (expected 1979–2030)"
    if cite.kind == "state_council":
        m = _YEAR_RE.search(cite.text)
        if m:
            y = int(m.group(0))
            if y < 1949 or y > 2030:
                return "flagged", f"国函/国办发 year {y} implausible (expected 1949–2030)"
    if cite.kind == "ministry_order":
        m = _YEAR_RE.search(cite.text)
        if m:
            y = int(m.group(0))
            if y < 1949 or y > 2030:
                return "flagged", f"部令/部发 year {y} implausible (expected 1949–2030)"
    # Structure is sound, but structural validation is NOT authority: offline we
    # cannot tell a real 第一千零八十七条 from a fabricated 第五千条, nor confirm a
    # well-formed 案号 names a real case. Returning "verified" here would be the
    # exact false-positive validate.py warns is worse than "could_not_check".
    # Authority confirmation is grounding.py's job (PKULaw round-trip); until
    # that runs, the honest status is could_not_check.
    return (
        "could_not_check",
        "structure valid; authority not confirmed offline — round-trip via "
        "PKULaw (ground_answer) to verify the provision exists",
    )
