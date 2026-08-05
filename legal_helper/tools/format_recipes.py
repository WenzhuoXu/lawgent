"""Office file-format recipe tools.

Read deeper how-to recipes for ``write_docx`` / ``write_xlsx`` / ``write_pptx``
/ ``write_pdf`` on demand. Lives outside any specialist skill so the
orchestrator, the general-answer direct-LLM path, and every specialist can
load them.
"""

from __future__ import annotations

import json
from pathlib import Path

from anthropic import beta_tool


_RECIPE_DIR = (
    Path(__file__).resolve().parent.parent / "playbook" / "format_recipes"
)
_VALID_FORMATS = ("docx", "xlsx", "pptx", "pdf")


@beta_tool
def list_format_recipes() -> str:
    """List office file-format recipes available for write_docx / write_xlsx
    / write_pptx / write_pdf. Returns a JSON object whose ``recipes`` field
    enumerates the supported format keys.
    """
    recipes = [
        name for name in _VALID_FORMATS
        if (_RECIPE_DIR / f"{name}.md").is_file()
    ]
    return json.dumps({"recipes": recipes}, ensure_ascii=False)


@beta_tool
def read_format_recipe(format: str) -> str:
    """Read the recipe for one office file format. Call this before invoking
    ``write_docx`` / ``write_xlsx`` / ``write_pptx`` / ``write_pdf`` when the
    request needs charts, conditional formatting, master slides, or other
    structure that goes beyond plain headings and bullet lists.

    Args:
        format: One of "docx", "xlsx", "pptx", "pdf".
    """
    key = (format or "").strip().lower()
    if key not in _VALID_FORMATS:
        return (
            f"ERROR: unknown format '{format}'. "
            f"Valid: {', '.join(_VALID_FORMATS)}."
        )
    path = _RECIPE_DIR / f"{key}.md"
    if not path.is_file():
        return f"ERROR: recipe file missing for format '{key}'"
    return path.read_text(encoding="utf-8")
