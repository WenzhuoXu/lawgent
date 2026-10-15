"""Attachment ingestion: turn uploaded files into provider content blocks.

Single source of truth for converting an uploaded file path into the
structured content the provider expects:

- PDFs              → Anthropic `document` (Files API) / OpenAI `input_file`
- Images            → base64 `image` / `input_image` (HEIC/HEIF, BMP and TIFF
                      are converted to JPEG first — providers take neither)
- Office documents  → server-side text extraction wrapped in a labeled block,
  incl. legacy        through the same reader as ``read_document``
  (.doc .xls .wps …), (``documents/intake.py``): tracked changes, headers and
  OFD, EPUB, .zip     footers and numbering labels kept; a .zip lists members
- TXT/MD/code       → inlined as a labeled text block (UTF-8/UTF-16/GB18030)
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
import os
import re
import threading
import zipfile
from collections import OrderedDict
from dataclasses import dataclass
from pathlib import Path
from typing import Any, Iterable, Literal, Optional, Union

from .config import current_settings
from .documents import extract_pptx_text


Category = Literal["native_pdf", "native_image", "text_inline", "binary_stub"]
# .pptx is treated as a "convert-to-PDF" attachment so both providers see the
# slides as vision content. The text fallback is kept as a companion block.
_PPTX_EXTS = {".pptx", ".ppt", ".pptm", ".ppsx", ".pps", ".odp", ".dps"}

_IMAGE_EXTS = {".png", ".jpg", ".jpeg", ".gif", ".webp"}
# Formats the providers do not take: converted to JPEG before sending. 17
# iPhone photos in one chat never reached the model because .heic was not an
# image extension at all.
_CONVERTED_IMAGE_EXTS = {".heic", ".heif", ".bmp", ".tif", ".tiff"}
_IMAGE_MIMES = {"image/png", "image/jpeg", "image/gif", "image/webp"}
_OFFICE_EXTS = {
    ".docx", ".docm", ".xlsx", ".xlsm", ".pptx", ".csv",
    # Legacy and national formats, read through documents/intake (LibreOffice
    # conversion for word processing, the tiered converter otherwise).
    ".doc", ".dot", ".rtf", ".odt", ".wps", ".xls", ".ods", ".et", ".ofd", ".epub", ".zip",
}
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
# Aggregate ceiling across ONE turn's attachments. The two caps above bound a
# single file; nothing bounded the sum, so a chat carrying 33 screenshots
# shipped ~16 MB (~136k tokens) of identical image payload on every turn.
# Files past the budget degrade to the same on-demand stub the binary category
# already uses, so nothing becomes unreachable — only deferred to a tool call.
_MAX_TOTAL_INLINE_BYTES = 10_000_000

# ---- inject vs navigate -----------------------------------------------------
# A single document belongs in the prompt; a *corpus* does not. Measured on
# transactional legal documents (arXiv 2607.05764), navigating a structured
# index matched or beat full injection on every document-bound question at
# roughly a 56x smaller answering context, and embedding retrieval alone tied
# on 16 of 18 at 17.3x fewer tokens — while the honest counter-case (Thomson
# Reuters' production finding that full-document context beats RAG for
# document-bound legal skills) holds for the single-document case.
#
# The decision is made in extracted *tokens*, against a share of the turn's
# input ceiling (``context.turn_input_ceiling_for``), and it applies to one
# attachment as much as to many. It used to sum file *bytes* and always inline
# a lone attachment: a 353 KB workbook extracted to ~367K tokens — over the
# 259K OpenAI working ceiling on its own — and was rebuilt into every
# sub-agent. Text attachments are inlined in order while they fit the share;
# the rest are replaced by a navigable OUTLINE (clause labels from the clause
# index, headings) plus the call that opens them, so nothing is unreachable.
#
# The share leaves the rest of the ceiling to what it is already divided
# among: ~21K of fixed overhead, 30% for the transcript digest, and the tool
# results of the turn. LEGAL_HELPER_INLINE_ATTACHMENT_TOKENS sets the budget
# in tokens instead; 0 turns outlining off (everything inlines).
_INLINE_SHARE_OF_TURN_CEILING = 0.4
_OUTLINE_MAX_ENTRIES = 60
# Office files carry images; their text is what is measured, so the byte cap
# only guards extraction cost. Plain text is bounded by _MAX_INLINE_BYTES.
_MAX_EXTRACT_BYTES = 25_000_000


def _inline_token_budget() -> int:
    """Tokens of attachment text one turn may inline; 0 means unlimited."""
    raw = os.getenv("LEGAL_HELPER_INLINE_ATTACHMENT_TOKENS")
    if raw is not None:
        try:
            return max(int(raw), 0)
        except ValueError:
            pass
    from .context import turn_input_ceiling_for

    try:
        ceiling = turn_input_ceiling_for(current_settings())
    except Exception:  # noqa: BLE001 — settings unreadable: a conservative default
        ceiling = 200_000
    return max(1_000, int(_INLINE_SHARE_OF_TURN_CEILING * ceiling))


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
    if ext in _IMAGE_EXTS or ext in _CONVERTED_IMAGE_EXTS or mime in _IMAGE_MIMES:
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


def _extract_csv(path: Path) -> str:
    import csv

    from .documents.intake import sniff_text_encoding

    encoding = sniff_text_encoding(path) or "utf-8"
    out: list[str] = []
    with path.open("r", encoding=encoding, errors="replace", newline="") as f:
        reader = csv.reader(f)
        for row in reader:
            out.append(" | ".join(row))
    return "\n".join(out)


_TEXT_CACHE: "OrderedDict[str, str]" = OrderedDict()
_TEXT_CACHE_LOCK = threading.Lock()
_TEXT_CACHE_SIZE = 64


def extract_inline_text(info: AttachmentInfo) -> str:
    """Text of a text-category attachment, through the same reader as
    ``read_document`` (cached by content hash: a turn's attachments are
    re-extracted for every sub-agent otherwise)."""
    ext = info.path.suffix.lower()
    plain = ext in _TEXT_EXTS or ext in _CODE_EXTS or ext == ".csv"
    limit = _MAX_INLINE_BYTES if plain else _MAX_EXTRACT_BYTES
    if info.size > limit:
        return (
            f"[file too large to inline ({info.size} bytes > {limit}). "
            f"Call `read_document(path={str(info.path)!r})` to read in chunks.]"
        )
    key = f"{info.sha256}:{ext}" if info.sha256 else ""
    if key:
        with _TEXT_CACHE_LOCK:
            cached = _TEXT_CACHE.get(key)
            if cached is not None:
                _TEXT_CACHE.move_to_end(key)
                return cached
    try:
        if ext == ".csv":
            text = _extract_csv(info.path)
        else:
            from .documents.intake import read_any

            text = read_any(info.path)
    except Exception as exc:
        return f"[error extracting text from {info.filename}: {exc}]"
    if key:
        with _TEXT_CACHE_LOCK:
            _TEXT_CACHE[key] = text
            while len(_TEXT_CACHE) > _TEXT_CACHE_SIZE:
                _TEXT_CACHE.popitem(last=False)
    return text


def _wrap_inline(info: AttachmentInfo, text: str) -> str:
    return (
        f"<attached-file name={info.filename!r} path={str(info.path)!r} "
        f"mime={info.mime!r} size={info.size}>\n"
        f"{text}\n"
        f"</attached-file>"
    )


_EXTRACTION_FAILED_PREFIXES = ("[file too large to inline", "[error extracting text")
_MEDIA_DIRS = {".docx": "word/media/", ".docm": "word/media/", ".xlsx": "xl/media/", ".xlsm": "xl/media/"}


def _embedded_image_count(info: AttachmentInfo) -> int:
    """Images inside an OOXML file, which its extracted text cannot show."""
    prefix = _MEDIA_DIRS.get(info.path.suffix.lower())
    if not prefix:
        return 0
    try:
        with zipfile.ZipFile(info.path) as z:
            return sum(1 for n in z.namelist() if n.startswith(prefix) and not n.endswith("/"))
    except Exception:  # noqa: BLE001 — not a readable zip: say nothing about images
        return 0


def _wrap_full_text(info: AttachmentInfo, text: str) -> str:
    """An attachment inlined in full, saying so.

    Without the statement the model re-opened inlined files with
    ``read_document`` to be sure it had all of them: two of five rounds of a
    27-file turn re-read text it already held. What the text cannot carry —
    embedded images, and for a workbook its cell addresses — is named, so a
    second look is taken only for those.
    """
    if text.startswith(_EXTRACTION_FAILED_PREFIXES):
        return _wrap_inline(info, text)
    notes = ["This is the file's complete extracted text; read_document would return the same text."]
    images = _embedded_image_count(info)
    if images:
        notes.append(
            f"The file also holds {images} embedded image(s), which text cannot show; render the file to see them."
        )
    if info.path.suffix.lower() in {".xlsx", ".xlsm", ".xls"}:
        notes.append("Cell addresses are not in this text; inspect_xlsx gives them.")
    return _wrap_inline(info, "[" + " ".join(notes) + "]\n" + text)


def _inline_cost(info: AttachmentInfo) -> int:
    """Bytes this attachment adds to the prompt itself.

    PDFs (and .pptx, which converts to one) travel as provider file_id
    references rather than inlined bytes, so they do not draw on the turn's
    inline budget.
    """
    if _is_pptx(info) or info.category == "native_pdf":
        return 0
    if info.category in {"native_image", "text_inline"}:
        return info.size
    return 0


def _deferred_text(info: AttachmentInfo) -> str:
    opener = {
        "native_image": "view_image",
        "text_inline": "read_document",
    }.get(info.category, "read_document")
    return (
        f"[attachment deferred — this turn's inline budget "
        f"({_MAX_TOTAL_INLINE_BYTES} bytes) is spent: {info.filename} "
        f"({info.mime}, {info.size} bytes). Saved at {info.path}. "
        f"Open it when you need it with `{opener}(path={str(info.path)!r})`.]"
    )


def _outline_entries(text: str) -> list[str]:
    """Navigable landmarks in a document: clause labels with their titles
    (第一条 总则, 1.1 Definitions, Article 5 …) and headings.

    Built from the shared clause index, which accepts the forms converters
    actually emit (``- 第1条``, ``1. Heading``); the bare-line regexes it
    replaces found nothing in 36 of 52 real uploads. The RAG chunker's
    boundary patterns remain the fallback, so the labels the outline
    advertises are ones retrieval can seek to as well.
    """
    try:
        from .documents.clauses import clause_index_from_text, outline_labels

        _, clauses = clause_index_from_text(text)
        entries = outline_labels(clauses, max_entries=_OUTLINE_MAX_ENTRIES)
        if entries:
            return entries
    except Exception:  # noqa: BLE001 — fall back to the chunker's patterns
        pass
    try:
        from .rag.chunker import _ARTICLE_RES  # type: ignore[attr-defined]

        patterns = list(_ARTICLE_RES)
    except Exception:  # noqa: BLE001 — outline must work without the RAG extras
        patterns = [
            re.compile(r"(?m)^\s*(第[一二三四五六七八九十百千零0-9]+条)"),
            re.compile(r"(?m)^\s*(#{1,6}\s+.+)$"),
            re.compile(r"(?m)^\s*(Article\s+\d+[A-Za-z]?)"),
            re.compile(r"(?m)^\s*(§\s*\d+)"),
        ]
    seen: set[str] = set()
    entries: list[str] = []
    for pat in patterns:
        for m in pat.finditer(text):
            label = " ".join(m.group(1).split())[:120]
            if label and label not in seen:
                seen.add(label)
                entries.append(label)
            if len(entries) >= _OUTLINE_MAX_ENTRIES:
                return entries
    return entries


def _outline_text(info: AttachmentInfo, body: Optional[str] = None, *, tokens: int = 0, budget: int = 0) -> str:
    """Navigable map of a text attachment, in place of its full body."""
    if body is None:
        body = extract_inline_text(info)
    tokens = tokens or _estimate_tokens(body)
    entries = _outline_entries(body)
    head = " ".join(body[:400].split())
    why = (
        f"≈{tokens:,} tokens of text, more than this turn's attachment budget"
        + (f" of {budget:,}" if budget else "")
    )
    lines = [
        f"[attachment not inlined — {why}, so it is indexed rather than pasted: "
        f"{info.filename} ({info.mime}, {info.size} bytes). Saved at {info.path}.]",
        f"opens with: {head}…" if head else "",
    ]
    if entries:
        shown = "; ".join(entries)
        more = " …" if len(entries) >= _OUTLINE_MAX_ENTRIES else ""
        lines.append(f"sections: {shown}{more}")
    lines.append(
        f"Read what you need with `read_document(path={str(info.path)!r}, section='<label>')` "
        "(a label above, e.g. 第三条), or page through it with offset/limit; the retrieval tools "
        "search it too."
    )
    return "\n".join(x for x in lines if x)


def _estimate_tokens(text: str) -> int:
    from .context import estimate_tokens

    return estimate_tokens(text)


def _plan_text_attachments(infos: list[AttachmentInfo]) -> dict[int, tuple[str, str]]:
    """``{index: ("inline" | "outline", text)}`` for each text attachment.

    Attachments are taken in the order given (the current turn's uploads
    first); each one is inlined while its extracted tokens fit what is left
    of :func:`_inline_token_budget`, and outlined otherwise — a single huge
    attachment included.
    """
    budget = _inline_token_budget()
    left = budget
    plan: dict[int, tuple[str, str]] = {}
    for idx, info in enumerate(infos):
        if info.category != "text_inline" or _is_pptx(info):
            continue
        text = extract_inline_text(info)
        tokens = _estimate_tokens(text)
        if budget <= 0 or tokens <= left:
            if budget > 0:
                left -= tokens
            plan[idx] = ("inline", text)
        else:
            plan[idx] = ("outline", _outline_text(info, text, tokens=tokens, budget=budget))
    return plan


def _should_navigate(infos: list[AttachmentInfo]) -> bool:
    """True when at least one text attachment will be outlined, not inlined."""
    return any(kind == "outline" for kind, _ in _plan_text_attachments(infos).values())


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
    re-used across requests in the same project. Runs under a per-call
    LibreOffice profile, so two uploads converting at once do not collide.
    """
    import subprocess

    from .documents.soffice import SofficeNotInstalled, convert

    cache_key = info.sha256 or _sha256_of(info.path)
    with _PPTX_PDF_LOCK:
        cached = _PPTX_PDF_CACHE.get(cache_key)
    if cached and cached.is_file():
        return cached
    settings = current_settings()
    out_dir = settings.outputs_dir / "_pptx_pdf_cache" / cache_key
    out_dir.mkdir(parents=True, exist_ok=True)
    on_disk = out_dir / f"{info.path.stem}.pdf"
    if on_disk.is_file():
        with _PPTX_PDF_LOCK:
            _PPTX_PDF_CACHE[cache_key] = on_disk
        return on_disk
    try:
        pdf = convert(info.path, out_dir, "pdf", timeout=120)
    except SofficeNotInstalled as exc:
        raise RuntimeError("soffice/libreoffice not found on PATH; cannot convert .pptx to PDF") from exc
    except subprocess.CalledProcessError as exc:
        raise RuntimeError(
            f"soffice failed converting {info.filename}: {(exc.stderr or exc.stdout or str(exc)).strip()[:400]}"
        ) from exc
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


_IMAGE_CONVERT_LOCK = threading.Lock()
# Long edge for converted photos: legible for a photographed page, ~1 MB.
_CONVERTED_IMAGE_EDGE = 2048


def _provider_image(info: AttachmentInfo) -> AttachmentInfo:
    """``info`` itself, or a JPEG rendering of it when the providers cannot
    take its format (HEIC/HEIF phone photos, BMP, TIFF). Cached by sha256."""
    if info.path.suffix.lower() not in _CONVERTED_IMAGE_EXTS:
        return info
    from PIL import Image, ImageOps

    sha = info.sha256 or _sha256_of(info.path)
    # Under state/, not outputs/: everything in a chat's outputs directory is
    # listed to the user as an artifact.
    out = current_settings().state_dir / "image_cache" / f"{sha}.jpg"
    with _IMAGE_CONVERT_LOCK:
        if not out.is_file():
            from .documents.images import register_heif_opener

            register_heif_opener()
            out.parent.mkdir(parents=True, exist_ok=True)
            with Image.open(info.path) as im:
                im = ImageOps.exif_transpose(im).convert("RGB")
                if max(im.size) > _CONVERTED_IMAGE_EDGE:
                    im.thumbnail((_CONVERTED_IMAGE_EDGE, _CONVERTED_IMAGE_EDGE), Image.LANCZOS)
                im.save(out, format="JPEG", quality=88, optimize=True)
    return AttachmentInfo(
        path=out,
        filename=f"{info.path.stem}.jpg",
        size=out.stat().st_size,
        mime="image/jpeg",
        sha256=_sha256_of(out),
        category="native_image",
    )


# ---------- block builders -------------------------------------------------


def _is_pptx(info: AttachmentInfo) -> bool:
    return info.path.suffix.lower() in _PPTX_EXTS


def _deck_text(info: AttachmentInfo) -> str:
    """Slide text for the companion block; legacy decks (.ppt, .odp, Kingsoft
    .dps) go through the same reader as read_document."""
    if info.path.suffix.lower() == ".pptx":
        return extract_pptx_text(info.path)
    from .documents.intake import read_any

    return read_any(info.path)


def _pptx_text_companion_anthropic(info: AttachmentInfo) -> dict[str, Any]:
    try:
        text = _deck_text(info)
    except Exception as exc:
        text = f"[text extraction failed: {exc}]"
    return {"type": "text", "text": _wrap_inline(info, text)}


def _pptx_text_companion_openai(info: AttachmentInfo) -> dict[str, Any]:
    try:
        text = _deck_text(info)
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
        try:
            info = _provider_image(info)
        except Exception as exc:
            return {"type": "text", "text": f"[could not convert image {info.filename} for the model: {exc}. Saved at {info.path}.]"}
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
        return {"type": "text", "text": _wrap_full_text(info, extract_inline_text(info))}
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
        try:
            info = _provider_image(info)
        except Exception as exc:
            return {"type": "input_text", "text": f"[could not convert image {info.filename} for the model: {exc}. Saved at {info.path}.]"}
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
        return {"type": "input_text", "text": _wrap_full_text(info, extract_inline_text(info))}
    return {"type": "input_text", "text": _stub_text(info)}


def _safe_pptx_text(info: AttachmentInfo) -> str:
    try:
        return _deck_text(info)
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

    # Aggregate inline budget, spent in the order given (the current turn's
    # own uploads come first). Overflow degrades to a path stub the model can
    # open with a tool, so a large batch costs a tool call instead of the
    # whole context window. Text attachments are additionally planned in
    # tokens: past the turn's attachment share they become outlines.
    budget_left = _MAX_TOTAL_INLINE_BYTES
    plan = _plan_text_attachments(infos)

    def fits(info: AttachmentInfo) -> bool:
        nonlocal budget_left
        cost = _inline_cost(info)
        if cost == 0:
            return True
        if cost > budget_left:
            return False
        budget_left -= cost
        return True

    def block_text(idx: int, info: AttachmentInfo) -> Optional[str]:
        """Outline or deferred-stub text when ``info`` is not inlined, else None."""
        decision = plan.get(idx)
        if decision is not None and decision[0] == "outline":
            return decision[1]
        if not fits(info):
            return _deferred_text(info)
        return None

    if provider == "anthropic":
        for idx, info in enumerate(infos):
            replacement = block_text(idx, info)
            if replacement is not None:
                blocks.append({"type": "text", "text": replacement})
                continue
            if idx in plan:
                blocks.append({"type": "text", "text": _wrap_full_text(info, plan[idx][1])})
                continue
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
        for idx, info in enumerate(infos):
            replacement = block_text(idx, info)
            if replacement is not None:
                blocks.append({"type": "input_text", "text": replacement})
                continue
            if idx in plan:
                blocks.append({"type": "input_text", "text": _wrap_full_text(info, plan[idx][1])})
                continue
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
