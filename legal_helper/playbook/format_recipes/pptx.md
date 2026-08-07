# PPTX Recipe

Read this before any non-trivial deck edit. PPTX has three distinct surfaces
(text, structure, vision) and each has its own tool.

## Tools at a glance

| Tool | When to use |
|---|---|
| Vision (multimodal) | **Read uploaded decks here.** The runtime converts attached `.pptx`/`.ppt` to PDF and sends pages as a multimodal block alongside a text companion with selectable text + speaker notes. Use the slide images for layout, shapes, arrows, colors, swimlanes. |
| `inspect_pptx` | Slide count, layouts, shape counts, tables/images, text runs, speaker notes. Text-only — a fallback, not the primary surface. |
| `read_document` | Plain text + notes extraction. |
| `write_pptx_from_html` | **Preferred for new decks.** Lay slides out in HTML/CSS; Chromium measures them and the geometry maps onto native PowerPoint shapes, text, and tables. Use whenever the deck needs real visual structure. |
| `read_deck_stylesheet` | The CSS design system to author against. Read before writing HTML slides. |
| `write_pptx` | New decks constrained to the ten fixed layouts. Use when the content genuinely is a title + bullets + one chart. |
| `edit_pptx_text` | Exact text find/replace, keyed by 1-based slide number. |
| `reshape_pptx` | **Any structural reshape** — duplicate / delete / reorder slides, rewrite a specific shape's text by index/name/placeholder idx, update speaker notes. |
| `render_pptx_slides` | Visual QA. Returns the slides **as images** — a labeled contact sheet you must actually look at. |
| `view_image` | Full-resolution look at one slide after the contact sheet flags a problem. |

## The cardinal rule

`edit_pptx_text` is *exact* find/replace — it cannot insert, delete, or
reorder slides, and it cannot target one specific shape by position. For
those, use `reshape_pptx`. For richer reshape (new charts, new tables, new
layouts), build a fresh deck with `write_pptx` and `edit_pptx_text` only
the carry-over text.

## Standard workflow

1. If the deck was uploaded, **read the vision blocks first** for layout
   ground truth. `inspect_pptx` is a structural summary, not a substitute.
2. `inspect_pptx(path)` for slide count, shape names, placeholder indices,
   speaker-notes presence.
3. Pick by intent:
   - exact text swap across known strings → `edit_pptx_text`
   - duplicate / delete / reorder a slide, retarget one shape, update
     speaker notes → `reshape_pptx`
   - new deck from scratch → `write_pptx`
4. `render_pptx_slides(output)` after **every** deck write or non-trivial
   edit. It returns the slides as images — look at them. You are checking
   for: blank or near-empty slides, text overflowing its box, overlapping
   elements, clipped tables, leftover placeholders, inconsistent margins,
   speaker notes that no longer match the slide. Fix what you see and
   re-render. Do not report a deck as finished on a render you did not read.

## Authoring with `write_pptx_from_html` (preferred)

The ten fixed layouts cannot express a card grid, a sidebar, a KPI band, or
a numbered process strip — anything they do not cover degrades to a bullet
list. Author those in HTML instead:

1. `read_deck_stylesheet()` and inline the CSS in a `<style>` block.
2. Write one `<section class="slide">` per slide — exactly 1280x720 px
   (13.333in x 7.5in at 96 px/in). Keep content inside the ~52px margin.
3. Compose from the supplied components: `.slide--cover`, `.slide--section`,
   `.card` (+`--accent`/`--risk`/`--ok`), `.grid--2|3|4`, `.kpi`, `.steps`,
   `<table>`, `.footer`. Override the `:root` custom properties for a client
   palette.
4. Put speaker notes in `data-notes` on the slide element.
5. Mark SVG, gradients, and charts with `data-raster` so they are captured as
   pictures rather than rebuilt as shapes.
6. `write_pptx_from_html(...)` then `render_pptx_slides(...)` and look.

Text stays editable text, boxes stay shapes, `<table>` stays a real table —
the client can restyle the deck in PowerPoint afterwards.

**A note on renderer failures.** If `write_pptx` returns
`"status": "DEGRADED"`, the pptxgenjs renderer was unavailable and the deck
was written by a fallback that drops tables, charts, flowcharts, stats,
images, and masters. Fix the environment and write it again — never hand a
degraded deck to the user.

## `reshape_pptx` operations

Each entry is `{"op": "<kind>", ...}`; ops apply in order. Slide indices
are 1-based.

- `duplicate_slide` — `index`, optional `after` (1-based; defaults to
  immediately after the source).
- `delete_slide` — `index`.
- `move_slide` — `index`, `to`.
- `set_shape_text` — `slide` plus **one** of `shape_index` (0-based),
  `shape_name`, or `placeholder_idx`, plus `text`.
- `set_speaker_notes` — `slide`, `text`.

## Authoring with `write_pptx`

Prefer varied layouts: `title`, `section`, `bullets`, `two_column`,
`comparison`, `table`, `quote`, `stat`, `timeline`, or `chart`. Every
substantive slide should carry visual structure (shapes, tables, large
numbers, side-by-side comparison, chart). Avoid a whole deck of plain
bullets.

**Charts.** Each `Slide` accepts an optional `chart` (`SlideChart`) with
`type` in {bar, line, pie, doughnut, scatter, bubble, radar}, `title`,
`categories`, and `series` (`name` + numeric `values`). With
`layout="chart"` the chart fills the body; on other layouts it docks on
the right half. Bar for severity counts, line for time series,
pie/doughnut for share-of-total, scatter for correlations.

**Images.** Each `Slide` accepts an `images` list — `path` or base64
`data` URI, plus `x`/`y`/`w`/`h` in inches and `sizing`
(`contain`/`cover`/`crop`/`stretch`). Use for logos, evidence photos, MSN
plates, clause screenshots, regulator headers.

**Master slides.** Pass a `masters` list (`SlideMaster`) on `write_pptx`
for firm-wide branding: `name`, `background_color`, `accent_color`,
`footer`, `show_slide_number`. Each `Slide.master` field references one
master by name. Define once, reference everywhere — keeps footer, slide
number, and background consistent.

Use a topic-specific palette: one dominant color, one or two supporting,
one accent. Keep ≥0.5 inch margins and ≥0.3 inch spacing between blocks.
