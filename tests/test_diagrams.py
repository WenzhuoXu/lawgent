"""Diagram pictures: Mermaid parsed here and drawn by Graphviz, not a browser.

A diagram on a slide is ppt-master's job inside a deck; what is left here is a
PNG or SVG for a Word document or a chat reply. The common sources — Mermaid
flowcharts and mind maps — used to need Node and headless Chrome through
mermaid-cli. These tests pin the replacement: the parser keeps every node,
edge and label a model writes, the DOT carries them, Graphviz draws them with
a CJK font, and only the Mermaid types Graphviz cannot draw go to mermaid-cli.
"""

from __future__ import annotations

import pytest

from legal_helper.documents import diagrams, graph_layout
from legal_helper.documents.diagrams import (
    flowchart_to_dot,
    mermaid_direction,
    mermaid_kind,
    mindmap_to_dot,
    parse_mermaid_flowchart,
    parse_mermaid_mindmap,
    parse_mermaid_subgraphs,
    render_mermaid_picture,
)
from legal_helper.documents.graph_layout import display_width, wrap_label

_needs_dot = pytest.mark.skipif(
    not graph_layout.graphviz_available(), reason="graphviz (dot) is not installed"
)

PROCESS = """flowchart LR
  subgraph legal [法务部]
    A([数据出境盘点]) --> B[个人信息保护影响评估]
  end
  subgraph ops [产品与人力资源]
    C[更新告知书] --> D{是否以同意为处理依据?}
  end
  B --> C
  D -->|是| E[取得单独同意]
  D -- 否 --> F[履行告知义务]
  E --> G[签署标准合同] --> H[[10个工作日内备案]]
  F --> G
  H -.-> I>每季度复核]
"""

MINDMAP = """mindmap
  root((个人信息出境))
    安全评估
      重要数据
    标准合同
         10万至100万人
    豁免
      合同履行
"""


# ---------------------------------------------------------------------------
# The parser keeps what the author wrote
# ---------------------------------------------------------------------------


def test_inline_edge_labels_do_not_become_phantom_nodes():
    """`A -- yes --> B` must yield two nodes, not four.

    `_strip_constructs` stripped only the pipe form, and its asymmetric-shape
    alternative `>.+?\\]` matched across the arrow — consuming `> B[Accept]`
    and leaving `A -- yes --` with no arrow and no target, so the label was
    promoted to a process box. ASCII-only, which is why a Chinese test suite
    missed it.
    """
    nodes, edges = parse_mermaid_flowchart(
        "flowchart TD\n"
        "  A{Complete?} -- yes --> B[Accept]\n"
        "  A -- no --> C[Return]\n"
        "  B --> D[Grant]"
    )
    assert [n.id for n in nodes] == ["A", "B", "C", "D"]
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
    nodes, edges = parse_mermaid_flowchart(source)
    assert [n.id for n in nodes] == ["A", "B"]
    assert len(edges) == 1 and edges[0].label == label


def test_asymmetric_shape_still_parses():
    nodes, edges = parse_mermaid_flowchart("flowchart LR\n  A>Note] --> B[Next]")
    assert [n.id for n in nodes] == ["A", "B"]
    assert len(edges) == 1


def test_a_chain_on_one_line_is_every_edge_in_it():
    """`A --> B --> C` is two edges; a plain finditer consumed B and lost B→C."""
    _, edges = parse_mermaid_flowchart("flowchart LR\n  A[一] --> B[二] --> C[三] -.-> D[四]")
    assert [(e.from_id, e.to_id, e.style) for e in edges] == [
        ("A", "B", "solid"),
        ("B", "C", "solid"),
        ("C", "D", "dashed"),
    ]


def test_open_and_thick_links_keep_their_meaning():
    _, edges = parse_mermaid_flowchart("flowchart LR\n  A --- B\n  B ==> C\n  C -.- D")
    assert [(e.arrow, e.thick, e.style) for e in edges] == [
        ("none", False, "solid"),
        ("end", True, "solid"),
        ("none", False, "dashed"),
    ]


def test_quoted_and_broken_labels_read_as_the_reader_sees_them():
    nodes, _ = parse_mermaid_flowchart('flowchart TB\n  A["第一步：受理"] --> B[审查<br/>决定]')
    assert [n.text for n in nodes] == ["第一步：受理", "审查\n决定"]


def test_kind_and_direction_come_from_the_header():
    assert mermaid_kind("%% comment\nflowchart LR\n A-->B") == "flowchart"
    assert mermaid_kind("sequenceDiagram\n A->>B: hi") == "sequencediagram"
    assert mermaid_direction("graph TD\n A-->B") == "TB"
    assert mermaid_direction("flowchart RL\n A-->B") == "RL"
    assert mermaid_direction("flowchart\n A-->B") == "TB"


def test_subgraphs_become_clusters_holding_the_steps_listed_in_them():
    clusters, membership = parse_mermaid_subgraphs(PROCESS)
    assert [(c.id, c.title) for c in clusters] == [("legal", "法务部"), ("ops", "产品与人力资源")]
    assert membership == {"A": "legal", "B": "legal", "C": "ops", "D": "ops"}
    nested, _ = parse_mermaid_subgraphs(
        "flowchart TB\n subgraph outer [集团]\n  subgraph inner [子公司]\n   X --> Y\n  end\n end"
    )
    assert [(c.id, c.parent) for c in nested] == [("outer", None), ("inner", "outer")]


def test_a_mindmap_nests_by_relative_indentation_and_drops_shape_syntax():
    nodes, edges = parse_mermaid_mindmap(MINDMAP)
    labels = [label for label, _ in nodes]
    assert labels == ["个人信息出境", "安全评估", "重要数据", "标准合同", "10万至100万人", "豁免", "合同履行"]
    by_label = {label: i for i, (label, _) in enumerate(nodes)}
    pairs = {(labels[p], labels[c]) for p, c in edges}
    assert ("标准合同", "10万至100万人") in pairs, "a deeper indent of any width is a child"
    assert ("个人信息出境", "豁免") in pairs
    assert nodes[by_label["个人信息出境"]][1] == 0


# ---------------------------------------------------------------------------
# DOT
# ---------------------------------------------------------------------------


def test_the_dot_carries_every_node_edge_label_and_lane():
    dot = flowchart_to_dot(PROCESS, font="Noto Sans CJK SC")
    assert "rankdir=LR" in dot
    assert 'subgraph "cluster_legal"' in dot and 'label="法务部"' in dot
    assert '"D" [shape="diamond"' in dot
    assert '"H" [shape="box", peripheries="2"' in dot
    assert '"D" -> "E" [label=" 是 "]' in dot and '"D" -> "F" [label=" 否 "]' in dot
    assert '"H" -> "I" [style="dashed"]' in dot
    assert '"G" -> "H"' in dot, "the chained edge is drawn"
    assert 'fontname="Noto Sans CJK SC"' in dot


def test_a_long_label_wraps_into_balanced_lines_not_an_orphan_glyph():
    dot = flowchart_to_dot("flowchart TB\n  A{关键信息基础设施运营者是否适用?} --> B[下一步]", font="x")
    label = dot.split('"A" [')[1].split('label="')[1].split('"')[0]
    lines = label.split("\\n")
    assert len(lines) >= 2
    widths = [display_width(line) for line in lines]
    assert min(widths) >= max(widths) / 2, f"unbalanced wrap: {lines}"


def test_an_unparseable_flowchart_is_refused_rather_than_drawn_empty():
    with pytest.raises(ValueError):
        flowchart_to_dot("flowchart TB\n  %% nothing here")


def test_a_mindmap_is_a_left_to_right_tree_without_arrowheads():
    dot = mindmap_to_dot(MINDMAP, font="x")
    assert "rankdir=LR" in dot
    assert "edge [arrowhead=none];" in dot
    assert dot.count(" -> ") == 6


# ---------------------------------------------------------------------------
# Routing between engines
# ---------------------------------------------------------------------------


@_needs_dot
def test_a_flowchart_is_drawn_by_graphviz_with_no_browser(tmp_path, monkeypatch):
    def no_mmdc(*args, **kwargs):
        raise AssertionError("mermaid-cli must not be needed for a flowchart")

    monkeypatch.setattr(diagrams, "render_mermaid_to_file", no_mmdc)
    path, engine = render_mermaid_picture(PROCESS, tmp_path / "flow.png")
    assert engine == "graphviz"
    assert path.is_file() and path.stat().st_size > 5_000
    svg, _ = render_mermaid_picture(MINDMAP, tmp_path / "mind.svg")
    text = svg.read_text(encoding="utf-8")
    assert "个人信息出境" in text, "an SVG keeps its labels as text"


def test_other_mermaid_types_go_to_mermaid_cli(tmp_path, monkeypatch):
    seen = []

    def fake_mmdc(source, output_path, **kwargs):
        seen.append(source)
        output_path.write_bytes(b"png")
        return output_path

    monkeypatch.setattr(diagrams, "render_mermaid_to_file", fake_mmdc)
    _, engine = render_mermaid_picture("sequenceDiagram\n  A->>B: 送达", tmp_path / "seq.png")
    assert engine == "mermaid-cli" and seen


def test_without_graphviz_a_flowchart_falls_back_and_says_what_to_install(tmp_path, monkeypatch):
    monkeypatch.setattr(graph_layout, "graphviz_available", lambda engine="dot": False)

    def missing(*args, **kwargs):
        raise diagrams.MermaidNotInstalled("mmdc missing")

    monkeypatch.setattr(diagrams, "render_mermaid_to_file", missing)
    with pytest.raises(diagrams.MermaidNotInstalled) as raised:
        render_mermaid_picture(PROCESS, tmp_path / "flow.png")
    assert "graphviz" in str(raised.value)


def test_unresolvable_render_binaries_names_graphviz_when_it_is_missing(monkeypatch):
    monkeypatch.setattr(graph_layout, "graphviz_available", lambda engine="dot": False)
    monkeypatch.setattr(graph_layout, "d2_available", lambda: True)
    (problem,) = graph_layout.unresolvable_render_binaries()
    assert problem.startswith("graphviz dot") and "conda install -n llm graphviz" in problem


# ---------------------------------------------------------------------------
# The tool
# ---------------------------------------------------------------------------


@_needs_dot
def test_render_flowchart_image_returns_the_picture_path(tmp_path, monkeypatch):
    from legal_helper import config
    from legal_helper.tools.diagrams import render_flowchart_image

    settings = config.current_settings()
    monkeypatch.setattr(settings, "outputs_dir", tmp_path / "out", raising=False)
    result = render_flowchart_image.call({"filename": "流程图", "source": PROCESS})
    assert not result.startswith("ERROR"), result
    assert result.endswith("流程图.png")


def test_the_tool_points_slides_at_create_deck_and_names_no_removed_machinery():
    from legal_helper.tools.diagrams import render_flowchart_image

    description = render_flowchart_image.to_dict()["description"]
    assert "create_deck" in description
    for stale in ("write_pptx", "dagre", "flowchart block"):
        assert stale not in description, stale


# ---------------------------------------------------------------------------
# Text measurement, shared with the DOCX writer and the linter
# ---------------------------------------------------------------------------


def test_cjk_is_measured_at_double_width_and_wraps_anywhere():
    assert display_width("合同当事人应当遵循") == 2 * display_width("abcdefghi")
    assert len(wrap_label("合" * 40, 10.0)) == 4
    assert len(wrap_label("a" * 40, 10.0)) == 2
    lines = wrap_label("The lessee shall maintain the register", 8.0)
    assert all(display_width(line.strip()) <= 8.0 for line in lines)
