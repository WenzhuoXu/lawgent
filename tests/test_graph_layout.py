"""Diagram geometry has to be laid out, not scraped back out of a picture.

The Mermaid path rendered an SVG in headless Chromium only to read node
centres out of it, then scaled those centres independently in x and y:
fixed-size nodes collided on one axis while floating apart on the other, arrows
ran corner-to-corner straight through the boxes they joined, and a seven-step
chain occupied a quarter of a 16:9 slide because nothing chose its direction.
These tests pin the properties that replaced it — no collisions, a drawing that
fills the box it was given, a type size that answers to the box, routed edges
that end where the arrowhead goes, and a fallback that still draws arrows on a
machine with no Graphviz.
"""

from __future__ import annotations

import pytest

from legal_helper.documents import graph_layout
from legal_helper.documents.graph_layout import (
    MAX_DIAGRAM_FONT_PT,
    MIN_DIAGRAM_FONT_PT,
    GraphvizNotInstalled,
    display_width,
    graphviz_available,
    layout_graph,
    layout_mindmap,
    node_size_for,
    outline_to_graph,
    wrap_label,
)

#: The content box of a 13.333x7.5in slide under a title.
SLIDE_BOX = (0.5, 1.2, 12.3, 5.5)
#: The same slide with a chart above it: wide enough for anything, too shallow
#: for a column.
SHALLOW_BOX = (0.5, 4.6, 12.3, 1.8)

#: Inches of slack allowed against a box edge, to absorb the float error of
#: points -> inches -> uniform scale.
EPS = 0.02

_needs_dot = pytest.mark.skipif(
    not graphviz_available(), reason="graphviz (dot) is not installed"
)


def _chain(count: int, label: str = "Step {} of the review") -> tuple[list[dict], list[dict]]:
    return (
        [{"id": f"n{i}", "text": label.format(i)} for i in range(count)],
        [{"from_id": f"n{i}", "to_id": f"n{i + 1}"} for i in range(count - 1)],
    )


def _decision_flow() -> tuple[list[dict], list[dict]]:
    """A shape of every kind, so the inflation table is exercised too."""
    return (
        [
            {"id": "s", "text": "受理申请", "kind": "terminator"},
            {"id": "d", "text": "材料是否齐备", "kind": "decision"},
            {"id": "y", "text": "进入实质审查", "kind": "process"},
            {"id": "n", "text": "一次性告知补正", "kind": "io"},
            {"id": "r", "text": "登记簿", "kind": "data"},
            {"id": "e", "text": "作出决定", "kind": "terminator"},
        ],
        [
            {"from_id": "s", "to_id": "d"},
            {"from_id": "d", "to_id": "y", "label": "是"},
            {"from_id": "d", "to_id": "n", "label": "否"},
            {"from_id": "n", "to_id": "d", "style": "dashed"},
            {"from_id": "y", "to_id": "r"},
            {"from_id": "y", "to_id": "e"},
        ],
    )


def _fan_out() -> tuple[list[dict], list[dict]]:
    return (
        [{"id": "root", "text": "Gate", "kind": "decision"}]
        + [{"id": f"c{i}", "text": f"Branch {i}"} for i in range(6)],
        [{"from_id": "root", "to_id": f"c{i}"} for i in range(6)],
    )


def _worst_overlap(layout) -> float:
    """Largest pairwise node intersection, in square inches."""
    worst = 0.0
    for i, a in enumerate(layout.nodes):
        for b in layout.nodes[i + 1:]:
            wide = min(a.x + a.w, b.x + b.w) - max(a.x, b.x)
            high = min(a.y + a.h, b.y + b.h) - max(a.y, b.y)
            if wide > 0 and high > 0:
                worst = max(worst, wide * high)
    return worst


def _covered(layout, box) -> float:
    """Fraction of the box's area the drawing's extent takes up."""
    return (layout.extent[2] * layout.extent[3]) / (box[2] * box[3])


@_needs_dot
@pytest.mark.parametrize(
    "graph,box",
    [
        (_chain(3), SLIDE_BOX),
        (_chain(6), SLIDE_BOX),
        (_chain(9), SLIDE_BOX),
        (_chain(6), SHALLOW_BOX),
        (_decision_flow(), SLIDE_BOX),
        (_fan_out(), SLIDE_BOX),
        (_fan_out(), SHALLOW_BOX),
    ],
)
def test_no_two_laid_out_nodes_ever_overlap(graph, box):
    """The regression: independent x/y scaling collided fixed-size boxes."""
    layout = layout_graph(*graph, box=box)
    assert layout.nodes
    assert _worst_overlap(layout) == 0.0


@_needs_dot
@pytest.mark.parametrize(
    "graph,box",
    [
        (_chain(6), SLIDE_BOX),
        (_chain(9), SLIDE_BOX),
        (_chain(6), SHALLOW_BOX),
        (_decision_flow(), SLIDE_BOX),
        (_fan_out(), SLIDE_BOX),
    ],
)
def test_the_drawing_stays_inside_its_box_and_its_binding_side_fills_it(graph, box):
    bx, by, bw, bh = box
    layout = layout_graph(*graph, box=box)
    for node in layout.nodes:
        assert node.x >= bx - EPS and node.y >= by - EPS
        assert node.x + node.w <= bx + bw + EPS
        assert node.y + node.h <= by + bh + EPS
    for edge in layout.edges:
        for x, y in edge.points:
            assert bx - EPS <= x <= bx + bw + EPS
            assert by - EPS <= y <= by + bh + EPS
    ex, ey, ew, eh = layout.extent
    assert ex >= bx - EPS and ey >= by - EPS
    # One side binds and the other is free; the bound one has to be nearly
    # full, or the diagram is floating in dead canvas.
    assert max(ew / bw, eh / bh) >= 0.8


@_needs_dot
def test_the_type_size_comes_down_when_the_graph_outgrows_the_box():
    asked = 20.0
    layout = layout_graph(*_chain(6), box=SLIDE_BOX, font_pt=asked)
    assert layout.font_pt < asked
    assert MIN_DIAGRAM_FONT_PT <= layout.font_pt <= MAX_DIAGRAM_FONT_PT


@_needs_dot
def test_the_type_size_comes_up_when_the_graph_is_lost_in_the_box():
    asked = 10.0
    layout = layout_graph(*_chain(2), box=SLIDE_BOX, font_pt=asked)
    assert layout.font_pt > asked
    assert layout.font_pt == MAX_DIAGRAM_FONT_PT
    # Two boxes cannot fill a 16:9 slide at a type size anyone would call a
    # diagram, so the ceiling binds instead of the box.
    assert _covered(layout, SLIDE_BOX) < 1.0


@_needs_dot
@pytest.mark.parametrize(
    "graph,box,asked",
    [
        (_chain(2), SLIDE_BOX, 12.0),
        (_chain(4), SLIDE_BOX, 12.0),
        (_chain(6), SLIDE_BOX, 12.0),
        (_decision_flow(), SLIDE_BOX, 12.0),
        (_fan_out(), SLIDE_BOX, 9.0),
        (_fan_out(), SLIDE_BOX, 24.0),
    ],
)
def test_a_fitted_type_size_stays_in_the_readable_range(graph, box, asked):
    layout = layout_graph(*graph, box=box, font_pt=asked)
    assert MIN_DIAGRAM_FONT_PT <= layout.font_pt <= MAX_DIAGRAM_FONT_PT


@_needs_dot
def test_a_graph_too_dense_to_set_at_the_floor_holds_the_floor():
    """Density is handed to the linter, not paid for in unreadable type.

    A twelve-step column cannot be set at MIN_DIAGRAM_FONT_PT inside 5.5in.
    The geometry still scales the last few percent so the drawing stays in its
    box, but the reported type size holds at the floor and the emitter's
    shrink-to-fit absorbs the residual — multiplying the floor by that scale
    put 7.8pt labels in a shipped deck, under ``quality``'s own 9pt check.
    """
    layout = layout_graph(*_chain(12), box=SLIDE_BOX, font_pt=12.0)
    assert _worst_overlap(layout) == 0.0
    assert layout.extent[3] <= SLIDE_BOX[3] + EPS
    assert layout.font_pt == pytest.approx(MIN_DIAGRAM_FONT_PT)


@_needs_dot
@pytest.mark.parametrize("graph", [_chain(6), _fan_out()])
@pytest.mark.parametrize("box", [SLIDE_BOX, SHALLOW_BOX])
def test_an_unset_rankdir_picks_whichever_direction_fills_the_box(graph, box):
    """Guessing TB is what left a chain drawn down a column of dead canvas."""
    auto = layout_graph(*graph, box=box)
    scored = {
        direction: _covered(layout_graph(*graph, box=box, rankdir=direction), box)
        for direction in ("TB", "LR")
    }
    better, worse = sorted(scored, key=scored.__getitem__, reverse=True)
    # The direction is scored before the type size is re-fitted around it, so
    # the pick can land a few percent under the best final coverage. What it
    # must never do is take the direction that plainly wastes the box.
    assert _covered(auto, box) >= scored[worse]
    assert _covered(auto, box) >= 0.9 * scored[better]


#: The right-hand column of a slide whose left half carries the takeaways.
TALL_BOX = (7.0, 1.2, 5.5, 5.5)


@_needs_dot
def test_an_unset_rankdir_follows_the_box_rather_than_a_convention():
    """A chain goes on its side unless the box is the one that is tall.

    Measured: a six-step chain drawn top-down in a 12.3x5.5in box fills 15% of
    it and drawn left-to-right fills 27%; in a 5.5x5.5in column those numbers
    invert to 65% and 27%. So there is no default direction that is right —
    only the box can say, which is why "auto" fits both in full and keeps the
    one that achieved more.
    """
    assert layout_graph(*_chain(6), box=SHALLOW_BOX).rankdir == "LR"
    assert layout_graph(*_chain(6), box=SLIDE_BOX).rankdir == "LR"
    assert layout_graph(*_chain(6), box=TALL_BOX).rankdir == "TB"


@_needs_dot
def test_every_edge_is_routed_from_its_source_to_its_arrowhead():
    graph = _decision_flow()
    layout = layout_graph(*graph, box=SLIDE_BOX)
    boxes = {n.id: n for n in layout.nodes}
    assert len(layout.edges) == len(graph[1])

    def touches(node, point) -> bool:
        x, y = point
        return (node.x - 0.08 <= x <= node.x + node.w + 0.08
                and node.y - 0.08 <= y <= node.y + node.h + 0.08)

    for edge in layout.edges:
        assert len(edge.points) >= 2
        assert touches(boxes[edge.from_id], edge.points[0])
        # The last point IS the arrowhead, so the emitter can put one
        # arrowhead on the final segment and leave the rest plain.
        assert touches(boxes[edge.to_id], edge.points[-1])
        if edge.label:
            assert edge.label_x is not None and edge.label_y is not None
            assert SLIDE_BOX[0] <= edge.label_x <= SLIDE_BOX[0] + SLIDE_BOX[2]
            assert SLIDE_BOX[1] <= edge.label_y <= SLIDE_BOX[1] + SLIDE_BOX[3]
        else:
            assert edge.label_x is None and edge.label_y is None
    assert any(e.style == "dashed" for e in layout.edges)


@_needs_dot
def test_a_straight_edge_is_two_points_not_a_sampled_curve():
    """Five shapes per straight arrow is what made the old decks slow to open."""
    layout = layout_graph(*_chain(5), box=SLIDE_BOX, rankdir="TB")
    assert [len(e.points) for e in layout.edges] == [2] * 4


@_needs_dot
def test_an_edge_to_a_missing_node_is_dropped_rather_than_drawn():
    nodes, edges = _chain(3)
    edges.append({"from_id": "n2", "to_id": "nowhere"})
    layout = layout_graph(nodes, edges, box=SLIDE_BOX)
    assert {(e.from_id, e.to_id) for e in layout.edges} == {("n0", "n1"), ("n1", "n2")}


def test_cjk_labels_are_measured_as_double_width():
    """A Han glyph is one em; sizing it as Latin is how labels overran boxes."""
    han, latin = "合同当事人应当遵循", "abcdefghi"
    assert display_width(han) == 2 * display_width(latin)
    han_w, han_h, _ = node_size_for(han)
    latin_w, latin_h, _ = node_size_for(latin)
    assert han_w > latin_w
    assert han_h == latin_h  # one line either way
    # Wrapping follows the same measure: half as many Han glyphs per line.
    assert len(wrap_label("合" * 40, 10.0)) == 4
    assert len(wrap_label("a" * 40, 10.0)) == 2


def test_a_long_label_wraps_instead_of_widening_the_box_without_limit():
    text = "The lessee shall maintain the register in accordance with the rules"
    w, h, lines = node_size_for(text, font_pt=12.0, max_w_in=2.4)
    assert w <= 2.4
    assert len(lines) > 1
    assert h > node_size_for("Short", font_pt=12.0)[1]


def test_stack_layout_still_draws_a_diagram_when_graphviz_is_absent(monkeypatch):
    """A machine with no engine gets a column of arrows, not a pile of boxes.

    Replacing the module attribute is enough: ``layout_graph`` resolves
    ``find_engine`` by name on every call, and the real function keeps its
    lru_cache, which monkeypatch restores untouched.
    """
    monkeypatch.setattr(graph_layout, "find_engine", lambda engine="dot": None)
    assert graph_layout.graphviz_available() is False

    with pytest.raises(GraphvizNotInstalled) as raised:
        layout_graph(*_chain(3), box=SLIDE_BOX)
    assert "graphviz" in str(raised.value)

    layout = layout_mindmap("# Root\n  - One\n  - Two\n  - Three\n", box=SLIDE_BOX)
    assert layout.engine == "stack"
    assert len(layout.nodes) == 4
    assert _worst_overlap(layout) == 0.0
    assert len(layout.edges) == 3
    for edge in layout.edges:
        assert len(edge.points) == 2
        assert edge.points[0][1] < edge.points[1][1]  # top-down, source first
    for node in layout.nodes:
        assert node.x >= SLIDE_BOX[0] - EPS
        assert node.x + node.w <= SLIDE_BOX[0] + SLIDE_BOX[2] + EPS


def test_outline_to_graph_nests_headings_and_two_space_bullets():
    nodes, edges = outline_to_graph(
        "# 合同审查\n"
        "## 主体资格\n"
        "  - 营业执照\n"
        "    - 有效期\n"
        "## 履约能力\n"
    )
    assert [n["text"] for n in nodes] == [
        "合同审查", "主体资格", "营业执照", "有效期", "履约能力",
    ]
    by_text = {n["id"]: n["text"] for n in nodes}
    assert {(by_text[e["from_id"]], by_text[e["to_id"]]) for e in edges} == {
        ("合同审查", "主体资格"),
        ("主体资格", "营业执照"),
        ("营业执照", "有效期"),
        ("合同审查", "履约能力"),
    }
    # A mindmap edge is a tie, not a step, so it carries no arrowhead.
    assert all(e["arrow"] == "none" for e in edges)


def test_an_empty_outline_yields_an_empty_layout():
    assert outline_to_graph("") == ([], [])
    empty = layout_mindmap("\n  \n", box=SLIDE_BOX)
    assert empty.nodes == [] and empty.edges == []


@_needs_dot
def test_as_slide_dicts_hands_the_emitter_rounded_inches():
    layout = layout_graph(*_decision_flow(), box=SLIDE_BOX)
    nodes, edges = layout.as_slide_dicts()
    assert len(nodes) == len(layout.nodes) and len(edges) == len(layout.edges)
    assert {"id", "kind", "text", "x", "y", "w", "h", "fill", "stroke"} == set(nodes[0])
    assert all(isinstance(p, list) and len(p) == 2 for p in edges[0]["points"])
    labelled = next(e for e in edges if e["label"])
    assert labelled["label_x"] is not None
    unlabelled = next(e for e in edges if not e["label"])
    assert unlabelled["label_x"] is None


# ---------------------------------------------------------------------------
# Mermaid edge labels must not become nodes
# ---------------------------------------------------------------------------


def test_inline_edge_labels_do_not_become_phantom_nodes():
    """`A -- yes --> B` must yield two nodes, not four.

    `_strip_constructs` stripped only the pipe form, and its asymmetric-shape
    alternative `>.+?\\]` matched across the arrow — consuming `> B[Accept]`
    and leaving `A -- yes --` with no arrow and no target, so the caller's
    edge-collapsing pass no-oped and the label was promoted to a process box.
    A four-node decision tree came back with six nodes, two of them the words
    "yes" and "no". ASCII-only, which is why a Chinese test suite missed it.
    """
    from legal_helper.documents.diagrams import parse_mermaid_flowchart

    nodes, edges = parse_mermaid_flowchart(
        "flowchart TD\n"
        "  A{Complete?} -- yes --> B[Accept]\n"
        "  A -- no --> C[Return]\n"
        "  B --> D[Grant]"
    )
    assert [n.id for n in nodes] == ["A", "B", "C", "D"]
    assert {n.id for n in nodes}.isdisjoint({"yes", "no"})
    assert [(e.from_id, e.to_id, e.label) for e in edges] == [
        ("A", "B", "yes"),
        ("A", "C", "no"),
        ("B", "D", ""),
    ]


@pytest.mark.parametrize(
    "source,label",
    [
        ("flowchart LR\n  A[X] -->|pipe| B[Y]", "pipe"),
        ("flowchart LR\n  A[X] -- inline --> B[Y]", "inline"),
        ("flowchart LR\n  A[X] -. dotted .-> B[Y]", "dotted"),
        ("flowchart LR\n  A[X] == thick ==> B[Y]", "thick"),
    ],
)
def test_every_labelled_edge_form_keeps_two_nodes_and_its_label(source, label):
    from legal_helper.documents.diagrams import parse_mermaid_flowchart

    nodes, edges = parse_mermaid_flowchart(source)
    assert [n.id for n in nodes] == ["A", "B"]
    assert len(edges) == 1 and edges[0].label == label


def test_asymmetric_shape_still_parses():
    """The `>text]` shape must survive the arrowhead-aware strip."""
    from legal_helper.documents.diagrams import parse_mermaid_flowchart

    nodes, edges = parse_mermaid_flowchart("flowchart LR\n  A>Note] --> B[Next]")
    assert [n.id for n in nodes] == ["A", "B"]
    assert len(edges) == 1


# ---------------------------------------------------------------------------
# A failed diagram must be loud, and must not take the deck down with it
# ---------------------------------------------------------------------------


def test_one_unlayoutable_diagram_does_not_abort_the_other_slides():
    from legal_helper.documents import diagrams

    slides = [
        {"layout": "content", "title": "bad", "flowchart": {"kind": "explode"}},
        {
            "layout": "content",
            "title": "good",
            "flowchart": {
                "kind": "flow",
                "mermaid": "flowchart LR\n  A[One] --> B[Two]",
            },
        },
    ]

    real = diagrams.autolayout_flowchart

    def _boom(fc):
        if fc.get("kind") == "explode":
            raise RuntimeError("no such kind")
        return real(fc)

    diagrams.autolayout_flowchart = _boom
    try:
        failures = diagrams.prepare_flowchart_slides(slides)
    finally:
        diagrams.autolayout_flowchart = real

    assert len(failures) == 1 and failures[0].startswith("slide 1:")
    # The failed slide is emptied rather than left with half-built geometry...
    assert slides[0]["flowchart"]["nodes"] == []
    # ...and the later, valid slide still got laid out.
    assert len(slides[1]["flowchart"]["nodes"]) == 2


def test_a_missing_diagram_is_reported_as_degraded(tmp_path, monkeypatch):
    """write_pptx must not return a bare success path for a diagram-less deck."""
    from legal_helper.documents import diagrams
    from legal_helper.tools import documents as doc_tools

    monkeypatch.setattr(
        diagrams, "prepare_flowchart_slides", lambda payload: ["slide 1: RuntimeError: nope"]
    )
    monkeypatch.setattr(
        doc_tools, "write_pptx_deck", lambda *a, **k: {"renderer": "pptxgenjs"}
    )
    monkeypatch.setattr(doc_tools, "_output_path", lambda *a, **k: tmp_path / "d.pptx")

    fn = getattr(doc_tools.write_pptx, "func", doc_tools.write_pptx)
    result = fn(filename="d.pptx", title="T", slides=[])
    assert result.startswith("ERROR")
    assert "DEGRADED" in result
    assert "slide 1" in result
