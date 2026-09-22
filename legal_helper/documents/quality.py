"""Deterministic conformance checks on a document we just wrote.

**This is a floor, not a quality score, and it cannot tell you whether the
document is any good.** It caught a shape 0.4in off the canvas, 8pt labels,
1.9:1 contrast and a deck that had silently fallen back to the degraded
writer — defects that are objective, invisible in a thumbnail, and free to
find. It also passed, as "clean", a deck whose author called it empty with no
storytelling, because a card rectangle counts as content whether or not
anything is written in it.

So: a clean conformance result means nothing here is provably broken. Whether
the deck says anything is a judgement, and it belongs to the rubric critique in
:mod:`legal_helper.documents.review` and ultimately to the reader.

**A density metric was considered and deliberately not added.** The generated
deck averaged 97 em of text per slide; the reference decks in
``outputs/chat_uploads/`` average 82–206, so a density floor would have passed
it too. What distinguishes the real decks is the *distribution* — sparse
dividers and then a 500–660 em exhibit that does the work — and a floor on the
mean is satisfied by padding every slide, which is worse than an empty one.
Do not add one.

Two sources, deliberately:

* the file, which knows what was *specified* — shape boxes, run sizes, fills,
  table grids — and so can prove a shape is off-canvas or a label cannot fit;
* the render, which knows what a reader will *see* — how much of the canvas
  carries ink, and where the dead bands are — which no amount of reading the
  XML can tell you.

Findings are structured, not prose, because they are appended to tool results
that carry inline images and therefore skip the per-tool token budget
(``providers/*.py`` bypass ``apply_result_budget`` for image-bearing results).
``QualityReport.to_payload`` enforces its own cap instead.
"""

from __future__ import annotations

from collections import Counter
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, Literal, Sequence

from .graph_layout import display_width, wrap_label

Severity = Literal["critical", "major", "minor"]

#: Reported, never graded. Retained only because the degraded fallback writer
#: is unmistakable at ~0.08 and that is worth seeing in the metrics.
CANVAS_COVERAGE_REPORT_ONLY = True

#: Type floors for a 13.333in canvas seen from the back of a room.
BODY_FONT_FLOOR_PT = 11.0
NOTE_FONT_FLOOR_PT = 9.0

#: Two shapes may overlap by this fraction of the smaller one's area before it
#: reads as a collision rather than a deliberate nudge.
OVERLAP_TOLERANCE = 0.02

#: An empty box has to be big in BOTH directions to be a hole in the layout;
#: a 12x0.25in strip is a rule or a spacer, and flagging those buries the
#: finding that matters under noise.
EMPTY_SHAPE_MIN_AREA_IN2 = 3.0
EMPTY_SHAPE_MIN_EDGE_IN = 0.5

#: A shape thinner than this in its short dimension is a rule, a divider or a
#: connector, all of which are meant to sit under other shapes.
HAIRLINE_IN = 0.5

#: Line height multiplier used to estimate wrapped text height. Kept slightly
#: under PowerPoint's own single spacing: the HTML path sizes each box from the
#: browser's computed line box, so an over-estimate here reports every heading
#: it produces as overflowing.
LINE_HEIGHT = 1.16

#: How far past its box text has to run before it is worth reporting. Text in
#: a PowerPoint text box spills rather than clips, so a few percent is invisible;
#: a quarter of a line is not.
OVERFLOW_WARN = 1.08
OVERFLOW_BLOCK = 1.25

#: Serialized size ceiling for a report handed back through a tool result.
PAYLOAD_CHAR_CAP = 2000
_MAX_EXAMPLES_PER_CHECK = 3


#: Checks that need pixels. Empty by design: every occupancy measure was
#: deleted, so the render stage now reports metrics and finds nothing.
RENDER_CHECKS: frozenset[str] = frozenset()


@dataclass
class Finding:
    """One defect, with the fix stated so the model does not have to infer it.

    ``slide`` and ``shape`` are indices rather than prose because the wireframe
    renderer colours the offending box with them: a critic that is handed a
    picture with the collision already outlined in red never has to find it.
    """

    check: str
    severity: Severity
    where: str
    detail: str
    fix: str
    #: 1-based slide number, 0 for a whole-deck finding.
    slide: int = 0
    #: 1-based z-order index of the offending shape, when there is one.
    shape: int | None = None

    def as_dict(self) -> dict[str, Any]:
        return {
            "check": self.check,
            "severity": self.severity,
            "where": self.where,
            "detail": self.detail,
            "fix": self.fix,
        }


@dataclass
class QualityReport:
    findings: list[Finding] = field(default_factory=list)
    metrics: dict[str, Any] = field(default_factory=dict)

    @property
    def critical(self) -> list[Finding]:
        return [f for f in self.findings if f.severity == "critical"]

    @property
    def ok(self) -> bool:
        return not self.critical

    def to_payload(self, *, cap: int = PAYLOAD_CHAR_CAP) -> dict[str, Any]:
        """Collapse to a bounded, machine-readable payload.

        Identical checks are grouped: twenty overflowing boxes are one finding
        with twenty locations, not twenty findings, which is both shorter and
        more useful — it says "the type scale is wrong", not "slide 7 again".
        """
        grouped: dict[tuple[str, str], list[Finding]] = {}
        for f in self.findings:
            grouped.setdefault((f.check, f.severity), []).append(f)
        order = {"critical": 0, "major": 1, "minor": 2}
        rows: list[dict[str, Any]] = []
        for (check, severity), group in sorted(
            grouped.items(), key=lambda kv: (order[kv[0][1]], kv[0][0])
        ):
            rows.append({
                "check": check,
                "severity": severity,
                "count": len(group),
                "where": [f.where for f in group[:_MAX_EXAMPLES_PER_CHECK]],
                "detail": group[0].detail,
                "fix": group[0].fix,
            })
        payload: dict[str, Any] = {
            # "conformance", never "quality": the only thing a pass means is
            # that nothing here is provably broken.
            "conformance": (
                "pass" if not self.findings else ("blocked" if self.critical else "warnings")
            ),
            "counts": {s: sum(1 for f in self.findings if f.severity == s)
                       for s in ("critical", "major", "minor") if any(f.severity == s for f in self.findings)},
            "metrics": self.metrics,
            "findings": rows,
        }
        while len(repr(payload)) > cap and payload["findings"]:
            payload["findings"].pop()
            payload["truncated"] = True
        return payload

    def summary(self) -> str:
        if not self.findings:
            return "conformance: pass (says nothing about whether it reads well)"
        parts = [f"{sum(1 for f in self.findings if f.severity == s)} {s}"
                 for s in ("critical", "major", "minor")
                 if any(f.severity == s for f in self.findings)]
        return "conformance: " + ", ".join(parts)


# ---------------------------------------------------------------------------
# Colour
# ---------------------------------------------------------------------------


def relative_luminance(rgb: tuple[int, int, int]) -> float:
    """WCAG 2.x relative luminance of an sRGB triple."""
    channels = []
    for raw in rgb:
        c = raw / 255.0
        channels.append(c / 12.92 if c <= 0.03928 else ((c + 0.055) / 1.055) ** 2.4)
    r, g, b = channels
    return 0.2126 * r + 0.7152 * g + 0.0722 * b


def contrast_ratio(fg: tuple[int, int, int], bg: tuple[int, int, int]) -> float:
    """WCAG 2.x contrast ratio, 1.0 (identical) to 21.0 (black on white)."""
    l1, l2 = relative_luminance(fg), relative_luminance(bg)
    lighter, darker = max(l1, l2), min(l1, l2)
    return (lighter + 0.05) / (darker + 0.05)


def contrast_floor(font_pt: float, bold: bool) -> float:
    """WCAG large-text exemption: 18pt, or 14pt when bold, drops to 3:1."""
    return 3.0 if font_pt >= 18.0 or (bold and font_pt >= 14.0) else 4.5


# ---------------------------------------------------------------------------
# Geometry helpers
# ---------------------------------------------------------------------------


def _area(box: tuple[float, float, float, float]) -> float:
    return max(box[2], 0.0) * max(box[3], 0.0)


def _intersection(a: tuple[float, float, float, float], b: tuple[float, float, float, float]) -> float:
    ox = min(a[0] + a[2], b[0] + b[2]) - max(a[0], b[0])
    oy = min(a[1] + a[3], b[1] + b[3]) - max(a[1], b[1])
    return max(ox, 0.0) * max(oy, 0.0)


def _contains(outer: tuple[float, float, float, float], inner: tuple[float, float, float, float],
              slack: float = 0.05) -> bool:
    return (outer[0] - slack <= inner[0] and outer[1] - slack <= inner[1]
            and outer[0] + outer[2] + slack >= inner[0] + inner[2]
            and outer[1] + outer[3] + slack >= inner[1] + inner[3])


def estimate_text_block(
    text: str, *, font_pt: float, width_in: float, wrap: bool = True
) -> tuple[float, float]:
    """Estimated ``(width_in, height_in)`` of ``text`` set at ``font_pt``.

    Shares :func:`~.graph_layout.display_width` with the diagram layout so the
    two never disagree about how wide a Han glyph is.
    """
    em_in = font_pt / 72.0
    if not wrap:
        return display_width(text) * em_in, font_pt * LINE_HEIGHT / 72.0
    max_em = max(width_in / em_in, 1.0)
    lines: list[str] = []
    for para in (text or "").splitlines() or [""]:
        lines.extend(wrap_label(para, max_em))
    widest = max((display_width(ln) for ln in lines), default=0.0) * em_in
    return widest, len(lines) * font_pt * LINE_HEIGHT / 72.0


# ---------------------------------------------------------------------------
# PPTX
# ---------------------------------------------------------------------------


def _run_colour(run: Any) -> tuple[int, int, int] | None:
    """Explicit RGB of a run, or None when it inherits from theme or layout.

    python-pptx raises rather than returning a value for a theme-coloured run,
    and an inherited colour is genuinely unknowable without resolving the whole
    theme chain — so an unresolved colour is skipped rather than guessed, which
    would manufacture contrast findings out of nothing.
    """
    try:
        colour = run.font.color
        if colour is None or colour.type is None:
            return None
        rgb = colour.rgb
    except Exception:
        return None
    return (rgb[0], rgb[1], rgb[2]) if rgb is not None else None


def _shape_fill_colour(shape: Any) -> tuple[int, int, int] | None:
    """Solid fill colour, or None when it is inherited, absent or translucent.

    ``fore_color`` raises rather than returning None on a no-fill shape, and a
    fill carrying an ``<a:alpha>`` composites against whatever is behind it —
    so its effective colour is unknowable and a contrast ratio computed from
    the nominal one is fiction. The flowchart emitter fills nodes with the
    accent at 70% transparency, which reads as pale blue and measured 1.9:1
    against the nominal navy.
    """
    from pptx.oxml.ns import qn

    try:
        fill = shape.fill
        if fill.type is None or fill.type != 1:  # MSO_FILL.SOLID
            return None
        if fill._xPr is not None and fill._xPr.findall(".//" + qn("a:alpha")):
            return None
        rgb = fill.fore_color.rgb
    except Exception:
        return None
    return (rgb[0], rgb[1], rgb[2]) if rgb is not None else None


#: Preset geometries that are a stroke rather than a box. A diagonal line's
#: bounding box is as large as the rectangle it spans, so a connector between
#: two nodes "overlaps" both of them by a fifth of their area — measuring
#: thinness cannot tell them apart, but the geometry can.
_STROKE_PRESETS = frozenset({
    "line", "straightConnector1", "bentConnector2", "bentConnector3",
    "bentConnector4", "bentConnector5", "curvedConnector2", "curvedConnector3",
    "curvedConnector4", "curvedConnector5",
})


def _preset(shape: Any) -> str:
    from pptx.oxml.ns import qn

    try:
        geom = shape._element.find(".//" + qn("a:prstGeom"))
        return str(geom.get("prst") or "") if geom is not None else ""
    except Exception:
        return ""


def _is_stroke(shape: Any) -> bool:
    return _preset(shape) in _STROKE_PRESETS


def _is_diagram_node(shape: Any) -> bool:
    """A flowchart / diagram node, whatever size the layout gave it.

    The type floor cannot be decided from the box: the diagram fit widens and
    heightens nodes to fill their slot, which pushed them over the height
    threshold and into the body-copy floor — so a 9pt node label, sized by
    ``graph_layout``'s own MIN_DIAGRAM_FONT_PT, was reported as too small.
    """
    return _preset(shape).startswith("flowChart")


def _has_visible_fill(shape: Any) -> bool:
    try:
        return shape.fill.type is not None and shape.fill.type != 5  # MSO_FILL.BACKGROUND
    except Exception:
        return False


def _run_sizes(shape: Any, default_pt: float = 18.0) -> list[tuple[float, bool, str]]:
    """``(size_pt, bold, text)`` per run, with the paragraph/default fallback."""
    out: list[tuple[float, bool, str]] = []
    for para in shape.text_frame.paragraphs:
        para_pt = para.font.size.pt if para.font.size is not None else None
        for run in para.runs:
            if not run.text.strip():
                continue
            pt = run.font.size.pt if run.font.size is not None else (para_pt or default_pt)
            out.append((pt, bool(run.font.bold), run.text))
    return out


def lint_pptx(path: str | Path) -> tuple[list[Finding], dict[str, Any]]:
    """Geometry, typography and contrast checks read straight off the deck."""
    from pptx import Presentation
    from pptx.util import Emu

    prs = Presentation(str(path))
    canvas_w = Emu(prs.slide_width).inches
    canvas_h = Emu(prs.slide_height).inches
    findings: list[Finding] = []
    shape_total = 0

    for index, slide in enumerate(prs.slides, 1):
        where = f"slide {index}"
        boxes: list[tuple[tuple[float, float, float, float], Any, int]] = []
        for z, shape in enumerate(slide.shapes, 1):
            shape_total += 1
            if shape.left is None or shape.top is None or shape.width is None or shape.height is None:
                continue
            box = (Emu(shape.left).inches, Emu(shape.top).inches,
                   Emu(shape.width).inches, Emu(shape.height).inches)
            boxes.append((box, shape, z))

            x, y, w, h = box
            if x < -0.02 or y < -0.02 or x + w > canvas_w + 0.02 or y + h > canvas_h + 0.02:
                findings.append(Finding(
                    "shape_off_canvas", "critical", where,
                    f"{shape.shape_type} at ({x:.2f}, {y:.2f}) {w:.2f}x{h:.2f}in "
                    f"leaves the {canvas_w:.2f}x{canvas_h:.2f}in canvas",
                    "Move or resize the shape inside the canvas; coordinates past the edge are "
                    "written, not clamped, so the shape is simply not on the slide.",
                    slide=index, shape=z,
                ))

            if not shape.has_text_frame:
                continue
            text = shape.text_frame.text.strip()
            if not text:
                if (_area(box) >= EMPTY_SHAPE_MIN_AREA_IN2
                        and min(w, h) >= EMPTY_SHAPE_MIN_EDGE_IN
                        and not _has_visible_fill(shape)
                        # A diagonal connector's bounding box is as large as
                        # the rectangle it spans and its text frame is empty,
                        # so without this it reads as a hole in the layout.
                        and not _is_stroke(shape)):
                    findings.append(Finding(
                        "empty_text_box", "major", where,
                        f"an empty {w:.2f}x{h:.2f}in text box holds {_area(box):.1f}in² of the slide",
                        "Fill it or delete it — an unfilled placeholder is the dead space a reader "
                        "sees as an unfinished slide.",
                        slide=index, shape=z,
                    ))
                continue

            runs = _run_sizes(shape)
            for pt, bold, run_text in runs:
                # A short box is a label, a chip or a footer, and a diagram
                # node is one whatever its size; none are read as body copy,
                # and the diagram layout is allowed down to
                # MIN_DIAGRAM_FONT_PT for exactly that reason.
                floor = (
                    NOTE_FONT_FLOOR_PT
                    if h < 0.7 or _is_diagram_node(shape)
                    else BODY_FONT_FLOOR_PT
                )
                if pt < floor:
                    findings.append(Finding(
                        "type_too_small", "major", where,
                        f"{pt:.0f}pt run {run_text.strip()[:24]!r} (floor {floor:.0f}pt)",
                        f"Raise the run to at least {floor:.0f}pt.",
                        slide=index, shape=z,
                    ))
                fg = None
                for para in shape.text_frame.paragraphs:
                    for run in para.runs:
                        if run.text == run_text:
                            fg = _run_colour(run)
                            break
                bg = _shape_fill_colour(shape)
                if fg and bg:
                    ratio = contrast_ratio(fg, bg)
                    need = contrast_floor(pt, bold)
                    if ratio < need:
                        findings.append(Finding(
                            "low_contrast", "major", where,
                            f"{ratio:.1f}:1 for {pt:.0f}pt text (WCAG needs {need:.1f}:1)",
                            "Darken the text or lighten the fill until the ratio clears the floor.",
                            slide=index, shape=z,
                        ))

            if runs:
                biggest = max(pt for pt, _, _ in runs)
                # Read the real insets rather than assuming PowerPoint's
                # defaults: the HTML path writes margin-0 boxes sized to the
                # browser's line box, and charging those the default 0.05in
                # top and bottom reports every heading it makes as overflowing.
                frame = shape.text_frame
                inset_w = sum(
                    (m.inches if m is not None else 0.1)
                    for m in (frame.margin_left, frame.margin_right)
                )
                inset_h = sum(
                    (m.inches if m is not None else 0.05)
                    for m in (frame.margin_top, frame.margin_bottom)
                )
                needed_w, needed_h = estimate_text_block(
                    text, font_pt=biggest, width_in=max(w - inset_w, 0.2),
                    wrap=shape.text_frame.word_wrap is not False,
                )
                if shape.text_frame.auto_size is None or int(getattr(shape.text_frame.auto_size, "real", 0) or 0) != 1:
                    ratio = needed_h / max(h - inset_h, 0.05)
                    if ratio > OVERFLOW_WARN:
                        # Only a filled shape clips its text; a bare text box
                        # spills, which is ugly rather than lossy.
                        blocking = ratio > OVERFLOW_BLOCK and _has_visible_fill(shape)
                        findings.append(Finding(
                            "text_overflow", "critical" if blocking else "major", where,
                            f"{needed_h:.2f}in of text at {biggest:.0f}pt in a {h:.2f}in box "
                            f"({ratio:.0%} of the room, {text[:24]!r})",
                            "Shorten the text, enlarge the box, or drop the type size.",
                            slide=index, shape=z,
                        ))
                    elif needed_w > max(w - inset_w, 0.2) * OVERFLOW_WARN and shape.text_frame.word_wrap is False:
                        findings.append(Finding(
                            "text_overflow", "major", where,
                            f"{needed_w:.2f}in of unwrapped text in a {w:.2f}in box",
                            "Enable word wrap or widen the box.",
                            slide=index, shape=z,
                        ))

        for i, (box_a, shape_a, z_a) in enumerate(boxes):
            for box_b, shape_b, z_b in boxes[i + 1:]:
                overlap = _intersection(box_a, box_b)
                if overlap <= 0:
                    continue
                if _contains(box_a, box_b) or _contains(box_b, box_a):
                    continue  # deliberate layering: a card behind its own text
                smaller = min(_area(box_a), _area(box_b))
                if smaller <= 0 or overlap / smaller <= OVERLAP_TOLERANCE:
                    continue
                # Rules, dividers and connectors are drawn under or between the
                # shapes they join, so their overlap is the design.
                if (min(box_a[2], box_a[3]) < HAIRLINE_IN
                        or min(box_b[2], box_b[3]) < HAIRLINE_IN
                        or _is_stroke(shape_a) or _is_stroke(shape_b)):
                    continue
                findings.append(Finding(
                    "shape_overlap", "major", where,
                    f"{shape_a.shape_type} and {shape_b.shape_type} overlap by "
                    f"{overlap / smaller:.0%} of the smaller shape",
                    "Separate them — overlapping filled shapes read as a layout error, not a design.",
                    slide=index, shape=z_b,
                ))

    return findings, {
        "canvas_in": [round(canvas_w, 3), round(canvas_h, 3)],
        "slides": len(prs.slides._sldIdLst),
        "shapes": shape_total,
    }


# ---------------------------------------------------------------------------
# Renders
# ---------------------------------------------------------------------------


#: A background this far from white is a deliberate panel, not paper.
FULL_BLEED_DELTA = 26


@dataclass
class PageInk:
    coverage: float
    ink: float
    dead_rows: float
    dead_band: float
    top: float
    bottom: float
    left: float
    right: float
    #: True when the page's own background is a colour rather than paper. The
    #: ink metric then measures only what sits *on* the panel, so a cover slide
    #: whose whole canvas is filled reads as 11% covered — which is how a
    #: correct full-bleed cover burned a repair round.
    full_bleed: bool = False


def page_ink(path: str | Path, *, threshold: int = 18) -> PageInk:
    """Where the ink is on one rendered page.

    The background is the modal value of the outer border ring rather than the
    four corners — a corner can land inside a full-bleed panel, and one wrong
    corner inverts every number downstream.
    """
    from PIL import Image

    im = Image.open(str(path)).convert("L")
    w, h = im.size
    px = im.load()
    ring = Counter()
    for x in range(0, w, max(w // 200, 1)):
        ring[px[x, 0]] += 1
        ring[px[x, h - 1]] += 1
    for y in range(0, h, max(h // 200, 1)):
        ring[px[0, y]] += 1
        ring[px[w - 1, y]] += 1
    bg = ring.most_common(1)[0][0]
    full_bleed = abs(bg - 255) > FULL_BLEED_DELTA

    step = max(min(w, h) // 400, 1)
    rows = h // step + 1
    row_ink = [0] * rows
    ink = total = 0
    minx, miny, maxx, maxy = w, h, -1, -1
    for y in range(0, h, step):
        for x in range(0, w, step):
            total += 1
            if abs(px[x, y] - bg) > threshold:
                ink += 1
                row_ink[y // step] += 1
                minx, miny = min(minx, x), min(miny, y)
                maxx, maxy = max(maxx, x), max(maxy, y)
    if maxx < 0:
        return PageInk(0.0, 0.0, 1.0, 1.0, 1.0, 0.0, 1.0, 0.0, full_bleed)

    longest = run = 0
    for count in row_ink:
        run = run + 1 if count == 0 else 0
        longest = max(longest, run)
    return PageInk(
        coverage=((maxx - minx) * (maxy - miny)) / float(w * h),
        ink=ink / float(total or 1),
        dead_rows=sum(1 for c in row_ink if c == 0) / float(rows),
        dead_band=longest / float(rows),
        top=miny / h, bottom=maxy / h, left=minx / w, right=maxx / w,
        full_bleed=full_bleed,
    )


def lint_renders(
    paths: Sequence[str | Path], *, label: str = "slide"
) -> tuple[list[Finding], dict[str, Any]]:
    """Report where the ink is. Deliberately produces no findings.

    A check earns its place here when a pass means "nothing is provably
    broken", and loses it the moment a pass could be read as "this is good".
    Every occupancy measure fails that test: each one can be passed by padding.
    """
    findings: list[Finding] = []
    pages: list[PageInk] = []
    for index, path in enumerate(paths, 1):
        try:
            metrics = page_ink(path)
        except Exception:
            continue
        pages.append(metrics)
    # No findings from this stage any more. Coverage, overfill and dead-band
    # were all measures of pixel occupancy, and occupancy is a proxy for effort
    # that is satisfied by making shapes bigger — which is exactly what
    # produced the deck its author called empty. Tested over 98 reference
    # slides: `canvas_underfilled` ranked our emptiest slide (0.81, three cards
    # holding 45 characters each) above the reference set's densest statutory
    # slide (0.66, ten articles with a holding for each), because a pale 6.7in
    # card is ink; `canvas_overfilled` fired four times and all four were
    # correct work; `dead_band` sat 0.01 from flagging eleven correct section
    # dividers, and its advice — fill the height — is the opposite of what a
    # divider wants. The numbers are still reported, because 0.08 is how the
    # degraded fallback writer looks, but nothing is graded on them.
    measured = [p for p in pages if not p.full_bleed]
    metrics: dict[str, Any] = {f"{label}s_measured": len(pages)}
    if len(measured) != len(pages):
        metrics["full_bleed_pages"] = len(pages) - len(measured)
    if measured:
        metrics |= {
            "coverage_mean": round(sum(p.coverage for p in measured) / len(measured), 3),
            "coverage_min": round(min(p.coverage for p in measured), 3),
            "dead_band_max": round(max(p.dead_band for p in measured), 3),
        }
    return findings, metrics


# ---------------------------------------------------------------------------
# Diagram encoding
# ---------------------------------------------------------------------------

def diagram_encoding(fc: dict[str, Any]) -> dict[str, Any]:
    """Count the dimensions of meaning a diagram carries, and report them.

    Sequence is dimension zero and free. Reported, never graded: a model that
    can see its own drawing is a chain of identical boxes does not need a
    threshold to be told so, and any threshold I picked would be my taste
    dressed up as arithmetic.
    """
    nodes = list(fc.get("nodes") or [])
    edges = list(fc.get("edges") or [])
    dimensions: list[str] = []
    if len(fc.get("lanes") or []) >= 2:
        dimensions.append("lanes")
    if len(fc.get("groups") or []) >= 1:
        dimensions.append("groups")
    if len({(n.get("role") or "") for n in nodes} - {""}) >= 2:
        dimensions.append("roles")
    if len({(n.get("fill") or "") for n in nodes} - {""}) >= 2:
        dimensions.append("fills")
    if any((e.get("label") or "").strip() for e in edges):
        dimensions.append("edge_labels")
    if len({(e.get("style") or "solid") for e in edges}) >= 2:
        dimensions.append("line_styles")
    if any((n.get("sub") or "").strip() for n in nodes):
        dimensions.append("deliverables")
    if len({(n.get("kind") or "process") for n in nodes}) >= 2:
        dimensions.append("node_kinds")
    return {
        "nodes": len(nodes),
        "edges": len(edges),
        "encodes": dimensions,
    }


# ---------------------------------------------------------------------------
# DOCX
# ---------------------------------------------------------------------------


def _effective_first_line_indent(para: Any, styles: Any) -> float:
    """First-line indent in twips, resolved through the style chain.

    Reading only direct formatting is how this check scored zero on a memo
    whose table cells provably wrapped a statute title across two lines: the
    indent lived on ``Normal``, and every heading and cell paragraph inherited
    it through ``basedOn`` without carrying a ``w:ind`` of its own.
    """
    from docx.oxml.ns import qn

    def own(element: Any) -> float | None:
        pPr = element.find(qn("w:pPr")) if element is not None else None
        node = pPr.find(qn("w:ind")) if pPr is not None else None
        if node is None:
            return None
        chars = node.get(qn("w:firstLineChars"))
        if chars is not None:
            return float(chars) / 100.0 * 240.0  # hundredths of a char → twips at 12pt
        raw = node.get(qn("w:firstLine"))
        return float(raw) if raw is not None else None

    direct = own(para._p)
    if direct is not None:
        return direct
    style = para.style
    seen = set()
    while style is not None and style.style_id not in seen:
        seen.add(style.style_id)
        value = own(style.element)
        if value is not None:
            return value
        style = getattr(style, "base_style", None)
    try:
        return own(styles["Normal"].element) or 0.0
    except Exception:
        return 0.0


def lint_docx(path: str | Path) -> tuple[list[Finding], dict[str, Any]]:
    """Checks for the defects that actually show up in generated memos."""
    from docx import Document
    from docx.oxml.ns import qn


    doc = Document(str(path))
    findings: list[Finding] = []

    for t_index, table in enumerate(doc.tables, 1):
        where = f"table {t_index}"
        indented = 0
        for row in table.rows:
            for cell in row.cells:
                for para in cell.paragraphs:
                    if _effective_first_line_indent(para, doc.styles) > 0:
                        indented += 1
        if indented:
            findings.append(Finding(
                "cell_first_line_indent", "major", where,
                f"{indented} cell paragraph(s) carry a first-line indent",
                "Zero w:ind on paragraphs inside w:tbl — a cell is one short field, and the indent "
                "pushes its text into a second line.",
            ))

        grid = table._tbl.find(qn("w:tblGrid"))
        widths = [int(c.get(qn("w:w")) or 0) for c in grid.findall(qn("w:gridCol"))] if grid is not None else []
        if len(widths) > 1 and len(set(widths)) == 1:
            demand = []
            for col in range(len(widths)):
                longest = 0.0
                for row in table.rows:
                    if col < len(row.cells):
                        longest = max(longest, display_width(row.cells[col].text.strip()))
                demand.append(longest or 1.0)
            if max(demand) / max(min(demand), 1e-6) >= 2.5:
                findings.append(Finding(
                    "table_columns_unweighted", "major", where,
                    "every column has the same width while content demand varies "
                    f"{max(demand) / max(min(demand), 1e-6):.1f}x",
                    "Weight w:gridCol by the CJK-aware content width so long citations get the room "
                    "and short codes do not hoard it.",
                ))

    headings = [p for p in doc.paragraphs if (p.style.name or "").lower().startswith("heading")]
    for index, para in enumerate(headings):
        if _effective_first_line_indent(para, doc.styles) > 0:
            findings.append(Finding(
                "heading_indent", "minor", f"heading {index + 1}",
                f"{para.text.strip()[:20]!r} carries a first-line indent",
                "Headings sit on the margin; the CJK body indent must not reach them.",
            ))

    body = [p for p in doc.paragraphs if p.text.strip()]
    for a, b in zip(body, body[1:]):
        if ((a.style.name or "").lower().startswith("heading")
                and (b.style.name or "").lower().startswith("heading")
                and (a.style.name or "") == (b.style.name or "")):
            findings.append(Finding(
                "empty_section", "minor", a.text.strip()[:24],
                "a heading is immediately followed by a sibling heading with no body",
                "Write the section or drop the heading.",
            ))

    return findings, {"tables": len(doc.tables), "paragraphs": len(doc.paragraphs)}


# ---------------------------------------------------------------------------
# Entry point
# ---------------------------------------------------------------------------


def lint_artifact(
    path: str | Path,
    renders: Sequence[str | Path] | None = None,
    *,
    render_label: str = "slide",
) -> QualityReport:
    """Lint whatever ``path`` is, plus its renders when they are available.

    Every check is best effort: a linter that can fail a document write would
    be worse than no linter, so an unreadable file yields a report saying so
    rather than an exception.
    """
    path = Path(path)
    findings: list[Finding] = []
    metrics: dict[str, Any] = {}
    suffix = path.suffix.lower()

    try:
        if suffix == ".pptx":
            f, m = lint_pptx(path)
            findings += f
            metrics |= m
        elif suffix == ".docx":
            f, m = lint_docx(path)
            findings += f
            metrics |= m
    except Exception as exc:
        metrics["file_check_error"] = f"{type(exc).__name__}: {exc}"

    if renders:
        try:
            f, m = lint_renders(renders, label=render_label)
            findings += f
            metrics |= m
        except Exception as exc:
            metrics["render_check_error"] = f"{type(exc).__name__}: {exc}"

    return QualityReport(findings=findings, metrics=metrics)


__all__ = [
    "BODY_FONT_FLOOR_PT",
    "CANVAS_COVERAGE_REPORT_ONLY",
    "FULL_BLEED_DELTA",
    "Finding",
    "HAIRLINE_IN",
    "RENDER_CHECKS",
    "PageInk",
    "QualityReport",
    "Severity",
    "contrast_floor",
    "contrast_ratio",
    "diagram_encoding",
    "estimate_text_block",
    "lint_artifact",
    "lint_docx",
    "lint_pptx",
    "lint_renders",
    "page_ink",
    "relative_luminance",
]
