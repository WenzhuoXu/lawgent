"""Citation subsystem.

Preserves the legacy ``audit_citations`` import surface used by
``workflow.py`` and adds:

* ``extract_citations`` / ``validate_citations`` — eyecite (US) + PRC
  regex + EU Celex/OJ/Reg/Dir regex with a three-valued ``status``
  (``verified`` | ``flagged`` | ``could_not_check``).
* ``groundedness`` — Mode-2 round-trip primitives (does the source
  actually say what's claimed).
* ``provenance`` — provenance-tag taxonomy
  (``[PKULaw]`` / ``[CourtListener]`` / ``[model knowledge — verify]`` / …).
* ``report`` — severity-tiered ``verification-report.md`` renderer with
  coverage line + do-not-file escalation.
* ``log`` — append-only ``verification-log.md`` so repeat-verified cites
  don't get re-checked.
"""

from .audit import audit_citations
from .extract import Citation, extract_citations
from .format import format_citation
from .groundedness import (
    Assertion,
    QuotedPhrase,
    RoundtripResult,
    extract_assertions,
    extract_quoted_phrases,
    nearest_citation_after,
    roundtrip_quote,
)
from .grounding import QuoteVerdict, ground_answer, prc_aware_fetch
from .provenance import (
    RETRIEVAL_TAGS,
    VERIFY_TAGS,
    TagInspection,
    find_untagged_citations,
    inspect_tag,
    is_provenance_tag,
)
from .report import (
    CoverageStats,
    ReportItem,
    Severity,
    TrustTag,
    Verdict,
    VerificationReport,
    annotate_sources_with_trust,
    assign_trust_tiers,
)
from .validate import Status, ValidationResult, validate_citations

__all__ = [
    "Assertion",
    "Citation",
    "CoverageStats",
    "QuotedPhrase",
    "RETRIEVAL_TAGS",
    "ReportItem",
    "RoundtripResult",
    "QuoteVerdict",
    "Severity",
    "Status",
    "TagInspection",
    "TrustTag",
    "VERIFY_TAGS",
    "Verdict",
    "ValidationResult",
    "VerificationReport",
    "annotate_sources_with_trust",
    "assign_trust_tiers",
    "audit_citations",
    "extract_assertions",
    "extract_citations",
    "extract_quoted_phrases",
    "find_untagged_citations",
    "format_citation",
    "ground_answer",
    "inspect_tag",
    "is_provenance_tag",
    "nearest_citation_after",
    "prc_aware_fetch",
    "roundtrip_quote",
    "validate_citations",
]
