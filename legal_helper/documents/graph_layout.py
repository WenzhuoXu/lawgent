"""Diagram geometry, computed by Graphviz instead of by a browser.

The previous path rendered a Mermaid diagram to SVG with headless Chromium
purely to scrape node centres back out of it, then threw the SVG away. That
cost a Node runtime and a Chrome process per diagram, and it lost everything
except the centres: no edge routing, so arrows were drawn corner-to-corner
between node centres, and no aspect ratio, because the centres were scaled
independently in x and y — which is why fixed-size nodes ended up overlapping.

``dot -Tjson`` gives the whole layout: node boxes, edge splines with the
arrowhead endpoint, and edge-label positions, from a binary that is part of a
normal Linux install. This module owns the geometry and nothing else — it
knows about inches and boxes, not about PowerPoint — so the same result feeds
the PPTX shape emitter, the wireframe renderer, and the quality linter.

Coordinates in are Graphviz points with the origin bottom-left; coordinates
out are slide inches with the origin top-left, scaled uniformly so the drawing
keeps its proportions and centred in the box it was given.
"""

from __future__ import annotations

import json
import os
import shutil
import subprocess
import sys
from dataclasses import dataclass, field
from functools import lru_cache
from pathlib import Path
from typing import Any, Literal, Sequence

PT_PER_IN = 72.0

Engine = Literal["dot", "neato", "circo", "fdp", "twopi"]

#: Node kinds understood by the flowchart authoring surface, mapped to the
#: Graphviz shape that reserves the right amount of room for them. The shape
#: also travels to the emitter, which picks the PowerPoint preset.
_KIND_TO_GV_SHAPE = {
    "process": "box",
    "decision": "diamond",
    "terminator": "ellipse",
    "io": "parallelogram",
    "data": "note",
    "subprocess": "box3d",
}

#: A diamond wastes most of its bounding box on the corners, and an ellipse
#: nearly half; inflate the measured text box so the label still fits inside
#: the drawn shape rather than spilling over its edges.
_SHAPE_INFLATION = {
    "diamond": (1.65, 1.8),
    "ellipse": (1.3, 1.25),
    "parallelogram": (1.25, 1.0),
    "note": (1.15, 1.0),
    "box3d": (1.1, 1.1),
}


class GraphvizNotInstalled(RuntimeError):
    """Raised when no Graphviz engine can be located."""


@dataclass
class NodeBox:
    """One laid-out node, in slide inches with the origin at the top-left."""

    id: str
    x: float
    y: float
    w: float
    h: float
    kind: str = "process"
    text: str = ""
    fill: str = ""
    stroke: str = ""

    @property
    def cx(self) -> float:
        return self.x + self.w / 2.0

    @property
    def cy(self) -> float:
        return self.y + self.h / 2.0


@dataclass
class EdgePath:
    """One laid-out edge: a routed polyline, not a corner-to-corner line.

    ``points`` is sampled from the Graphviz bezier and always ends at the
    arrowhead position, so the emitter can put a single arrowhead on the final
    segment and leave the rest as plain segments.
    """

    from_id: str
    to_id: str
    points: list[tuple[float, float]] = field(default_factory=list)
    label: str = ""
    label_x: float | None = None
    label_y: float | None = None
    style: str = "solid"
    arrow: str = "end"


@dataclass
class DiagramLayout:
    """Everything a renderer needs, in slide inches."""

    nodes: list[NodeBox] = field(default_factory=list)
    edges: list[EdgePath] = field(default_factory=list)
    engine: str = "dot"
    #: The extent actually used inside the requested box, so a caller can
    #: report how much of the canvas the diagram fills.
    extent: tuple[float, float, float, float] = (0.0, 0.0, 0.0, 0.0)
    #: The type size the boxes were measured at. The emitter must draw the
    #: labels at this size, not at its own default, or they will not fit.
    font_pt: float = 12.0
    rankdir: str = "TB"

    def as_slide_dicts(self) -> tuple[list[dict[str, Any]], list[dict[str, Any]]]:
        """Flatten to the dict form the PPTX writers consume."""
        nodes = [
            {
                "id": n.id,
                "kind": n.kind,
                "text": n.text,
                "x": round(n.x, 3),
                "y": round(n.y, 3),
                "w": round(n.w, 3),
                "h": round(n.h, 3),
                "fill": n.fill,
                "stroke": n.stroke,
            }
            for n in self.nodes
        ]
        edges = [
            {
                "from_id": e.from_id,
                "to_id": e.to_id,
                "label": e.label,
                "style": e.style,
                "arrow": e.arrow,
                "points": [[round(x, 3), round(y, 3)] for x, y in e.points],
                "label_x": None if e.label_x is None else round(e.label_x, 3),
                "label_y": None if e.label_y is None else round(e.label_y, 3),
            }
            for e in self.edges
        ]
        return nodes, edges


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

    Node boxes only need to be right to within a few percent — Graphviz is
    being told the size, not asked to measure it — so this beats loading a
    font file, and it is the same estimator the quality linter uses for
    overflow, which keeps the two in agreement.
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


def node_size_for(
    text: str,
    *,
    font_pt: float = 12.0,
    max_w_in: float = 2.4,
    min_w_in: float = 1.3,
    pad_in: float = 0.28,
    line_spacing: float = 1.32,
) -> tuple[float, float, list[str]]:
    """Size a node box to its label. Returns ``(w_in, h_in, lines)``."""
    em_in = font_pt / PT_PER_IN
    max_em = max((max_w_in - pad_in) / em_in, 4.0)
    lines = wrap_label(text, max_em)
    widest = max((display_width(ln) for ln in lines), default=1.0)
    w = min(max(widest * em_in + pad_in, min_w_in), max_w_in)
    h = len(lines) * font_pt * line_spacing / PT_PER_IN + pad_in
    return round(w, 3), round(h, 3), lines


# ---------------------------------------------------------------------------
# Graphviz
# ---------------------------------------------------------------------------


@lru_cache(maxsize=8)
def find_engine(engine: str = "dot") -> str | None:
    """Absolute path to a Graphviz engine, or None when it is absent."""
    return shutil.which(engine)


def graphviz_available(engine: str = "dot") -> bool:
    return find_engine(engine) is not None


@lru_cache(maxsize=1)
def find_d2() -> str | None:
    """Absolute path to the ``d2`` binary, or None.

    D2 is a picture engine here, not a geometry engine: it has no coordinate
    export, so it cannot feed native PowerPoint shapes. It earns its place for
    nested containers, which no Graphviz cluster arrangement matches for
    readability on an architecture or 结构图.
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
        missing.append("graphviz dot: diagram layout falls back to a plain column — "
                       "`conda install -n llm graphviz`")
    if not d2_available():
        missing.append("d2: optional, for nested-container structure charts — "
                       "`conda install -n llm d2`")
    return missing


def _quote(value: str) -> str:
    return '"' + str(value).replace("\\", "\\\\").replace('"', '\\"') + '"'


def _dot_source(
    nodes: Sequence[dict[str, Any]],
    edges: Sequence[dict[str, Any]],
    *,
    rankdir: str,
    sizes: dict[str, tuple[float, float]],
    node_sep: float,
    rank_sep: float,
) -> str:
    """Build DOT with every node's size pinned, so no font is consulted.

    ``fixedsize=true`` is what makes this reproducible on a box with no fonts
    installed: Graphviz lays out boxes of exactly the sizes we measured rather
    than measuring the label itself.
    """
    out = [
        "digraph G {",
        f"  rankdir={rankdir}; nodesep={node_sep}; ranksep={rank_sep};",
        # polyline, not ortho: orthogonal routing is the prettiest option for a
        # flowchart and it silently drops every edge-label position — graphviz
        # warns on stderr, exits 0, and emits JSON with no `lp` key.
        "  splines=polyline; pad=0;",
        "  node [shape=box, fixedsize=true];",
        "  edge [arrowsize=0.8];",
    ]
    for n in nodes:
        w, h = sizes[n["id"]]
        shape = _KIND_TO_GV_SHAPE.get(n.get("kind", "process"), "box")
        out.append(f'  {_quote(n["id"])} [shape={shape}, width={w:.4f}, height={h:.4f}];')
    for e in edges:
        attrs = []
        if e.get("label"):
            attrs.append(f'label={_quote(e["label"])}')
            # Graphviz sizes the label gap from the font it thinks it will
            # use; state it so the reserved gap matches what we draw.
            attrs.append("fontsize=10")
        if e.get("style") == "dashed":
            attrs.append("style=dashed")
        suffix = f' [{", ".join(attrs)}]' if attrs else ""
        out.append(f'  {_quote(e["from_id"])} -> {_quote(e["to_id"])}{suffix};')
    out.append("}")
    return "\n".join(out)


def _parse_pos(pos: str) -> tuple[tuple[float, float] | None, list[tuple[float, float]]]:
    """Split a Graphviz edge ``pos`` into its arrowhead point and its spline."""
    head: tuple[float, float] | None = None
    pts: list[tuple[float, float]] = []
    for token in pos.split():
        if token.startswith(("e,", "s,")):
            kind, x, y = token.split(",")
            if kind == "e":
                head = (float(x), float(y))
            continue
        x, y = token.split(",")
        pts.append((float(x), float(y)))
    return head, pts


def _bezier(seg: Sequence[tuple[float, float]], t: float) -> tuple[float, float]:
    (x0, y0), (x1, y1), (x2, y2), (x3, y3) = seg
    mt = 1.0 - t
    a, b, c, d = mt**3, 3 * mt * mt * t, 3 * mt * t * t, t**3
    return (a * x0 + b * x1 + c * x2 + d * x3, a * y0 + b * y1 + c * y2 + d * y3)


def _sample_spline(pts: Sequence[tuple[float, float]], per_segment: int = 4) -> list[tuple[float, float]]:
    """Graphviz splines are chained cubics sharing endpoints; flatten them.

    Four samples a segment is enough that a PowerPoint polyline reads as a
    curve without producing dozens of shapes per edge.
    """
    out = [pts[0]]
    for i in range(0, len(pts) - 3, 3):
        seg = pts[i : i + 4]
        out.extend(_bezier(seg, k / per_segment) for k in range(1, per_segment + 1))
    return out


def _collinear_prune(points: list[tuple[float, float]], tol: float = 0.01) -> list[tuple[float, float]]:
    """Drop interior points that sit on the line between their neighbours.

    A straight edge comes back from Graphviz as five sampled points; emitting
    five shapes for one straight arrow is what made the old decks slow to open.
    """
    if len(points) < 3:
        return points
    kept = [points[0]]
    for prev, cur, nxt in zip(points, points[1:], points[2:]):
        (x0, y0), (x1, y1), (x2, y2) = prev, cur, nxt
        area = abs((x1 - x0) * (y2 - y0) - (x2 - x0) * (y1 - y0))
        span = max(abs(x2 - x0), abs(y2 - y0), 1e-6)
        if area / span > tol:
            kept.append(cur)
    kept.append(points[-1])
    return kept


def _run_engine(binary: str, source: str, engine: str, timeout: int) -> dict[str, Any]:
    try:
        proc = subprocess.run(
            [binary, "-Tjson"], input=source, text=True, capture_output=True,
            check=True, timeout=timeout,
        )
    except subprocess.CalledProcessError as exc:
        raise RuntimeError(f"{engine} failed: {(exc.stderr or '').strip()[:400]}") from exc
    except subprocess.TimeoutExpired as exc:
        raise RuntimeError(f"{engine} timed out after {timeout}s") from exc
    return json.loads(proc.stdout or "{}")


def _graph_extent_in(graph: dict[str, Any]) -> tuple[float, float, float, float, float, float]:
    """Return ``(gx0, gy0, gx1, gy1, width_in, height_in)`` for a dot result."""
    gx0, gy0, gx1, gy1 = (float(v) for v in graph.get("bb", "0,0,1,1").split(","))
    return gx0, gy0, gx1, gy1, max((gx1 - gx0) / PT_PER_IN, 1e-6), max((gy1 - gy0) / PT_PER_IN, 1e-6)


#: Below this the label is unreadable on a projected slide, so the layout
#: stops shrinking type and lets the linter report the diagram as too dense.
MIN_DIAGRAM_FONT_PT = 9.0
#: Above this the boxes stop reading as a diagram and start reading as slabs.
MAX_DIAGRAM_FONT_PT = 22.0

#: The fit loop aims for the drawing's binding dimension to land just inside
#: the box; anything under _FIT_LOW is a diagram floating in dead space.
_FIT_TARGET = 0.96
_FIT_LOW = 0.82
FIT_ATTEMPTS = 5

#: Once the binding side fills, the other side can still be thin — a chain is
#: a band whichever way it is turned. Two different levers, because raising the
#: label-width *cap* does nothing when the labels are already shorter than it:
#: a top-down chain is widened by a node-width **floor**, and a left-right one
#: is heightened by a tighter cap that wraps labels onto more lines.
_ASPECT_LOW = 0.62
ASPECT_ATTEMPTS = 3
_MIN_W_BOUNDS = (1.3, 3.6)
#: The mirror lever. Tightening the width cap to force a label onto two lines
#: only works if the label is long enough to wrap; a node-height floor makes a
#: left-right chain taller whatever its labels say.
_MIN_H_BOUNDS = (0.0, 1.5)


def layout_graph(
    nodes: Sequence[dict[str, Any]],
    edges: Sequence[dict[str, Any]],
    *,
    box: tuple[float, float, float, float],
    engine: Engine = "dot",
    rankdir: str | None = None,
    font_pt: float = 12.0,
    max_node_w_in: float = 2.4,
    node_sep: float = 0.4,
    rank_sep: float = 0.55,
    timeout: int = 20,
) -> DiagramLayout:
    """Lay ``nodes``/``edges`` out inside ``box`` (x, y, w, h) in slide inches.

    Node dicts need ``id`` and may carry ``kind``, ``text``, ``fill``,
    ``stroke``. Edge dicts need ``from_id``/``to_id`` and may carry ``label``,
    ``style``, ``arrow``. Raises :class:`GraphvizNotInstalled` when the engine
    is missing, so callers can fall back deliberately.

    ``rankdir=None`` tries both TB and LR and keeps whichever drawing's aspect
    ratio is closer to the box's — a seven-step chain on a 16:9 slide belongs
    on its side, and guessing wrong is what left a diagram occupying a quarter
    of the canvas. When the drawing still does not fit, the type size comes
    down and the boxes are re-measured around it, because scaling the geometry
    alone shrinks the boxes while leaving the text at its original size.
    """
    binary = find_engine(engine) or find_engine("dot")
    if not binary:
        raise GraphvizNotInstalled(
            f"graphviz ({engine}) is not installed. Install it with "
            "`conda install -n llm graphviz` or `apt-get install graphviz`."
        )

    bx, by, bw, bh = box
    known = {n["id"] for n in nodes}
    live_edges = [e for e in edges if e.get("from_id") in known and e.get("to_id") in known]
    labels = {n["id"]: (n.get("text") or n["id"]) for n in nodes}

    min_node_w_in = _MIN_W_BOUNDS[0]
    min_node_h_in = _MIN_H_BOUNDS[0]

    def measure(pt: float) -> dict[str, tuple[float, float]]:
        out: dict[str, tuple[float, float]] = {}
        for n in nodes:
            w, h, _ = node_size_for(
                labels[n["id"]], font_pt=pt,
                max_w_in=max(max_node_w_in, min_node_w_in),
                min_w_in=min_node_w_in,
            )
            shape = _KIND_TO_GV_SHAPE.get(n.get("kind", "process"), "box")
            fw, fh = _SHAPE_INFLATION.get(shape, (1.0, 1.0))
            out[n["id"]] = (w * fw, max(h * fh, min_node_h_in))
        return out

    def run(pt: float, direction: str) -> tuple[dict[str, Any], float, float]:
        src = _dot_source(
            nodes, live_edges, rankdir=direction, sizes=measure(pt),
            node_sep=node_sep * (pt / 12.0), rank_sep=rank_sep * (pt / 12.0),
        )
        result = _run_engine(binary, src, engine, timeout)
        *_, gw, gh = _graph_extent_in(result)
        return result, gw, gh

    directions = [rankdir] if rankdir else ["TB", "LR"]

    def fit(direction: str) -> tuple[dict[str, Any], float, float, float]:
        """Fit one direction fully, and report what it achieved.

        Two nested adjustments, and both are necessary. The type size balances
        the drawing's *binding* side against the box — geometry scaling alone
        would shrink the boxes and leave the text at its original size. Node
        proportions then fill the *free* side, because no type size can fix a
        drawing whose aspect is wrong: a seven-step column is 2in wide in a 7in
        box however large its labels are.

        Returns ``(graph, width_in, height_in, font_pt)``.
        """
        nonlocal min_node_w_in, min_node_h_in, max_node_w_in
        min_node_w_in, min_node_h_in = _MIN_W_BOUNDS[0], _MIN_H_BOUNDS[0]
        max_node_w_in = asked_max_w
        pt = font_pt
        graph, gw, gh = run(pt, direction)

        for _ in range(FIT_ATTEMPTS):
            need = max(gw / bw, gh / bh)
            if _FIT_LOW <= need <= 1.0:
                break
            # The exponent damps the step: node padding does not scale with the
            # font, so a proportional correction overshoots.
            target = min(max(pt * (_FIT_TARGET / need) ** 0.85, MIN_DIAGRAM_FONT_PT),
                         MAX_DIAGRAM_FONT_PT)
            if abs(target - pt) < 0.2:
                break
            pt = target
            graph, gw, gh = run(pt, direction)

        for _ in range(ASPECT_ATTEMPTS):
            along, across = max(gw / bw, gh / bh), min(gw / bw, gh / bh)
            if across >= _ASPECT_LOW or along < _FIT_LOW:
                break
            saved = (min_node_w_in, min_node_h_in)
            if gh / bh > gw / bw:
                # Too narrow. Raising the label-width *cap* does nothing when
                # the labels are already shorter than it; the floor widens the
                # node whatever the label says.
                candidate = min(min_node_w_in * 1.5, _MIN_W_BOUNDS[1])
                if candidate - min_node_w_in < 0.05:
                    break
                min_node_w_in = candidate
            else:
                # Too short, and the mirror argument applies: tightening the
                # cap only helps if the label is long enough to wrap.
                candidate = min(max(min_node_h_in, 0.42) * 1.6, _MIN_H_BOUNDS[1])
                if candidate - min_node_h_in < 0.05:
                    break
                min_node_h_in = candidate
            trial_pt = pt
            trial, tw, th = run(trial_pt, direction)
            for _ in range(FIT_ATTEMPTS):
                need = max(tw / bw, th / bh)
                if _FIT_LOW <= need <= 1.0:
                    break
                target = min(max(trial_pt * (_FIT_TARGET / need) ** 0.85, MIN_DIAGRAM_FONT_PT),
                             MAX_DIAGRAM_FONT_PT)
                if abs(target - trial_pt) < 0.2:
                    break
                trial_pt = target
                trial, tw, th = run(trial_pt, direction)
            # Judge on the area the drawing will fill *after* the uniform
            # scale that always fits it to the box. A hard "must already fit"
            # gate rejected every improvement once the font was at its floor
            # and could not shrink any further to pay for the wider nodes.
            def filled(w: float, h: float) -> float:
                fit_scale = min(bw / w, bh / h, 1.0)
                return (w * fit_scale) * (h * fit_scale) / max(bw * bh, 1e-6)

            if filled(tw, th) > filled(gw, gh) + 0.02:
                graph, gw, gh, pt = trial, tw, th, trial_pt
            else:
                min_node_w_in, min_node_h_in = saved
                break
        return graph, gw, gh, pt

    asked_max_w = max_node_w_in
    min_node_h_in = _MIN_H_BOUNDS[0]
    # Each direction is fitted in full and judged on what it actually achieved.
    # Scoring the direction up front, before the node proportions settle, picked
    # the loser often enough to be measurable.
    attempts: list[tuple[float, str, dict[str, Any], float, float, float, float, float]] = []
    for direction in directions:
        candidate, cw, ch, cpt = fit(direction)
        fit_scale = min(bw / cw, bh / ch, 1.0)
        achieved = (cw * fit_scale) * (ch * fit_scale) / max(bw * bh, 1e-6)
        attempts.append((achieved, direction, candidate, cw, ch, cpt, min_node_w_in, min_node_h_in))
    attempts.sort(key=lambda a: a[0], reverse=True)
    _, chosen, graph, gw_in, gh_in, pt, min_node_w_in, min_node_h_in = attempts[0]

    graph = graph or {}
    gx0, gy0, gx1, gy1, gw_in, gh_in = _graph_extent_in(graph)
    # Uniform scale: the old path scaled x and y independently, which is what
    # let fixed-size nodes collide on one axis while floating apart on the other.
    scale = min(bw / gw_in, bh / gh_in, 1.0)
    used_w, used_h = gw_in * scale, gh_in * scale
    ox = bx + (bw - used_w) / 2.0
    oy = by + (bh - used_h) / 2.0

    def to_in(x: float, y: float) -> tuple[float, float]:
        return (ox + ((x - gx0) / PT_PER_IN) * scale, oy + ((gy1 - y) / PT_PER_IN) * scale)

    by_id = {n["id"]: n for n in nodes}
    out_nodes: list[NodeBox] = []
    # An edge's `tail`/`head` are _gvids over a namespace that also holds
    # clusters, and clusters are emitted first. Indexing `objects` positionally
    # happens to work for a flat graph and breaks the moment one is grouped.
    by_gvid: dict[int, str] = {}
    for obj in graph.get("objects", []):
        name = obj.get("name", "")
        if "_gvid" in obj:
            by_gvid[int(obj["_gvid"])] = name
        # A cluster carries `bb` and `nodes` and no `pos`; a node is the reverse.
        if "pos" not in obj or name not in by_id:
            continue
        cx, cy = (float(v) for v in obj["pos"].split(","))
        w_in = float(obj.get("width", 1.0)) * scale
        h_in = float(obj.get("height", 0.5)) * scale
        x, y = to_in(cx, cy)
        src = by_id[name]
        out_nodes.append(NodeBox(
            id=name, x=x - w_in / 2.0, y=y - h_in / 2.0, w=w_in, h=h_in,
            kind=src.get("kind", "process"), text=labels[name],
            fill=src.get("fill", "") or "", stroke=src.get("stroke", "") or "",
        ))

    by_pair = {(e["from_id"], e["to_id"]): e for e in live_edges}
    out_edges: list[EdgePath] = []
    for gv_edge in graph.get("edges", []):
        tail = by_gvid.get(gv_edge.get("tail", -1))
        head_name = by_gvid.get(gv_edge.get("head", -1))
        if tail is None or head_name is None:
            continue
        arrow_pt, pts = _parse_pos(gv_edge.get("pos", ""))
        if len(pts) < 4:
            continue
        poly = [to_in(*p) for p in _sample_spline(pts)]
        if arrow_pt:
            poly.append(to_in(*arrow_pt))
        src = by_pair.get((tail, head_name), {})
        lp = gv_edge.get("lp")
        lx, ly = (to_in(*(float(v) for v in lp.split(","))) if lp else (None, None))
        out_edges.append(EdgePath(
            from_id=tail, to_id=head_name, points=_collinear_prune(poly),
            label=src.get("label", "") or "", label_x=lx, label_y=ly,
            style=src.get("style", "solid") or "solid",
            arrow=src.get("arrow", "end") or "end",
        ))

    return DiagramLayout(
        nodes=out_nodes, edges=out_edges, engine=engine,
        extent=(ox, oy, used_w, used_h),
        # Never below the floor: when the drawing still does not fit at the
        # minimum size, the geometry scales the last few percent but the label
        # stays legible and the emitter's shrink-to-fit absorbs the residual.
        # Reporting pt*scale here put 7.8pt labels in the file.
        font_pt=max(pt * scale, MIN_DIAGRAM_FONT_PT),
        rankdir=chosen,
    )


def stack_layout(
    nodes: Sequence[dict[str, Any]],
    edges: Sequence[dict[str, Any]],
    *,
    box: tuple[float, float, float, float],
    font_pt: float = 12.0,
) -> DiagramLayout:
    """A single column, used when no Graphviz engine is installed.

    Edges become straight two-point paths between consecutive boxes, so the
    result is still a diagram with arrows rather than a pile of text boxes.
    """
    bx, by, bw, bh = box
    count = max(len(nodes), 1)
    sizes = [node_size_for(n.get("text") or n["id"], font_pt=font_pt) for n in nodes]
    total_h = sum(h for _, h, _ in sizes)
    gap = max((bh - total_h) / (count + 1), 0.12)
    out_nodes: list[NodeBox] = []
    y = by + gap
    for n, (w, h, _) in zip(nodes, sizes):
        out_nodes.append(NodeBox(
            id=n["id"], x=bx + (bw - w) / 2.0, y=y, w=w, h=h,
            kind=n.get("kind", "process"), text=n.get("text") or n["id"],
            fill=n.get("fill", "") or "", stroke=n.get("stroke", "") or "",
        ))
        y += h + gap
    index = {n.id: n for n in out_nodes}
    out_edges = [
        EdgePath(
            from_id=e["from_id"], to_id=e["to_id"],
            points=[
                (index[e["from_id"]].cx, index[e["from_id"]].y + index[e["from_id"]].h),
                (index[e["to_id"]].cx, index[e["to_id"]].y),
            ],
            label=e.get("label", "") or "", style=e.get("style", "solid") or "solid",
            arrow=e.get("arrow", "end") or "end",
        )
        for e in edges
        if e.get("from_id") in index and e.get("to_id") in index
    ]
    extent_h = (y - gap) - by
    return DiagramLayout(nodes=out_nodes, edges=out_edges, engine="stack",
                         extent=(bx, by, bw, max(extent_h, 0.0)), font_pt=font_pt)


# ---------------------------------------------------------------------------
# Mindmaps (思维导图)
# ---------------------------------------------------------------------------


def outline_to_graph(outline: str) -> tuple[list[dict[str, Any]], list[dict[str, Any]]]:
    """Parse an indented / markdown outline into nodes and parent edges.

    Accepts ``#`` heading levels and ``-``/``*`` bullets with two-space
    indentation, which is what a model produces without being taught a new
    syntax.
    """
    nodes: list[dict[str, Any]] = []
    edges: list[dict[str, Any]] = []
    stack: list[tuple[int, str]] = []
    for raw in (outline or "").splitlines():
        if not raw.strip():
            continue
        stripped = raw.lstrip()
        if stripped.startswith("#"):
            level = len(stripped) - len(stripped.lstrip("#")) - 1
            text = stripped.lstrip("#").strip()
        elif stripped[0] in "-*+":
            level = (len(raw) - len(stripped)) // 2 + 1
            text = stripped[1:].strip()
        else:
            level = (len(raw) - len(stripped)) // 2 + 1
            text = stripped
        if not text:
            continue
        node_id = f"n{len(nodes)}"
        nodes.append({"id": node_id, "text": text, "kind": "process"})
        while stack and stack[-1][0] >= level:
            stack.pop()
        if stack:
            edges.append({"from_id": stack[-1][1], "to_id": node_id, "style": "solid", "arrow": "none"})
        stack.append((level, node_id))
    return nodes, edges


def layout_structure(
    outline: str,
    *,
    box: tuple[float, float, float, float],
    font_pt: float = 12.0,
    engine: Engine = "dot",
) -> DiagramLayout:
    """Lay an outline out as a top-down structure chart (结构图 / org chart).

    Top-down with no arrowheads, because a reporting line is a relationship
    rather than a step: an arrow on it invites the reader to trace a sequence
    that is not there.
    """
    nodes, edges = outline_to_graph(outline)
    if not nodes:
        return DiagramLayout()
    for edge in edges:
        edge["arrow"] = "none"
    try:
        return layout_graph(
            nodes, edges, box=box, engine=engine, rankdir="TB", font_pt=font_pt,
            max_node_w_in=2.6, node_sep=0.3, rank_sep=0.6,
        )
    except GraphvizNotInstalled:
        return stack_layout(nodes, edges, box=box, font_pt=font_pt)


def layout_mindmap(
    outline: str,
    *,
    box: tuple[float, float, float, float],
    font_pt: float = 12.0,
) -> DiagramLayout:
    """Lay an outline out as a left-to-right mindmap.

    ``dot`` with ``rankdir=LR`` is the right engine here: a mindmap is a tree,
    and a tree laid out by rank reads far better on a 16:9 slide than the
    radial arrangement ``twopi`` produces, which wastes the corners.
    """
    nodes, edges = outline_to_graph(outline)
    if not nodes:
        return DiagramLayout()
    try:
        return layout_graph(
            nodes, edges, box=box, engine="dot", rankdir="LR", font_pt=font_pt,
            max_node_w_in=2.8, node_sep=0.22, rank_sep=0.7,
        )
    except GraphvizNotInstalled:
        return stack_layout(nodes, edges, box=box, font_pt=font_pt)


__all__ = [
    "DiagramLayout",
    "EdgePath",
    "Engine",
    "GraphvizNotInstalled",
    "NodeBox",
    "d2_available",
    "display_width",
    "find_d2",
    "find_engine",
    "graphviz_available",
    "layout_graph",
    "layout_mindmap",
    "layout_structure",
    "render_d2_to_file",
    "unresolvable_render_binaries",
    "node_size_for",
    "outline_to_graph",
    "stack_layout",
    "wrap_label",
]
