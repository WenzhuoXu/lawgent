"""Document loaders for the RAG ingest pipeline.

Returns plain UTF-8 text from PDF, HTML, or plain-text sources so the
existing legal-hierarchical chunker (``chunker.chunk_document``) can run
unchanged. Each loader is lazy on its heavy dependency to keep startup
cheap for skills that never touch RAG.
"""

from __future__ import annotations

import re
from pathlib import Path
from typing import Callable

_TEXT_EXTS = {".md", ".txt", ".rst"}


def load_text(path: Path) -> str:
    return path.read_text(encoding="utf-8", errors="ignore")


def _is_valid_pdf(path: Path) -> bool:
    """Reject HTML error pages saved with a .pdf extension. A real PDF
    starts with ``%PDF-`` within the first ~8 bytes."""
    try:
        with path.open("rb") as f:
            head = f.read(8)
    except OSError:
        return False
    return b"%PDF-" in head


def load_pdf(path: Path) -> str:
    """Extract text from a PDF with pypdfium2 (Apache-2.0 / BSD).

    Returns plain text with one blank line between pages. A page with no text
    layer (a scan) contributes nothing; the caller can detect a short total
    and flag it for OCR (``documents.pdf.read_pdf_text`` OCRs such pages).

    This used PyMuPDF, which is AGPL — not shippable in this MIT tree or its
    public mirror. PDFium's text order follows the content stream, which for
    the two-column statute layouts the old block-sorting fallback handled is
    already column order.
    """
    if not _is_valid_pdf(path):
        raise RuntimeError(
            f"Not a PDF (likely an HTML error page): {path}. "
            f"Delete the file and re-fetch with a corrected URL."
        )
    import pypdfium2 as pdfium

    out: list[str] = []
    pdf = pdfium.PdfDocument(str(path))
    try:
        for index in range(len(pdf)):
            page = pdf[index]
            try:
                textpage = page.get_textpage()
                try:
                    text = textpage.get_text_range() or ""
                finally:
                    textpage.close()
            finally:
                page.close()
            text = text.replace("\r\n", "\n").replace("\r", "\n")
            if text.strip():
                out.append(text)
    finally:
        pdf.close()
    return _normalize_whitespace("\n\n".join(out))


def load_html(path: Path) -> str:
    try:
        from bs4 import BeautifulSoup
    except ImportError as e:
        raise RuntimeError("beautifulsoup4 not installed.") from e
    raw = path.read_text(encoding="utf-8", errors="ignore")
    soup = BeautifulSoup(raw, "lxml")
    for tag in soup(["script", "style", "noscript", "header", "footer", "nav"]):
        tag.decompose()
    text = soup.get_text(separator="\n")
    return _normalize_whitespace(text)


def _normalize_whitespace(text: str) -> str:
    """Collapse runs of blank lines but preserve paragraph breaks; trim
    trailing spaces per line."""
    lines = [ln.rstrip() for ln in text.splitlines()]
    out: list[str] = []
    blank_run = 0
    for ln in lines:
        if ln.strip():
            out.append(re.sub(r"[ \t]+", " ", ln))
            blank_run = 0
        else:
            blank_run += 1
            if blank_run <= 1:
                out.append("")
    return "\n".join(out).strip()


_LOADERS: dict[str, Callable[[Path], str]] = {
    ".pdf": load_pdf,
    ".html": load_html,
    ".htm": load_html,
    ".md": load_text,
    ".txt": load_text,
    ".rst": load_text,
}


def supported_suffixes() -> set[str]:
    return set(_LOADERS.keys())


def load_any(path: Path) -> str:
    """Dispatch on suffix. Raises ValueError for unsupported types."""
    loader = _LOADERS.get(path.suffix.lower())
    if loader is None:
        raise ValueError(f"Unsupported file type: {path.suffix} ({path})")
    return loader(path)
