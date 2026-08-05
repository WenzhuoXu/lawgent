"""Per-style citation formatting (gb_t_7714 | bluebook | oscola)."""

from __future__ import annotations

from typing import Literal

from .extract import Citation


Style = Literal["gb_t_7714", "bluebook", "oscola"]


def _format_gb_t_7714(c: Citation) -> str:
    # GB/T 7714 (Chinese national bibliographic standard). For PRC cites we
    # keep the source verbatim — case numbers and 法释 numbers are already in
    # their canonical form. For non-CN sources we render a minimal form.
    return c.text


def _format_bluebook(c: Citation) -> str:
    if c.raw is not None and hasattr(c.raw, "corrected_citation"):
        try:
            return c.raw.corrected_citation()  # eyecite helper
        except Exception:
            pass
    return c.text


def _format_oscola(c: Citation) -> str:
    return c.text


def format_citation(citation: Citation, style: Style = "gb_t_7714") -> str:
    if style == "gb_t_7714":
        return _format_gb_t_7714(citation)
    if style == "bluebook":
        return _format_bluebook(citation)
    return _format_oscola(citation)
