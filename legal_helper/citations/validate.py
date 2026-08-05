"""Validate citations against reporters-db (US) and structural rules (PRC).

A validation result carries a three-valued ``status``:

* ``verified``      — checked against a primary-source list and OK.
* ``flagged``       — checked and failed (unknown reporter, malformed 案号 / 法释).
* ``could_not_check`` — the check did not actually happen (reporters-db
                       missing, no underlying eyecite object, source text
                       not provided). Anthropic's claude-for-legal rule:
                       *never* return "confirmed" if no primary source was
                       consulted. A false positive is worse than a "couldn't
                       check" because it lets a bad cite through silently.

``ok`` is preserved as a bool for back-compat — but is only True when
``status == "verified"``.
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import Literal

from .extract import Citation
from .prc import PrcCitation, validate_prc


Status = Literal["verified", "flagged", "could_not_check"]


@dataclass(frozen=True)
class ValidationResult:
    citation: Citation
    status: Status
    reason: str | None = None

    @property
    def ok(self) -> bool:
        return self.status == "verified"


_US_REPORTER_TYPES = {"FullCaseCitation", "ShortCaseCitation"}

# Anaphoric cites (Id./supra/reference) resolve to an antecedent full cite
# that is checked on its own; there is nothing independent to verify here.
_US_ANAPHORIC_TYPES = {"IdCitation", "SupraCitation", "ReferenceCitation"}


def _us_reporter_string(raw: object) -> str | None:
    cr = getattr(raw, "corrected_reporter", None)
    if callable(cr):
        try:
            cr = cr()
        except Exception:  # noqa: BLE001
            cr = None
    if isinstance(cr, str):
        return cr
    groups = getattr(raw, "groups", None)
    if isinstance(groups, dict):
        value = groups.get("reporter")
        if isinstance(value, str):
            return value
    return None


def _validate_us(citation: Citation) -> ValidationResult:
    raw = citation.raw
    if raw is None:
        return ValidationResult(citation, "could_not_check", "no underlying eyecite object")

    cite_type = type(raw).__name__
    if cite_type in _US_ANAPHORIC_TYPES:
        return ValidationResult(
            citation, "verified", "anaphoric cite — antecedent full cite checked separately"
        )
    # Statute / journal / unknown cites have no reporters-db check — there is
    # no offline list to consult, so "verified" would be a false positive.
    if cite_type not in _US_REPORTER_TYPES:
        return ValidationResult(
            citation,
            "could_not_check",
            f"eyecite {cite_type} — no offline check for statute/journal cites; "
            "round-trip against a primary source (eCFR / GovInfo / CourtListener)",
        )

    reporter = _us_reporter_string(raw)
    if reporter is None:
        return ValidationResult(
            citation,
            "could_not_check",
            "eyecite parsed a case citation but no reporter was identified",
        )

    try:
        from reporters_db import REPORTERS  # type: ignore
    except ImportError:
        return ValidationResult(
            citation,
            "could_not_check",
            "reporters_db not installed — cannot verify US reporter",
        )

    if reporter in REPORTERS or reporter.lower() in REPORTERS:
        return ValidationResult(citation, "verified")
    return ValidationResult(citation, "flagged", f"unknown reporter: {reporter}")


def _validate_cn(citation: Citation) -> ValidationResult:
    raw = citation.raw
    if not isinstance(raw, PrcCitation):
        return ValidationResult(
            citation, "could_not_check", "no underlying PrcCitation object to check"
        )
    status, reason = validate_prc(raw)
    return ValidationResult(citation, status, reason)


def _validate_eu(citation: Citation) -> ValidationResult:
    # Structural EU cites (Celex, OJ, TFEU/TEU article, Reg/Dir) are matched
    # by regex; full authority confirmation requires an EUR-Lex round-trip,
    # which lives in `groundedness.py`. Until that round-trip is run we are
    # explicit that we have not consulted a primary source.
    return ValidationResult(
        citation,
        "could_not_check",
        "EU cite — round-trip against EUR-Lex required for confirmation",
    )


def validate_citations(citations: list[Citation]) -> list[ValidationResult]:
    results: list[ValidationResult] = []
    for c in citations:
        if c.jurisdiction == "US":
            results.append(_validate_us(c))
        elif c.jurisdiction == "CN":
            results.append(_validate_cn(c))
        elif c.jurisdiction == "EU":
            results.append(_validate_eu(c))
        else:
            results.append(
                ValidationResult(
                    c,
                    "could_not_check",
                    f"no validator for jurisdiction {c.jurisdiction}",
                )
            )
    return results
