"""Unified document subsystem.

Implementation code for Office/PDF files lives here. Provider-facing tools in
``legal_helper.tools.documents`` should be thin wrappers over these functions.

Layout:

- ``office.py`` — DOCX / PPTX / XLSX inspect, extract, and edit.
- ``redline.py``— Native Word tracked changes (w:ins/w:del) + comments.
- ``pdf.py``    — PDF inspect, render, merge, split, rotate primitives.
- ``writers/``  — Polished artifact writers, one module per output format
  (docx, pdf, pptx, xlsx).
"""

from .office import (
    copy_xlsx_worksheet,
    edit_docx_text_document,
    edit_pptx_text,
    edit_xlsx_workbook_checked,
    edit_xlsx_workbook,
    diff_xlsx_workbooks,
    extract_docx_text,
    extract_pptx_text,
    extract_xlsx_text,
    inspect_docx_document,
    inspect_pptx_deck,
    inspect_xlsx_range_workbook,
    inspect_xlsx_workbook,
    render_xlsx_to_images,
    reshape_docx_document,
    reshape_pptx_deck,
    reshape_xlsx_workbook,
)
from .redline import (
    extract_docx_revisions,
    preview_docx_revisions,
    redline_docx_document,
)
from .pdf import (
    extract_pdf_tables_document,
    inspect_pdf_document,
    merge_pdf_files,
    read_pdf_text,
    render_pdf_to_images,
    rotate_pdf_file,
    split_pdf_file,
)
from .writers import (
    recalc_xlsx_workbook,
    write_docx_document,
    write_pdf_document,
    write_pptx_deck,
    write_xlsx_workbook,
)

__all__ = [
    "copy_xlsx_worksheet",
    "edit_docx_text_document",
    "edit_pptx_text",
    "edit_xlsx_workbook_checked",
    "edit_xlsx_workbook",
    "diff_xlsx_workbooks",
    "extract_docx_revisions",
    "extract_docx_text",
    "extract_pdf_tables_document",
    "extract_pptx_text",
    "extract_xlsx_text",
    "inspect_docx_document",
    "inspect_pdf_document",
    "inspect_pptx_deck",
    "inspect_xlsx_range_workbook",
    "inspect_xlsx_workbook",
    "merge_pdf_files",
    "preview_docx_revisions",
    "read_pdf_text",
    "recalc_xlsx_workbook",
    "redline_docx_document",
    "render_pdf_to_images",
    "render_xlsx_to_images",
    "reshape_docx_document",
    "reshape_pptx_deck",
    "reshape_xlsx_workbook",
    "rotate_pdf_file",
    "split_pdf_file",
    "write_docx_document",
    "write_pdf_document",
    "write_pptx_deck",
    "write_xlsx_workbook",
]
