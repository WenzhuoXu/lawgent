"""Shared body-text fetcher used by ``*_fetch`` connectors.

Downloads a URL, classifies the response, and extracts plain text inline so
the calling tool can return it as ``body_text`` in the same turn — agents
don't need to wait a turn for an attachment to land in the next message.

Supported formats:

  - text/html, application/xhtml+xml, application/xml → strip tags
  - application/pdf → ``pdfminer.high_level.extract_text``
  - application/vnd.openxmlformats-officedocument.wordprocessingml.document
    (.docx) → ``python-docx`` paragraph + table extraction
  - text/* (plain, xml, json) → returned verbatim

Other content types fall through with ``body_text=""`` and a ``note`` so the
caller can advise the agent to follow up with ``fetch_url_to_artifact``.
"""

from __future__ import annotations

import io
import re
from typing import Any

import httpx

from .base import TIMEOUT, USER_AGENT

_MAX_BYTES = 25 * 1024 * 1024  # 25 MB hard cap on inline body fetches
_HTML_TAG_RE = re.compile(r"<[^>]+>")


def _html_to_text(html: str, *, xml: bool = False) -> str:
    try:
        from bs4 import BeautifulSoup
    except ImportError:
        return " ".join(_HTML_TAG_RE.sub(" ", html).split())
    soup = BeautifulSoup(html, "lxml-xml" if xml else "lxml")
    for tag in soup(["script", "style", "noscript", "header", "footer", "nav"]):
        tag.decompose()
    return " ".join(soup.get_text(separator=" ").split())


def _pdf_to_text(data: bytes) -> str:
    from pdfminer.high_level import extract_text

    return (extract_text(io.BytesIO(data)) or "").strip()


def _docx_to_text(data: bytes) -> str:
    from docx import Document

    doc = Document(io.BytesIO(data))
    parts: list[str] = []
    for p in doc.paragraphs:
        if p.text:
            parts.append(p.text)
    for table in doc.tables:
        for row in table.rows:
            parts.append(" | ".join(cell.text for cell in row.cells))
    return "\n".join(parts)


def fetch_body_text(
    url: str,
    *,
    max_chars: int = 12000,
    headers: dict[str, str] | None = None,
    accept: str | None = None,
    timeout: httpx.Timeout | None = None,
) -> dict[str, Any]:
    """Download ``url`` and return ``{body_text, content_type, ...}``.

    Returns a dict with these keys (errors keep the same shape, with an
    ``error`` key set):

      url            — the resolved URL after redirects
      content_type   — Content-Type header
      size_bytes     — raw payload size before extraction
      body_text      — extracted plain text (truncated to ``max_chars``)
      truncated      — True iff the extracted text exceeded ``max_chars``
      extractor      — one of ``html``, ``pdf``, ``docx``, ``text``, ``none``
      note           — present when the extractor could not produce text
    """
    request_headers = {"User-Agent": USER_AGENT}
    if accept:
        request_headers["Accept"] = accept
    if headers:
        request_headers.update(headers)
    try:
        with httpx.Client(
            timeout=timeout or TIMEOUT, follow_redirects=True
        ) as client:
            resp = client.get(url, headers=request_headers)
            resp.raise_for_status()
            raw = resp.content
            final_url = str(resp.url)
            content_type = (
                resp.headers.get("content-type")
                or resp.headers.get("Content-Type")
                or "application/octet-stream"
            ).split(";")[0].strip().lower()
    except httpx.HTTPError as e:
        return {"url": url, "error": f"body fetch failed: {e!s}"}

    size = len(raw)
    if size > _MAX_BYTES:
        return {
            "url": final_url,
            "content_type": content_type,
            "size_bytes": size,
            "body_text": "",
            "truncated": True,
            "extractor": "none",
            "error": f"payload exceeds {_MAX_BYTES} byte cap",
        }

    text = ""
    extractor = "none"
    note: str | None = None
    try:
        if content_type in {"application/pdf"} or final_url.lower().endswith(".pdf"):
            text = _pdf_to_text(raw)
            extractor = "pdf"
        elif content_type in {
            "application/vnd.openxmlformats-officedocument.wordprocessingml.document",
        } or final_url.lower().endswith(".docx"):
            text = _docx_to_text(raw)
            extractor = "docx"
        elif content_type in {"text/html", "application/xhtml+xml"}:
            text = _html_to_text(raw.decode(resp.encoding or "utf-8", errors="replace"))
            extractor = "html"
        elif content_type in {"application/xml", "text/xml"}:
            text = _html_to_text(
                raw.decode(resp.encoding or "utf-8", errors="replace"), xml=True
            )
            extractor = "xml"
        elif content_type.startswith("text/") or content_type in {"application/json"}:
            text = raw.decode(resp.encoding or "utf-8", errors="replace")
            extractor = "text"
            # Some servers (notably Federal Register's raw_text_url) advertise
            # text/plain but ship an <html><body><pre>...</pre></body></html>
            # wrapper around the real text. Detect that and strip.
            stripped = text.lstrip()[:30].lower()
            if stripped.startswith("<html") or stripped.startswith("<!doctype html"):
                text = _html_to_text(text)
                extractor = "html"
        else:
            note = (
                f"content-type {content_type!r} not extractable inline; "
                f"call fetch_url_to_artifact on this URL to download the bytes "
                f"and read them on the next turn"
            )
    except Exception as exc:  # noqa: BLE001 — surface as a soft failure
        note = f"extractor {extractor!r} failed: {exc!s}"
        text = ""
        extractor = "none"

    truncated = False
    if len(text) > max_chars:
        text = text[: max_chars - 1] + "…"
        truncated = True

    out: dict[str, Any] = {
        "url": final_url,
        "content_type": content_type,
        "size_bytes": size,
        "body_text": text,
        "truncated": truncated,
        "extractor": extractor,
    }
    if note:
        out["note"] = note
    return out


__all__ = ["fetch_body_text"]
