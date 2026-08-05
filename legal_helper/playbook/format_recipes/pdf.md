# PDF Recipe

## Tools at a glance

| Tool | When to use |
|---|---|
| `inspect_pdf` | Page count, metadata, text availability, encryption status, per-page previews. |
| `read_document` | Text extraction — routes through MarkItDown (rich markdown for tables/headings/lists), falls back to pdfminer.six. |
| `extract_pdf_tables` | Tabular PDFs. Expect imperfect extraction on scanned or visually complex pages — verify headers and row alignment before reusing. |
| `merge_pdfs`, `split_pdf`, `rotate_pdf_pages` | Page operations. Always write a new output file and preserve originals. For legal bundles, keep page order explicit in the task or output notes. |
| `write_pdf` | Render markdown into a polished PDF. |
| `render_pdf_pages` | Visual QA / scanned documents. Look for blank pages, missing stamps/signatures, clipped margins, unexpected rotation. |

Rendering runs in-process via pypdfium2 (no `pdftoppm` binary required); the
same path backs `render_docx_pages`, `render_pptx_slides`, and
`render_xlsx_pages` after their soffice PDF conversion.

OCR requires both a Python wrapper and a system OCR binary. If the binary is
not available, render page images and report that OCR is unavailable rather
than inventing text.
