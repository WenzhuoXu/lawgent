# Flowchart authoring — what the renderers understand

This is the deep reference for the `/flowchart` skill: the Mermaid this
harness draws, and how to hand a diagram to a deck.

## Two destinations

| Destination | Tool | Who draws it |
|---|---|---|
| A slide or a deck | `create_deck` | ppt-master, as editable native shapes on the slide |
| A picture (Word document, chat, email) | `render_flowchart_image` | Graphviz for `flowchart`/`graph`/`mindmap`; mermaid-cli for other Mermaid types; d2 for nested containers |

## Mermaid flowcharts → Graphviz

`render_flowchart_image(filename, source)` with a `flowchart` or `graph`
source is parsed here and laid out by Graphviz `dot` in an installed CJK
font. No browser is involved, and Graphviz measures each label in the font
it draws, so boxes fit their text.

### Node shapes

| Mermaid | Drawn as | Use for |
|---|---|---|
| `A[label]` | rectangle | an ordinary step |
| `A([label])`, `A(label)`, `A((label))` | rounded box | start / end |
| `A{label?}` | diamond | a decision |
| `A[/label/]` | parallelogram | a document or form in / out |
| `A[[label]]` | double-bordered box | a sub-procedure |
| `A>label]` | note | a side note |
| `A` (bare) | rectangle | a step whose label is its id |

`A["quoted label"]` and `<br/>` inside a label work. Node ids are ASCII
(`A`, `step_2`); labels can be any language.

### Edges

| Mermaid | Meaning |
|---|---|
| `A --> B` | step to step |
| `A -->|是| B`, `A -- 是 --> B` | a labelled branch |
| `A -.-> B`, `A -. 可选 .-> B` | dashed: optional or conditional |
| `A ==> B` | thick: the main path |
| `A --- B`, `A -.- B` | a tie with no arrowhead |
| `A --> B --> C` | a chain (two edges) |

### Lanes

```
flowchart TB
  subgraph legal [法务部]
    A([受理]) --> B[合规审查]
  end
  subgraph biz [业务部门]
    C[补充材料]
  end
  B -->|材料不全| C
  C --> B
```

Each `subgraph` becomes a labelled cluster; nesting works. A step belongs to
the lane it is first written in, so declare each step inside its lane.

### Direction and page shape

`TB` for an approval chain and for anything pasted into a portrait Word page;
`LR` for a short hand-off timeline in a chat image. A long chain drawn `LR`
becomes a strip too wide to read on a page.

## Mind maps (思维导图)

```
mindmap
  root((个人信息出境))
    安全评估
      重要数据
    标准合同
    个人信息保护认证
```

Indentation is the hierarchy (any consistent depth). Drawn left to right with
plain ties: a branch is a relationship, not a step.

## Other diagram types

`sequenceDiagram`, `classDiagram`, `stateDiagram`, `erDiagram`, `gantt` and
`pie` are drawn by mermaid-cli, which needs Node and a Chrome on the host; the
tool returns an `ERROR:` naming what to install when it is missing. A 结构图
whose boxes sit inside boxes is a D2 source with `engine: "d2"`.

## Handing a diagram to a deck

`create_deck`'s sub-agent cannot see the conversation. Its `brief` must state:

1. every step, in order, with who owns it (the lanes);
2. every decision and the label on each branch;
3. timed steps with their period (`10个工作日内备案`) and the rule that sets it;
4. the citation, with pinpoint, for every statutory step;
5. the Mermaid source, which pins the structure exactly.

ppt-master then decides the slide's layout and draws the diagram as native,
editable shapes, and runs its own quality gate before exporting.

## Verification

- Picture: `view_image` on the returned PNG. A branch that is missing almost
  always means a node id was typed two ways; an unexpected extra box means a
  label was written where an id belongs.
- Deck: `create_deck` returns `status`, `slides`, `gate_verdict` and
  `lint_summary`; `render_pptx_slides` shows the pages.
