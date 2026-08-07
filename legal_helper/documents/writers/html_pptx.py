"""HTML/CSS → PPTX writer (Chromium-measured, editable output).

Complements the fixed-layout writer in :mod:`.pptx`. That one takes a slide
spec and drops it into one of ten hard-coded arrangements; this one lets the
author use a real layout engine and maps the computed result onto native
PowerPoint objects. Use it whenever the deck needs a layout the enum does not
have — a 2x3 card grid, an overlapping hero, a sidebar, a KPI band, a
process strip with numbered chevrons.
"""

from __future__ import annotations

import json
import subprocess
from pathlib import Path
from typing import Any

from ._node import find_node, node_diagnostic

_SCRIPT = Path(__file__).resolve().parent / "scripts" / "html2pptx.js"
_STYLE = Path(__file__).resolve().parent / "scripts" / "deck.css"


def deck_stylesheet() -> str:
    """Return the built-in deck stylesheet authors can start from."""
    return _STYLE.read_text(encoding="utf-8") if _STYLE.is_file() else ""


def write_pptx_from_html(
    path: Path,
    html: str,
    *,
    title: str = "Presentation",
    width_in: float = 13.333,
    height_in: float = 7.5,
    lang: str = "",
    raster_selectors: list[str] | None = None,
    timeout: int = 180,
) -> dict[str, Any]:
    """Render ``html`` to a .pptx at ``path``.

    Each ``.slide`` element becomes one slide. Returns the renderer's report
    (slide/shape/text/table/image counts plus warnings) so the caller can tell
    a real conversion from a degraded one — never a bare path.

    Raises ``RuntimeError`` when Node, Playwright, or the conversion itself
    fails, so callers can fall back to the fixed-layout writer.
    """
    node = find_node()
    if not node:
        raise RuntimeError(f"html2pptx requires Node — {node_diagnostic()}")
    if not _SCRIPT.is_file():
        raise RuntimeError(f"html2pptx script missing at {_SCRIPT}")
    path = Path(path)
    path.parent.mkdir(parents=True, exist_ok=True)

    payload = {
        "path": str(path),
        "html": html,
        "title": title,
        "width_in": width_in,
        "height_in": height_in,
        "lang": lang,
        "raster_selectors": raster_selectors or [],
    }
    try:
        proc = subprocess.run(
            [node, str(_SCRIPT)],
            input=json.dumps(payload, ensure_ascii=False),
            text=True,
            capture_output=True,
            check=True,
            timeout=timeout,
        )
    except subprocess.CalledProcessError as exc:
        raise RuntimeError(f"html2pptx failed: {(exc.stderr or exc.stdout or '').strip()[:2000]}") from exc
    except subprocess.TimeoutExpired as exc:
        raise RuntimeError(f"html2pptx timed out after {timeout}s") from exc

    try:
        return json.loads(proc.stdout or "{}")
    except json.JSONDecodeError:
        raise RuntimeError(f"html2pptx returned non-JSON: {proc.stdout[:500]}")
