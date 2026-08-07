"""Tiered document → markdown router used by the ``extract_*`` functions.

Three tiers, tried in order, so the cheap deterministic path handles the
overwhelming majority of files and the expensive model/OCR path is reserved
for pages that genuinely have no text layer:

  T0  ``anydoc`` (Firecrawl, MIT, Rust) — .doc/.docx/.ppt/.pptx/.xls/.xlsx/
      .odt/.ods/.odp/.rtf/.epub/.csv plus text-layer PDFs. No ML, no model
      call, ~5 ms median. Preserves merged table cells, nested lists, and
      footnotes better than the alternatives.
  T1  ``markitdown`` — fallback for anything anydoc rejects (.html, .msg,
      images with EXIF text, and any file that trips a T0 parser error).
  T2  the caller's own hand-rolled extractor (python-docx, pdfminer, …),
      invoked by the caller when both tiers above return empty.

``anydoc`` has no OCR: an image-only PDF parses successfully but yields
almost nothing. :func:`pdf_text_density` lets callers detect that case and
route to the vision path (``attachments.py`` already sends PDF pages to the
model as native multimodal blocks) instead of silently returning a blank
extraction.
"""

from __future__ import annotations

import os
import sys
from contextlib import contextmanager
from pathlib import Path
from typing import Iterator


# Formats anydoc parses natively. Anything else goes straight to markitdown.
_ANYDOC_SUFFIXES = frozenset({
    ".doc", ".docx", ".ppt", ".pptx", ".xls", ".xlsx",
    ".odt", ".ods", ".odp", ".rtf", ".epub", ".csv", ".pdf",
})

# Below this many extracted characters per page a PDF is treated as scanned.
# A dense legal page runs 1500-3000 chars; a header-only scan lands under 50.
_SCANNED_PDF_CHARS_PER_PAGE = 100


@contextmanager
def _silenced_stderr() -> Iterator[None]:
    """Suppress noisy onnxruntime GPU-probe warnings from markitdown deps."""
    saved = sys.stderr
    try:
        with open(os.devnull, "w") as devnull:
            sys.stderr = devnull
            yield
    finally:
        sys.stderr = saved


def anydoc_convert(path: Path) -> str:
    """Return markdown for ``path`` via anydoc. Raises on any failure."""
    import anydoc

    return anydoc.to_markdown(str(path)) or ""


def markitdown_convert(path: Path) -> str:
    """Return markdown for ``path`` via MarkItDown. Raises on any failure."""
    from markitdown import MarkItDown

    with _silenced_stderr():
        md = MarkItDown()
        result = md.convert(str(path))
    return getattr(result, "text_content", "") or ""


def convert_to_markdown(path: Path) -> str:
    """Return markdown for ``path``, trying anydoc then markitdown.

    Raises only when *both* tiers fail, so existing callers keep their
    hand-rolled T2 fallback semantics unchanged.
    """
    path = Path(path)
    errors: list[str] = []
    if path.suffix.lower() in _ANYDOC_SUFFIXES:
        try:
            text = anydoc_convert(path).strip()
            if text:
                return text
            errors.append("anydoc: empty result")
        except Exception as exc:
            errors.append(f"anydoc: {type(exc).__name__}: {exc}")
    try:
        return markitdown_convert(path)
    except Exception as exc:
        errors.append(f"markitdown: {type(exc).__name__}: {exc}")
        raise RuntimeError(f"no converter handled {path.name} — " + "; ".join(errors)) from exc


def pdf_text_density(path: Path, text: str | None = None) -> dict[str, object]:
    """Report how much of a PDF's text layer actually came through.

    ``text`` may be supplied when the caller already ran an extraction, to
    avoid parsing twice. Returns ``page_count``, ``chars``, ``chars_per_page``
    and ``likely_scanned`` — the last is the signal to route to OCR/vision
    rather than trusting a near-empty extraction.
    """
    path = Path(path)
    if text is None:
        try:
            text = convert_to_markdown(path)
        except Exception:
            text = ""
    chars = len((text or "").strip())
    page_count = 0
    try:
        import pypdf

        page_count = len(pypdf.PdfReader(str(path)).pages)
    except Exception:
        page_count = 0
    per_page = (chars / page_count) if page_count else float(chars)
    return {
        "page_count": page_count,
        "chars": chars,
        "chars_per_page": round(per_page, 1),
        "likely_scanned": per_page < _SCANNED_PDF_CHARS_PER_PAGE,
    }
