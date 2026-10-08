"""PDF primitives for inspection, rendering, and page operations."""

from __future__ import annotations

from dataclasses import dataclass
from pathlib import Path
from typing import Any, Optional

# A page with fewer non-whitespace characters than this, whose area is mostly
# image, is a scan: a dense legal page runs 1,500-3,000 characters, while a
# scanner's own text layer (a page number, "第1页") stays well under it.
SCANNED_PAGE_CHARS = 100
SCANNED_PAGE_IMAGE_SHARE = 0.25
# Below this many characters in the whole read, say so: 15 reads returned
# under 200 characters with no warning and were answered from as if complete.
LOW_TEXT_WARNING_CHARS = 200


@dataclass
class PageLayer:
    number: int  # 1-based
    text: str
    chars: int  # non-whitespace characters in the text layer
    image_share: float  # share of the page area covered by images (0-1)

    @property
    def image_only(self) -> bool:
        if self.chars >= SCANNED_PAGE_CHARS:
            return False
        return self.image_share >= SCANNED_PAGE_IMAGE_SHARE or (self.chars == 0 and self.image_share > 0)


def _image_share(page: Any, width: float, height: float) -> float:
    import pypdfium2.raw as pdfium_c

    area = max(width * height, 1.0)
    covered = 0.0
    try:
        for obj in page.get_objects(filter=[pdfium_c.FPDF_PAGEOBJ_IMAGE], max_depth=4):
            left, bottom, right, top = obj.get_bounds()
            w = max(0.0, min(right, width) - max(left, 0.0))
            h = max(0.0, min(top, height) - max(bottom, 0.0))
            covered += w * h
    except Exception:  # noqa: BLE001 — a malformed object must not fail the read
        return 0.0
    return min(1.0, covered / area)


def page_layers(pdf: Any, indices: list[int]) -> list[PageLayer]:
    """Text layer and image coverage of each selected page (0-based indices)."""
    out: list[PageLayer] = []
    for idx in indices:
        page = pdf[idx]
        try:
            width, height = page.get_size()
            textpage = page.get_textpage()
            try:
                text = textpage.get_text_range() or ""
            finally:
                textpage.close()
            text = text.replace("\r\n", "\n").replace("\r", "\n").replace("\x00", "")
            chars = sum(1 for ch in text if not ch.isspace())
            out.append(PageLayer(idx + 1, text.strip(), chars, _image_share(page, width, height)))
        finally:
            page.close()
    return out


def pdf_page_count(path: Path) -> int:
    import pypdfium2 as pdfium

    pdf = pdfium.PdfDocument(str(path))
    try:
        return len(pdf)
    finally:
        pdf.close()


def read_pdf_text(path: Path, *, pages: Optional[str] = None, ocr: bool = True) -> str:
    """Text of a PDF with ``[page N]`` markers; image-only pages are OCR'd.

    ``pages`` is a 1-based spec (``"3-5,8"``); default all. A page whose text
    layer is (nearly) empty and whose area is mostly image is read by OCR and
    marked ``[page N · OCR]``, under a header naming the pages and the mean
    recognition confidence. A read that still yields under 200 characters
    says so instead of returning a near-empty string as if it were complete.
    """
    import pypdfium2 as pdfium

    path = Path(path)
    try:
        pdf = pdfium.PdfDocument(str(path))
    except Exception as exc:  # noqa: BLE001 — encrypted / malformed: older path
        return _read_pdf_fallback(path, f"{type(exc).__name__}: {exc}")
    try:
        total = len(pdf)
        selected = parse_page_spec(pages or "all", total)
        layers = page_layers(pdf, selected)
        ocr_done: list[tuple[int, float]] = []
        ocr_failed: list[int] = []
        ocr_error = ""
        sha = ""
        blocks: list[str] = []
        content_chars = 0
        for layer in layers:
            marker = f"[page {layer.number}]"
            text = layer.text
            if layer.image_only:
                if ocr:
                    try:
                        from .ocr import file_sha256, ocr_pdf_page

                        sha = sha or file_sha256(path)
                        result = ocr_pdf_page(pdf, layer.number - 1, sha)
                        if result.text.strip():
                            text = result.text.strip()
                            marker = f"[page {layer.number} · OCR]"
                            ocr_done.append((layer.number, result.mean_score))
                        else:
                            ocr_failed.append(layer.number)
                    except Exception as exc:  # noqa: BLE001 — degrade to a stated gap
                        ocr_failed.append(layer.number)
                        ocr_error = ocr_error or f"{type(exc).__name__}: {exc}"
                else:
                    ocr_failed.append(layer.number)
            content_chars += sum(1 for ch in text if not ch.isspace())
            blocks.append(f"{marker}\n{text}" if text else f"{marker}\n[no text on this page]")
    finally:
        pdf.close()
    header: list[str] = []
    if pages and len(selected) < total:
        header.append(f"[PDF: {total} pages; showing {_spec(selected)}]")
    if ocr_done:
        mean = sum(score for _, score in ocr_done) / len(ocr_done)
        header.append(
            f"[OCR: page{'s' if len(ocr_done) > 1 else ''} {_spec([n - 1 for n, _ in ocr_done])} "
            f"{'have' if len(ocr_done) > 1 else 'has'} no text layer and "
            f"{'were' if len(ocr_done) > 1 else 'was'} read by OCR (RapidOCR PP-OCRv6, mean confidence "
            f"{mean:.2f}). Recognition errors are possible: check names, amounts, dates and article "
            "numbers against the page image with render_pdf_pages before relying on them.]"
        )
    if ocr_failed:
        reason = ocr_error or ("OCR was not requested" if not ocr else "OCR found no text")
        header.append(
            f"[warning: page{'s' if len(ocr_failed) > 1 else ''} {_spec([n - 1 for n in ocr_failed])} "
            f"{'are' if len(ocr_failed) > 1 else 'is'} image-only and could not be read as text ({reason}); "
            "look at them with render_pdf_pages.]"
        )
    body = "\n\n".join(blocks)
    chars = content_chars
    if chars < LOW_TEXT_WARNING_CHARS:
        header.append(
            f"[warning: only {chars} characters of text came out of {len(selected)} page(s); the file may be "
            "a scan, a form or mostly images. Do not treat this as the whole document — look at the pages "
            "with render_pdf_pages.]"
        )
    return "\n".join(header + [body]).strip()


def _spec(indices: list[int]) -> str:
    """0-based indices as a compact 1-based spec: 1-3, 5, 8-9."""
    numbers = sorted({i + 1 for i in indices})
    parts: list[str] = []
    start = prev = None
    for n in numbers:
        if start is None:
            start = prev = n
        elif n == prev + 1:
            prev = n
        else:
            parts.append(f"{start}-{prev}" if start != prev else str(start))
            start = prev = n
    if start is not None:
        parts.append(f"{start}-{prev}" if start != prev else str(start))
    return ", ".join(parts)


def _read_pdf_fallback(path: Path, reason: str) -> str:
    from ._convert import convert_to_markdown

    text = ""
    try:
        text = convert_to_markdown(path).strip()
    except Exception:  # noqa: BLE001
        text = ""
    if not text:
        try:
            from pdfminer.high_level import extract_text

            text = extract_text(str(path)) or ""
        except Exception as exc:  # noqa: BLE001
            return f"ERROR: could not read {path.name}: {reason}; pdfminer: {exc}"
    note = f"[read without page markers: pypdfium2 could not open the file ({reason[:160]})]"
    if len(text.strip()) < LOW_TEXT_WARNING_CHARS:
        note += f"\n[warning: only {len(text.strip())} characters of text; the file may be a scan or encrypted]"
    return f"{note}\n{text}".strip()


def parse_page_spec(pages: str, page_count: int) -> list[int]:
    """0-based indices for a 1-based spec: ``all``, ``3``, ``3-5``, ``3-`` (to
    the end), ``-2`` (from the start), comma-separated (``，`` and ``~`` too)."""
    raw = (pages or "all").strip().lower().replace("，", ",").replace("~", "-").replace("–", "-")
    if raw in {"", "all", "*"}:
        return list(range(page_count))
    selected: set[int] = set()
    for part in raw.split(","):
        token = part.strip().replace(" ", "")
        if not token:
            continue
        try:
            if "-" in token:
                start_s, end_s = token.split("-", 1)
                start = max(1, int(start_s)) if start_s else 1
                end = min(page_count, int(end_s)) if end_s else page_count
                selected.update(range(start - 1, end))
            else:
                page = int(token)
                if 1 <= page <= page_count:
                    selected.add(page - 1)
        except ValueError as exc:
            raise ValueError(f"bad page spec {pages!r}: use e.g. '3', '3-5', '3-', '1,4,7-9'") from exc
    return sorted(selected)


def render_pdf_to_images(
    source: Path,
    out_dir: Path,
    prefix: str,
    dpi: int,
    *,
    first_page: int | None = None,
    last_page: int | None = None,
) -> dict[str, Any]:
    """Render PDF pages to JPEGs via in-process pypdfium2."""
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
        # 1-based numeric suffixes padded to the page count width, matching what
        # pdftoppm produced before this moved in-process, so every glob still works.
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
    """Pages, metadata and, per page, whether it is image-only (needs OCR).

    Text and image coverage come from pypdfium2, the same measurement
    ``read_document`` uses to decide which pages to OCR, so the two agree.
    """
    from pypdf import PdfReader

    reader = PdfReader(str(path))
    meta = reader.metadata or {}
    limit = max(0, min(int(preview_chars), 2000))
    layers: dict[int, PageLayer] = {}
    try:
        import pypdfium2 as pdfium

        pdf = pdfium.PdfDocument(str(path))
        try:
            layers = {layer.number: layer for layer in page_layers(pdf, list(range(len(pdf))))}
        finally:
            pdf.close()
    except Exception:  # noqa: BLE001 — fall back to pypdf text below
        layers = {}
    pages = []
    for idx, page in enumerate(reader.pages, start=1):
        layer = layers.get(idx)
        text = layer.text if layer is not None else (page.extract_text() or "")
        entry: dict[str, Any] = {
            "page": idx,
            "rotation": page.get("/Rotate", 0),
            "text_chars": len(text),
            "preview": text[:limit],
        }
        if layer is not None and layer.image_only:
            entry["image_only"] = True
        pages.append(entry)
    image_only = [p["page"] for p in pages if p.get("image_only")]
    payload: dict[str, Any] = {
        "filename": path.name,
        "page_count": len(reader.pages),
        "is_encrypted": reader.is_encrypted,
        "metadata": {str(k).lstrip("/"): str(v) for k, v in meta.items()},
        "pages": pages,
    }
    if image_only:
        payload["image_only_pages"] = image_only
        payload["note"] = (
            "image_only pages have no usable text layer; read_document OCRs them (pass pages= to "
            "limit the work) and render_pdf_pages shows them."
        )
    return payload


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
