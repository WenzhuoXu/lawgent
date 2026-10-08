"""Read any supported file as text — one dispatcher for ``read_document`` and
attachment ingestion.

The two used to dispatch separately, on seven suffixes, and fall back to
``read_text(errors="replace")``. Every chat that uploaded a ``.doc`` (9 of
them, 64 reads, 4.5M characters) got binary garbage back, one file re-read 23
times, although a converter that reads ``.doc`` correctly was already in the
tree. Now:

- Word-processing formats (.docx/.docm/.dotx, and .doc/.rtf/.odt/.wps via a
  LibreOffice conversion to .docx) go through :mod:`.docx_reader`, so tracked
  changes, headers/footers and 第一条-style labels survive. Reading a ``.doc``
  through anydoc directly showed a tracked deletion as live text ("租期为三五年");
  LibreOffice keeps it as a revision. Without LibreOffice, anydoc is the
  fallback and the result says so.
- Spreadsheets and decks in legacy formats (.xls/.ods/.et, .ppt/.odp/.dps) use
  the tiered converter, then LibreOffice if it fails.
- PDFs get page markers and OCR (:mod:`.pdf`), OFD its own reader
  (:mod:`.ofd`), images OCR (:mod:`.ocr`), a .zip its member list.
- A file is read as text only if it *is* text — no NUL bytes, decodes as
  UTF-8/UTF-16 or GB18030 (common for Chinese .txt files). Anything else is a
  clear "unsupported binary format" error naming what is supported.
"""

from __future__ import annotations

import codecs
import hashlib
import os
import re
import shutil
import tempfile
import threading
import zipfile
from pathlib import Path
from typing import Optional

WORD_SUFFIXES = {".docx", ".docm", ".dotx", ".dotm"}
WORD_LEGACY_SUFFIXES = {".doc", ".dot", ".rtf", ".odt", ".ott", ".wps", ".wpt", ".fodt"}
SHEET_SUFFIXES = {".xlsx", ".xlsm"}
SHEET_LEGACY_SUFFIXES = {".xls", ".ods", ".et", ".ett", ".fods"}
SLIDE_SUFFIXES = {".pptx", ".pptm", ".ppsx"}
SLIDE_LEGACY_SUFFIXES = {".ppt", ".pps", ".odp", ".dps", ".dpt", ".fodp"}
IMAGE_SUFFIXES = {".png", ".jpg", ".jpeg", ".gif", ".webp", ".bmp", ".tif", ".tiff", ".heic", ".heif"}
OTHER_SUFFIXES = {".pdf", ".ofd", ".epub", ".zip"}
# Kingsoft WPS formats are their Microsoft counterparts' binary layout.
_KINGSOFT_ALIAS = {".wps": ".doc", ".wpt": ".dot", ".et": ".xls", ".ett": ".xls", ".dps": ".ppt", ".dpt": ".ppt"}

SUPPORTED_DESCRIPTION = (
    "text files (.txt .md .csv .json .html …, UTF-8/UTF-16/GB18030), Word (.docx .doc .rtf .odt .wps), "
    "spreadsheets (.xlsx .xls .ods .et), presentations (.pptx .ppt .odp .dps), PDF (OCR for scans), "
    "OFD, EPUB, images (OCR; HEIC included) and .zip archives (list, then read a member)"
)

_ZIP_MEMBER_LIMIT = 200 * 1024 * 1024


class UnsupportedFormat(ValueError):
    """The file is binary in a format no reader here handles."""


# ---------------------------------------------------------------------------
# Text sniffing
# ---------------------------------------------------------------------------


def sniff_text_encoding(path: Path, *, sample_size: int = 65536) -> Optional[str]:
    """The encoding ``path`` decodes in if it is a text file, else ``None``."""
    with Path(path).open("rb") as fh:
        data = fh.read(sample_size)
    if not data:
        return "utf-8"
    if data.startswith(codecs.BOM_UTF8):
        return "utf-8-sig"
    if data.startswith((codecs.BOM_UTF16_LE, codecs.BOM_UTF16_BE)):
        return "utf-16"
    if b"\x00" in data:
        return None
    truncated = len(data) == sample_size
    for encoding in ("utf-8", "gb18030"):
        try:
            text = data.decode(encoding)
        except UnicodeDecodeError as exc:
            if not (truncated and exc.start >= len(data) - 4):
                continue
            text = data[: exc.start].decode(encoding, errors="ignore")
        controls = sum(1 for ch in text if ord(ch) < 32 and ch not in "\t\n\r\f\v\x1b")
        if text and controls / len(text) > 0.02:
            return None
        return encoding
    return None


def read_text_file(path: Path) -> str:
    encoding = sniff_text_encoding(path)
    if encoding is None:
        raise UnsupportedFormat(_unsupported_message(path))
    return Path(path).read_text(encoding=encoding, errors="replace")


def _unsupported_message(path: Path) -> str:
    suffix = Path(path).suffix.lower() or "(no extension)"
    return f"unsupported binary format {suffix} ({Path(path).name}); supported: {SUPPORTED_DESCRIPTION}"


def _magic_kind(path: Path) -> Optional[str]:
    """Format from content, for a file whose extension is missing or wrong."""
    try:
        with Path(path).open("rb") as fh:
            head = fh.read(8)
    except OSError:
        return None
    if head.startswith(b"%PDF-"):
        return ".pdf"
    if head.startswith(b"\xd0\xcf\x11\xe0\xa1\xb1\x1a\xe1"):
        return "ole"
    if head.startswith(b"PK\x03\x04"):
        try:
            with zipfile.ZipFile(path) as zf:
                names = set(zf.namelist())
                mimetype = zf.read("mimetype").decode("ascii", "ignore").strip() if "mimetype" in names else ""
        except (zipfile.BadZipFile, OSError, KeyError):
            return None
        if "word/document.xml" in names:
            return ".docx"
        if "xl/workbook.xml" in names:
            return ".xlsx"
        if "ppt/presentation.xml" in names:
            return ".pptx"
        if "OFD.xml" in names:
            return ".ofd"
        if mimetype == "application/epub+zip":
            return ".epub"
        if mimetype.startswith("application/vnd.oasis.opendocument.text"):
            return ".odt"
        if mimetype.startswith("application/vnd.oasis.opendocument.spreadsheet"):
            return ".ods"
        if mimetype.startswith("application/vnd.oasis.opendocument.presentation"):
            return ".odp"
        return ".zip"
    if head.startswith(b"{\\rtf"):
        return ".rtf"
    try:
        from PIL import Image

        _register_heif()
        with Image.open(path):
            return ".png"
    except Exception:  # noqa: BLE001
        return None


def _register_heif() -> None:
    from .images import register_heif_opener

    register_heif_opener()


def effective_suffix(path: Path) -> str:
    """The suffix that decides how ``path`` is read (content wins when the
    extension is unknown or contradicts a recognisable signature)."""
    suffix = Path(path).suffix.lower()
    known = (
        WORD_SUFFIXES | WORD_LEGACY_SUFFIXES | SHEET_SUFFIXES | SHEET_LEGACY_SUFFIXES
        | SLIDE_SUFFIXES | SLIDE_LEGACY_SUFFIXES | IMAGE_SUFFIXES | OTHER_SUFFIXES
    )
    if suffix in known:
        return suffix
    if sniff_text_encoding(path) is not None:
        return suffix or ".txt"
    magic = _magic_kind(path)
    return magic or suffix


# ---------------------------------------------------------------------------
# Conversions, cached by content hash
# ---------------------------------------------------------------------------


def _sha256(path: Path) -> str:
    h = hashlib.sha256()
    with Path(path).open("rb") as fh:
        for chunk in iter(lambda: fh.read(1 << 20), b""):
            h.update(chunk)
    return h.hexdigest()


def _cache_root(kind: str) -> Path:
    from ..config import current_settings

    return current_settings().state_dir / kind


def convert_with_libreoffice(path: Path, target: str) -> Path:
    """``path`` converted by LibreOffice to ``target`` (docx/xlsx/pptx), cached
    under ``state/converted/<sha256>/``. Raises when LibreOffice is absent."""
    from .soffice import convert

    sha = _sha256(path)
    out_dir = _cache_root("converted") / sha[:2] / sha
    stem = re.sub(r"[^\w.\-]+", "_", Path(path).stem) or "document"
    cached = out_dir / f"{stem}.{target}"
    if cached.is_file() and cached.stat().st_size > 0:
        return cached
    out_dir.mkdir(parents=True, exist_ok=True)
    with tempfile.TemporaryDirectory(prefix="lh-convert-") as tmp:
        source = Path(tmp) / f"{stem}{Path(path).suffix.lower()}"
        shutil.copyfile(path, source)
        produced = convert(source, Path(tmp) / "out", target, timeout=180)
        # Publish atomically: two readers converting the same upload at once
        # (sub-agents do) must never see a half-written file.
        staging = out_dir / f".{stem}.{os.getpid()}.{threading.get_ident()}.tmp"
        shutil.copyfile(produced, staging)
        os.replace(staging, cached)
    return cached


def _converter_markdown(path: Path, alias: Optional[str] = None) -> str:
    from ._convert import convert_to_markdown

    if alias is None:
        return convert_to_markdown(path)
    with tempfile.TemporaryDirectory(prefix="lh-alias-") as tmp:
        copy = Path(tmp) / f"{Path(path).stem}{alias}"
        shutil.copyfile(path, copy)
        return convert_to_markdown(copy)


def read_legacy_word(path: Path) -> tuple[str, Optional[Path]]:
    """Text of a .doc/.rtf/.odt/.wps and the .docx it was converted to (for the
    clause index), or ``(text, None)`` when LibreOffice was not available."""
    from .office import extract_docx_text

    try:
        docx = convert_with_libreoffice(path, "docx")
        return extract_docx_text(docx, include_comments=True), docx
    except Exception as exc:  # noqa: BLE001 — degrade to the converter, and say so
        reason = f"{type(exc).__name__}: {exc}"
    alias = _KINGSOFT_ALIAS.get(Path(path).suffix.lower())
    text = _converter_markdown(path, alias).strip()
    note = (
        f"[read without LibreOffice ({reason[:160]}): tracked changes, headers/footers and "
        "auto-numbering labels may not be shown as Word shows them]"
    )
    return f"{note}\n\n{text}", None


def _read_legacy_sheet_or_deck(path: Path, target: str) -> str:
    from .office import extract_pptx_text, extract_xlsx_text

    alias = _KINGSOFT_ALIAS.get(Path(path).suffix.lower())
    try:
        text = _converter_markdown(path, alias).strip()
        if text:
            return text
    except Exception:  # noqa: BLE001 — LibreOffice below
        pass
    converted = convert_with_libreoffice(path, target)
    return extract_xlsx_text(converted) if target == "xlsx" else extract_pptx_text(converted)


# ---------------------------------------------------------------------------
# Zip archives
# ---------------------------------------------------------------------------


def _member_name(info: zipfile.ZipInfo) -> str:
    """Decode a member name: archives made on Chinese Windows store GBK names
    without the UTF-8 flag, which zipfile decodes as cp437 mojibake."""
    if info.flag_bits & 0x800:
        return info.filename
    try:
        raw = info.filename.encode("cp437")
    except UnicodeEncodeError:
        return info.filename
    for encoding in ("utf-8", "gb18030"):
        try:
            return raw.decode(encoding)
        except UnicodeDecodeError:
            continue
    return info.filename


def zip_members(path: Path) -> list[tuple[str, zipfile.ZipInfo]]:
    with zipfile.ZipFile(path) as zf:
        return [(_member_name(i), i) for i in zf.infolist() if not i.is_dir()]


def zip_listing(path: Path) -> str:
    members = zip_members(path)
    lines = [f"[zip archive {Path(path).name}: {len(members)} file(s)]"]
    for name, info in members:
        lines.append(f"- {name} ({info.file_size:,} bytes)")
    lines.append(
        f"Read a member with read_document(path={str(path)!r}, member='<name>') "
        f"or read_document(path='{path}::<name>')."
    )
    return "\n".join(lines)


def extract_zip_member(path: Path, member: str) -> Path:
    """Extract one member to ``state/zip_members/<sha>/`` and return its path.

    The member's own path is never used on disk (no traversal): it is saved
    under a sanitised basename with its original extension.
    """
    member = member.strip().strip("/")
    members = zip_members(path)
    match = next((info for name, info in members if name == member), None)
    if match is None:
        match = next((info for name, info in members if name.rsplit("/", 1)[-1] == member), None)
    if match is None:
        names = ", ".join(name for name, _ in members[:30])
        raise FileNotFoundError(f"no member {member!r} in {Path(path).name}; members: {names}")
    if match.file_size > _ZIP_MEMBER_LIMIT:
        raise ValueError(f"member {member!r} is {match.file_size:,} bytes uncompressed; refusing to extract over 200 MB")
    display = _member_name(match)
    base = re.sub(r"[^\w.\-一-鿿]+", "_", display.rsplit("/", 1)[-1]) or "member"
    out_dir = _cache_root("zip_members") / _sha256(path)[:16] / hashlib.sha1(display.encode("utf-8")).hexdigest()[:10]
    target = out_dir / base
    if not (target.is_file() and target.stat().st_size == match.file_size):
        out_dir.mkdir(parents=True, exist_ok=True)
        staging = out_dir / f".{base}.{os.getpid()}.{threading.get_ident()}.tmp"
        written = 0
        with zipfile.ZipFile(path) as zf, zf.open(match) as src, staging.open("wb") as dst:
            # Bounded by bytes actually inflated, not the header's claim: a
            # crafted header can understate the size of a zip bomb.
            for chunk in iter(lambda: src.read(1 << 20), b""):
                written += len(chunk)
                if written > _ZIP_MEMBER_LIMIT:
                    dst.close()
                    staging.unlink(missing_ok=True)
                    raise ValueError(f"member {member!r} inflates past 200 MB; refusing to extract it")
                dst.write(chunk)
        os.replace(staging, target)
    return target


def resolve_member_path(path: Path, member: str = "") -> Path:
    """Follow ``archive.zip::member`` (repeatable for nested archives) and/or a
    ``member`` argument to an extracted file on disk."""
    parts = str(path).split("::")
    current = Path(parts[0])
    for part in [*parts[1:], member] if member else parts[1:]:
        if not part:
            continue
        current = extract_zip_member(current, part)
    return current


# ---------------------------------------------------------------------------
# The dispatcher
# ---------------------------------------------------------------------------


def _select_marked(text: str, pages: str, pattern: str, noun: str) -> str:
    from .pdf import parse_page_spec

    marker = re.compile(pattern, re.M)
    hits = list(marker.finditer(text))
    if not hits:
        return text
    numbers = [int(m.group(1)) for m in hits]
    wanted = {i + 1 for i in parse_page_spec(pages, max(numbers))}
    chunks = []
    for i, m in enumerate(hits):
        end = hits[i + 1].start() if i + 1 < len(hits) else len(text)
        if int(m.group(1)) in wanted:
            chunks.append(text[m.start():end].rstrip())
    head = text[: hits[0].start()].strip()
    shown = f"[showing {noun}s {pages} of {max(numbers)}]"
    return "\n".join(x for x in [head, shown, "\n\n".join(chunks)] if x)


def read_any(path: Path, *, pages: str = "") -> str:
    """Text of ``path``; raises :class:`UnsupportedFormat` for unknown binary.

    ``pages`` (1-based spec) selects PDF/OFD pages and presentation slides.
    """
    from .office import extract_docx_text, extract_pptx_text, extract_xlsx_text

    path = Path(path)
    suffix = effective_suffix(path)
    paged = suffix in {".pdf", ".ofd"} | SLIDE_SUFFIXES | SLIDE_LEGACY_SUFFIXES
    if pages and not paged:
        text = read_any(path)
        return (
            f"[pages={pages!r} applies to PDF, OFD and presentations; {path.name} has no pages — "
            "use section= (a clause label or heading) or offset/limit instead. Showing the whole file.]\n" + text
        )
    if suffix == ".pdf":
        from .pdf import read_pdf_text

        return read_pdf_text(path, pages=pages or None)
    if suffix == ".ofd":
        from .ofd import read_ofd_text

        text = read_ofd_text(path)
        return _select_marked(text, pages, r"^\[page (\d+)\]", "page") if pages else text
    if suffix in WORD_SUFFIXES:
        return extract_docx_text(path, include_comments=True)
    if suffix in WORD_LEGACY_SUFFIXES:
        return read_legacy_word(path)[0]
    if suffix in SHEET_SUFFIXES:
        return extract_xlsx_text(path)
    if suffix in SHEET_LEGACY_SUFFIXES:
        return _read_legacy_sheet_or_deck(path, "xlsx")
    if suffix in SLIDE_SUFFIXES or suffix in SLIDE_LEGACY_SUFFIXES:
        if not pages:
            return extract_pptx_text(path) if suffix in SLIDE_SUFFIXES else _read_legacy_sheet_or_deck(path, "pptx")
        # Selecting slides needs slide numbers, which only the python-pptx
        # reading carries ("## Slide N"); the converter's markdown has none.
        from .office import _pptx_text_via_python_pptx

        deck = path if suffix in SLIDE_SUFFIXES else convert_with_libreoffice(path, "pptx")
        return _select_marked(_pptx_text_via_python_pptx(deck), pages, r"^## Slide (\d+)", "slide")
    if suffix == ".epub":
        return _converter_markdown(path)
    if suffix == ".zip":
        return zip_listing(path)
    if suffix in IMAGE_SUFFIXES:
        from .ocr import ocr_image_file

        try:
            result = ocr_image_file(path)
        except Exception as exc:  # noqa: BLE001
            raise UnsupportedFormat(
                f"{path.name} is an image and OCR is unavailable ({exc}); look at it with view_image"
            ) from exc
        if not result.text.strip():
            return f"[image {path.name}: OCR found no text — look at it with view_image]"
        return (
            f"[image {path.name}: text read by OCR (RapidOCR, mean confidence {result.mean_score:.2f}); "
            "check names, figures and stamps against the image with view_image]\n" + result.text
        )
    if suffix == "ole":
        for alias in (".doc", ".xls", ".ppt"):
            try:
                text = _converter_markdown(path, alias).strip()
            except Exception:  # noqa: BLE001
                continue
            if text:
                return text
        raise UnsupportedFormat(_unsupported_message(path))
    return read_text_file(path)


__all__ = [
    "SUPPORTED_DESCRIPTION",
    "UnsupportedFormat",
    "convert_with_libreoffice",
    "effective_suffix",
    "extract_zip_member",
    "read_any",
    "read_legacy_word",
    "read_text_file",
    "resolve_member_path",
    "sniff_text_encoding",
    "zip_listing",
    "zip_members",
]
