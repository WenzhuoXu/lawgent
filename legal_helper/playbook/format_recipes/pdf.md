# PDF Recipe

## Tools at a glance

| Tool | When to use |
|---|---|
| `inspect_pdf` | Page count, metadata, encryption status, per-page previews, and `image_only_pages` — scanned pages with no usable text layer. |
| `read_document` | Text, each page marked `[page N]` for pinpoints; `pages="3-5,8"` reads part of a long file; scanned pages are OCR'd and marked as OCR. Also reads OFD. |
| `extract_pdf_tables` | Tabular PDFs. Expect imperfect extraction on scanned or visually complex pages — verify headers and row alignment before reusing. |
| `merge_pdfs`, `split_pdf`, `rotate_pdf_pages` | Page operations. Always write a new output file and preserve originals. For legal bundles, keep page order explicit in the task or output notes. |
| `write_pdf` | Render Markdown into a finished PDF (see below). |
| `render_pdf_pages` | Visual QA / scanned documents. Look for blank pages, missing stamps/signatures, clipped margins, unexpected rotation. |

Rendering runs in-process via pypdfium2 (no `pdftoppm` binary required); the
same path backs `render_docx_pages`, `render_pptx_slides`, and
`render_xlsx_pages` after their soffice PDF conversion.

## Writing a PDF

`write_pdf(filename, title, body_markdown, lang="auto", layout="memo",
page_size="auto", disclaimer="properties", disclaimer_reason="", subtitle="")`

- **The same Markdown as `write_docx`**, through the same parser: headings,
  `**bold**` (also next to Chinese punctuation, `**结论：**正文`), `~~strike~~`,
  lists nested by indentation, pipe tables, `[text](https://…)` links,
  `[^1]` footnotes printed at the foot of the page, `<br>`. Each line of a
  plain paragraph is its own paragraph; other raw HTML is printed as text.
- **Page.** Chinese content (most words Chinese, or `lang="zh"`) is A4 with
  "第 X 页 共 Y 页"; English is Letter with "Page X of Y"; `page_size`
  overrides. `layout="gbt9704"` gives the GB/T 9704 公文 page: 37/26/35/28 mm
  margins, 3号仿宋 on a 28 pt line, 28 characters a line, 一、（一）1.（1）
  levels in 黑体/楷体/仿宋, "— n —" page numbers outside.
- **Tables** repeat their header row on every page and keep rows whole.
- **Disclaimer.** The not-legal-advice notice is the PDF's Subject metadata in
  the document's language, not page content. Pass `disclaimer="none"` with a
  `disclaimer_reason` for a filing or a signed letter.
- **Fonts.** A PDF embeds the faces it was drawn with: the GB fonts when the
  server has them, open Noto CJK faces otherwise.
- **Nothing is fetched.** Images load only when they are already under
  outputs/; every other URL in the document is refused.
- `write_pdf` returns the path, or `{"path", "lint"}` when the written text
  still contains literal Markdown — rewrite the source that produced it.

## Scans

`read_document` OCRs every page without a usable text layer (RapidOCR, on the
CPU) and marks those pages as OCR'd. OCR is evidence of what the page
probably says, not a certified transcript: for a figure, date, name or amount
the answer turns on, look at the page itself with `render_pdf_pages` (or
`view_image` for fine detail) before relying on it, and say when a reading is
uncertain. When OCR is unavailable the result says so — render the page and
read it from the image rather than inventing text.
