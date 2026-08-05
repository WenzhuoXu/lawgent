# Flowchart authoring — Mermaid → native PPTX shapes

This file is the **deep reference** for the `/flowchart` skill. SKILL.md
delegates here for schema details, shape kind selection, and limits.

## The `SlideFlowchart` block

Attach to any `Slide` in a `write_pptx` call:

```python
Slide(
    title="制度治理流程",
    layout="bullets",        # any layout works; flowchart overlays on top
    bullets=[],              # leave empty when the flowchart IS the content
    flowchart=SlideFlowchart(
        mermaid="""flowchart TB
            S([提交制度起草]) --> A[法务审查]
            A --> D{合规通过?}
            D -->|是| E([发布])
            D -->|否| A""",
        layout="auto",       # auto = let mermaid+dagre solve positions
        direction="TB",      # TB | BT | LR | RL
        x=0.55, y=1.25,      # bounding-box (inches) on the slide
        w=12.2, h=5.6,
        node_w=2.2, node_h=0.9,   # default node size when not per-node
    ),
)
```

### Mermaid → kind mapping

| Mermaid syntax     | FlowNode.kind | OOXML shape                  |
|---|---|---|
| `A[label]`         | `process`     | `flowChartProcess`           |
| `A{label}`         | `decision`    | `flowChartDecision`          |
| `A([label])`       | `terminator`  | `flowChartTerminator`        |
| `A((label))`       | `terminator`  | `flowChartTerminator`        |
| `A[/label/]`       | `io`          | `flowChartInputOutput`       |
| `A[[label]]`       | `subprocess`  | `flowChartPredefinedProcess` |
| `A>label]`         | `data`        | `flowChartManualInput`       |
| `A` (bare)         | `process`     | `flowChartProcess`           |

### Edge syntax we handle

- `A --> B` solid arrow
- `A -.-> B` dashed arrow
- `A --> |label| B` arrow with label
- `A --- B` solid line, no arrow
- `A ==> B` thick arrow (rendered as solid)

Anything fancier (subgraphs, `classDef`, `linkStyle`, click bindings) is
**ignored** by the layout helper. The shapes still draw; the styling
just falls back to the deck's accent color.

## Explicit nodes/edges (skip the parser)

When you need full control over per-node fill or kind override:

```python
SlideFlowchart(
    nodes=[
        FlowNode(id="S", kind="terminator", text="Start"),
        FlowNode(id="A", kind="process",  text="审查"),
        FlowNode(id="D", kind="decision", text="批准?", fill="FFF3CD"),
        FlowNode(id="E", kind="terminator", text="End"),
    ],
    edges=[
        FlowEdge(from_id="S", to_id="A"),
        FlowEdge(from_id="A", to_id="D"),
        FlowEdge(from_id="D", to_id="E", label="是"),
        FlowEdge(from_id="D", to_id="A", label="否", style="dashed"),
    ],
    layout="auto",
    direction="TB",
)
```

When both `mermaid` and `nodes` are set, the explicit nodes/edges win.

## Manual coordinates

Set `layout="manual"` and supply `x`, `y`, `w`, `h` on every node (inches
from slide top-left). Useful when matching a fixed corporate template.
Skipping any of the four falls back to auto.

## Known limitations

1. **Connector arrows do not snap.** The helper draws each edge as a
   pptxgenjs `ShapeType.line` with an arrowhead, not a true OOXML
   `<p:cxnSp>`. When the user drags a node in PowerPoint, the arrow
   stays put. Workaround: re-emit the deck rather than dragging.
2. **Direction-only arrowheads on diagonals.** Lines that flip both
   horizontally and vertically may render the arrow at the wrong end
   for loopback edges. Use `arrow="both"` to be safe.
3. **Mermaid layout requires Chrome.** `mmdc` runs headless Chrome via
   puppeteer. The helper auto-detects `/usr/bin/google-chrome`; if
   that is missing, the deck still writes but with a vertical stack
   fallback layout (still real shapes, just not graph-aware).
4. **CJK labels are supported** as long as the system font stack has a
   CJK fallback; default mermaid CSS uses `sans-serif`, which inherits
   the host browser's font. Verify in `outputs/` after generation.

## Decision: native shapes vs. image

| Editable in PowerPoint? | Path |
|---|---|
| Yes (default) | `write_pptx(..., flowchart=SlideFlowchart(mermaid="..."))` |
| No, but pixel-perfect | `render_flowchart_image("foo.png", source="flowchart ...")` then embed via `Slide(images=[SlideImage(path="<png>")])` |

Use the image path for >12 nodes, swimlanes (mermaid `subgraph`),
multi-column layouts, or when matching a corporate diagram template.

## Verification checklist (before reporting success)

1. `inspect_pptx(<path>)` reports `flowchart_node_count == authored
   node count` for the relevant slide.
2. `inspect_pptx` reports `flowchart_edge_count == authored edge
   count`. If lower, an edge endpoint missed a node center — re-run
   `autolayout_flowchart` (most likely the node ID had a typo).
3. Edge `confidence` averages > 0.85. Lower means lines didn't land
   on node centers — usually fine visually but flag low confidence in
   the Artifact block.
