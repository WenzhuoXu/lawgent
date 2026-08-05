---
name: flowchart
description: Build flowcharts and process maps as editable PPTX shapes or standalone images. Use whenever the user asks for 流程图 / 工作流程 / 流程地图 / process map / decision tree / SOP diagram — never reply with a vertical bullet list when the user wants a diagram.
argument-hint: "<process or workflow description>"
---

# /flowchart — Process maps and decision trees

Produce real boxes-and-arrows diagrams, not stacks of text. Two output
paths exist; choose by what the user will do with the result.

**Not legal advice.** Diagrams are work product; counsel must review the
underlying process and any embedded statutory references.

## Output Contract (binding)

Emit exactly one block — nothing before, nothing after — naming what you
produced and the path:

````
## Artifact
- kind: native_pptx | image
- path: <absolute path under outputs/>
- shape_count: <int>            # nodes (native_pptx) or 1 (image)
- edge_count: <int>
- mermaid_source: |
    flowchart ...

## Sources
- [process owner or document] — [pinpoint or pinpoint unavailable]
````

Hard ceiling: **≤ 1500 characters**. No prose summary, no walk-through,
no "I have created…" preamble.

## Decision: native shapes vs. image

| Use the native PPTX block when… | Use a standalone image when… |
|---|---|
| The deck will be edited in PowerPoint | The deck is read-only / exported to PDF |
| Nodes ≤ 12, single direction (TB or LR) | Nodes > 12, swimlanes, sub-graphs |
| User asked for an editable .pptx | User asked for a PNG or wants it embedded in a DOCX |
| You need master / footer / accent integration | The diagram must match a specific design system |

Default to **native shapes** when in doubt — `write_pptx` + a `flowchart`
slide block. See `references/flowchart_authoring.md` for the schema and
examples; `references/cookbook_patterns.md` for prompt patterns adapted
from the Anthropic cookbook.

## Tools

- `write_pptx` — slide.`flowchart` block (Mermaid string or explicit
  nodes+edges). The auto-layout helper renders the Mermaid via `mmdc`,
  extracts positions, and emits native `flowChartProcess` /
  `flowChartDecision` / `flowChartTerminator` / line shapes. Editable.
- `render_flowchart_image` — Mermaid → PNG/SVG under `outputs/`. Use
  for DOCX embeds or one-off chat replies.
- `inspect_pptx` — reads back `flowcharts: {nodes, edges}` per slide so
  you can verify a generated deck before reporting success.
- `reshape_pptx` — supports `set_flow_node_text` and `move_flow_node`
  ops for surgical edits without redrawing.

## Workflow

1. Restate the process in 1-2 sentences. If the user gave a vague
   directive ("我们的合同审批流程"), ask one targeted question before
   drawing.
2. Author the Mermaid string. Use shape kinds intentionally:
   - `S([Start])` / `E([End])` for terminators
   - `A[step]` for normal process steps
   - `D{decision?}` for branches (always end with `?`)
   - `>note]` for callouts / annotations
3. Call `write_pptx` with one slide whose `flowchart` field carries the
   Mermaid string. Default `layout: auto`, `direction: TB`.
4. Call `inspect_pptx` on the result; confirm `flowchart_node_count`
   and `flowchart_edge_count` match what you authored. If a node is
   missing, fix the Mermaid (usually a typo'd ID) and retry.
5. Emit the Artifact block.

## Common mistakes to avoid

- **Numbered text bullets in a "bullets" slide instead of a flowchart.**
  This is the failure mode the tool was built to replace. If the user
  said 流程图, the answer is a `flowchart` block, not bullets.
- **Auto-layout without a direction.** Always set `direction: TB` for
  approval flows and `LR` for hand-off timelines.
- **Edge-label noise.** Only label edges when the branch matters
  (`|是|`, `|否|`); skip labels on linear sequences.

## Output language

User-facing labels follow the user's language (keep CJK strings as
authored). Internal tool calls and reasoning may use whichever language fits
the material. Quote any referenced statute / SOP / policy with a pinpoint in
the `Sources` block (line format per `/playbook/general_playbook.md` §5).
