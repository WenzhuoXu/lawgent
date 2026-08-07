"""Attachment ingestion: turn uploaded files into provider content blocks.

Single source of truth for converting an uploaded file path into the
structured content the provider expects:

- PDFs              → Anthropic `document` (Files API) / OpenAI `input_file`
- Images            → base64 `image` / `input_image`
- DOCX/XLSX/CSV     → server-side text extraction wrapped in a labeled block
- TXT/MD/code       → inlined as a labeled text block
- Opaque binary     → metadata stub; model can still call `read_document`

Both providers consume the same call site via `build_user_content(text,
attachments, provider)`. It returns a plain string when there are no
attachments (so existing call sites keep working) or a list of content
blocks shaped for the requested provider.
"""

from __future__ import annotations

import base64
import hashlib
import mimetypes
import shutil
import subprocess
import threading
from dataclasses import dataclass
from pathlib import Path
from typing import Any, Iterable, Literal, Optional, Union

from .config import current_settings
from .documents import extract_docx_text, extract_pptx_text, extract_xlsx_text


Category = Literal["native_pdf", "native_image", "text_inline", "binary_stub"]
# .pptx is treated as a "convert-to-PDF" attachment so both providers see the
# slides as vision content. The text fallback is kept as a companion block.
_PPTX_EXTS = {".pptx", ".ppt"}

_IMAGE_EXTS = {".png", ".jpg", ".jpeg", ".gif", ".webp"}
_IMAGE_MIMES = {"image/png", "image/jpeg", "image/gif", "image/webp"}
_OFFICE_EXTS = {".docx", ".xlsx", ".pptx", ".csv"}
_TEXT_EXTS = {".txt", ".md", ".rst", ".log"}
_CODE_EXTS = {
    ".py", ".js", ".ts", ".tsx", ".jsx", ".json", ".yaml", ".yml", ".toml",
    ".ini", ".cfg", ".html", ".htm", ".css", ".scss", ".sql", ".sh", ".bash",
    ".zsh", ".fish", ".java", ".kt", ".swift", ".c", ".h", ".cpp", ".hpp",
    ".cs", ".rb", ".go", ".rs", ".r", ".php", ".lua", ".m", ".mm",
}
_INLINE_EXTS = _OFFICE_EXTS | _TEXT_EXTS | _CODE_EXTS

_MAX_INLINE_BYTES = 2_000_000
_MAX_INLINE_IMAGE_BYTES = 8_000_000


@dataclass
class AttachmentInfo:
    path: Path
    filename: str
    size: int
    mime: str
    sha256: str
    category: Category


def _ext_mime(path: Path) -> tuple[str, str]:
    ext = path.suffix.lower()
    mime, _ = mimetypes.guess_type(path.name)
    return ext, (mime or "application/octet-stream")


def detect_category(path: Path) -> Category:
    ext, mime = _ext_mime(path)
    if ext == ".pdf" or mime == "application/pdf":
        return "native_pdf"
    if ext in _IMAGE_EXTS or mime in _IMAGE_MIMES:
        return "native_image"
    if ext in _INLINE_EXTS or mime.startswith("text/"):
        return "text_inline"
    return "binary_stub"


def _sha256_of(path: Path) -> str:
    h = hashlib.sha256()
    with path.open("rb") as f:
        for chunk in iter(lambda: f.read(65536), b""):
            h.update(chunk)
    return h.hexdigest()


def describe(path: Union[str, Path]) -> AttachmentInfo:
    p = Path(path).resolve()
    _, mime = _ext_mime(p)
    return AttachmentInfo(
        path=p,
        filename=p.name,
        size=p.stat().st_size,
        mime=mime,
        sha256=_sha256_of(p),
        category=detect_category(p),
    )


# ---------- text extraction for inlined formats ----------------------------


def _extract_docx(path: Path) -> str:
    return extract_docx_text(path, include_comments=True)


def _extract_xlsx(path: Path) -> str:
    return extract_xlsx_text(path)


def _extract_pptx(path: Path) -> str:
    return extract_pptx_text(path)


def _extract_csv(path: Path) -> str:
    import csv

    out: list[str] = []
    with path.open("r", encoding="utf-8", errors="replace", newline="") as f:
        reader = csv.reader(f)
        for row in reader:
            out.append(" | ".join(row))
    return "\n".join(out)


def _extract_text(path: Path) -> str:
    return path.read_text(encoding="utf-8", errors="replace")


def extract_inline_text(info: AttachmentInfo) -> str:
    ext = info.path.suffix.lower()
    if info.size > _MAX_INLINE_BYTES:
        return (
            f"[file too large to inline ({info.size} bytes > {_MAX_INLINE_BYTES}). "
            f"Call `read_document(path={str(info.path)!r})` to read in chunks.]"
        )
    try:
        if ext == ".docx":
            return _extract_docx(info.path)
        if ext == ".xlsx":
            return _extract_xlsx(info.path)
        if ext == ".pptx":
            return _extract_pptx(info.path)
        if ext == ".csv":
            return _extract_csv(info.path)
        return _extract_text(info.path)
    except Exception as exc:
        return f"[error extracting text from {info.filename}: {exc}]"


def _wrap_inline(info: AttachmentInfo, text: str) -> str:
    return (
        f"<attached-file name={info.filename!r} path={str(info.path)!r} "
        f"mime={info.mime!r} size={info.size}>\n"
        f"{text}\n"
        f"</attached-file>"
    )


def _stub_text(info: AttachmentInfo) -> str:
    short_sha = info.sha256[:12] if info.sha256 else "unknown"
    return (
        f"[attached binary file: {info.filename} ({info.mime}, {info.size} bytes, "
        f"sha256={short_sha}…). Saved at {info.path}. If it might be readable as text "
        f"or PDF/DOCX, call `read_document(path={str(info.path)!r})`.]"
    )


# ---------- PPTX → PDF conversion (soffice) -------------------------------

_PPTX_PDF_CACHE: dict[str, Path] = {}
_PPTX_PDF_LOCK = threading.Lock()


def _pptx_to_pdf(info: AttachmentInfo) -> Path:
    """Convert a .pptx/.ppt to PDF via LibreOffice, cached by sha256.

    Raises RuntimeError if soffice is not on PATH or conversion fails. The
    output lives under ``outputs_dir/_pptx_pdf_cache/<sha256>.pdf`` so it is
    re-used across requests in the same project.
    """
    cache_key = info.sha256 or _sha256_of(info.path)
    with _PPTX_PDF_LOCK:
        cached = _PPTX_PDF_CACHE.get(cache_key)
    if cached and cached.is_file():
        return cached
    soffice = shutil.which("soffice") or shutil.which("libreoffice")
    if not soffice:
        raise RuntimeError("soffice/libreoffice not found on PATH; cannot convert .pptx to PDF")
    settings = current_settings()
    out_dir = settings.outputs_dir / "_pptx_pdf_cache" / cache_key
    out_dir.mkdir(parents=True, exist_ok=True)
    on_disk = out_dir / f"{info.path.stem}.pdf"
    if on_disk.is_file():
        with _PPTX_PDF_LOCK:
            _PPTX_PDF_CACHE[cache_key] = on_disk
        return on_disk
    try:
        subprocess.run(
            [soffice, "--headless", "--convert-to", "pdf", "--outdir", str(out_dir), str(info.path)],
            check=True,
            capture_output=True,
            text=True,
            timeout=120,
        )
    except subprocess.CalledProcessError as exc:
        raise RuntimeError(
            f"soffice failed converting {info.filename}: {(exc.stderr or exc.stdout or str(exc)).strip()[:400]}"
        ) from exc
    pdf = out_dir / f"{info.path.stem}.pdf"
    if not pdf.is_file():
        candidates = sorted(out_dir.glob("*.pdf"))
        if not candidates:
            raise RuntimeError(f"soffice did not produce a PDF for {info.filename}")
        pdf = candidates[0]
    with _PPTX_PDF_LOCK:
        _PPTX_PDF_CACHE[cache_key] = pdf
    return pdf


def _pdf_info_for_pptx(info: AttachmentInfo) -> AttachmentInfo:
    """Return an AttachmentInfo describing the PDF rendering of a PPTX."""
    pdf_path = _pptx_to_pdf(info)
    return AttachmentInfo(
        path=pdf_path,
        filename=f"{info.path.stem}.pdf",
        size=pdf_path.stat().st_size,
        mime="application/pdf",
        sha256=_sha256_of(pdf_path),
        category="native_pdf",
    )


# ---------- Files API upload + per-process cache ---------------------------

_CACHE_LOCK = threading.Lock()
_FILE_ID_CACHE: dict[tuple[str, str], str] = {}


def _cache_get(provider: str, sha256: str) -> Optional[str]:
    with _CACHE_LOCK:
        return _FILE_ID_CACHE.get((provider, sha256))


def _cache_set(provider: str, sha256: str, file_id: str) -> None:
    with _CACHE_LOCK:
        _FILE_ID_CACHE[(provider, sha256)] = file_id


def upload_to_anthropic(info: AttachmentInfo) -> str:
    cached = _cache_get("anthropic", info.sha256)
    if cached:
        return cached
    from anthropic import Anthropic

    settings = current_settings()
    if not settings.anthropic_api_key:
        raise RuntimeError("ANTHROPIC_API_KEY is not set; cannot upload to Anthropic Files API")
    client = Anthropic(api_key=settings.anthropic_api_key)
    with info.path.open("rb") as f:
        result = client.beta.files.upload(file=(info.filename, f, info.mime))
    file_id = getattr(result, "id", None) or result["id"]
    _cache_set("anthropic", info.sha256, file_id)
    return file_id


def upload_to_openai(info: AttachmentInfo, *, purpose: str = "user_data") -> str:
    cache_key = f"openai:{purpose}"
    cached = _cache_get(cache_key, info.sha256)
    if cached:
        return cached
    from openai import OpenAI

    settings = current_settings()
    if not settings.openai_api_key:
        raise RuntimeError("OPENAI_API_KEY is not set; cannot upload to OpenAI Files API")
    client = OpenAI(api_key=settings.openai_api_key)
    with info.path.open("rb") as f:
        result = client.files.create(file=(info.filename, f, info.mime), purpose=purpose)
    file_id = getattr(result, "id", None) or result["id"]
    _cache_set(cache_key, info.sha256, file_id)
    return file_id


def _image_base64(info: AttachmentInfo) -> str:
    with info.path.open("rb") as f:
        return base64.standard_b64encode(f.read()).decode("ascii")


# ---------- block builders -------------------------------------------------


def _is_pptx(info: AttachmentInfo) -> bool:
    return info.path.suffix.lower() in _PPTX_EXTS


def _pptx_text_companion_anthropic(info: AttachmentInfo) -> dict[str, Any]:
    try:
        text = extract_pptx_text(info.path)
    except Exception as exc:
        text = f"[text extraction failed: {exc}]"
    return {"type": "text", "text": _wrap_inline(info, text)}


def _pptx_text_companion_openai(info: AttachmentInfo) -> dict[str, Any]:
    try:
        text = extract_pptx_text(info.path)
    except Exception as exc:
        text = f"[text extraction failed: {exc}]"
    return {"type": "input_text", "text": _wrap_inline(info, text)}


def build_anthropic_block(info: AttachmentInfo) -> dict[str, Any]:
    if _is_pptx(info):
        try:
            pdf_info = _pdf_info_for_pptx(info)
            file_id = upload_to_anthropic(pdf_info)
            return {
                "type": "document",
                "source": {"type": "file", "file_id": file_id},
                "title": info.filename,
            }
        except Exception as exc:
            return {"type": "text", "text": _wrap_inline(
                info,
                f"[pptx→pdf conversion failed, falling back to text only: {exc}]\n"
                + (_safe_pptx_text(info)),
            )}
    if info.category == "native_pdf":
        try:
            file_id = upload_to_anthropic(info)
            return {
                "type": "document",
                "source": {"type": "file", "file_id": file_id},
                "title": info.filename,
            }
        except Exception as exc:
            return {"type": "text", "text": f"[failed to upload PDF {info.filename}: {exc}. Saved at {info.path}.]"}
    if info.category == "native_image":
        if info.size > _MAX_INLINE_IMAGE_BYTES:
            try:
                file_id = upload_to_anthropic(info)
                return {"type": "image", "source": {"type": "file", "file_id": file_id}}
            except Exception as exc:
                return {"type": "text", "text": f"[failed to attach image {info.filename}: {exc}]"}
        return {
            "type": "image",
            "source": {
                "type": "base64",
                "media_type": info.mime if info.mime in _IMAGE_MIMES else "image/png",
                "data": _image_base64(info),
            },
        }
    if info.category == "text_inline":
        return {"type": "text", "text": _wrap_inline(info, extract_inline_text(info))}
    return {"type": "text", "text": _stub_text(info)}


def build_openai_block(info: AttachmentInfo) -> dict[str, Any]:
    if _is_pptx(info):
        try:
            pdf_info = _pdf_info_for_pptx(info)
            file_id = upload_to_openai(pdf_info, purpose="user_data")
            return {"type": "input_file", "file_id": file_id}
        except Exception as exc:
            return {"type": "input_text", "text": _wrap_inline(
                info,
                f"[pptx→pdf conversion failed, falling back to text only: {exc}]\n"
                + (_safe_pptx_text(info)),
            )}
    if info.category == "native_pdf":
        try:
            file_id = upload_to_openai(info, purpose="user_data")
            return {"type": "input_file", "file_id": file_id}
        except Exception as exc:
            return {"type": "input_text", "text": f"[failed to upload PDF {info.filename}: {exc}. Saved at {info.path}.]"}
    if info.category == "native_image":
        if info.size > _MAX_INLINE_IMAGE_BYTES:
            try:
                file_id = upload_to_openai(info, purpose="vision")
                return {"type": "input_image", "file_id": file_id}
            except Exception as exc:
                return {"type": "input_text", "text": f"[failed to attach image {info.filename}: {exc}]"}
        media_type = info.mime if info.mime in _IMAGE_MIMES else "image/png"
        return {
            "type": "input_image",
            "image_url": f"data:{media_type};base64,{_image_base64(info)}",
        }
    if info.category == "text_inline":
        return {"type": "input_text", "text": _wrap_inline(info, extract_inline_text(info))}
    return {"type": "input_text", "text": _stub_text(info)}


def _safe_pptx_text(info: AttachmentInfo) -> str:
    try:
        return extract_pptx_text(info.path)
    except Exception as exc:
        return f"[text extraction failed: {exc}]"


def _missing_info(path: Path) -> AttachmentInfo:
    return AttachmentInfo(
        path=path,
        filename=path.name,
        size=0,
        mime="application/octet-stream",
        sha256="",
        category="binary_stub",
    )


def _to_info(item: Union[Path, str, AttachmentInfo]) -> AttachmentInfo:
    if isinstance(item, AttachmentInfo):
        return item
    p = Path(item)
    try:
        return describe(p)
    except FileNotFoundError:
        return _missing_info(p)


def build_user_content(
    text: str,
    attachments: Iterable[Union[Path, str, AttachmentInfo]],
    provider: str,
    *,
    cache_prefix: str = "",
) -> Any:
    """Return either a plain string (no attachments) or a list of provider blocks.

    For ``.pptx``/``.ppt`` attachments, two blocks are emitted: a vision PDF
    (the deck converted via LibreOffice — the canonical Anthropic-cookbook
    pattern, which uses both text and vision per page) and a companion text
    block (selectable text + speaker notes via python-pptx). This mirrors
    Anthropic's official PPTX skill split (`markitdown` for text +
    thumbnails for visual analysis).

    ``cache_prefix`` is the STABLE part of the turn (the chat context digest):
    it is emitted as its own leading block so the growing conversation prefix
    can be cached, with ``text`` — the volatile current request — after it.
    Anthropic prompt caching is an explicit prefix breakpoint, and the provider
    only marks ``system`` + ``tools``; without this, everything in ``messages``
    (i.e. the entire context digest) is re-billed at full input rate every
    turn. OpenAI caches prefixes automatically and needs no marker, but gets
    the same ordering so the two providers stay byte-comparable.
    """
    items = [a for a in attachments if a is not None]
    prefix = cache_prefix if cache_prefix and cache_prefix.strip() else ""
    if not items and not prefix:
        return text
    infos = [_to_info(a) for a in items]
    blocks: list[dict[str, Any]] = []
    if provider == "anthropic":
        for info in infos:
            blocks.append(build_anthropic_block(info))
            if _is_pptx(info):
                blocks.append(_pptx_text_companion_anthropic(info))
        if prefix:
            # Breakpoint on the last stable block: caches attachments + digest.
            blocks.append(
                {"type": "text", "text": prefix, "cache_control": {"type": "ephemeral"}}
            )
        if text and text.strip():
            blocks.append({"type": "text", "text": text})
    else:
        for info in infos:
            blocks.append(build_openai_block(info))
            if _is_pptx(info):
                blocks.append(_pptx_text_companion_openai(info))
        if prefix:
            blocks.append({"type": "input_text", "text": prefix})
        if text and text.strip():
            blocks.append({"type": "input_text", "text": text})
    return blocks


def needs_anthropic_files_beta(messages: Iterable[Any]) -> bool:
    """True when any message references an uploaded Anthropic file_id."""
    for m in messages:
        content = m.get("content") if isinstance(m, dict) else getattr(m, "content", None)
        if not isinstance(content, list):
            continue
        for block in content:
            source = None
            if isinstance(block, dict):
                source = block.get("source")
            else:
                source = getattr(block, "source", None)
            if isinstance(source, dict) and source.get("type") == "file":
                return True
            if hasattr(source, "type") and getattr(source, "type", None) == "file":
                return True
    return False


__all__ = [
    "AttachmentInfo",
    "Category",
    "build_anthropic_block",
    "build_openai_block",
    "build_user_content",
    "describe",
    "detect_category",
    "extract_inline_text",
    "needs_anthropic_files_beta",
    "upload_to_anthropic",
    "upload_to_openai",
]
