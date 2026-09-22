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
    engine: Literal["mermaid", "d2"] = "mermaid",
    theme: Literal["default", "neutral", "dark", "forest"] = "default",
    background: str = "white",
) -> str:
    """Render a diagram to a standalone image under outputs/. Returns the path.

    For a diagram that belongs on a slide, prefer ``write_pptx`` with a
    ``flowchart`` block: that path produces editable native shapes with routed
    arrows, and a picture cannot be edited by whoever receives the deck. Use
    this when the diagram is going into a DOCX, is being sent as a PNG, or
    needs something the shape emitter has no equivalent for.

    Two engines. ``mermaid`` covers flowcharts, sequence, class, state, ER, pie
    and mindmap diagrams. ``d2`` is the one to reach for when the diagram has
    **nested containers** — a system or 结构图 where boxes live inside boxes —
    which is what it lays out better than anything else available here.

    Args:
        filename: Desired filename (with or without extension).
        source: Diagram source in the chosen engine's syntax, e.g. Mermaid
            "flowchart TB\n  A[Step 1] --> B{OK?}" or D2
            "direction: down\n  CAAC: 民航局 { 运输司 }\n  CAAC -> Operator".
        format: Output format — png (default, raster) or svg (vector).
        engine: mermaid (default) or d2.
        theme: Mermaid theme — default, neutral, dark, or forest. Ignored by d2.
        background: Background color. Ignored by d2, which uses its own theme.
    """
    fmt = "png" if format not in ("png", "svg") else format
    out = _resolve_output(filename, fmt)
    if engine == "d2":
        from ..documents.graph_layout import GraphvizNotInstalled, render_d2_to_file

        try:
            render_d2_to_file(source, out)
        except GraphvizNotInstalled as exc:
            return f"ERROR: {exc}"
        except RuntimeError as exc:
            return f"ERROR rendering d2: {exc}"
        return str(out.resolve())
    try:
        render_mermaid_to_file(source, out, theme=theme, background=background)
    except MermaidNotInstalled as exc:
        return f"ERROR: {exc}"
    except RuntimeError as exc:
        return f"ERROR rendering mermaid: {exc}"
    return str(out.resolve())


__all__ = ["render_flowchart_image"]
