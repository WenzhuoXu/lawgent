"""One clause index for outline, section reads, extract_clauses and redline anchors.

Review item D-7: the outline was empty for 36 of 52 real uploads and found 0
of 120 clauses in a synthetic PRC contract (0 of 40 in an English one),
because the patterns expected a bare ``第X条`` / ``N Title`` while converters
emit ``- 第1条`` and ``1. Heading``; ``extract_clauses`` returned a single
PREAMBLE for every contract shape tried.
"""

from __future__ import annotations

import json

from docx_fixtures import PRC_CONTRACT_NUMBERING, build_docx, p
from legal_helper.documents.clauses import clause_index_from_text, detect_label, find_clauses
from legal_helper.tools import extract_clauses

_ZH_CONVERTED = "\n".join(
    ["# 租赁合同", ""]
    + [f"- 第{i}条 条款{i}\n\n      - （1） 内容。\n" for i in range(1, 41)]
)
_EN = "\n".join(
    ["MASTER SERVICES AGREEMENT", "This Agreement is made between A and B.", ""]
    + [f"{i}. Heading {i}\n{i}.1 The Supplier shall perform obligation {i}.\n" for i in range(1, 21)]
)


def test_converter_forms_are_clause_starts():
    for line, family, number in [
        ("- 第1条 总则", "cn_tiao", "1"),
        ("## 第三条 租金", "cn_tiao", "3"),
        ("1. Definitions", "dec1", "1"),
        ("1.1 The Lessee shall", "dec2", "1.1"),
        ("一、基本情况", "cn_dun", "1"),
        ("（二）争议焦点", "cn_paren", "2"),
        ("ARTICLE IV — INSURANCE", "article", "4"),
        ("第十二条之一 新增", "cn_tiao", "12-1"),
    ]:
        match = detect_label(line)
        assert match is not None and (match.family, match.number) == (family, number), line
    for prose in ("1.5 million dollars are due", "3 days later", "2026年10月7日", "第一条 总则 ........ 3"):
        assert detect_label(prose) is None, prose


def test_outline_finds_every_clause_in_converted_text():
    from legal_helper.attachments import _outline_entries
    from legal_helper.rag.chunker import _ARTICLE_RES

    _, clauses = clause_index_from_text(_ZH_CONVERTED)
    assert sum(1 for c in clauses if c.family == "cn_tiao") == 40
    entries = _outline_entries(_ZH_CONVERTED)
    assert "第1条 条款1" in entries and "第40条 条款40" in entries
    # The RAG chunker's own boundaries accept the same forms.
    starts = {m.group(1) for pat in _ARTICLE_RES for m in pat.finditer(_ZH_CONVERTED)}
    assert {"第1条", "第40条"} <= starts
    en_starts = {m.group(1) for pat in _ARTICLE_RES for m in pat.finditer(_EN)}
    assert {"1", "20", "1.1"} <= en_starts


def test_label_forms_resolve_to_the_same_clause():
    text = "第一条 总则\n甲方。\n第二条 租期\n三年。\n第三条 租金\n一万元。"
    _, clauses = clause_index_from_text(text)
    for query in ("第三条", "第3条", "3", "租金"):
        hit = find_clauses(clauses, query)[0]
        assert hit.label == "第三条", query


def test_extract_clauses_on_english_text():
    clauses = json.loads(extract_clauses.call({"text": _EN}))
    assert clauses[0]["heading"] == "PREAMBLE"
    tops = [c for c in clauses if c["label"] and c["level"] == 0]
    assert len(tops) == 20 and tops[0]["heading"] == "1. Heading 1"
    subs = [c for c in clauses if c["label"] == "1.1"]
    assert subs and subs[0]["parent"] == "1. Heading 1"
    assert "obligation 1" in subs[0]["body"] or "obligation 1" in subs[0]["title"]


def test_extract_clauses_on_chinese_text_tags_chinese_categories():
    text = "第一条 保密\n双方应对商业秘密保密。\n第二条 争议解决\n提交上海国际仲裁中心仲裁。\n第三条 不可抗力\n因不可抗力不能履行的，免除责任。"
    clauses = json.loads(extract_clauses.call({"text": text}))
    by_label = {c["label"]: c for c in clauses}
    assert set(by_label) == {"第一条", "第二条", "第三条"}
    assert "confidentiality" in by_label["第一条"]["categories"]
    assert "dispute_resolution" in by_label["第二条"]["categories"]
    assert "force_majeure" in by_label["第三条"]["categories"]


def test_extract_clauses_on_a_docx_uses_word_numbering(tmp_path):
    body = (
        p("租赁合同", style="Title")
        + p("总则", num=(1, 0))
        + p("本合同双方为甲方与乙方。", num=(1, 1))
        + p("租金", num=(1, 0))
        + p("租金按月支付，逾期支付的，按日加收违约金。")
    )
    path = build_docx(tmp_path / "lease.docx", body, numbering=PRC_CONTRACT_NUMBERING)
    clauses = json.loads(extract_clauses.call({"path": str(path)}))
    labels = [c["label"] for c in clauses]
    assert "第一条" in labels and "第二条" in labels and "（一）" in labels
    rent = next(c for c in clauses if c["label"] == "第二条")
    assert rent["title"] == "租金" and rent["paragraph"] == 3
    assert "违约金" in rent["body"] and "payment_terms" in rent["categories"]


def test_extract_clauses_reports_unreadable_files(tmp_path):
    blob = tmp_path / "x.bin"
    blob.write_bytes(b"\x00\x01" * 100)
    assert "unsupported binary format" in json.loads(extract_clauses.call({"path": str(blob)}))["error"]
