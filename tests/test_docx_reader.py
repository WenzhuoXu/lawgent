"""DOCX reading as Word shows it: revisions, headers/footers, numbering labels.

Repros from the 2026-10-07 capability review:
- D-2: tracked changes read as accepted (7,954 deleted characters hidden
  across 5 uploads); headers and footers dropped.
- D-7: anydoc rendered ``chineseCounting`` as Arabic — 第一条 became
  ``- 第1条`` — in 108 of 286 Chinese clause labels.
"""

from __future__ import annotations

import json
from pathlib import Path

import pytest

from docx_fixtures import (
    PRC_CONTRACT_NUMBERING,
    build_docx,
    dele,
    ins,
    numbering_xml,
    p,
    r,
)
from legal_helper.documents.docx_reader import read_docx
from legal_helper.tools import read_document


def _contract(tmp_path: Path) -> Path:
    body = (
        p("租赁合同", style="Title")
        + p("总则", num=(1, 0))
        + p("本合同双方为甲方与乙方。", num=(1, 1))
        + p("乙方应按月支付租金。", num=(1, 1))
        + p("租期", num=(1, 0))
        + p("", runs=r("租期为") + dele("三") + ins("五") + r("年，自交付之日起算。"))
        + p("租金", num=(1, 0))
        + p("每月租金人民币壹万元。")
    )
    return build_docx(
        tmp_path / "lease.docx",
        body,
        numbering=PRC_CONTRACT_NUMBERING,
        header=p("机密 — 仅供谈判使用"),
        footer=p("第 1 页"),
    )


def test_chinese_auto_numbering_renders_as_word_shows_it(tmp_path):
    text = read_document.call({"path": str(_contract(tmp_path))})
    assert "第一条 总则" in text
    assert "第二条 租期" in text
    assert "第三条 租金" in text
    assert "（一） 本合同双方" in text and "（二） 乙方应按月" in text
    # The converter's forms must not appear.
    assert "第1条" not in text
    assert "- 第" not in text


def test_tracked_changes_are_marked_not_accepted(tmp_path):
    text = read_document.call({"path": str(_contract(tmp_path))})
    assert "租期为{--三--}{++五++}年" in text
    assert "租期为五年" not in text  # the counterparty's markup is not agreed text
    first_line = text.splitlines()[0]
    assert first_line.startswith("[Tracked changes:")
    assert "1 tracked insertion and 1 deletion by Counterparty" in first_line


def test_multiple_authors_are_attributed_inline(tmp_path):
    body = p(
        "",
        runs=r("Fee: ")
        + dele("USD 100", author="Seller", rid=11)
        + ins("USD 90", author="Buyer", rid=12, date="2026-09-02T00:00:00Z"),
    )
    text = read_document.call({"path": str(build_docx(tmp_path / "two.docx", body))})
    assert "{--USD 100--}{++USD 90++}{>>Seller 2026-09-01; Buyer 2026-09-02<<}" in text
    assert "by Seller, Buyer" in text


def test_headers_and_footers_are_read_and_labelled(tmp_path):
    text = read_document.call({"path": str(_contract(tmp_path))})
    assert "[Header] 机密 — 仅供谈判使用" in text
    assert "[Footer] 第 1 页" in text


def test_footnotes_and_comments_keep_their_anchors(tmp_path):
    body = p(
        "",
        runs=r("See ")
        + '<w:commentRangeStart w:id="3"/>'
        + r("the deposit")
        + '<w:commentRangeEnd w:id="3"/><w:r><w:commentReference w:id="3"/></w:r>'
        + '<w:r><w:footnoteReference w:id="1"/></w:r>'
        + r(" clause."),
    )
    path = build_docx(
        tmp_path / "notes.docx",
        body,
        footnotes='<w:footnote w:id="1"><w:p><w:r><w:t>Deposit is refundable.</w:t></w:r></w:p></w:footnote>',
        comments='<w:comment w:id="3" w:author="Reviewer" w:date="2026-09-01T00:00:00Z"><w:p><w:r><w:t>Which deposit?</w:t></w:r></w:p></w:comment>',
    )
    text = read_document.call({"path": str(path)})
    assert "See the deposit[^1] clause." in text
    assert "[^1]: Deposit is refundable." in text
    assert "Comment 3 (Reviewer, 2026-09-01T00:00:00Z): Which deposit?" in text
    assert "Anchor/context: the deposit" in text


def test_numbering_formats_restarts_and_multilevel(tmp_path):
    numbering = numbering_xml(
        {
            20: [
                {"fmt": "ideographTraditional", "text": "%1、"},
                {"fmt": "decimalEnclosedCircle", "text": "%2"},
            ],
            21: [
                {"fmt": "upperRoman", "text": "Article %1"},
                {"fmt": "lowerLetter", "text": "(%2)"},
                {"fmt": "decimal", "text": "%1.%2.%3", "restart": 1},
                {"fmt": "decimal", "text": "%1.%2.%3.%4", "lgl": True},
            ],
            22: [{"fmt": "chineseLegalSimplified", "text": "第%1部分"}],
        },
        {5: 20, 6: 21, 7: 21, 8: 22},
        overrides={7: {0: 1}},
    )
    body = (
        p("甲项", num=(5, 0))
        + p("子项一", num=(5, 1))
        + p("子项二", num=(5, 1))
        + p("乙项", num=(5, 0))
        + p("子项", num=(5, 1))  # restarts under the new parent
        + p("Definitions", num=(6, 0))
        + p("Lessee", num=(6, 1))
        + p("Deep", num=(6, 2))
        + p("Legal", num=(6, 3))  # isLgl: every level decimal
        + p("Term", num=(6, 0))
        + p("Restarted list", num=(7, 0))  # startOverride: its own list from I
        + p("Part ten", num=(8, 0))
    )
    blocks = read_docx(build_docx(tmp_path / "fmt.docx", body, numbering=numbering)).body_paragraphs()
    labels = [b.label() for b in blocks]
    assert labels == [
        "甲、", "①", "②", "乙、", "①",
        # Each %n takes its own level's format (Word shows "I.a.1") unless
        # the level is isLgl ("legal numbering"), which forces decimal.
        "Article I", "(a)", "I.a.1", "1.1.1.1", "Article II",
        "Article I",
        "第壹部分",
    ]


def test_chinese_numerals_and_parsing_round_trip():
    from legal_helper.documents.clauses import chinese_numeral, format_number, parse_number

    cases = {1: "一", 10: "十", 11: "十一", 20: "二十", 101: "一百零一", 110: "一百一十", 1010: "一千零一十"}
    for value, text in cases.items():
        assert chinese_numeral(value) == text
        assert parse_number(text) == value
    assert chinese_numeral(12, upper=True) == "壹拾贰"
    assert format_number(3, "decimalEnclosedCircle") == "③"
    assert format_number(28, "upperLetter") == "BB"
    assert parse_number("③") == 3 and parse_number("XIV") == 14


def test_section_returns_one_clause_by_label_in_any_form(tmp_path):
    path = _contract(tmp_path)
    for query in ("第二条", "第2条", "租期"):
        text = read_document.call({"path": str(path), "section": query})
        assert text.startswith("[第二条 租期"), query
        assert "租期为{--三--}{++五++}年" in text
        assert "每月租金" not in text and "本合同双方" not in text
    missing = read_document.call({"path": str(path), "section": "第九条"})
    assert missing.startswith("ERROR") and "第一条 总则" in missing


def test_inspect_docx_index_is_the_index_reshape_docx_edits(tmp_path, monkeypatch):
    """D-4: inspect_docx numbered headings from 1 while reshape_docx indexes
    paragraphs from 0, so an index passed across edited the next paragraph."""
    from docx import Document

    from legal_helper.tools import inspect_docx, reshape_docx

    body = p("Intro", style="Heading1") + p("") + p("First clause body.") + p("Scope", style="Heading1") + p("Second clause body.")
    src = build_docx(tmp_path / "idx.docx", body)
    payload = json.loads(inspect_docx.call({"path": str(src)}))
    scope = next(e for e in payload["paragraphs"] if e["text"] == "Scope")
    assert scope["heading"] == 1 and scope["style"] == "Heading 1"  # the name set_paragraph_style takes
    assert scope["index"] == 3
    assert payload["paragraph_count"] == 5 and payload["empty_paragraph_count"] == 1
    out = json.loads(
        reshape_docx.call(
            {
                "source_path": str(src),
                "filename": "idx_out.docx",
                "operations": [{"op": "replace_paragraph", "index": scope["index"], "text": "Scope and Term"}],
            }
        )
    )
    texts = [para.text for para in Document(out["path"]).paragraphs]
    assert texts[3] == "Scope and Term"
    assert texts[4] == "Second clause body."


def test_legacy_doc_reads_through_libreoffice(tmp_path):
    """D-1: every chat that uploaded a .doc got binary garbage back. Read
    through LibreOffice, the labels and the tracked change survive too."""
    from legal_helper.documents.soffice import convert, soffice_binary

    if not soffice_binary():
        pytest.skip("LibreOffice not installed")
    doc = convert(_contract(tmp_path), tmp_path / "doc", "doc")
    text = read_document.call({"path": str(doc)})
    assert "\x00" not in text and "�" not in text
    assert "第一条 总则" in text
    assert "{--三--}" in text and "{++五++}" in text


def test_direct_formatting_survives_as_markdown(tmp_path):
    """anydoc kept bold/italic; a hand-marked strikethrough (no tracked change)
    is how many counterparties delete text, so it must not read as live text."""
    body = (
        p("", runs=r("Lessor", bold=True) + r(": Aurora Capital"))
        + p("", runs='<w:r><w:rPr><w:strike/></w:rPr><w:t>三年</w:t></w:r>' + r("五年"))
        + p("", runs='<w:r><w:rPr><w:i/></w:rPr><w:t xml:space="preserve">Note </w:t></w:r>' + r("well."))
    )
    text = read_document.call({"path": str(build_docx(tmp_path / "fmt.docx", body))})
    assert "**Lessor**: Aurora Capital" in text
    assert "~~三年~~五年" in text
    assert "*Note* well." in text


def test_consecutive_list_items_are_not_double_spaced(tmp_path):
    numbering = numbering_xml({30: [{"fmt": "bullet", "text": "•"}]}, {9: 30})
    body = p("Parties:") + p("Lessor", num=(9, 0)) + p("Lessee", num=(9, 0)) + p("Recitals follow.")
    text = read_document.call({"path": str(build_docx(tmp_path / "list.docx", body, numbering=numbering))})
    assert "Parties:\n\n- Lessor\n- Lessee\n\nRecitals follow." in text


def test_restarts_follow_word_and_libreoffice(tmp_path):
    """Lists sharing an abstractNum share counters; a startOverride resets the
    shared counter on first use and later items continue from it — checked
    against LibreOffice's own rendering of this fixture. lvlRestart=0 ("never
    restart") follows the spec, which LibreOffice 7.3 does not honour."""
    numbering = numbering_xml(
        {
            40: [{"fmt": "chineseCounting", "text": "第%1条"}, {"fmt": "decimal", "text": "%2."}],
            41: [{"fmt": "decimal", "text": "%1."}, {"fmt": "decimal", "text": "%1.%2", "restart": 0}],
            42: [{"fmt": "upperLetter", "text": "%1)", "start": 3}],
        },
        {1: 40, 2: 40, 3: 40, 4: 41, 5: 42, 6: 42},
        overrides={3: {0: 10}, 6: {0: 1}},
    )
    paras = [
        (1, 0), (1, 1), (2, 0), (2, 1), (3, 0), (3, 1), (1, 0),
        (4, 0), (4, 1), (4, 0), (4, 1),
        (5, 0), (5, 0), (6, 0), (5, 0),
    ]
    body = "".join(p(f"item {i}", num=n) for i, n in enumerate(paras))
    blocks = read_docx(build_docx(tmp_path / "restarts.docx", body, numbering=numbering)).body_paragraphs()
    assert [b.label() for b in blocks] == [
        "第一条", "1.", "第二条", "1.", "第十条", "1.", "第十一条",
        "1.", "1.1", "2.", "2.2",
        "C)", "D)", "A)", "B)",
    ]
