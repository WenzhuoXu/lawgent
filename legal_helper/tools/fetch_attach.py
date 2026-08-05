"""`fetch_url_to_artifact`: download any external file and hand it back as a chat artifact.

Use this when the user wants to **possess** an external file (PDF, image,
DOCX, XLSX, CSV, ZIP, dataset, HTML page saved as a file — anything by URL).
Contrast with hosted `web_fetch`, which is for *reading and summarizing* a
web page. Pick `fetch_url_to_artifact` when the goal is to hand the bytes
back as a clickable artifact, then optionally describe them.
"""

from __future__ import annotations

import hashlib
import json
import mimetypes
import re
import uuid
from pathlib import Path
from typing import Any
from urllib.parse import unquote, urlparse

import httpx
from anthropic import beta_tool

from ..artifact_origin import record_origin
from ..attachments import detect_category
from ..config import current_settings
from ..connectors.base import USER_AGENT, TIMEOUT


_MAX_BYTES = 50 * 1024 * 1024  # 50 MB
_CD_FILENAME_RE = re.compile(r'filename\*?=(?:UTF-8\'\')?"?([^";]+)"?', re.IGNORECASE)


def _safe_filename(name: str, default: str) -> str:
    name = Path(name).name
    name = "".join(ch if ch.isalnum() or ch in {" ", ".", "_", "-"} else "_" for ch in name).strip()
    return name or default


def _filename_from_response(url: str, headers: dict[str, str], content_type: str) -> str:
    cd = headers.get("content-disposition") or headers.get("Content-Disposition") or ""
    if cd:
        m = _CD_FILENAME_RE.search(cd)
        if m:
            cand = unquote(m.group(1))
            cand = _safe_filename(cand, "")
            if cand:
                return cand
    path = urlparse(url).path
    cand = _safe_filename(Path(unquote(path)).name, "")
    if cand and "." in cand:
        return cand
    # Final fallback: synthesize from content-type extension.
    ext = mimetypes.guess_extension(content_type.split(";")[0].strip()) or ".bin"
    return f"fetched_{uuid.uuid4().hex[:8]}{ext}"


@beta_tool
def fetch_url_to_artifact(url: str) -> str:
    """Download an external file by URL and save it as a chat artifact.

    Use this when the user wants the file itself — any file type (PDF,
    image, DOCX, XLSX, CSV, ZIP, dataset, HTML, etc.). The saved file
    shows up automatically as a downloadable artifact in the chat UI.
    The download is also automatically visible to the model on the
    NEXT user turn (PDFs and images flow through the Files API;
    text-extractable formats are inlined).

    Prefer this over hosted `web_fetch` whenever the user wants the
    bytes back. Use `web_fetch` only when the goal is to read and
    summarize a web page.

    Args:
        url: The HTTP/HTTPS URL of the file to download.

    Returns:
        JSON string containing: filename, path, url, content_type,
        sha256, size, category (native_pdf/native_image/text_inline/
        binary_stub). On failure, an `error` key explains why.
    """
    if not (url.startswith("http://") or url.startswith("https://")):
        return json.dumps({"error": "URL must start with http:// or https://", "url": url})

    settings = current_settings()
    outputs_dir = settings.outputs_dir
    outputs_dir.mkdir(parents=True, exist_ok=True)

    headers = {
        "User-Agent": USER_AGENT,
        "Accept": "*/*",
    }
    sha = hashlib.sha256()
    total = 0
    tmp_path = outputs_dir / f".fetch_{uuid.uuid4().hex}.partial"

    try:
        with httpx.Client(timeout=TIMEOUT, follow_redirects=True) as client:
            with client.stream("GET", url, headers=headers) as resp:
                resp.raise_for_status()
                content_type = (
                    resp.headers.get("content-type")
                    or resp.headers.get("Content-Type")
                    or "application/octet-stream"
                ).split(";")[0].strip()
                filename = _filename_from_response(url, dict(resp.headers), content_type)
                with tmp_path.open("wb") as f:
                    for chunk in resp.iter_bytes(chunk_size=65536):
                        if not chunk:
                            continue
                        total += len(chunk)
                        if total > _MAX_BYTES:
                            raise RuntimeError(
                                f"File exceeds size cap ({total} bytes > {_MAX_BYTES})"
                            )
                        sha.update(chunk)
                        f.write(chunk)
    except Exception as exc:
        try:
            tmp_path.unlink(missing_ok=True)
        except Exception:
            pass
        return json.dumps({"error": str(exc), "url": url})

    sha_hex = sha.hexdigest()
    # If a file with the same name already exists, prefix a short hash
    # so we never overwrite a previous artifact.
    final_path = outputs_dir / filename
    if final_path.exists():
        stem = Path(filename).stem
        suffix = Path(filename).suffix
        final_path = outputs_dir / f"{stem}_{sha_hex[:8]}{suffix}"
    tmp_path.rename(final_path)

    # Mark provenance so the post-turn artifact scan tags this as a download
    # rather than a generated document.
    record_origin(final_path, "fetched")

    category = detect_category(final_path)
    return json.dumps({
        "filename": final_path.name,
        "path": str(final_path.resolve()),
        "url": f"/api/artifacts/chat_artifacts/{final_path.parent.name}/{final_path.name}",
        "content_type": content_type,
        "sha256": sha_hex,
        "size": total,
        "category": category,
    })


__all__ = ["fetch_url_to_artifact"]
