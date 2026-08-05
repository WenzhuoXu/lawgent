# Cookbook patterns — prompting flowchart authoring well

Attribution: prompting patterns below are distilled from the public
**[anthropics/claude-cookbook](https://github.com/anthropics/anthropic-cookbook)**
diagram-authoring notebooks (specifically the "iterative refinement" and
"thinking before coding" recipes) and adapted to Mermaid + the
`SlideFlowchart` schema. License of the original recipes: MIT.

## Pattern 1 — Restate before drawing

Before authoring any Mermaid, write back the user's process in 1-2
sentences:

> "我理解的流程是: 业务部门起草 → 法务初审 → 合规复核 → 总经理批准。
> 如果合规复核不通过，回到法务初审。这样对吗？"

If you cannot do this without inventing details, **ask one targeted
question** instead of guessing. Asking is cheaper than re-drawing.

## Pattern 2 — Author shape kinds intentionally

For each step, pick the shape kind first, then the label:

- Has an obvious "start" or "end"? Always wrap in `([…])` terminator.
- Splits into ≥ 2 paths? `{…?}` decision — and **end the label with `?`**.
  Decision shapes without a question mark consistently confuse readers.
- Refers to an existing SOP / sub-procedure? `[[…]]` subprocess. Hyperlink
  later via a separate annotation in the slide notes.
- External input (form filed, document submitted)? `[/…/]` IO.
- Side-note that interrupts but doesn't progress the flow? `>…]` data /
  callout.

## Pattern 3 — Direction by intent

- Approval / sequential workflows: `direction: TB` (top-down reads as
  "first this, then this").
- Hand-off timelines, parallel tracks, or comparative steps: `direction:
  LR` (left-right reads as a swimlane).
- Decision trees with many fan-outs: `direction: TB` keeps fan-outs
  visually grouped; `LR` makes them cramped.
- Never use `RL` or `BT` unless the user explicitly requested it; both
  read as "backwards" to most readers.

## Pattern 4 — Inspect after writing, before reporting

The skill's success criterion is **what's actually in the file**, not
what the model intended. Always:

1. `path = write_pptx(...)`.
2. `inspect = inspect_pptx(path)`.
3. Compare `inspect.slides[i].flowchart_node_count` against the number
   of nodes you authored.
4. If they don't match, you authored a typo (most often a node ID
   referenced in an edge but never defined). Fix the Mermaid and rerun.

If you skip this, the user sees a broken deck before you do.

## Pattern 5 — Loopback handling

Backward edges (`D --> A` to re-do a step) are common in approval flows.
The pptxgenjs line shape can only orient one way per axis, so a long
loopback may render the arrowhead on the wrong end. Two fixes:

1. **Short loopbacks** — add `arrow="both"` so the arrow shows on both
   ends. Visually unambiguous.
2. **Long loopbacks** — author the loopback edge with an explicit
   intermediate node so the segment direction matches the natural flow.
   Example: `D --> R[复审]; R --> A` instead of `D --> A`.

## Pattern 6 — When to escalate to an image

If the diagram needs any of:

- swimlanes (`subgraph`),
- > 12 nodes,
- cross-edges that the auto-layout solver can't untangle,
- a corporate-style theme the layout helper doesn't reproduce,

…stop trying to make `SlideFlowchart` do it and switch to
`render_flowchart_image(...)` → embed via `SlideImage`. The Artifact
block then reports `kind: image`. This is not a failure — it's the
right tool for dense or visually-prescriptive diagrams.

## Pattern 7 — Source the process, not just the diagram

A flowchart for an internal SOP must cite the SOP. A flowchart for a
statutory procedure must cite the statute with article-level pinpoints
(中华人民共和国行政许可法 第三十二条 etc.). If you can't cite, write
`pinpoint unavailable` in the Sources block — never invent.
