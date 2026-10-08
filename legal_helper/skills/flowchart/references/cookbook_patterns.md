# Cookbook patterns — prompting flowchart authoring well

Attribution: prompting patterns below are distilled from the public
**[anthropics/claude-cookbook](https://github.com/anthropics/anthropic-cookbook)**
diagram-authoring notebooks (specifically the "iterative refinement" and
"thinking before coding" recipes) and adapted to Mermaid. License of the
original recipes: MIT.

## Pattern 1 — Restate before drawing

Before authoring any Mermaid, write back the user's process in 1-2
sentences:

> "我理解的流程是: 业务部门起草 → 法务初审 → 合规复核 → 总经理批准。
> 如果合规复核不通过，回到法务初审。这样对吗？"

If you cannot do this without inventing details, **ask one targeted
question** instead of guessing. Asking is cheaper than re-drawing.

## Pattern 2 — Author shape kinds intentionally

For each step, pick the shape kind first, then the label:

- Has an obvious "start" or "end"? Always wrap in `([…])`.
- Splits into ≥ 2 paths? `{…?}` decision — and **end the label with `?`**.
  Decision shapes without a question mark consistently confuse readers.
- Refers to an existing SOP / sub-procedure? `[[…]]` subprocess.
- External input (form filed, document submitted)? `[/…/]` IO.
- Side-note that interrupts but doesn't progress the flow? `>…]` note.

## Pattern 3 — Direction by destination and intent

- Approval / sequential workflows: `flowchart TB` (top-down reads as
  "first this, then this"), and always `TB` for a picture going onto a
  portrait Word page.
- Hand-off timelines and parallel tracks in a chat image: `LR`.
- Decision trees with many fan-outs: `TB` keeps fan-outs grouped.
- Never use `RL` or `BT` unless the user explicitly asked; both read as
  "backwards" to most readers.

## Pattern 4 — Look after drawing, before reporting

The success criterion is **what is actually in the file**, not what was
intended:

1. Picture: `render_flowchart_image(...)`, then `view_image` on the PNG.
   Count the decision branches you drew against the ones you meant.
2. Deck: `create_deck(...)` returns the slide count, ppt-master's gate verdict
   and the lint; `render_pptx_slides` shows the slide.

A branch that is missing almost always means a node id typed two ways
(`D` in one line, `d` in another).

## Pattern 5 — Slide or picture

The destination decides, not the diagram's complexity:

- Anything that will be presented — a deck, a 汇报 page, a single slide — is
  `create_deck`. ppt-master draws the diagram as editable shapes and checks
  it with its own gate; put every step, branch, owner and period in the brief.
- A diagram for a memo, an email or the chat is `render_flowchart_image`.
  Swimlanes are `subgraph` blocks; a 思维导图 is a `mindmap` source; a
  structure of boxes inside boxes is D2 (`engine: "d2"`).

## Pattern 6 — Source the process, not just the diagram

A flowchart for an internal SOP must cite the SOP. A flowchart for a
statutory procedure must cite the statute with article-level pinpoints
(中华人民共和国行政许可法 第三十二条 etc.). If you can't cite, write
`pinpoint unavailable` / `无法获得具体条款` — never invent.
