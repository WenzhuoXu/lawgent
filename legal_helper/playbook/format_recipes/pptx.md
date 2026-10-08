# PPTX Recipe

Two jobs, two sets of tools. A **new** deck — a presentation, a single slide,
a diagram on a slide, 汇报材料, 课件 — is made by `create_deck`, which drives
ppt-master. A deck the user **already has** is read and changed with the
existing-deck tools.

## Making a deck: `create_deck`

`create_deck` runs ppt-master in a sub-agent: it plans the deck, draws every
page as editable native PowerPoint shapes, runs ppt-master's own quality gate,
and exports one `.pptx`. The tool then opens the export, counts its slides,
lints it, and returns JSON. A run takes several minutes.

The sub-agent does not see the conversation. Everything the slides say goes
in `brief`:

- the finished analysis or outline, in the deck's language;
- every fact, figure, date, party and amount, exactly as it must appear;
- every citation with its pinpoint (《个人信息保护法》第三十八条);
- for a diagram: each step, decision, branch label, owner and period — and
  the Mermaid source (see the `flowchart` skill).

Nothing absent from the brief or the `source_paths` files appears on a slide.

| Argument | Use |
|---|---|
| `title`, `audience`, `purpose` | who it is for and what it must do (汇报 / 宣贯 / pitch) |
| `page_count` | a target; 0 lets the procedure decide; 1 for a single slide |
| `language` | defaults to the brief's language |
| `source_paths` | uploaded files the deck draws on |
| `template_path` | the user's own .pptx/.potx whose design must be kept or filled |
| `quick` | only when the user asked for speed over the full planning pass |

Reading the result:

- `status: ok` — give the user `pptx_path`. `slides`, `gate_verdict`
  (ppt-master's postflight) and `decisions_under_delegation` (the choices the
  sub-agent made in the user's place) say what was made.
- `lint_summary` with critical findings — look (`render_pptx_slides`), then
  fix with the edit tools below or call `create_deck` again with the problem
  named in the brief.
- `status: failed` — tell the user what stopped it (`sub_agent_report`,
  `error`); never present another file as the deck.
- `status: unavailable` — ppt-master is not installed; say so. There is no
  other deck writer.

## Changing a deck the user already has

| Tool | When to use |
|---|---|
| Vision (multimodal) | **Read uploaded decks here.** Attached `.pptx`/`.ppt` arrive as page images plus a text companion; use the images for layout, arrows, colours. |
| `inspect_pptx` | Slide count, layouts, groups, tables, images, text runs (including text inside groups), speaker notes. |
| `read_document` | Plain text + notes extraction. |
| `edit_pptx_text` | Exact text find/replace, keyed by 1-based slide number. Reaches grouped text and text split across runs. |
| `reshape_pptx` | Duplicate / delete / reorder slides, rewrite one shape's text, update speaker notes. |
| `render_pptx_slides` | Visual QA: the slides come back **as images**, with the conformance lint attached. Look at them. |
| `view_image` | Full-resolution look at one slide after the contact sheet flags a problem. |

`edit_pptx_text` is *exact* find/replace — it cannot insert, delete or
reorder slides, and it cannot target one specific shape by position. For
those, use `reshape_pptx`. A new layout, a new chart or a redesign is not an
edit: call `create_deck` (with `template_path` set to the user's deck to keep
its design).

Standard workflow:

1. If the deck was uploaded, read the vision blocks first; `inspect_pptx` is a
   structural summary, not a substitute.
2. `inspect_pptx(path)` for slide count, shape names and speaker notes.
3. Exact text swap → `edit_pptx_text`; slide order, one shape, notes →
   `reshape_pptx`. `edit_pptx_text` lists finds that matched nothing under
   `not_found` — re-anchor them or report them as not done. An edit that names
   its source as the output writes `<name>_v2.pptx` instead and says so.
4. `render_pptx_slides(output)` and look before reporting the deck as done.

## `reshape_pptx` operations

Each entry is `{"op": "<kind>", ...}`; ops apply in order. Slide indices are
1-based.

- `duplicate_slide` — `index`, optional `after` (1-based; defaults to
  immediately after the source).
- `delete_slide` — `index`.
- `move_slide` — `index`, `to`.
- `set_shape_text` — `slide` plus **one** of `shape_index` (0-based, top-level
  shapes), `shape_name` (also finds a shape inside a group), or
  `placeholder_idx`, plus `text`.
- `set_speaker_notes` — `slide`, `text`.
