"""Diagram tools surfaced to the LLM.

One function-tool, ``render_flowchart_image``: a diagram as a picture under
``outputs/``, for a Word document or a chat reply. A diagram that belongs on a
slide is made by ``create_deck``, which draws it as native shapes inside the
deck. The engines live in :mod:`legal_helper.documents.diagrams`.
"""

from __future__ import annotations

from pathlib import Path
from typing import Literal

from anthropic import beta_tool

from ..config import current_settings
from ..documents.diagrams import MermaidNotInstalled, render_mermaid_picture


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
    """Draw a diagram as a picture under outputs/. Returns the path.

    For a diagram going into a Word document, sent as an image, or shown in
    the chat. A diagram that belongs on a slide or in a deck is made with
    ``create_deck`` instead, which draws it as editable shapes on the slide.

    ``mermaid`` (default): ``flowchart`` / ``graph`` sources — process maps,
    decision trees, swimlanes as ``subgraph`` blocks — and ``mindmap`` sources
    (思维导图) are laid out and drawn by Graphviz with a CJK font, so labels
    fit their boxes. Other Mermaid types (sequence, class, state, ER, gantt,
    pie) are drawn by mermaid-cli. ``d2``: diagrams of nested containers —
    an organisation or system 结构图 where boxes sit inside boxes.

    Args:
        filename: Desired filename (with or without extension).
        source: Diagram source in the chosen engine's syntax, e.g. Mermaid
            "flowchart TB\\n  A[受理] --> B{材料齐全?}\\n  B -->|是| C[审查]" or
            D2 "direction: down\\n  集团: { 法务部 }\\n  集团 -> 子公司".
        format: png (default; embeds in a .docx) or svg (vector).
        engine: mermaid (default) or d2.
        theme: Colour theme — default, neutral, dark, or forest. Ignored by d2.
        background: Background colour, or "transparent". Ignored by d2.
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
        path, _engine = render_mermaid_picture(source, out, theme=theme, background=background)
    except MermaidNotInstalled as exc:
        return f"ERROR: {exc}"
    except RuntimeError as exc:
        return f"ERROR rendering mermaid: {exc}"
    return str(path.resolve())


__all__ = ["render_flowchart_image"]
