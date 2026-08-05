"""Unit tests for the `fetch_url_to_artifact` connector."""

from __future__ import annotations

import json
from pathlib import Path

import httpx
import pytest

from legal_helper.tools import fetch_attach as fa


def _call(url: str = "https://example.com/foo.pdf") -> dict:
    # @beta_tool returns a wrapper object; call the underlying callable.
    fn = getattr(fa.fetch_url_to_artifact, "callable", None) or fa.fetch_url_to_artifact
    raw = fn(url=url)
    return json.loads(raw) if isinstance(raw, str) else raw


@pytest.fixture
def outputs_dir(tmp_path, monkeypatch):
    out = tmp_path / "out"
    out.mkdir()
    monkeypatch.setenv("LEGAL_HELPER_OUTPUTS_DIR", str(out))
    from legal_helper import config as cfg

    cfg._CACHED = None
    yield out
    cfg._CACHED = None


def _patch_transport(monkeypatch, body: bytes, *, content_type: str = "application/pdf",
                     content_disposition: str | None = None, status: int = 200) -> None:
    def handler(request: httpx.Request) -> httpx.Response:
        headers = {"content-type": content_type}
        if content_disposition:
            headers["content-disposition"] = content_disposition
        return httpx.Response(status, content=body, headers=headers)

    transport = httpx.MockTransport(handler)
    original_init = httpx.Client.__init__

    def patched_init(self, *args, **kwargs):
        kwargs["transport"] = transport
        original_init(self, *args, **kwargs)

    monkeypatch.setattr(httpx.Client, "__init__", patched_init)


def test_fetch_pdf_lands_under_outputs(outputs_dir, monkeypatch):
    pdf_bytes = b"%PDF-1.4\n%fake-pdf-bytes\n"
    _patch_transport(monkeypatch, pdf_bytes)
    result = _call("https://example.com/path/report.pdf")
    assert "error" not in result, result
    assert result["filename"] == "report.pdf"
    assert result["size"] == len(pdf_bytes)
    assert result["content_type"] == "application/pdf"
    assert result["category"] == "native_pdf"
    assert Path(result["path"]).is_file()
    assert Path(result["path"]).read_bytes() == pdf_bytes


def test_fetch_tool_returns_string_for_provider_parity(outputs_dir, monkeypatch):
    """Anthropic's `tool_result.content` rejects non-string values; OpenAI's
    Responses path also expects a string. The decorator must hand back a
    string for both providers — never a bare dict."""
    pdf_bytes = b"%PDF-1.4"
    _patch_transport(monkeypatch, pdf_bytes)
    fn = getattr(fa.fetch_url_to_artifact, "callable", None) or fa.fetch_url_to_artifact
    raw = fn(url="https://example.com/parity.pdf")
    assert isinstance(raw, str), f"expected str, got {type(raw).__name__}"
    parsed = json.loads(raw)
    assert parsed["filename"] == "parity.pdf"


def test_fetch_uses_content_disposition_filename(outputs_dir, monkeypatch):
    body = b"name,age\nA,1"
    _patch_transport(
        monkeypatch, body,
        content_type="text/csv",
        content_disposition='attachment; filename="real_name.csv"',
    )
    result = _call("https://example.com/download")
    assert result["filename"] == "real_name.csv"
    assert result["category"] == "text_inline"


def test_fetch_rejects_non_http(outputs_dir):
    result = _call("file:///etc/passwd")
    assert "error" in result


def test_fetch_size_cap(outputs_dir, monkeypatch):
    monkeypatch.setattr(fa, "_MAX_BYTES", 16)
    _patch_transport(monkeypatch, b"x" * 64)
    result = _call("https://example.com/big.bin")
    assert "error" in result
    # Partial file must not be left behind.
    assert not list(outputs_dir.glob(".fetch_*.partial"))


def test_fetch_avoids_overwriting_existing_file(outputs_dir, monkeypatch):
    pdf_bytes = b"%PDF-1.4 first"
    _patch_transport(monkeypatch, pdf_bytes)
    r1 = _call("https://example.com/duplicate.pdf")
    assert r1["filename"] == "duplicate.pdf"

    pdf_bytes2 = b"%PDF-1.4 second"
    _patch_transport(monkeypatch, pdf_bytes2)
    r2 = _call("https://example.com/duplicate.pdf")
    assert r2["filename"] != "duplicate.pdf"
    assert r2["filename"].startswith("duplicate_") and r2["filename"].endswith(".pdf")
