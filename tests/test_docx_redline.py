"""Round-trip tests for native Word tracked-changes redlining (w:ins/w:del)."""

from __future__ import annotations

import zipfile
from xml.etree import ElementTree as ET

import pytest
from docx import Document

from legal_helper.documents.redline import (
    extract_docx_revisions,
    preview_docx_revisions,
    redline_docx_document,
)


_W = "{http://schemas.openxmlformats.org/wordprocessingml/2006/main}"


def _make_source(path):
    doc = Document()
    p = doc.add_paragraph()
    p.add_run("The lessee shall pay ")
    amount = p.add_run("USD 1,000,000")
    amount.bold = True
    p.add_run(" within thirty (30) days.")
    doc.add_paragraph("Governing law: the laws of England.")
    table = doc.add_table(rows=1, cols=2)
    table.rows[0].cells[0].text = "Deposit"
    table.rows[0].cells[1].text = "USD 50,000"
    doc.save(str(path))
    return path


@pytest.fixture()
def redlined(tmp_path):
    src = _make_source(tmp_path / "src.docx")
    out = tmp_path / "redlined.docx"
    result = redline_docx_document(
        src,
        out,
        [
            {"op": "replace", "find": "USD 1,000,000", "replace": "USD 850,000",
             "comment": "Reduced per term sheet."},
            {"op": "delete", "find": "thirty (30) "},
            {"op": "insert_after", "find": "days.",
             "text": " Payment shall be in immediately available funds."},
            {"op": "replace", "find": "USD 50,000", "replace": "USD 25,000"},
            {"op": "comment", "find": "Governing law", "text": "Confirm dispute-resolution seat."},
            {"op": "replace", "find": "not present anywhere", "replace": "x"},
        ],
        timestamp="2026-07-21T09:00:00Z",
    )
    return src, out, result


def test_redline_reports_counts_and_misses(redlined):
    _, out, result = redlined
    assert out.is_file()
    counts = {item["find"]: item["count"] for item in result["applied"]}
    assert counts["USD 1,000,000"] == 1
    assert counts["USD 50,000"] == 1  # table-cell paragraphs are covered
    assert counts["not present anywhere"] == 0
    assert result["not_found"] == ["not present anywhere"]
    assert result["revision_count"] == 6  # 2 + 1 + 1 + 2
    assert result["comment_count"] == 2


def test_redline_emits_real_ins_del_marks(redlined):
    _, out, _ = redlined
    with zipfile.ZipFile(out) as zf:
        xml = zf.read("word/document.xml").decode("utf-8")
    root = ET.fromstring(xml)
    ins = list(root.iter(f"{_W}ins"))
    dels = list(root.iter(f"{_W}del"))
    assert len(ins) == 3 and len(dels) == 3
    ids = [el.get(f"{_W}id") for el in ins + dels]
    assert len(ids) == len(set(ids)), "revision ids must be unique"
    for el in ins + dels:
        assert el.get(f"{_W}author") == "Legal AI Helper"
        assert el.get(f"{_W}date") == "2026-07-21T09:00:00Z"
    # Deleted text must use w:delText, never w:t (Word rejects the latter).
    for del_el in dels:
        assert list(del_el.iter(f"{_W}delText"))
        assert not list(del_el.iter(f"{_W}t"))


def test_redline_round_trips_accept_and_reject(redlined):
    src, out, _ = redlined
    accepted = preview_docx_revisions(out, "accept")
    rejected = preview_docx_revisions(out, "reject")
    assert (
        "The lessee shall pay USD 850,000 within days."
        " Payment shall be in immediately available funds." in accepted
    )
    assert "USD 25,000" in accepted
    # Rejecting every mark restores the original document text exactly.
    original = Document(str(src))
    original_text = "\n".join(p.text for p in original.paragraphs)
    for line in original_text.splitlines():
        assert line in rejected
    assert "USD 850,000" not in rejected
    assert "USD 1,000,000" in rejected


def test_extract_docx_revisions_lists_marks(redlined):
    _, out, _ = redlined
    revisions = extract_docx_revisions(out)
    by_type = {}
    for rev in revisions:
        by_type.setdefault(rev["type"], []).append(rev["text"])
    assert "USD 1,000,000" in by_type["deletion"]
    assert "USD 850,000" in by_type["insertion"]
    assert " Payment shall be in immediately available funds." in by_type["insertion"]
    assert all(rev["date"].endswith("Z") for rev in revisions)


def test_redline_preserves_run_formatting(redlined):
    _, out, _ = redlined
    with zipfile.ZipFile(out) as zf:
        root = ET.fromstring(zf.read("word/document.xml"))
    # The replacement for the bold "USD 1,000,000" run copies its rPr.
    for ins in root.iter(f"{_W}ins"):
        text = "".join(t.text or "" for t in ins.iter(f"{_W}t"))
        if text == "USD 850,000":
            run = ins.find(f"{_W}r")
            rpr = run.find(f"{_W}rPr")
            assert rpr is not None and rpr.find(f"{_W}b") is not None
            break
    else:
        pytest.fail("inserted replacement run not found")


def test_redline_comments_round_trip_via_python_docx(redlined):
    _, out, _ = redlined
    doc = Document(str(out))
    comments = {c.text for c in doc.comments}
    assert "Reduced per term sheet." in comments
    assert "Confirm dispute-resolution seat." in comments
    for c in doc.comments:
        assert c.author == "Legal AI Helper"


def test_redline_output_reopens_cleanly(redlined):
    _, out, _ = redlined
    # python-docx must parse the revised body without error and still see
    # the untouched text as regular runs.
    doc = Document(str(out))
    texts = [p.text for p in doc.paragraphs]
    assert any("The lessee shall pay" in t for t in texts)


def test_redline_cross_run_match(tmp_path):
    src = tmp_path / "src.docx"
    doc = Document()
    p = doc.add_paragraph()
    p.add_run("Liquidated ")
    p.add_run("damages ")
    p.add_run("apply here.")
    doc.save(str(src))

    out = tmp_path / "out.docx"
    result = redline_docx_document(
        src, out,
        [{"op": "replace", "find": "Liquidated damages apply",
          "replace": "违约金条款适用"}],
    )
    assert result["applied"][0]["count"] == 1
    assert "违约金条款适用 here." in preview_docx_revisions(out, "accept")
    assert "Liquidated damages apply here." in preview_docx_revisions(out, "reject")


def test_redline_multiple_occurrences_all_marked(tmp_path):
    src = tmp_path / "src.docx"
    doc = Document()
    doc.add_paragraph("Party A pays Party A's costs; Party A indemnifies.")
    doc.save(str(src))

    out = tmp_path / "out.docx"
    result = redline_docx_document(
        src, out, [{"op": "replace", "find": "Party A", "replace": "Party B"}]
    )
    assert result["applied"][0]["count"] == 3
    accepted = preview_docx_revisions(out, "accept")
    assert accepted.count("Party B") == 3
    assert "Party A" not in accepted


def test_redline_rejects_empty_find(tmp_path):
    src = _make_source(tmp_path / "src.docx")
    with pytest.raises(ValueError):
        redline_docx_document(src, tmp_path / "out.docx", [{"op": "replace", "find": ""}])
