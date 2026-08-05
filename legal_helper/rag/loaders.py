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
    """Extract text from a PDF using PyMuPDF.

    Returns plain text with one blank line between pages. If a page yields
    almost no characters (likely a scanned page or image-only), it is
    skipped silently; the caller can detect a short total and flag for
    OCR follow-up.
    """
    if not _is_valid_pdf(path):
        raise RuntimeError(
            f"Not a PDF (likely an HTML error page): {path}. "
            f"Delete the file and re-fetch with a corrected URL."
        )
    try:
        import fitz  # PyMuPDF
    except ImportError as e:
        raise RuntimeError(
            "PyMuPDF not installed. Add `pymupdf>=1.24` to requirements.txt "
            "and `pip install pymupdf`."
        ) from e

    out: list[str] = []
    with fitz.open(path) as doc:
        for page in doc:
            # First try the default extractor — handles single-column well.
            text = page.get_text("text")
            if len(text.strip()) < 80:
                # Likely a 2-column layout; fall back to block extraction
                # sorted top-to-bottom, left-to-right.
                blocks = page.get_text("blocks") or []
                blocks = sorted(blocks, key=lambda b: (round(b[1], 1), round(b[0], 1)))
                text = "\n".join(b[4] for b in blocks if len(b) >= 5)
            if text and text.strip():
                out.append(text)
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
