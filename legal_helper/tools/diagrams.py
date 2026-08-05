"""Diagram tools surfaced to the LLM.

Currently exposes a single function-tool, ``render_flowchart_image``,
that renders a Mermaid string to a PNG/SVG under ``outputs/``. The
underlying engine wraps ``@mermaid-js/mermaid-cli`` via
``legal_helper.documents.diagrams``.
"""

from __future__ import annotations

from pathlib import Path
from typing import Literal

from anthropic import beta_tool

from ..config import current_settings
from ..documents.diagrams import (
    MermaidNotInstalled,
    render_mermaid_to_file,
)


def _safe_filename(name: str, default: str = "diagram") -> str:
    cleaned = "".join(ch if (ch.isalnum() or ch in "._- ") else "_" for ch in name)
    cleaned = "_".join(part for part in cleaned.split("_") if part)
    return cleaned or default


def _resolve_output(filename: str, fmt: str) -> Path:
    settings = current_settings()
    settings.outputs_dir.mkdir(parents=True, exist_ok=True)
    stem = _safe_filename(Path(filename).stem, default="diagram")
    return settings.outputs_dir / f"{stem}.{fmt.lstrip('.')}"


@beta_tool
def render_flowchart_image(
    filename: str,
    source: str,
    format: Literal["png", "svg"] = "png",
    theme: Literal["default", "neutral", "dark", "forest"] = "default",
    background: str = "white",
) -> str:
    """Render a Mermaid diagram to an image under outputs/. Returns the absolute path.

    Use this for standalone flowcharts/process maps that should go into a
    DOCX, be embedded as an image on a slide via SlideImage, or be sent to
    the user as a PNG. For native PPTX shapes (editable boxes + arrows),
    prefer ``write_pptx`` with a ``flowchart`` block on the slide.

    Args:
        filename: Desired filename (with or without extension).
        source: Mermaid diagram source, e.g. "flowchart TB\\n  A[Step 1] --> B{OK?}".
        format: Output format — png (default, raster) or svg (vector).
        theme: Mermaid theme — default, neutral, dark, or forest.
        background: Background color (e.g. 'white', 'transparent', or '#FFF8E7').
    """
    fmt = "png" if format not in ("png", "svg") else format
    out = _resolve_output(filename, fmt)
    try:
        render_mermaid_to_file(source, out, theme=theme, background=background)
    except MermaidNotInstalled as exc:
        return f"ERROR: {exc}"
    except RuntimeError as exc:
        return f"ERROR rendering mermaid: {exc}"
    return str(out.resolve())


__all__ = ["render_flowchart_image"]
