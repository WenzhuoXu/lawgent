"""Thin MarkItDown wrapper used by the extract_* functions.

MarkItDown gives a single, well-maintained path from .docx/.pptx/.xlsx/.pdf/
.html/.csv to markdown. Callers fall back to hand-rolled extractors when it
is unavailable or raises on a specific file.
"""

from __future__ import annotations

import os
import sys
from contextlib import contextmanager
from pathlib import Path
from typing import Iterator


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


def markitdown_convert(path: Path) -> str:
    """Return markdown text for ``path`` via MarkItDown.

    Raises on any failure; callers handle fallback.
    """
    from markitdown import MarkItDown

    with _silenced_stderr():
        md = MarkItDown()
        result = md.convert(str(path))
    return getattr(result, "text_content", "") or ""
