"""Aviation-specific clause-category tags consumed by ``tools/contract.py``.

The generic ``extract_clauses`` tool keeps a small, jurisdiction-agnostic set
of commercial categories. When the aviation pack is active, ``extract_clauses``
pulls these additional category patterns so the downstream LLM can quickly
locate aviation-specific provisions (IDERA, Cape Town, AD/SB, hull/liability,
return conditions, maintenance reserves, total loss, tax indemnity).
"""

from __future__ import annotations

import re


AVIATION_CATEGORY_TAGS: list[tuple[str, re.Pattern[str]]] = [
    ("insurance_hull_liability", re.compile(r"\b(hull|aviation liability|combined single limit|AVN\s*\d+|LSW\s*\d+|war risk)\b", re.I)),
    ("return_conditions", re.compile(r"\b(return condition|redelivery|return acceptance|technical record)\b", re.I)),
    ("ad_sb_compliance", re.compile(r"\b(airworthiness directives?|service bulletins?|AD/SB|AD\s*&\s*SB)\b", re.I)),
    ("idera_cape_town", re.compile(r"\b(IDERA|Cape Town Convention|international registry|Aircraft Protocol)\b", re.I)),
    ("total_loss", re.compile(r"\b(total loss|constructive total loss|agreed value|stipulated loss)\b", re.I)),
    ("maintenance_reserves", re.compile(r"\b(maintenance reserve|supplemental rent|EOL adjustment|end of lease)\b", re.I)),
    ("tax_indemnity", re.compile(r"\b(tax indemnit|after[\- ]tax|gross[\- ]up)\b", re.I)),
]
