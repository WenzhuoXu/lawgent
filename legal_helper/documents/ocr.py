"""OCR for pages and images that have no text layer.

14 of 92 uploaded PDFs were image-only scans and 3 more were mixed; 15 PDF
reads returned under 200 characters with no warning, so the model answered
from a near-empty extraction without knowing it. ``read_document`` now finds
the image-only pages, reads them with RapidOCR, and labels the result as OCR.

RapidOCR (Apache-2.0) runs PP-OCRv6 small models on CPU through onnxruntime:
on clean synthetic pages it scored 0.999 (EN) / 1.000 (ZH) character
similarity at 1–5 s a page. Its model files (~31 MB: det, rec, cls ``.onnx``)
live in ``site-packages/rapidocr/models`` and are fetched once from
ModelScope, SHA-256 checked, on first use; they are already present on this
host. If they are missing and the download fails, OCR reports itself
unavailable and the read says so rather than failing.

Results are cached by file SHA-256 and page under ``state/ocr_cache`` so a
scan is recognised once, not on every read.
"""

from __future__ import annotations

import hashlib
import json
import re
import threading
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, Optional

_ENGINE = None
_ENGINE_ERROR: Optional[str] = None
_LOCK = threading.Lock()
# Bump when line assembly or rendering changes, so stale cache entries are redone.
_CACHE_VERSION = "rapidocr-ppocrv6-small/1"
# Long edge, in pixels, a page is rendered at for recognition. RapidOCR
# resizes anything larger down to 2000 px anyway.
_TARGET_EDGE_PX = 2000

_CJK = re.compile(r"[　-〿㐀-䶿一-鿿＀-￯]")


@dataclass
class OcrResult:
    text: str
    mean_score: float = 0.0
    line_count: int = 0
    engine: str = _CACHE_VERSION
    cached: bool = False
    lines: list[dict[str, Any]] = field(default_factory=list)


def _engine():
    global _ENGINE, _ENGINE_ERROR
    if _ENGINE is not None or _ENGINE_ERROR is not None:
        return _ENGINE
    with _LOCK:
        if _ENGINE is None and _ENGINE_ERROR is None:
            try:
                from rapidocr import RapidOCR

                _ENGINE = RapidOCR(params={"Global.log_level": "error"})
            except Exception as exc:  # noqa: BLE001 — missing package or models
                _ENGINE_ERROR = f"{type(exc).__name__}: {exc}"
    return _ENGINE


def ocr_unavailable_reason() -> Optional[str]:
    """``None`` when OCR works here, else why not."""
    _engine()
    return _ENGINE_ERROR


def _join(left: str, right: str) -> str:
    if not left:
        return right
    if left and right and _CJK.match(left[-1]) and _CJK.match(right[0]):
        return left + right
    return left + " " + right


def _assemble(boxes: Any, texts: Any, scores: Any) -> tuple[str, list[dict[str, Any]]]:
    """Reading-order text: boxes grouped into lines by vertical overlap, each
    line read left to right; CJK neighbours join without a space."""
    items = []
    for box, text, score in zip(boxes, texts, scores):
        ys = [float(pt[1]) for pt in box]
        xs = [float(pt[0]) for pt in box]
        items.append({
            "top": min(ys), "bottom": max(ys), "left": min(xs),
            "text": str(text), "score": float(score),
        })
    items.sort(key=lambda it: (it["top"], it["left"]))
    lines: list[list[dict[str, Any]]] = []
    for item in items:
        center = (item["top"] + item["bottom"]) / 2
        height = max(1.0, item["bottom"] - item["top"])
        if lines:
            line = lines[-1]
            line_top = min(i["top"] for i in line)
            line_bottom = max(i["bottom"] for i in line)
            line_height = max(1.0, line_bottom - line_top)
            if abs(center - (line_top + line_bottom) / 2) < 0.5 * min(height, line_height):
                line.append(item)
                continue
        lines.append([item])
    out_lines = []
    records = []
    for line in lines:
        line.sort(key=lambda it: it["left"])
        text = ""
        for item in line:
            text = _join(text, item["text"].strip())
        out_lines.append(text)
        records.append({"text": text, "score": round(sum(i["score"] for i in line) / len(line), 4)})
    return "\n".join(out_lines), records


def ocr_image(image: Any) -> OcrResult:
    """Recognise a PIL image (or a path); raises RuntimeError when OCR is unavailable."""
    engine = _engine()
    if engine is None:
        raise RuntimeError(f"OCR unavailable: {_ENGINE_ERROR}")
    import numpy as np
    from PIL import Image

    if not isinstance(image, Image.Image):
        image = Image.open(image)
    array = np.asarray(image.convert("RGB"))
    with _LOCK:
        result = engine(array)
    boxes = getattr(result, "boxes", None)
    texts = getattr(result, "txts", None)
    scores = getattr(result, "scores", None)
    if boxes is None or not texts:
        return OcrResult(text="", mean_score=0.0, line_count=0)
    text, lines = _assemble(boxes, texts, scores)
    mean = sum(float(s) for s in scores) / len(scores) if scores else 0.0
    return OcrResult(text=text, mean_score=round(mean, 4), line_count=len(lines), lines=lines)


def file_sha256(path: Path) -> str:
    h = hashlib.sha256()
    with Path(path).open("rb") as fh:
        for chunk in iter(lambda: fh.read(1 << 20), b""):
            h.update(chunk)
    return h.hexdigest()


def _cache_dir(cache_dir: Optional[Path]) -> Path:
    if cache_dir is not None:
        return Path(cache_dir)
    from ..config import current_settings

    return current_settings().state_dir / "ocr_cache"


def _cache_file(cache_dir: Optional[Path], sha256: str, key: str) -> Path:
    return _cache_dir(cache_dir) / sha256[:2] / f"{sha256}-{key}.json"


def _load_cached(path: Path) -> Optional[OcrResult]:
    try:
        data = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return None
    if data.get("engine") != _CACHE_VERSION:
        return None
    return OcrResult(
        text=data.get("text", ""),
        mean_score=float(data.get("mean_score", 0.0)),
        line_count=int(data.get("line_count", 0)),
        engine=_CACHE_VERSION,
        cached=True,
    )


def _store(path: Path, result: OcrResult) -> None:
    try:
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(
            json.dumps(
                {
                    "engine": _CACHE_VERSION,
                    "text": result.text,
                    "mean_score": result.mean_score,
                    "line_count": result.line_count,
                },
                ensure_ascii=False,
            ),
            encoding="utf-8",
        )
    except OSError:
        return  # the cache is an optimisation; a read never fails on it


def ocr_pdf_page(pdf: Any, page_index: int, sha256: str, *, cache_dir: Optional[Path] = None) -> OcrResult:
    """OCR one page of an open ``pypdfium2.PdfDocument`` (0-based index)."""
    cache = _cache_file(cache_dir, sha256, f"p{page_index + 1}")
    cached = _load_cached(cache)
    if cached is not None:
        return cached
    page = pdf[page_index]
    try:
        width, height = page.get_size()
        scale = max(1.0, min(4.0, _TARGET_EDGE_PX / max(width, height, 1.0)))
        image = page.render(scale=scale).to_pil()
    finally:
        page.close()
    result = ocr_image(image)
    _store(cache, result)
    return result


def ocr_image_file(path: Path, *, cache_dir: Optional[Path] = None) -> OcrResult:
    sha = file_sha256(path)
    cache = _cache_file(cache_dir, sha, "img")
    cached = _load_cached(cache)
    if cached is not None:
        return cached
    from PIL import Image

    _register_heif()
    with Image.open(path) as im:
        result = ocr_image(im)
    _store(cache, result)
    return result


def _register_heif() -> None:
    from .images import register_heif_opener

    register_heif_opener()


__all__ = [
    "OcrResult",
    "file_sha256",
    "ocr_image",
    "ocr_image_file",
    "ocr_pdf_page",
    "ocr_unavailable_reason",
]
