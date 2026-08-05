"""PDF primitives for inspection, rendering, and page operations."""

from __future__ import annotations

from pathlib import Path
from typing import Any


def read_pdf_text(path: Path) -> str:
    """Markdown via MarkItDown (preferred), pdfminer.six fallback."""
    from ._markitdown import markitdown_convert

    try:
        text = markitdown_convert(path).strip()
        if text:
            return text
    except Exception:
        pass
    from pdfminer.high_level import extract_text

    return extract_text(str(path)) or ""


def parse_page_spec(pages: str, page_count: int) -> list[int]:
    raw = (pages or "all").strip().lower()
    if raw in {"", "all", "*"}:
        return list(range(page_count))
    selected: set[int] = set()
    for part in raw.split(","):
        token = part.strip()
        if not token:
            continue
        if "-" in token:
            start_s, end_s = token.split("-", 1)
            start = max(1, int(start_s))
            end = min(page_count, int(end_s))
            selected.update(range(start - 1, end))
        else:
            page = int(token)
            if 1 <= page <= page_count:
                selected.add(page - 1)
    return sorted(selected)


def render_pdf_to_images(
    source: Path,
    out_dir: Path,
    prefix: str,
    dpi: int,
    *,
    first_page: int | None = None,
    last_page: int | None = None,
    pdftoppm: str | None = None,
) -> dict[str, Any]:
    """Render PDF pages to JPEGs via in-process pypdfium2.

    ``pdftoppm`` kwarg is retained for back-compat with call sites; it is no
    longer used.
    """
    import pypdfium2 as pdfium

    dpi_value = max(72, min(int(dpi), 200))
    scale = dpi_value / 72.0
    out_dir.mkdir(parents=True, exist_ok=True)
    pdf = pdfium.PdfDocument(str(source))
    try:
        total = len(pdf)
        start = max(1, int(first_page)) if first_page is not None else 1
        end = min(total, int(last_page)) if last_page is not None else total
        outputs: list[str] = []
        # pdftoppm uses 1-based numeric suffixes padded to the page count width.
        width = max(1, len(str(total)))
        for page_num in range(start, end + 1):
            page = pdf[page_num - 1]
            try:
                bitmap = page.render(scale=scale)
                pil_image = bitmap.to_pil()
                out = out_dir / f"{prefix}-{page_num:0{width}d}.jpg"
                pil_image.save(out, format="JPEG", quality=90)
                outputs.append(str(out.resolve()))
            finally:
                page.close()
        return {"images": outputs, "count": len(outputs)}
    finally:
        pdf.close()


def inspect_pdf_document(path: Path, *, preview_chars: int = 800) -> dict[str, Any]:
    from pypdf import PdfReader

    reader = PdfReader(str(path))
    meta = reader.metadata or {}
    limit = max(0, min(int(preview_chars), 2000))
    pages = []
    for idx, page in enumerate(reader.pages, start=1):
        text = page.extract_text() or ""
        pages.append(
            {
                "page": idx,
                "rotation": page.get("/Rotate", 0),
                "text_chars": len(text),
                "preview": text[:limit],
            }
        )
    return {
        "filename": path.name,
        "page_count": len(reader.pages),
        "is_encrypted": reader.is_encrypted,
        "metadata": {str(k).lstrip("/"): str(v) for k, v in meta.items()},
        "pages": pages,
    }


def extract_pdf_tables_document(path: Path, *, max_pages: int = 20) -> dict[str, Any]:
    import pdfplumber

    limit = max(1, min(int(max_pages), 100))
    tables = []
    with pdfplumber.open(str(path)) as pdf:
        for page_idx, page in enumerate(pdf.pages[:limit], start=1):
            for table_idx, table in enumerate(page.extract_tables() or [], start=1):
                tables.append({"page": page_idx, "table": table_idx, "rows": table})
    return {"filename": path.name, "table_count": len(tables), "tables": tables}


def merge_pdf_files(paths: list[Path], output_path: Path) -> dict[str, Any]:
    from pypdf import PdfReader, PdfWriter

    output_path.parent.mkdir(parents=True, exist_ok=True)
    writer = PdfWriter()
    resolved = []
    for path in paths:
        resolved.append(str(path.resolve()))
        reader = PdfReader(str(path))
        for page in reader.pages:
            writer.add_page(page)
    with output_path.open("wb") as f:
        writer.write(f)
    return {"output_path": str(output_path.resolve()), "inputs": resolved}


def split_pdf_file(source: Path, out_dir: Path, prefix: str, pages: str = "all") -> dict[str, Any]:
    from pypdf import PdfReader, PdfWriter

    out_dir.mkdir(parents=True, exist_ok=True)
    reader = PdfReader(str(source))
    selected = parse_page_spec(pages, len(reader.pages))
    outputs = []
    for page_idx in selected:
        writer = PdfWriter()
        writer.add_page(reader.pages[page_idx])
        out = out_dir / f"{prefix}_page_{page_idx + 1}.pdf"
        with out.open("wb") as f:
            writer.write(f)
        outputs.append(str(out.resolve()))
    return {"outputs": outputs, "count": len(outputs)}


def rotate_pdf_file(source: Path, output_path: Path, *, pages: str = "all", degrees: int = 90) -> Path:
    from pypdf import PdfReader, PdfWriter

    output_path.parent.mkdir(parents=True, exist_ok=True)
    reader = PdfReader(str(source))
    writer = PdfWriter()
    selected = set(parse_page_spec(pages, len(reader.pages)))
    rotation = int(degrees) % 360
    if rotation not in {0, 90, 180, 270}:
        raise ValueError("degrees must normalize to one of 0, 90, 180, 270")
    for idx, page in enumerate(reader.pages):
        if idx in selected and rotation:
            page.rotate(rotation)
        writer.add_page(page)
    with output_path.open("wb") as f:
        writer.write(f)
    return output_path
