---
name: flowchart
description: Process maps, decision trees, swimlanes, 结构图 and 思维导图 — on a slide (made with create_deck) or as a picture for a Word document or a chat reply (render_flowchart_image). Use whenever the user asks for 流程图 / 工作流程 / 流程地图 / 结构图 / 思维导图 / process map / decision tree / SOP diagram; never answer a diagram request with a numbered list.
argument-hint: "<process or workflow description>"
---

# /flowchart — Process maps and decision trees

Produce a real boxes-and-arrows diagram, not a stack of text. Where the
diagram goes decides the tool.

**Not legal advice.** A diagram is work product; counsel must review the
underlying process and every statutory step drawn in it.

## Output Contract (binding)

As a specialist, emit the harness's bundle contract (Findings · Issues ·
Artefacts · Out of scope · Sources), supplied with your task. As the author of
a final answer, follow the author contract instead. Artefacts this skill
produces: the deck path from `create_deck`, or the image path from
`render_flowchart_image`, plus the Mermaid source.

## Where the diagram goes

| The user wants… | Tool |
|---|---|
| a slide, a deck, a 汇报 page, anything to present | `create_deck` — ppt-master draws the diagram on the slide as editable native shapes |
| a picture for a Word document, a chat reply, an email | `render_flowchart_image` — a PNG (or SVG) under `outputs/` |
| a change to a diagram in a deck they already have | `edit_pptx_text` / `reshape_pptx` on that deck |

`create_deck`'s sub-agent cannot see this conversation, so its `brief` carries
the diagram itself: every step in order, each decision and its branches with
their labels, who owns each step (lanes), the statutory period on any timed
step, and the citation for each statutory step. Put the Mermaid source in the
brief too; it states the structure exactly. A single diagram slide is a
one-page deck (`page_count: 1`).

## Authoring the Mermaid source

1. Restate the process in one or two sentences. If the user gave only a name
   ("我们的合同审批流程"), ask one targeted question before drawing.
2. Pick each node's shape for what it is:
   - `S([受理])` — start or end;
   - `A[法务审查]` — an ordinary step;
   - `D{材料是否齐全?}` — a decision; end the label with `?`;
   - `F[/提交申请表/]` — a document or form going in or out;
   - `P[[另行启动的程序]]` — a sub-procedure;
   - `N>备注]` — a note that interrupts but does not advance the flow.
3. Label only the branches that matter: `D -->|是| E` or `D -- 否 --> A`.
   A dashed edge `-.->` is optional or conditional; `---` is a tie, not a step.
4. Lanes are `subgraph 法务部 … end` blocks; a step belongs to the lane it is
   first written in.
5. Direction: `flowchart TB` for approval chains and anything going onto a
   portrait page; `LR` for a hand-off timeline in a chat image. Never `RL`/`BT`
   unless asked.
6. A 思维导图 is a `mindmap` source (indentation is the hierarchy); a
   结构图 of boxes inside boxes is a `d2` source (`engine: "d2"`).

## Workflow

1. Decide where it goes (table above).
2. Write the Mermaid source.
3. Slide or deck → `create_deck`. Picture → `render_flowchart_image`, then
   `view_image` on the PNG and fix what you see: a missing branch is almost
   always a node id typed two ways.
4. Report the artefact and the source per the output contract.

Read `references/flowchart_authoring.md` for the full syntax this renderer
understands and examples; `references/cookbook_patterns.md` for prompting
patterns that keep diagrams honest.

## Common mistakes to avoid

- **A numbered bullet list where the user asked for a diagram.** If they said
  流程图, the answer is a drawing.
- **A statutory step without its source.** A step taken from a statute or
  SOP carries its pinpoint (《行政许可法》第三十二条) in the Sources; when it
  cannot be found, say `无法获得具体条款` rather than inventing one.
- **A one-line brief to `create_deck`.** The sub-agent draws what the brief
  says and nothing else.

## Output language

Labels follow the user's language (keep CJK strings as authored). Citations
follow `/playbook/general_playbook.md`.
