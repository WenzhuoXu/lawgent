# XLSX Recipe

Read this **before** any non-trivial spreadsheet edit. The structural vs.
scalar choice is the most common source of bad xlsx outputs.

## Tools at a glance

| Tool | When to use |
|---|---|
| `inspect_xlsx` | First call on any unknown workbook — sheets, dimensions, formula counts, preview rows. |
| `inspect_xlsx_range` | Before editing a region — real cell coordinates, formulas, number formats, merged ranges, hidden rows/columns. |
| `read_document` | Quick text dump only. Never derive edit coordinates from its markdown output: blank rows and merged headers shift row numbers. |
| `write_xlsx` | Build a new workbook from scratch (rows + charts + conditional formats + data validations). |
| `edit_xlsx_cells` | Low-risk scalar cell replacements. |
| `edit_xlsx_cells_checked` | High-stakes scalar replacements with `check_expected=true` preconditions and `protected_ranges` guards. |
| `reshape_xlsx` | **Any structural reshape** — insert / delete / move rows or columns, merge / unmerge, copy a styled row, bulk styling, row heights, column widths. |
| `copy_xlsx_sheet` | Duplicate a styled worksheet under a new name (e.g. `A类` → `B类`). |
| `diff_xlsx` | Prove what changed and that protected ranges did not. |
| `render_xlsx_pages` | Visual QA — soffice → PDF → JPGs. Catches broken merges, gap rows, overflowing wrap-text. |

## The cardinal rule

**Never simulate a structural reshape with a string of scalar `edit_xlsx_cells`
calls.** If the user asks you to insert / delete / move / level-shift / dedupe
/ reorder / restyle rows, the right tool is `reshape_xlsx`. Scalar edits will
leave behind empty gap rows, duplicate labels, broken merges, and orphan
styling — exactly the failure mode of `附件4_手册地图模板_..._A类调整版.xlsx`.

## Standard workflow

1. `inspect_xlsx(path)` to learn sheets, dimensions, formula counts.
2. `inspect_xlsx_range(path, sheet, "A1:K30")` (or whatever covers the edit
   region) for real coordinates, formulas, merges, hidden rows/cols.
3. Choose tool by **intent**, not habit:
   - cell value swap → `edit_xlsx_cells_checked` (always set
     `check_expected=true` and pass `protected_ranges`)
   - structural reshape → `reshape_xlsx` (see ops below)
   - new sibling sheet from template → `copy_xlsx_sheet`
   - new workbook from scratch → `write_xlsx`
4. `diff_xlsx(source, output, ranges=[...])` to prove changed range == intent
   and `protected_ranges` have `diff_count=0`.
5. When layout matters, `render_xlsx_pages(output)` and look at the JPGs for
   broken merges, missing borders, overflowing wrap-text, gap rows.
6. `inspect_xlsx` / `inspect_xlsx_range` the output one more time before
   declaring done.

## `reshape_xlsx` operations

Each entry in `operations` is `{"op": "<kind>", "sheet": "<name>", ...}`.
Ops apply in order; later ops see the state left by earlier ones.

- `insert_rows` / `delete_rows` — `at` (1-based row), `count` (default 1).
- `insert_cols` / `delete_cols` — `at` (1-based col index), `count`.
- `move_range` — `range` (`"A15:E18"`), `rows`, `cols`, `translate` (bool,
  default true; rewrites formula refs). Use this for level-shifts like
  `A15:A18` → `B15:B18`.
- `copy_row` — `src_row`, `dest_row`. Copies values, per-cell styles
  (font/fill/border/alignment/number_format), row height, and any
  single-row merges. Use to duplicate a template row before populating.
- `merge_range` / `unmerge_range` — `range`.
- `set_styles` — `range` plus any of `font_name`, `font_size`, `font_bold`,
  `font_italic`, `font_color`, `fill_color`, `border` (`"thin"` /
  `"medium"` / `"thick"` for all four sides, or
  `{"top":"thin","bottom":"thin"}`), `align_h`, `align_v`, `wrap_text`,
  `number_format`.
- `set_row_height` — `row`, `height` (points).
- `set_col_width` — `col` (letter or 1-based index), `width` (Excel units).

## Formulas

Strings beginning with `=` in `write_xlsx`'s row values are persisted as live
formulas (e.g. `"=SUM(B2:B9)"`). On save, `write_xlsx` runs a pure-Python
recalc pass and surfaces any `#REF!` / `#DIV/0!` / `#NAME?` / `#VALUE!` /
`#N/A` / `#NUM!` / `#NULL!` errors as `formula_errors` on the return value.
Fix every flagged error before declaring the workbook done. When using
`reshape_xlsx`'s `move_range`, keep `translate=true` so formula refs follow
the moved cells.

## Charts (write_xlsx only)

Each `Sheet` accepts a `charts` list (`XlsxChart`): `type` in
{bar, line, pie, scatter}, `title`, `anchor` (e.g. `F2`),
`categories_range` (e.g. `A2:A11`), `series` (each `name` +
`values_range`). Use for risk waterfalls, severity distributions, payment
schedules.

## Conditional formatting (write_xlsx only)

Each `Sheet` accepts a `conditional_formats` list:

- `type="color_scale"` — 3-color heatmap; tune `color_min`/`color_mid`/`color_max`.
- `type="cell_is"` — operator-based literal comparison
  (`operator="equal"`, `formula=['"RED"']`). Add `background`,
  `foreground`, `bold` to style matching cells.
- `type="formula"` — arbitrary expression in `formula[0]`, same styling fields.
- `type="data_bar"` — in-cell bar chart; tune `color`.

## Data validation (write_xlsx only)

Each `Sheet` accepts a `data_validations` list. Drop-down:
`type="list"`, `formula1='"RED,YELLOW,GREEN"'`. Numeric range:
`type="whole"` (or `"decimal"`), `operator="between"`,
`formula1`/`formula2`. Add `prompt` (in-cell hint) and `error_msg`
(rejection message).

## Template hygiene

When the user uploads an existing workbook to edit, **never rebuild it
with `write_xlsx`** — that drops every fill, border, validation, merge,
and named style. Operate on a copy via `edit_xlsx_cells_checked`,
`reshape_xlsx`, or `copy_xlsx_sheet`. Preserve formulas, number formats,
colors, merged cells, sheet names, file/manual names, and protected
columns unless the user explicitly asks for restructuring.

For financial models created from scratch, use conventional colors: blue
for hardcoded inputs, black for formulas, green for same-workbook links,
red for external links, yellow fill for key assumptions.
