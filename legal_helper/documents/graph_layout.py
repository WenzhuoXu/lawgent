"""Text measurement and the diagram render binaries.

Two things live here, and both outlived the slide-geometry engine this module
was named for:

* **CJK-aware text measurement** (:func:`display_width`, :func:`wrap_label`).
  The DOCX writer weights table columns with it and the quality linter
  estimates whether text fits its box with it, so the two agree on how wide a
  Han glyph is.
* **The render binaries** — Graphviz ``dot`` and ``d2`` — resolved, reported
  at startup when missing, and run.

It used to compute node boxes and routed edges in slide inches for an in-tree
PPTX emitter. That emitter is gone: a diagram on a slide is now drawn by
ppt-master inside the deck (``tools/decks.create_deck``), and a diagram as a
picture is drawn whole by Graphviz from DOT that :mod:`.diagrams` builds, with
a real CJK font measuring the labels instead of an estimate.
"""

from __future__ import annotations

import os
import shutil
import subprocess
import sys
from functools import lru_cache
from pathlib import Path


class GraphvizNotInstalled(RuntimeError):
    """Raised when a render binary (Graphviz ``dot``, ``d2``) cannot be located."""


# ---------------------------------------------------------------------------
# Text measurement — CJK-aware, and deliberately not font-metric-accurate
# ---------------------------------------------------------------------------

_WIDE_RANGES = (
    (0x1100, 0x115F), (0x2E80, 0x303E), (0x3041, 0x33FF), (0x3400, 0x4DBF),
    (0x4E00, 0x9FFF), (0xA000, 0xA4CF), (0xAC00, 0xD7A3), (0xF900, 0xFAFF),
    (0xFE30, 0xFE4F), (0xFF00, 0xFF60), (0xFFE0, 0xFFE6),
)


def _is_wide(ch: str) -> bool:
    cp = ord(ch)
    return any(lo <= cp <= hi for lo, hi in _WIDE_RANGES)


def display_width(text: str) -> float:
    """Width of ``text`` in em units: a Han glyph is one em, Latin about half.

    Within a few percent is all any caller needs — a column weight, an
    overflow estimate, a label wrap — so this beats loading a font file, and
    every caller shares it so none of them disagree.
    """
    return sum(1.0 if _is_wide(c) else 0.5 for c in text)


def wrap_label(text: str, max_em: float) -> list[str]:
    """Greedy wrap on ``max_em`` em units per line, breaking CJK anywhere.

    Latin words are kept whole; CJK has no spaces, so it wraps mid-run, which
    is what Chinese typesetting does anyway.
    """
    text = (text or "").strip()
    if not text:
        return [""]
    lines: list[str] = []
    for hard in text.replace("\\n", "\n").splitlines() or [""]:
        cur, cur_w, word, word_w = "", 0.0, "", 0.0

        def flush_word() -> None:
            nonlocal cur, cur_w, word, word_w
            if word:
                cur += word
                cur_w += word_w
                word, word_w = "", 0.0

        for ch in hard:
            w = display_width(ch)
            if _is_wide(ch) or ch == " ":
                flush_word()
                if cur_w + w > max_em and cur:
                    lines.append(cur)
                    cur, cur_w = "", 0.0
                if ch == " " and not cur:
                    continue
                cur += ch
                cur_w += w
            else:
                if cur_w + word_w + w > max_em and (cur or word):
                    if cur:
                        lines.append(cur)
                        cur, cur_w = "", 0.0
                    elif word_w + w > max_em:
                        lines.append(word)
                        word, word_w = "", 0.0
                word += ch
                word_w += w
        flush_word()
        lines.append(cur)
    return [ln for ln in lines if ln != ""] or [""]


# ---------------------------------------------------------------------------
# Graphviz
# ---------------------------------------------------------------------------


@lru_cache(maxsize=8)
def find_engine(engine: str = "dot") -> str | None:
    """Absolute path to a Graphviz engine, or None when it is absent."""
    return shutil.which(engine)


def graphviz_available(engine: str = "dot") -> bool:
    return find_engine(engine) is not None


#: Families tried in order for diagram labels. A Han label set in a Latin face
#: renders through fontconfig fallback glyph by glyph, which mixes faces inside
#: one word; naming a CJK family keeps a label in one face.
_CJK_FAMILIES = (
    "Noto Sans CJK SC",
    "Source Han Sans SC",
    "Source Han Sans CN",
    "Microsoft YaHei",
    "PingFang SC",
    "WenQuanYi Micro Hei",
    "WenQuanYi Zen Hei",
    "Droid Sans Fallback",
)


@lru_cache(maxsize=1)
def cjk_font_family() -> str:
    """The installed family diagrams set their labels in.

    The first of :data:`_CJK_FAMILIES` that fontconfig reports as covering
    Chinese, else the generic ``sans-serif``, which still renders Han through
    fontconfig's fallback rather than as boxes.
    """
    fc_list = shutil.which("fc-list")
    if not fc_list:
        return "sans-serif"
    try:
        proc = subprocess.run(
            [fc_list, ":lang=zh", "family"], capture_output=True, text=True, timeout=10, check=False
        )
    except (OSError, subprocess.SubprocessError):
        return "sans-serif"
    installed = {
        name.strip()
        for line in (proc.stdout or "").splitlines()
        for name in line.split(",")
        if name.strip()
    }
    for family in _CJK_FAMILIES:
        if family in installed:
            return family
    return "sans-serif"


def render_dot_to_file(source: str, output_path: str | Path, *, timeout: int = 60) -> Path:
    """Lay out and draw DOT ``source`` with Graphviz into ``output_path``.

    The suffix picks the format (``.png``, ``.svg``, ``.pdf``). Graphviz
    measures every label in the real font it draws with, so boxes fit their
    text by construction. Raises :class:`GraphvizNotInstalled` without ``dot``
    and ``RuntimeError`` when the layout fails.
    """
    binary = find_engine("dot")
    if not binary:
        raise GraphvizNotInstalled(
            "graphviz (dot) is not installed. Install it with "
            "`conda install -n llm graphviz` or `apt-get install graphviz`."
        )
    output_path = Path(output_path).resolve()
    output_path.parent.mkdir(parents=True, exist_ok=True)
    fmt = output_path.suffix.lower().lstrip(".") or "png"
    try:
        subprocess.run(
            [binary, f"-T{fmt}", "-o", str(output_path)],
            input=source, text=True, capture_output=True, check=True, timeout=timeout,
        )
    except subprocess.CalledProcessError as exc:
        raise RuntimeError(f"dot failed: {(exc.stderr or '').strip()[:400]}") from exc
    except subprocess.TimeoutExpired as exc:
        raise RuntimeError(f"dot timed out after {timeout}s") from exc
    if not output_path.is_file():
        raise RuntimeError(f"dot did not produce output at {output_path}")
    return output_path


# ---------------------------------------------------------------------------
# D2
# ---------------------------------------------------------------------------


@lru_cache(maxsize=1)
def find_d2() -> str | None:
    """Absolute path to the ``d2`` binary, or None.

    D2 earns its place for nested containers, which no Graphviz cluster
    arrangement matches for readability on an architecture or 结构图.
    """
    override = os.environ.get("LEGAL_HELPER_D2", "").strip()
    if override and Path(override).is_file():
        return override
    sibling = Path(sys.executable).resolve().parent / "d2"
    if sibling.is_file() and os.access(sibling, os.X_OK):
        return str(sibling)
    return shutil.which("d2")


def d2_available() -> bool:
    return find_d2() is not None


def render_d2_to_file(
    source: str,
    output_path: str | Path,
    *,
    layout: str = "elk",
    theme: int = 0,
    timeout: int = 60,
) -> Path:
    """Render D2 ``source`` to ``output_path`` (``.svg``, ``.png`` or ``.pdf``).

    ``elk`` rather than the ``dagre`` default: it routes edges around obstacles
    and keeps nested containers legible, which is the whole reason to reach for
    D2 over Graphviz.
    """
    binary = find_d2()
    if not binary:
        raise GraphvizNotInstalled(
            "d2 is not installed. Install it with `conda install -n llm d2` or from "
            "https://github.com/terrastruct/d2/releases, then retry."
        )
    output_path = Path(output_path).resolve()
    output_path.parent.mkdir(parents=True, exist_ok=True)
    src = output_path.with_suffix(".d2")
    src.write_text(source, encoding="utf-8")
    try:
        subprocess.run(
            [binary, "--layout", layout, "--theme", str(theme), str(src), str(output_path)],
            check=True, capture_output=True, text=True, timeout=timeout,
        )
    except subprocess.CalledProcessError as exc:
        raise RuntimeError(f"d2 failed: {(exc.stderr or exc.stdout or '').strip()[:400]}") from exc
    except subprocess.TimeoutExpired as exc:
        raise RuntimeError(f"d2 timed out after {timeout}s") from exc
    finally:
        src.unlink(missing_ok=True)
    if not output_path.is_file():
        raise RuntimeError(f"d2 did not produce output at {output_path}")
    return output_path


def unresolvable_render_binaries() -> list[str]:
    """Name each render binary a diagram path needs and cannot find.

    Reported at startup next to the unresolvable MCP specs, because a missing
    binary here is silent at author time and only shows up as a diagram that
    never appeared.
    """
    missing: list[str] = []
    if not graphviz_available("dot"):
        missing.append("graphviz dot: render_flowchart_image cannot draw Mermaid flowcharts "
                       "or mind maps without it — `conda install -n llm graphviz`")
    if not d2_available():
        missing.append("d2: optional, for nested-container structure charts — "
                       "`conda install -n llm d2`")
    return missing


__all__ = [
    "GraphvizNotInstalled",
    "cjk_font_family",
    "d2_available",
    "display_width",
    "find_d2",
    "find_engine",
    "graphviz_available",
    "render_d2_to_file",
    "render_dot_to_file",
    "unresolvable_render_binaries",
    "wrap_label",
]
