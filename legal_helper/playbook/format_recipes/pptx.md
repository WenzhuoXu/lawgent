# PPTX Recipe

Read this before any non-trivial deck edit. PPTX has three distinct surfaces
(text, structure, vision) and each has its own tool.

## Tools at a glance

| Tool | When to use |
|---|---|
| Vision (multimodal) | **Read uploaded decks here.** The runtime converts attached `.pptx`/`.ppt` to PDF and sends pages as a multimodal block alongside a text companion with selectable text + speaker notes. Use the slide images for layout, shapes, arrows, colors, swimlanes. |
| `inspect_pptx` | Slide count, layouts, shape counts, tables/images, text runs, speaker notes. Text-only — a fallback, not the primary surface. |
| `read_document` | Plain text + notes extraction. |
| `write_pptx` | New decks with varied layouts, charts, images, master slides. |
| `edit_pptx_text` | Exact text find/replace, keyed by 1-based slide number. |
| `reshape_pptx` | **Any structural reshape** — duplicate / delete / reorder slides, rewrite a specific shape's text by index/name/placeholder idx, update speaker notes. |
| `render_pptx_slides` | Visual QA via soffice → PDF → JPGs. Catches overlapping elements, clipped text, low contrast, alignment drift. |

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
4. `render_pptx_slides(output)` after non-trivial edits and look for
   overlapping elements, clipped text, leftover placeholders, speaker
   notes that no longer match the slide.

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
