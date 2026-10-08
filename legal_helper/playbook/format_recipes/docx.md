# DOCX Recipe

Read this before a non-trivial Word edit. Structural reshape and pure text
find/replace are different tools — picking the wrong one corrupts layout.

## Tools at a glance

| Tool | When to use |
|---|---|
| `read_document` | Text of any Word file (.docx .doc .rtf .odt .wps): tracked changes as `{++ins++}` / `{--del--}` under a one-line who-changed-what summary, `[Header]` / `[Footer]`, auto-numbering as Word shows it (第一条). `section="第三条"` (or "3.2", "Article 5", heading words) returns one clause. |
| `inspect_docx` | Structure: every body paragraph under the 0-based `index` that `reshape_docx` and `redline_docx` take, headings, tables, comments, tracked changes, style names, core properties. |
| `write_docx` | New documents from a title + ordered (heading, body_markdown) sections; optional `lang`, `layout`, `page_size`, `disclaimer`, `reference_docx`. See **Writing a new document**. |
| `edit_docx_text` | Exact text find/replace, untracked — body, tables, text boxes, headers, footers. Run-boundary aware. |
| `reshape_docx` | **Any structural reshape** — insert / delete / restyle paragraphs, insert / delete table rows, rewrite a specific cell. |
| `redline_docx` | **Changes as native tracked changes** (修订模式) with optional margin comments — the deliverable when a counterparty or partner reviews the markup. |
| `compare_docx` | 比对稿 / blackline of two versions (.docx .doc .odt .rtf): every difference becomes a tracked change, checked by accepting and rejecting all of them. |
| `render_docx_pages` | Visual QA via soffice → PDF → JPGs. Catches clipped tables, missing headers/footers, page-number issues. |

## The cardinal rule

Use `edit_docx_text` **only** for exact text find/replace. If the user asks
to add a paragraph, demote a heading, insert a new table row, change a
cell's text, or delete a stale section, reach for `reshape_docx`. Repeated
`edit_docx_text` calls cannot insert structure — they will at best leave
the same text in the same place.

When the user will review the change — a markup of the counterparty's draft,
修订稿, "show me what you changed" — use `redline_docx`: each insertion and
deletion is an attributed revision they can accept or reject. To show what
changed between two versions someone else wrote, use `compare_docx`.

## Standard workflow

1. `inspect_docx(path)` to learn heading hierarchy, tables, comments and the
   paragraph indexes edits take.
2. `read_document(path)` only when you need the body text — never when you
   only need coordinates.
3. Pick by intent:
   - exact string replacement (boilerplate names, dates, party names) →
     `edit_docx_text`
   - paragraph / table row inserts, deletes, restyles → `reshape_docx`
   - a markup the reader reviews → `redline_docx`; two versions → `compare_docx`
   - whole new document → `write_docx`
4. Read the result, not just the path. `not_found` lists finds that matched
   nothing, with a hint — re-anchor them, never report them as done.
   `refused` lists paragraphs carrying tracked changes, which `edit_docx_text`
   will not touch: edit those with `redline_docx`. `integrity` lists anything
   the edit lost against the source (comment anchors, revisions, footnote
   references, fields, images); redo the edit if a loss was not intended.
5. When layout matters, `render_docx_pages(output)`. It returns the pages
   **as images** — look at them and check for clipped tables, overlapping
   text, missing headers/footers, orphaned headings, and page breaks in the
   wrong place. Use `view_image` on a single page for fine detail.
6. Re-inspect the output before declaring done. A render you did not read is
   not visual QA.

## Writing a new document

`write_docx(filename, title, sections, lang="auto", layout="memo",
page_size="auto", disclaimer="properties", disclaimer_reason="",
reference_docx="", subtitle="")`

- **Language and page.** `lang="auto"` treats the document as Chinese when most
  of its words are (Han characters against Latin words — a party name or one
  fullwidth comma does not count). Chinese: A4, 宋体 body, 黑体 headings,
  首行缩进 2 字, footer "第 X 页 共 Y 页". English: Letter, "Page X of Y".
  `page_size` ("A4" / "Letter") overrides the paper; `lang` overrides the rest.
- **`layout="gbt9704"`** for a 公文 (请示, 报告, 通知, 法律意见 in 公文 form):
  GB/T 9704-2012 page (A4; 天头 37 mm, 订口 28 mm, 版心 156 × 225 mm), 22 lines
  × 28 characters, 3号仿宋 body, 2号小标宋 title. Structure levels come from the
  text: 一、 → 黑体, （一） → 楷体, 1. → 仿宋, （1） → 仿宋, each 空二字 and not
  bold. Page numbers "— n —" in 4号宋体, odd pages right and even pages left.
  Ordered lists become typed "1." / "（1）" items, as 公文 numbers in the text.
  A red-head 正式发文 (版头, 发文字号, 版记) is beyond this layout: when a
  公文 procedure is installed, `run_procedure` is on your tool list and lists
  it — pass every element (发文机关, 文号, 签发人, 主送, 正文, 成文日期, 抄送,
  印发机关) in the brief. Without one, say the red head is not available.
- **Disclaimer.** The not-legal-advice notice is written to the document
  properties (File → Info) in the document's language — never into the body.
  For a court filing (起诉状、答辩状、上诉状), a 律师函 or anything the user will
  sign, pass `disclaimer="none"` with a `disclaimer_reason`. Do not type a
  disclaimer paragraph into a filing yourself.
- **Templates.** `reference_docx` takes a firm or court `.docx`/`.dotx`: its
  body is cleared and your sections are written in its styles, page setup,
  headers and footers (its footer stays; page numbers are added only if it has
  none). The output is recorded as derived from the template.
- **Markdown that renders** (CommonMark + GFM): `#`–`######` headings, nested
  under the section heading; `**bold**`, `*italic*`, `~~strike~~`, `` `code` ``,
  fenced code; `> quote`; `[text](https://…)` as live hyperlinks; `[^1]`
  footnotes as real Word footnotes; ordered and bullet lists nested by
  indentation (indent a sub-list under the item's text — three spaces after
  `1.`); pipe tables with `:---:` alignment; `<br>`; `![alt](file.png)` for an
  image already under outputs/ (e.g. from `render_flowchart_image`). **Each
  line of a plain paragraph is its own paragraph** — party and signature blocks
  keep their lines. Other raw HTML (`<甲方名称>`) is printed as written.
- **Lists restart.** Every list numbers from its own first item ("3." starts at
  3), so a second 诉讼请求 list after a heading is 1, 2, 3 — never 19, 20, 21.

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
- **The header row repeats on every page and rows never split** (`w:tblHeader`,
  `w:cantSplit`). A table that runs onto a second page without a header row is
  reported by the render's lint as `table_header_not_repeated`.
- **Check the result.** `documents.quality.lint_docx` reports
  `table_columns_unweighted` and `cell_first_line_indent`, resolving the style
  chain rather than reading direct formatting — the defect usually lives on
  `Normal` and reaches the cell through `basedOn` — plus
  `list_numbering_continues` (a plain numbered list counting on across a
  heading) and `markdown_residue` (literal `**`, `[x](http`, `####`, `|---|`,
  `[^1]`, `~~`, `<br>`), each with its paragraph or cell.

## What the render cannot show

A positioned (floating) table — the 版记 a 公文 generator anchors to the foot
of the last page — is drawn by Word and WPS but not by this host's
LibreOffice. `render_docx_pages` lists any such table whose text is missing
from the pages under `floating_tables_not_rendered`; that is the preview's
limit, not the document's, so do not move or rebuild the table to make it
appear.

## Fonts in the render

The file names the GB fonts (仿宋, 黑体, 楷体, 宋体, 方正小标宋简体); the reader's
Word or WPS has them. The render host usually does not, so `render_docx_pages`
draws them with open stand-ins (serif for 宋/仿宋/楷, sans for 黑体, bold serif
for 小标宋) and lists them under `font_stand_ins`. A face that looks off in the
render for that reason is not a defect — do not change the document's fonts to
match it.

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

Every edit writes a new file. Naming the source itself as the output writes
`<name>_v2.docx` (then `_v3`, …) instead, and the result's `note` says so —
use the new path from then on. A result carrying `warning` means the file
you edited has a newer derived version; edit that one unless the user meant
the older.

When a file has tracked changes, comments, custom fields, or heavy template
styling, use `inspect_docx`'s tracked-change and comment counts to decide how
to touch it: new text near revisions goes in with `redline_docx`, and
`integrity` in every edit result shows whether anything was lost.

For new authoring, use `write_docx` and keep headings semantic, tables
simple, and lists rendered through Markdown bullets/numbering rather than
random characters. To start a fresh draft from the firm's or court's own
template, pass it as `reference_docx`.

Legacy `.doc` / `.wps` / `.rtf` / `.odt` files are read by `read_document`
and compared by `compare_docx`; to edit one, write the edited text as a new
`.docx` and say so.
