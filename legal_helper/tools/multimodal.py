"""Provider-neutral protocol for tool results that carry images.

The ``render_*`` tools used to return a JSON blob of file paths. Tool results
reach the model as *text*, so the model never saw a single pixel of what it
had just produced — while ``playbook/format_recipes/pptx.md`` instructed it to
"render and look for overlapping elements, clipped text". That instruction was
structurally impossible to follow, which is why layout defects survived to the
user.

Both SDKs do accept images inside a tool result:

* Anthropic — ``BetaToolResultBlockParam.content`` takes a list of text/image
  blocks (``anthropic/lib/tools/_beta_runner.py`` passes the tool's return
  value straight through as ``content``).
* OpenAI — ``function_call_output.output`` takes
  ``ResponseFunctionCallOutputItemListParam``, i.e. ``input_text`` /
  ``input_image`` parts.

Tools stay provider-agnostic: they return a JSON string carrying an
``__inline_images__`` key, and each provider's tool-execution wrapper calls
:func:`to_anthropic_tool_content` / :func:`to_openai_tool_content` to expand it
into that provider's block shape. A tool result without the key is passed
through as a plain string exactly as before, so nothing else changes.
"""

from __future__ import annotations

import base64
import io
import json
from pathlib import Path
from typing import Any

# The key that marks a tool result as carrying inline images.
INLINE_IMAGES_KEY = "__inline_images__"

# Anthropic downsamples anything past ~1568px on the long edge, and each
# megapixel is real tokens. Slide/page thumbnails stay legible well below that.
_MAX_EDGE = 1400
_JPEG_QUALITY = 72

# Hard ceiling per tool call. A 60-page contract must not blow the context
# window; the model gets a contact sheet and can request specific pages.
_MAX_IMAGES = 6


def encode_image(path: Path, *, max_edge: int = _MAX_EDGE) -> dict[str, str]:
    """Downscale ``path`` to ``max_edge`` and return a base64 JPEG record."""
    from PIL import Image

    with Image.open(path) as im:
        im = im.convert("RGB")
        if max(im.size) > max_edge:
            scale = max_edge / max(im.size)
            im = im.resize((max(1, int(im.width * scale)), max(1, int(im.height * scale))), Image.LANCZOS)
        buf = io.BytesIO()
        im.save(buf, format="JPEG", quality=_JPEG_QUALITY, optimize=True)
    return {"media_type": "image/jpeg", "data": base64.standard_b64encode(buf.getvalue()).decode("ascii")}


def image_result(
    payload: dict[str, Any],
    images: list[Path],
    *,
    max_images: int = _MAX_IMAGES,
    max_edge: int = _MAX_EDGE,
) -> str:
    """Serialise a tool result that should reach the model with images attached.

    ``payload`` is the ordinary JSON body (paths, counts, warnings). ``images``
    are rendered alongside it. Images that fail to encode are reported in
    ``image_errors`` rather than failing the whole call — a partial render is
    still more useful than a bare path list.
    """
    encoded: list[dict[str, str]] = []
    errors: list[str] = []
    for p in list(images)[:max_images]:
        try:
            encoded.append(encode_image(Path(p), max_edge=max_edge))
        except Exception as exc:
            errors.append(f"{Path(p).name}: {type(exc).__name__}: {exc}")
    body = dict(payload)
    if errors:
        body["image_errors"] = errors
    if len(images) > max_images:
        body["images_truncated"] = (
            f"{len(images)} images available, {max_images} shown — "
            f"call view_image(path) on a specific file for the rest."
        )
    body[INLINE_IMAGES_KEY] = encoded
    return json.dumps(body, ensure_ascii=False, indent=2)


def split_inline_images(result: Any) -> tuple[str, list[dict[str, str]]]:
    """Split a tool result into (text, images).

    Returns ``(result, [])`` unchanged for any result that is not an
    image-carrying JSON object, so this is safe to call on every tool result.
    """
    if not isinstance(result, str) or INLINE_IMAGES_KEY not in result:
        return (result if isinstance(result, str) else str(result), [])
    try:
        body = json.loads(result)
    except Exception:
        return (result, [])
    if not isinstance(body, dict):
        return (result, [])
    images = body.pop(INLINE_IMAGES_KEY, None)
    if not isinstance(images, list) or not images:
        return (json.dumps(body, ensure_ascii=False, indent=2), [])
    return (json.dumps(body, ensure_ascii=False, indent=2), images)


def to_anthropic_tool_content(result: Any) -> Any:
    """Expand a tool result into Anthropic tool_result content blocks."""
    text, images = split_inline_images(result)
    if not images:
        return result
    blocks: list[dict[str, Any]] = [{"type": "text", "text": text}]
    for img in images:
        blocks.append({
            "type": "image",
            "source": {
                "type": "base64",
                "media_type": img.get("media_type", "image/jpeg"),
                "data": img.get("data", ""),
            },
        })
    return blocks


def to_openai_tool_content(result: Any) -> Any:
    """Expand a tool result into OpenAI function_call_output content parts."""
    text, images = split_inline_images(result)
    if not images:
        return text
    parts: list[dict[str, Any]] = [{"type": "input_text", "text": text}]
    for img in images:
        media = img.get("media_type", "image/jpeg")
        parts.append({
            "type": "input_image",
            "image_url": f"data:{media};base64,{img.get('data', '')}",
            "detail": "auto",
        })
    return parts
