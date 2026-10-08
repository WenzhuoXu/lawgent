"""PDF reads with page markers, scan detection and OCR (review item D-6).

14 of 92 uploaded PDFs were image-only and 3 mixed; ``read_document``
returned one stream with no page markers, and 15 reads came back under 200
characters with no warning.
"""

from __future__ import annotations

import json
from pathlib import Path

import pytest
from PIL import Image, ImageDraw, ImageFont

from legal_helper.tools import inspect_pdf, read_document

_FONT = Path("/usr/share/fonts/opentype/noto/NotoSansCJK-Regular.ttc")


def _scan_page(path: Path, lines: list[str]) -> Path:
    if not _FONT.is_file():
        pytest.skip("no CJK font to render the scan fixture")
    font = ImageFont.truetype(str(_FONT), 36)
    image = Image.new("RGB", (1240, 1754), "white")
    draw = ImageDraw.Draw(image)
    for i, line in enumerate(lines):
        draw.text((100, 150 + i * 90), line, fill="black", font=font)
    image.save(path, "PDF", resolution=150)
    return path


def _text_pdf(path: Path, pages: list[str]) -> Path:
    from reportlab.lib.pagesizes import A4
    from reportlab.pdfgen import canvas

    c = canvas.Canvas(str(path), pagesize=A4)
    for text in pages:
        y = 780
        for line in text.split("\n"):
            c.drawString(72, y, line)
            y -= 16
        c.showPage()
    c.save()
    return path


def _mixed(tmp_path: Path) -> Path:
    from pypdf import PdfReader, PdfWriter

    body = "\n".join(f"Clause {i}. The Lessee shall maintain the premises in good repair." for i in range(1, 12))
    text_pdf = _text_pdf(tmp_path / "text.pdf", [body, "Schedule A\n" + body])
    scan = _scan_page(tmp_path / "scan.pdf", ["民事判决书", "（2026）京0105民初12345号", "原告诉称，被告应支付租金人民币壹万元。"])
    writer = PdfWriter()
    for source in (text_pdf, scan):
        for page in PdfReader(str(source)).pages:
            writer.add_page(page)
    out = tmp_path / "mixed.pdf"
    with out.open("wb") as fh:
        writer.write(fh)
    return out


def test_pages_are_marked_and_selectable(tmp_path):
    pdf = _text_pdf(tmp_path / "three.pdf", ["first page text " * 10, "second page text " * 10, "third page text " * 10])
    text = read_document.call({"path": str(pdf)})
    assert "[page 1]\nfirst page" in text and "[page 3]\nthird page" in text
    two = read_document.call({"path": str(pdf), "pages": "2-"})
    assert two.startswith("[PDF: 3 pages; showing 2-3]")
    assert "[page 1]" not in two and "[page 2]" in two and "[page 3]" in two
    assert read_document.call({"path": str(pdf), "pages": "x"}).startswith("ERROR")


def test_scanned_page_is_ocrd_labelled_and_cached(tmp_path):
    from legal_helper.documents.ocr import ocr_unavailable_reason

    if ocr_unavailable_reason():
        pytest.skip(f"OCR unavailable: {ocr_unavailable_reason()}")
    pdf = _mixed(tmp_path)
    text = read_document.call({"path": str(pdf)})
    assert "[page 3 · OCR]" in text
    assert "[OCR: page 3 has no text layer" in text and "render_pdf_pages" in text
    ocr_part = text[text.index("[page 3 · OCR]"):]
    assert "民事判决书" in ocr_part and "京0105民初12345号" in ocr_part and "壹万元" in ocr_part
    assert "Clause 11." in text  # text pages keep their text layer
    from legal_helper.config import current_settings

    cached = list((current_settings().state_dir / "ocr_cache").rglob("*-p3.json"))
    assert len(cached) == 1
    assert json.loads(cached[0].read_text(encoding="utf-8"))["text"].startswith("民事判决书")
    payload = json.loads(inspect_pdf.call({"path": str(pdf)}))
    assert payload["image_only_pages"] == [3]


def test_a_near_empty_read_never_passes_silently(tmp_path):
    blank = tmp_path / "blank.pdf"
    Image.new("RGB", (600, 800), "white").save(blank, "PDF")
    text = read_document.call({"path": str(blank)})
    assert "[warning: only 0 characters of text came out of 1 page(s)" in text
    assert "render_pdf_pages" in text


def test_image_files_are_read_by_ocr(tmp_path):
    from legal_helper.documents.ocr import ocr_unavailable_reason

    if ocr_unavailable_reason() or not _FONT.is_file():
        pytest.skip("OCR or the CJK font is unavailable")
    font = ImageFont.truetype(str(_FONT), 40)
    image = Image.new("RGB", (900, 200), "white")
    ImageDraw.Draw(image).text((40, 60), "合同编号：HT-2026-001", fill="black", font=font)
    path = tmp_path / "stamp.png"
    image.save(path)
    text = read_document.call({"path": str(path)})
    assert text.startswith("[image stamp.png: text read by OCR")
    assert "HT-2026-001" in text


def test_rag_loader_reads_pdfs_without_pymupdf(tmp_path):
    """PyMuPDF is AGPL; the loader now uses pypdfium2 and nothing imports fitz."""
    import ast

    from legal_helper.rag.loaders import load_pdf

    pdf = _text_pdf(tmp_path / "statute.pdf", ["Article 1 Scope.", "Article 2 Definitions."])
    text = load_pdf(pdf)
    assert "Article 1 Scope." in text and "Article 2 Definitions." in text
    import warnings

    root = Path(__file__).resolve().parent.parent / "legal_helper"
    for source in root.rglob("*.py"):
        with warnings.catch_warnings():
            warnings.simplefilter("ignore")  # other modules' escape-sequence warnings are not this test's
            tree = ast.parse(source.read_text(encoding="utf-8"))
        for node in ast.walk(tree):
            names = []
            if isinstance(node, ast.Import):
                names = [a.name for a in node.names]
            elif isinstance(node, ast.ImportFrom):
                names = [node.module or ""]
            assert not any(n.split(".")[0] in {"fitz", "pymupdf"} for n in names), source


def test_write_pdf_output_is_measured_as_authored(tmp_path):
    """render_pdf_pages lints a PDF the harness wrote and only shows one it reads."""
    from weasyprint import HTML

    from legal_helper.tools import render_pdf_pages

    ours = tmp_path / "ours.pdf"
    HTML(string='<html><head><meta name="generator" content="legal_helper write_pdf"></head><body><p>memo</p></body></html>').write_pdf(str(ours))
    theirs = _text_pdf(tmp_path / "theirs.pdf", ["an upload"])
    mine = json.loads(render_pdf_pages.call({"path": str(ours)}))
    other = json.loads(render_pdf_pages.call({"path": str(theirs)}))
    assert "lint" in mine and "reading" not in mine
    assert "reading" in other and "lint" not in other
