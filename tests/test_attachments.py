"""Unit tests for the attachment ingestion layer."""

from __future__ import annotations

from pathlib import Path

import pytest

from legal_helper import attachments as att


@pytest.fixture(autouse=True)
def _clear_file_id_cache():
    att._FILE_ID_CACHE.clear()
    yield
    att._FILE_ID_CACHE.clear()


def _write(path: Path, content: bytes) -> Path:
    path.write_bytes(content)
    return path


def test_detect_category_pdf(tmp_path):
    p = _write(tmp_path / "x.pdf", b"%PDF-1.4 ...")
    assert att.detect_category(p) == "native_pdf"


def test_detect_category_image(tmp_path):
    for ext in (".png", ".jpg", ".jpeg", ".gif", ".webp"):
        p = _write(tmp_path / f"x{ext}", b"\x89PNG")
        assert att.detect_category(p) == "native_image", ext


def test_detect_category_office_and_text(tmp_path):
    assert att.detect_category(_write(tmp_path / "a.docx", b"PK\x03\x04")) == "text_inline"
    assert att.detect_category(_write(tmp_path / "a.xlsx", b"PK\x03\x04")) == "text_inline"
    assert att.detect_category(_write(tmp_path / "a.pptx", b"PK\x03\x04")) == "text_inline"
    assert att.detect_category(_write(tmp_path / "a.csv", b"a,b\n1,2")) == "text_inline"
    assert att.detect_category(_write(tmp_path / "a.md", b"# hi")) == "text_inline"
    assert att.detect_category(_write(tmp_path / "a.txt", b"hi")) == "text_inline"
    assert att.detect_category(_write(tmp_path / "a.py", b"print(1)")) == "text_inline"


def test_detect_category_unknown(tmp_path):
    p = _write(tmp_path / "a.bin", b"\x00\x01\x02")
    assert att.detect_category(p) == "binary_stub"


def test_describe_size_and_sha(tmp_path):
    p = _write(tmp_path / "a.txt", b"hello")
    info = att.describe(p)
    assert info.filename == "a.txt"
    assert info.size == 5
    assert info.category == "text_inline"
    assert len(info.sha256) == 64


def test_extract_inline_text_csv(tmp_path):
    p = _write(tmp_path / "data.csv", b"name,age\nAlice,30\nBob,25")
    info = att.describe(p)
    text = att.extract_inline_text(info)
    assert "Alice | 30" in text
    assert "Bob | 25" in text


def test_extract_inline_text_too_large(tmp_path):
    big = b"x" * (att._MAX_INLINE_BYTES + 1)
    p = _write(tmp_path / "big.txt", big)
    info = att.describe(p)
    out = att.extract_inline_text(info)
    assert "too large" in out
    assert "read_document" in out


def test_build_user_content_no_attachments_passes_text(tmp_path):
    assert att.build_user_content("hi", [], "anthropic") == "hi"
    assert att.build_user_content("hi", [], "openai") == "hi"


def test_build_user_content_anthropic_text_block_for_inline(tmp_path):
    p = _write(tmp_path / "note.md", b"# title\nhello")
    blocks = att.build_user_content("summarize", [p], "anthropic")
    assert isinstance(blocks, list)
    assert blocks[0]["type"] == "text"
    assert "<attached-file" in blocks[0]["text"]
    assert "hello" in blocks[0]["text"]
    assert blocks[-1] == {"type": "text", "text": "summarize"}


def test_build_user_content_openai_text_block_for_inline(tmp_path):
    p = _write(tmp_path / "note.md", b"# title\nhello")
    blocks = att.build_user_content("summarize", [p], "openai")
    assert isinstance(blocks, list)
    assert blocks[0]["type"] == "input_text"
    assert "<attached-file" in blocks[0]["text"]
    assert blocks[-1] == {"type": "input_text", "text": "summarize"}


def test_build_user_content_binary_stub(tmp_path):
    p = _write(tmp_path / "x.bin", b"\x00\x01\x02")
    blocks_anthropic = att.build_user_content("what is this", [p], "anthropic")
    assert blocks_anthropic[0]["type"] == "text"
    assert "binary file" in blocks_anthropic[0]["text"]
    assert "read_document" in blocks_anthropic[0]["text"]

    blocks_openai = att.build_user_content("what is this", [p], "openai")
    assert blocks_openai[0]["type"] == "input_text"
    assert "binary file" in blocks_openai[0]["text"]


def test_build_user_content_image_base64_anthropic(tmp_path):
    png_bytes = b"\x89PNG\r\n\x1a\nfake-image-bytes"
    p = _write(tmp_path / "shot.png", png_bytes)
    blocks = att.build_user_content("describe this", [p], "anthropic")
    assert blocks[0]["type"] == "image"
    assert blocks[0]["source"]["type"] == "base64"
    assert blocks[0]["source"]["media_type"] == "image/png"
    assert blocks[0]["source"]["data"]


def test_build_user_content_image_data_url_openai(tmp_path):
    png_bytes = b"\x89PNG\r\n\x1a\nfake-image-bytes"
    p = _write(tmp_path / "shot.png", png_bytes)
    blocks = att.build_user_content("describe this", [p], "openai")
    assert blocks[0]["type"] == "input_image"
    assert blocks[0]["image_url"].startswith("data:image/png;base64,")


def test_pdf_routes_through_anthropic_files_api(tmp_path, monkeypatch):
    calls: list[dict] = []

    class FakeResult:
        id = "file_abc123"

    class FakeFiles:
        def upload(self, *, file):
            calls.append({"file": file})
            return FakeResult()

    class FakeBeta:
        files = FakeFiles()

    class FakeClient:
        def __init__(self, *a, **k):
            self.beta = FakeBeta()

    monkeypatch.setattr("legal_helper.attachments.Anthropic", FakeClient, raising=False)

    # The import is local, so we patch the symbol where it's resolved.
    import anthropic as _anth
    monkeypatch.setattr(_anth, "Anthropic", FakeClient)

    p = _write(tmp_path / "deck.pdf", b"%PDF-1.4 fake")
    blocks = att.build_user_content("read this", [p], "anthropic")
    assert blocks[0]["type"] == "document"
    assert blocks[0]["source"] == {"type": "file", "file_id": "file_abc123"}
    assert blocks[0]["title"] == "deck.pdf"
    assert len(calls) == 1

    # Cache hit on second call: no extra upload.
    blocks2 = att.build_user_content("again", [p], "anthropic")
    assert blocks2[0]["source"]["file_id"] == "file_abc123"
    assert len(calls) == 1


def test_pdf_routes_through_openai_files_api(tmp_path, monkeypatch):
    calls: list[dict] = []

    class FakeResult:
        id = "file_xyz789"

    class FakeFiles:
        def create(self, *, file, purpose):
            calls.append({"file": file, "purpose": purpose})
            return FakeResult()

    class FakeClient:
        def __init__(self, *a, **k):
            self.files = FakeFiles()

    import openai as _oai
    monkeypatch.setattr(_oai, "OpenAI", FakeClient)

    p = _write(tmp_path / "deck.pdf", b"%PDF-1.4 fake")
    blocks = att.build_user_content("read this", [p], "openai")
    assert blocks[0] == {"type": "input_file", "file_id": "file_xyz789"}
    assert calls[0]["purpose"] == "user_data"


def test_needs_anthropic_files_beta_detects_file_source():
    messages_no = [{"role": "user", "content": "plain text"}]
    messages_with_base64 = [
        {"role": "user", "content": [{"type": "image", "source": {"type": "base64", "data": "..."}}]}
    ]
    messages_with_file = [
        {"role": "user", "content": [{"type": "document", "source": {"type": "file", "file_id": "f"}}]}
    ]
    assert not att.needs_anthropic_files_beta(messages_no)
    assert not att.needs_anthropic_files_beta(messages_with_base64)
    assert att.needs_anthropic_files_beta(messages_with_file)


def test_missing_file_falls_back_to_stub(tmp_path):
    missing = tmp_path / "ghost.bin"
    blocks = att.build_user_content("hi", [missing], "anthropic")
    assert blocks[0]["type"] == "text"
    assert "ghost.bin" in blocks[0]["text"]
