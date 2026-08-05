"""Evaluate xlsx formulas in-process and report any error cells.

We use the pure-Python ``formulas`` engine (no soffice macro required). The
workbook on disk is left unchanged — Excel/LibreOffice will recompute the
formulas on open. The purpose of this pass is to surface ``#REF!`` /
``#DIV/0!`` / ``#NAME?`` / ``#VALUE!`` / ``#N/A`` / ``#NUM!`` / ``#NULL!``
errors back to the model before it claims the artifact is finished —
Anthropic's xlsx skill ships the same gate as ``scripts/recalc.py``.
"""

from __future__ import annotations

import re
from pathlib import Path
from typing import Any


_ERROR_TOKENS = ("#REF!", "#DIV/0!", "#NAME?", "#VALUE!", "#NULL!", "#N/A", "#NUM!")


def _coerce_value(node: Any) -> Any:
    """Unwrap the formulas-library cell wrapper to a Python scalar.

    Error cells come back as ``formulas.tokens.operand.XlError`` objects whose
    ``str()`` is one of ``_ERROR_TOKENS``; coerce them so the caller can
    string-compare.
    """
    try:
        import numpy as np
    except Exception:
        np = None  # type: ignore[assignment]
    val = node
    if hasattr(val, "value"):
        val = val.value
    if np is not None and isinstance(val, np.ndarray):
        flat = val.flatten()
        val = flat[0] if flat.size else None
    if hasattr(val, "item"):  # numpy scalars
        try:
            val = val.item()
        except Exception:
            pass
    # XlError objects: convert to their canonical string form ('#DIV/0!' etc.).
    if type(val).__name__ == "XlError":
        val = str(val)
    return val


def recalc_xlsx_workbook(path: Path) -> dict[str, Any]:
    """Evaluate every formula in ``path`` and return any error cells.

    Returns ``{"recalculated": bool, "errors": [...], "skipped_reason": str}``.
    Never raises — recalc is best-effort post-processing.
    """
    if not Path(path).is_file():
        return {"recalculated": False, "errors": [], "skipped_reason": "file not found"}
    try:
        import formulas
    except Exception as e:
        return {
            "recalculated": False,
            "errors": [],
            "skipped_reason": f"formulas library unavailable: {e}",
        }

    try:
        xl_model = formulas.ExcelModel().loads(str(path)).finish()
        solution = xl_model.calculate()
    except Exception as e:
        return {
            "recalculated": False,
            "errors": [],
            "skipped_reason": f"formula evaluation failed: {e}",
        }

    # Keys look like "'[file.xlsx]Sheet'!A4"; extract sheet + cell.
    key_re = re.compile(r"^'?\[[^\]]+\]([^'!]+)'?!([A-Z]+\d+)$")
    errors: list[dict[str, str]] = []
    for key, node in solution.items():
        m = key_re.match(str(key))
        if not m:
            continue
        value = _coerce_value(node)
        if isinstance(value, str) and value in _ERROR_TOKENS:
            errors.append({"sheet": m.group(1), "cell": m.group(2), "error": value})
    return {"recalculated": True, "errors": errors, "skipped_reason": ""}
