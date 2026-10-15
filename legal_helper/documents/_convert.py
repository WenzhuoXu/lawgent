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
almost nothing. PDFs therefore do not come through here on the read path:
:func:`legal_helper.documents.pdf.read_pdf_text` measures each page's text
layer, OCRs the image-only pages and marks every page.

Word-processing files (.docx, and .doc/.rtf/.odt/.wps via LibreOffice) are
read by :mod:`.docx_reader`, which keeps tracked changes, headers/footers and
numbering labels this converter loses; see :mod:`.intake`.
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

    Raises only when *both* tiers fail, so a caller can fall back to its own
    T2 extractor.
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
