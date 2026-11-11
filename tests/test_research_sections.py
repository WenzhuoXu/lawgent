"""The research layer's provision index: labels in every language resolve."""

from legal_helper.research.sections import detect_sections, find_section, outline, request_key


def test_request_keys_reduce_to_the_heading_key():
    cases = {
        "第128条": "128",
        "第一百二十八条第二款": "128",
        "第一百二十条之一": "120.1",
        "第3条の2": "3.2",
        "제3조의2": "3.2",
        "Article 9(1)(a)": "9",
        "art. 9": "9",
        "s 58(4)": "58",
        "Section 58": "58",
        "§ 254.4": "254.4",
        "Điều 5": "5",
        "Pasal 12": "12",
        "มาตรา ๗": "7",
        "Article L. 121-1": "L121-1",
        "Artikel 7:17": "7:17",
        "[45]": "45",
        "para 45": "45",
        "58A": "58A",
    }
    for request, key in cases.items():
        assert request_key(request) == key, request


def test_cn_statute_sections_and_outline():
    text = "\n".join([
        "中华人民共和国示例法",
        "第一章　总　　则",
        "第一条　为了规范示例，制定本法。",
        "第二条　本法适用于示例。",
        "第二章　责任",
        "第一节　承运人责任",
        "第一百二十八条　因发生在民用航空器上的事故，承运人应当承担责任。",
        "前款规定不适用于健康原因。",
        "第一百二十八条之一　补充规定。",
    ])
    secs = detect_sections(text)
    arts = [s for s in secs if s.level == "article"]
    assert [s.key for s in arts] == ["1", "2", "128", "128.1"]
    hit = find_section(secs, "第128条第二款")[0]
    assert "前款规定" in text[hit.start:hit.end]
    lines = outline(secs, text)
    assert lines[0].startswith("[第一章") and "第一条–第二条" in lines[0]


def test_in_sentence_reference_is_not_a_heading():
    text = "Article 9\nRight to care\n1. Passengers shall be offered meals.\nArticle 37 of the Constitution applies.\nArticle 10\nUpgrading"
    keys = [s.key for s in detect_sections(text) if s.level == "article"]
    assert keys == ["9", "10"]


def test_judgment_paragraphs():
    text = "JUDGMENT\n1 | This request concerns Article 2.\nArticle 7 of that regulation provides:\n2 | The dispute arose."
    secs = detect_sections(text, kind="case")
    assert [s.label for s in secs] == ["[1]", "[2]"]
    assert "Article 7 of that regulation" in text[secs[0].start:secs[0].end]
