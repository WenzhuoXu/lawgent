# DOCX Recipe

Read this before a non-trivial Word edit. Structural reshape and pure text
find/replace are different tools — picking the wrong one corrupts layout.

## Tools at a glance

| Tool | When to use |
|---|---|
| `read_document` | Paragraphs, tables, and comments as text. |
| `inspect_docx` | Structure: paragraph/table counts, heading list, comments, core properties. |
| `write_docx` | New documents from a title + ordered (heading, body_markdown) sections. |
| `edit_docx_text` | Exact text find/replace in paragraphs and table cells. Run-boundary aware. |
| `reshape_docx` | **Any structural reshape** — insert / delete / restyle paragraphs, insert / delete table rows, rewrite a specific cell. |
| `render_docx_pages` | Visual QA via soffice → PDF → JPGs. Catches clipped tables, missing headers/footers, page-number issues. |

## The cardinal rule

Use `edit_docx_text` **only** for exact text find/replace. If the user asks
to add a paragraph, demote a heading, insert a new table row, change a
cell's text, or delete a stale section, reach for `reshape_docx`. Repeated
`edit_docx_text` calls cannot insert structure — they will at best leave
the same text in the same place.

## Standard workflow

1. `inspect_docx(path)` to learn heading hierarchy, tables, and comments.
2. `read_document(path)` only when you need the body text — never when you
   only need coordinates.
3. Pick by intent:
   - exact string replacement (boilerplate names, dates, party names) →
     `edit_docx_text`
   - paragraph / table row inserts, deletes, restyles → `reshape_docx`
   - whole new document → `write_docx`
4. When layout matters, `render_docx_pages(output)`. It returns the pages
   **as images** — look at them and check for clipped tables, overlapping
   text, missing headers/footers, orphaned headings, and page breaks in the
   wrong place. Use `view_image` on a single page for fine detail.
5. Re-inspect the output before declaring done. A render you did not read is
   not visual QA.

## Tables

- **Column widths come from the content, not from dividing by the column
  count.** `write_docx` measures each column's longest cell with a CJK-aware
  width (a Han glyph is one em, Latin about half), pulls that toward the
  column's typical cell so one outlier does not starve its neighbours, and
  normalises the result to the page's text width. Equal columns are what wrapped
  `《民用航空法》第九十二条` across two lines while the `60日` column sat three
  times wider than it needed to be.
- **A cell paragraph never carries the body's first-line indent.** The 2-character
  首行缩进 belongs to prose. A cell is one short field, so the indent pushes its
  first line right and the wrap resumes at the cell edge — a statute title
  broken mid-name. The writer suppresses it as *direct* paragraph formatting,
  because a table style resolves before paragraph styles and cannot override
  `Normal`.
- **Edit cells with `reshape_docx`, not by assigning `cell.text`.** Assigning
  replaces the cell's `w:p` and drops its `w:pPr`, which reintroduces the
  indent and loses the cell's alignment on the next edit.
- **Check the result.** `documents.quality.lint_docx` reports
  `table_columns_unweighted` and `cell_first_line_indent`, resolving the style
  chain rather than reading direct formatting — the defect usually lives on
  `Normal` and reaches the cell through `basedOn`.

## `reshape_docx` operations

Each entry is `{"op": "<kind>", ...}`; ops apply in order.

- `insert_paragraph` — `after` (0-based paragraph index; `-1` prepends),
  `text`, optional `style` (paragraph style name like `"Heading 2"`,
  `"List Bullet"`, `"Normal"`).
- `delete_paragraphs` — `indices` (list[int]) **or** `range` `[start, end_inclusive]`.
- `set_paragraph_style` — `index`, `style`.
- `replace_paragraph` — `index`, `text`. Keeps the paragraph's style;
  collapses mixed run formatting in that paragraph.
- `insert_table_row` — `table_index` (0-based), `at` (0-based; `-1`
  appends), `cells` (list[str]).
- `delete_table_rows` — `table_index`, `indices` (list[int]).
- `set_cell_text` — `table_index`, `row`, `col`, `text`.

## Templates, comments, and tracked changes

When a file has tracked changes, comments, custom fields, or heavy
template styling, preserve the original and state any limitations in your
reply. Write to a new output file via `_output_path`; never overwrite the
source. Use `inspect_docx`'s `comment_count` and `headings` to decide
whether the document is safe to touch.

For new authoring, use `write_docx` and keep headings semantic, tables
simple, and lists rendered through Markdown bullets/numbering rather than
random characters.

Convert legacy `.doc` files through LibreOffice first if support is added
to the harness — these tools assume `.docx`.
