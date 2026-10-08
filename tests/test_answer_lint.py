"""Deterministic answer clean-up, applied as a stream filter (review Y-6)."""

from __future__ import annotations

from legal_helper.answer_lint import AnswerLinter, lint_answer, resolve_local_link, strip_tracking


def test_tracking_parameters_are_stripped_from_links_and_bare_urls():
    text = (
        "见 [民法典](https://flk.npc.gov.cn/detail?id=1&utm_source=openai) 及 "
        "https://example.com/a?utm_medium=x&q=1 。\n"
    )
    out, counts = lint_answer(text)
    assert "utm_" not in out
    assert "id=1" in out and "q=1" in out
    assert counts.tracking_params == 2


def test_repeated_citation_chips_collapse_within_a_paragraph():
    chip = "([example.com](https://example.com/rule?utm_source=openai))"
    text = f"第一点{chip}。第二点{chip}。第三点{chip}。\n\n新段落{chip}。\n"
    out, counts = lint_answer(text)
    first, second = out.split("\n\n", 1)
    assert first.count("example.com/rule") == 1
    assert second.count("example.com/rule") == 1
    assert counts.repeated_chips == 2


def test_upload_and_artifact_links_resolve_to_served_routes():
    upload = "/storage/x/outputs/chat_uploads/abc123/u9f/合同 v2.docx"
    assert resolve_local_link(upload.replace(" ", "_")) == (
        "/api/chats/abc123/uploads/u9f/%E5%90%88%E5%90%8C_v2.docx"
    )
    out, counts = lint_answer(
        "见[合同](/storage/x/outputs/chat_uploads/abc123/u9f/contract.docx)与"
        "[报告](/srv/outputs/chat_artifacts/abc123/memo.docx)。\n"
    )
    assert "(/api/chats/abc123/uploads/u9f/contract.docx)" in out
    assert "(/api/artifacts/chat_artifacts/abc123/memo.docx)" in out
    assert counts.upload_links_resolved == 2


def test_unserved_local_paths_become_plain_names():
    out, counts = lint_answer("See [the lease](/home/user/secret/lease.pdf).\n")
    assert out == "See the lease.\n"
    assert counts.local_links_unlinked == 1


def test_english_token_in_chinese_answer_is_localised():
    out, counts = lint_answer("1. 某规定第5条 — pinpoint unavailable\n", language="zh")
    assert "无法获得具体条款" in out and "pinpoint unavailable" not in out
    assert counts.token_leaks == 1
    # An English answer keeps it.
    same, _ = lint_answer("1. Rule 5 — pinpoint unavailable\n", language="en")
    assert "pinpoint unavailable" in same


def test_stream_filter_never_splits_a_url():
    linter = AnswerLinter()
    pieces = ["See https://exa", "mple.com/p?utm_so", "urce=openai&k=v for", " details.\nNext"]
    emitted = "".join(linter.feed(p) for p in pieces) + linter.flush()
    assert emitted == "See https://example.com/p?k=v for details.\nNext"


def test_sources_sections_are_counted():
    linter = AnswerLinter(language="zh")
    linter.lint("正文\n## 资料来源\n1. 甲 — 已在线核验\n## 资料来源\n1. 乙\n")
    assert linter.counts.sources_sections == 2
    assert linter.counts.any()


def test_strip_tracking_is_idempotent():
    url = "https://a.example/x?b=2"
    assert strip_tracking(url) == url


def test_hosted_search_citation_ids_are_not_links():
    out, counts = lint_answer("1. [《民事诉讼法》第一百七十一条](turn2view0) — 已在线核验\n", language="zh")
    assert out == "1. 《民事诉讼法》第一百七十一条 — 已在线核验\n"
    assert counts.local_links_unlinked == 1


def test_links_without_an_address_become_plain_citations():
    out, _ = lint_answer(
        "见[《航班正常管理规定》第3条]()与[承运人运输总条件第7.2.1条](用户提供)，另见[官网](www.caac.gov.cn)和[§2](#s2)。\n",
        language="zh",
    )
    assert out == (
        "见《航班正常管理规定》第3条与承运人运输总条件第7.2.1条，另见[官网](https://www.caac.gov.cn)和[§2](#s2)。\n"
    )


def test_nested_deliverables_link_by_name_and_working_files_are_unlinked():
    out, counts = lint_answer(
        "[拆分](/srv/outputs/chat_artifacts/abc123/split_judgment/p1.pdf) "
        "[预览](/srv/outputs/chat_artifacts/abc123/_renders/pages_memo/pages-1.jpg)\n"
    )
    assert out == "[拆分](/api/artifacts/chat_artifacts/abc123/p1.pdf) 预览\n"
    assert counts.upload_links_resolved == 1 and counts.local_links_unlinked == 1


def test_sources_lines_get_the_link_on_the_name_and_the_status_last():
    """Hosted search puts its citation after the status and a URL may arrive
    bare; the interface links the name and reads the status after the last
    dash. "——" with no spaces is a separator too."""
    text = (
        "## 资料来源\n"
        "1. 用户提供《租赁合同.docx》——用户提供\n"
        "2. 《劳动合同法》，第17、19—26条 — 已在线核验 ([samr.gov.cn](https://www.samr.gov.cn/a.html))\n"
        "3. 《解释（一）》第36条 — 已在线核验 https://www.court.gov.cn/x.html\n"
        "4. [《劳动法》第36条]() — 未核验\n"
        "5. 《劳动合同法》第24条 — 已在线核验 ([samr.gov.cn](https://www.samr.gov.cn/a.html))\n"
    )
    out, counts = lint_answer(text, language="zh")
    assert out.splitlines()[1:] == [
        "1. 用户提供《租赁合同.docx》 — 用户提供",
        "2. [《劳动合同法》，第17、19—26条](https://www.samr.gov.cn/a.html) — 已在线核验",
        "3. [《解释（一）》第36条](https://www.court.gov.cn/x.html) — 已在线核验",
        "4. 《劳动法》第36条 — 未核验",
        # A second source on the same page keeps its link: chips are not
        # collapsed inside the sources list.
        "5. [《劳动合同法》第24条](https://www.samr.gov.cn/a.html) — 已在线核验",
    ]
    assert counts.sources_lines_normalized == 4
    again, _ = lint_answer(out, language="zh")
    assert again == out


def test_a_linked_source_drops_the_duplicate_chip_and_body_lines_are_untouched():
    text = (
        "正文一句。([a.gov.cn](https://a.gov.cn/x))\n"
        "## Sources\n"
        "1. [Wegener, C-537/17, paras 17-25](https://curia.europa.eu/w) — verified online "
        "([curia.europa.eu](https://curia.europa.eu/w))\n"
        "2. Smith v Jones, 12 F.4th 1 - pinpoint unavailable\n"
    )
    out, _ = lint_answer(text)
    lines = out.splitlines()
    assert lines[0] == "正文一句。([a.gov.cn](https://a.gov.cn/x))"
    assert lines[2] == "1. [Wegener, C-537/17, paras 17-25](https://curia.europa.eu/w) — verified online"
    assert lines[3] == "2. Smith v Jones, 12 F.4th 1 — pinpoint unavailable"


def test_a_status_with_a_qualifier_still_splits_from_the_name():
    out, _ = lint_answer(
        "## 资料来源\n1. [上海金融法院（2025）沪74民终140号民事裁定 — 参考案例；已在线核验，非指导性案例。](https://a.cn/x)\n",
        language="zh",
    )
    assert out.splitlines()[1] == (
        "1. [上海金融法院（2025）沪74民终140号民事裁定](https://a.cn/x) — 参考案例；已在线核验，非指导性案例"
    )
