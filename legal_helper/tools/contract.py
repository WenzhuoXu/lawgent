"""Heuristic clause extractor used by review-contract / triage-nda.

Generic commercial categories live here. Domain-pack-specific category
patterns (e.g. aviation IDERA / Cape Town / AD-SB) are pulled in only when
the corresponding pack is active, via ``_active_category_tags()``.
"""

import json
import re

from anthropic import beta_tool


# Recognize section/clause headings:
#   "1. Title", "1.2 Title", "ARTICLE I — Title", "SECTION 4.1 Title"
_HEADING_RE = re.compile(
    r"""^(
        (?:ARTICLE\s+[IVXLCDM\d]+|SECTION\s+[\d.]+|SCHEDULE\s+[A-Z\d]+|EXHIBIT\s+[A-Z\d]+|ANNEX\s+[A-Z\d]+|APPENDIX\s+[A-Z\d]+)
        (?:\s*[—\-:.\s]+.*)?
        |
        \d+(?:\.\d+){0,3}\s+[A-Z][A-Za-z0-9 ,/&()\-']+
    )$""",
    re.VERBOSE,
)

# Generic commercial clause categories — apply across jurisdictions and
# domains. Aviation-specific tags (hull insurance, IDERA, AD/SB, etc.) are
# kept in ``domains/aviation/contract_tags.py`` and merged in only when the
# aviation pack is active.
_CATEGORY_TAGS: list[tuple[str, re.Pattern[str]]] = [
    ("limitation_of_liability", re.compile(r"\b(limitation of liability|cap on liability|liability cap)\b", re.I)),
    ("indemnification", re.compile(r"\b(indemnif(y|ication)|hold harmless)\b", re.I)),
    ("insurance", re.compile(r"\b(insurance|insured|insurer)\b", re.I)),
    ("export_control", re.compile(r"\b(ITAR|EAR|export control|denied party|deemed export)\b", re.I)),
    ("governing_law", re.compile(r"\b(governing law|jurisdiction|venue|arbitration)\b", re.I)),
    ("term_and_termination", re.compile(r"\b(term|termination|renewal|expiry|expiration)\b", re.I)),
    ("assignment_novation", re.compile(r"\b(assignment|novation|change of control)\b", re.I)),
    ("data_protection", re.compile(r"\b(data protection|GDPR|DPA|personal data)\b", re.I)),
    ("confidentiality", re.compile(r"\b(confidential(?:ity)?|non-disclosure)\b", re.I)),
    ("force_majeure", re.compile(r"\b(force majeure)\b", re.I)),
    ("payment_terms", re.compile(r"\b(payment terms|invoice|late fee|interest)\b", re.I)),
    ("dispute_resolution", re.compile(r"\b(dispute resolution|arbitration|mediation)\b", re.I)),
    ("warranties", re.compile(r"\b(warrant(?:y|ies)|representations)\b", re.I)),
]


def _active_category_tags() -> list[tuple[str, re.Pattern[str]]]:
    """Return generic categories plus any pack-contributed categories for
    the currently active domain packs."""
    tags = list(_CATEGORY_TAGS)
    try:
        from ..config import current_settings

        active = list(current_settings().active_domain_packs)
    except Exception:
        active = []
    if "aviation" in active:
        try:
            from ..domains.aviation.contract_tags import AVIATION_CATEGORY_TAGS

            tags.extend(AVIATION_CATEGORY_TAGS)
        except Exception:
            pass
    return tags


def _categorize(heading: str, body: str) -> list[str]:
    text = f"{heading}\n{body}"
    hits = [name for name, pat in _active_category_tags() if pat.search(text)]
    return hits


def _split_clauses(text: str) -> list[dict]:
    """Split a contract into a list of {heading, body, categories}."""
    lines = text.splitlines()
    clauses: list[dict] = []
    current_heading: str | None = None
    current_body: list[str] = []

    def flush() -> None:
        if current_heading is None and not current_body:
            return
        heading = current_heading or "PREAMBLE"
        body = "\n".join(current_body).strip()
        clauses.append(
            {
                "heading": heading,
                "body": body,
                "categories": _categorize(heading, body),
            }
        )

    for raw in lines:
        line = raw.rstrip()
        if not line.strip():
            current_body.append("")
            continue
        if _HEADING_RE.match(line.strip()):
            flush()
            current_heading = line.strip()
            current_body = []
            continue
        current_body.append(line)
    flush()
    return [c for c in clauses if c["heading"] != "PREAMBLE" or c["body"]]


@beta_tool
def extract_clauses(text: str) -> str:
    """Heuristically split a contract into clauses and tag generic commercial
    categories (governing law, indemnification, payment terms, force majeure,
    etc.). When a domain pack is active, additional pack-specific tags are
    surfaced too.

    Returns a JSON string: ``[{heading, body, categories: [..]} ...]``.
    Use this to locate where each material provision lives before doing the
    detailed clause-by-clause review.

    Args:
        text: Full contract text (already extracted from PDF/DOCX).
    """
    clauses = _split_clauses(text)
    return json.dumps(clauses, ensure_ascii=False)
