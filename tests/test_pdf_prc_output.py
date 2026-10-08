"""PRC-ready PDF output, and the Markdown front end it now shares with Word.

Before: Letter for every document, an English disclaimer box on page one, and
Python-Markdown printing a two-space nested list as literal ``- item`` text.

Each CJK render costs seconds (WeasyPrint subsets the CJK CFF fonts), so the
documents are rendered once per module and the tests read them.
"""

from __future__ import annotations

import pypdfium2 as pdfium
import pytest

from legal_helper.documents.quality import markdown_residue
from legal_helper.documents.writers import write_pdf_document

ZH_NOTICE = "本文件由法律 AI 助手生成，不构成法律意见。"
ZH_BODY = (
    "**结论：**本交易可行[^1]。参见[《民法典》](https://flk.npc.gov.cn/)。~~删除~~ 第一行<br>第二行\n\n"
    "#### 四级标题\n\n"
    "- 第一项\n  - 子项甲\n  - 子项乙\n- 第二项\n\n"
    "3. 第三项请求\n4. 第四项请求\n\n"
    "| 事项 | 依据 |\n|---|---|\n| 许可 | 第九十二条 |\n\n"
    "[^1]: 《民法典》第一百四十三条。"
)


def _read(path):
    doc = pdfium.PdfDocument(str(path))
    try:
        return (
            [page.get_textpage().get_text_range() for page in doc],
            [page.get_size() for page in doc],
            doc.get_metadata_dict(),
        )
    finally:
        doc.close()


@pytest.fixture(scope="module")
def renders(tmp_path_factory):
    root = tmp_path_factory.mktemp("pdf")
    secret = root / "secret.txt"
    secret.write_text("TOP-SECRET-CONTENT", encoding="utf-8")
    out = {}
    out["zh"] = root / "zh.pdf"
    write_pdf_document(output_path=out["zh"], title="法律意见书", body_markdown=ZH_BODY, disclaimer=ZH_NOTICE)
    out["en"] = root / "en.pdf"
    write_pdf_document(
        output_path=out["en"],
        title="Lease Review",
        body_markdown=f'All conditions are met. <img src="file://{secret}"> ![chart]({secret})',
        image_roots=[root / "outputs"],
    )
    out["gb"] = root / "gb.pdf"
    write_pdf_document(
        output_path=out["gb"],
        title="关于某事项的请示",
        body_markdown="\n\n".join(["填充段落" * 40] * 12),
        layout="gbt9704",
    )
    return {key: _read(path) for key, path in out.items()}


def test_a_chinese_pdf_is_a4_with_a_chinese_page_number(renders):
    texts, sizes, _ = renders["zh"]
    assert [round(v) for v in sizes[0]] == [595, 842]
    assert "第1页共1页" in "".join(texts[0].split())


def test_an_english_pdf_is_letter_with_page_x_of_y(renders):
    texts, sizes, _ = renders["en"]
    assert [round(v) for v in sizes[0]] == [612, 792]
    assert "Page 1 of 1" in texts[0]


def test_the_disclaimer_is_pdf_metadata_not_page_content(renders):
    texts, _, meta = renders["zh"]
    assert meta.get("Subject") == ZH_NOTICE
    assert "不构成法律意见" not in "".join(texts)


def test_a_two_space_nested_list_is_nested_not_literal(renders):
    text = "".join(renders["zh"][0])
    assert "子项甲" in text
    assert "- 子项甲" not in text


def test_markdown_constructs_render_instead_of_printing(renders):
    text = "".join(renders["zh"][0])
    assert "第一百四十三条" in text  # the footnote is printed at the page foot
    assert markdown_residue(text) == [], markdown_residue(text)


def test_an_ordered_list_keeps_its_start_number(renders):
    text = renders["zh"][0][0]
    assert "3." in text and "4." in text


def test_unsafe_html_and_outside_images_never_load(renders):
    text = "".join(renders["en"][0])
    assert "TOP-SECRET-CONTENT" not in text
    assert "<img" in text  # printed as the text it is
    assert "chart" in text  # an image outside the roots is its alt text


def test_gbt9704_pdf_has_dashed_page_numbers_on_a4(renders):
    texts, sizes, _ = renders["gb"]
    assert len(texts) >= 2
    assert [round(v) for v in sizes[0]] == [595, 842]
    for number, text in enumerate(texts[:2], 1):
        assert f"—{number}—" in "".join(text.split())


def test_gbt9704_pdf_sets_28_characters_a_line(renders):
    lines = [
        line.strip() for line in renders["gb"][0][0].splitlines()
        if line.strip() and set(line.strip()) <= set("填充段落")
    ]
    assert len(lines) >= 3
    # Lines after a paragraph's first (which carries the 2-character indent)
    # hold the full GB/T 9704 measure.
    assert 28 in {len(line) for line in lines}
    assert max(len(line) for line in lines) == 28


def test_letter_is_refused_for_gbt9704(tmp_path):
    with pytest.raises(ValueError):
        write_pdf_document(
            output_path=tmp_path / "x.pdf", title="请示", body_markdown="正文",
            layout="gbt9704", page_size="Letter",
        )
