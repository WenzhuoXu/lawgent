"""Lightweight citation audit (preserves the legacy behaviour of the prior citations.py)."""

from __future__ import annotations

import re

from ..chat_models import CitationAuditResult


_SOURCE_LINE_RE = re.compile(r"^\s*(?:[-*]|\d+[.)])\s+(.+)$", re.MULTILINE)
_PINPOINT_RE = re.compile(
    r"\b(article|art\.|standard|recommended practice|sarps?|section|sec\.|§|paragraph|para\.|page|p\.|clause|chapter|appendix)\b",
    re.IGNORECASE,
)
# CJK pinpoints (条款 rule): 第一千零八十七条 / 第52条第3款 / 第5页.
# No \b anchors — CJK text has no word boundaries.
_PINPOINT_CJK_RE = re.compile(
    r"第[\d一二三四五六七八九十百千万零]+(?:条|款|项|目|章|节|页)"
)


def audit_citations(text: str) -> CitationAuditResult:
    warnings: list[str] = []
    sources_start = max(text.rfind("Sources"), text.rfind("资料来源"), text.rfind("来源"))
    if sources_start < 0:
        return CitationAuditResult(ok=False, warnings=["No Sources / 资料来源 section found."])

    source_block = text[sources_start:]
    source_lines = [m.group(1).strip() for m in _SOURCE_LINE_RE.finditer(source_block)]
    if not source_lines:
        warnings.append("Sources section does not contain source entries.")
    for line in source_lines:
        lower = line.lower()
        if "pinpoint unavailable" in lower or "无法获得具体条款" in lower:
            continue
        if not (_PINPOINT_RE.search(line) or _PINPOINT_CJK_RE.search(line)):
            warnings.append(f"Source lacks pinpoint citation: {line[:180]}")

    return CitationAuditResult(ok=not warnings, warnings=warnings[:20])
