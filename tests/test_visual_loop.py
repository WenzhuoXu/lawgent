"""The visual-QA loop: render tools must return images, at provider parity.

Before this, ``render_*`` returned a JSON list of file paths. Tool results
reach the model as text, so it never saw its own output while the recipes told
it to "look for overlapping elements" — decks shipped with blank slides. These
tests pin the contract that the pixels actually come back.
"""

from __future__ import annotations

import json
from pathlib import Path

import pytest

from legal_helper.documents.contact_sheet import build_contact_sheets
from legal_helper.tools.multimodal import (
    INLINE_IMAGES_KEY,
    image_result,
    split_inline_images,
    to_anthropic_tool_content,
    to_openai_tool_content,
)


@pytest.fixture
def jpegs(tmp_path: Path) -> list[Path]:
    from PIL import Image

    out = []
    for i in range(3):
        p = tmp_path / f"page-{i + 1}.jpg"
        Image.new("RGB", (400, 225), (200, 210 - i * 30, 220)).save(p)
        out.append(p)
    return out


def test_plain_results_pass_through_untouched():
    """Every non-image tool result must survive both translators byte-identical."""
    for payload in ('{"ok": true}', "ERROR: File not found", "/abs/path/out.pptx", ""):
        assert split_inline_images(payload) == (payload, [])
        assert to_anthropic_tool_content(payload) == payload
        assert to_openai_tool_content(payload) == payload


def test_image_result_round_trips(jpegs):
    raw = image_result({"count": len(jpegs)}, jpegs)
    text, images = split_inline_images(raw)
    assert json.loads(text)["count"] == 3
    assert INLINE_IMAGES_KEY not in text, "base64 must not leak into the text half"
    assert [i["media_type"] for i in images] == ["image/jpeg"] * 3
    assert all(i["data"] for i in images)


def test_anthropic_and_openai_see_the_same_images(jpegs):
    raw = image_result({"count": 3}, jpegs)
    a = to_anthropic_tool_content(raw)
    o = to_openai_tool_content(raw)

    assert [b["type"] for b in a] == ["text", "image", "image", "image"]
    assert [p["type"] for p in o] == ["input_text", "input_image", "input_image", "input_image"]
    assert a[0]["text"] == o[0]["text"]
    for ablock, opart in zip(a[1:], o[1:]):
        assert ablock["source"]["type"] == "base64"
        assert opart["image_url"] == (
            f"data:{ablock['source']['media_type']};base64,{ablock['source']['data']}"
        )


def test_image_count_is_capped(jpegs, tmp_path):
    from PIL import Image

    many = list(jpegs)
    for i in range(20):
        p = tmp_path / f"extra-{i}.jpg"
        Image.new("RGB", (80, 45), (10, 10, 10)).save(p)
        many.append(p)
    text, images = split_inline_images(image_result({}, many, max_images=4))
    assert len(images) == 4
    assert "images_truncated" in json.loads(text)


def test_unreadable_image_is_reported_not_fatal(tmp_path, jpegs):
    broken = tmp_path / "broken.jpg"
    broken.write_bytes(b"not a jpeg")
    text, images = split_inline_images(image_result({}, [jpegs[0], broken]))
    assert len(images) == 1, "the good image still comes through"
    assert json.loads(text)["image_errors"], "the bad one is reported"


def test_contact_sheet_labels_and_splits(jpegs, tmp_path):
    from PIL import Image

    single = build_contact_sheets(jpegs, tmp_path / "cs", "deck", label="Slide")
    assert len(single) == 1
    with Image.open(single[0]) as im:
        assert im.width > 400 and im.height > 200

    split = build_contact_sheets(jpegs * 5, tmp_path / "cs2", "deck", label="Slide", per_sheet=4)
    assert len(split) == 4, "15 pages at 4 per sheet"
    assert all(p.is_file() for p in split)


def test_contact_sheet_handles_empty_and_corrupt(tmp_path):
    assert build_contact_sheets([], tmp_path, "x") == []
    bad = tmp_path / "bad.jpg"
    bad.write_bytes(b"nope")
    sheets = build_contact_sheets([bad], tmp_path / "cs", "x")
    assert len(sheets) == 1, "a corrupt page renders as a placeholder cell, not a crash"


def test_render_pptx_slides_returns_images(tmp_path, monkeypatch):
    """End-to-end: write a deck, render it, get pixels back."""
    import shutil

    if not shutil.which("soffice"):
        pytest.skip("soffice not available")

    from legal_helper.documents.writers.pptx import write_pptx_deck
    from legal_helper.tools.documents import render_pptx_slides

    deck = tmp_path / "deck.pptx"
    write_pptx_deck(deck, [{"title": "One", "bullets": ["a"]}, {"title": "Two", "bullets": ["b"]}])

    from legal_helper import config

    settings = config.current_settings()
    monkeypatch.setattr(settings, "outputs_dir", tmp_path / "out", raising=False)

    raw = render_pptx_slides.call({"path": str(deck), "filename_prefix": "qa"})
    if isinstance(raw, str) and raw.startswith("ERROR"):
        pytest.skip(f"render unavailable: {raw[:80]}")
    text, images = split_inline_images(raw)
    body = json.loads(text)
    assert body["count"] == 2
    assert body["contact_sheets"], "a labeled contact sheet is produced"
    assert images, "the model receives the rendered slides, not just paths"
