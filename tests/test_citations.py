"""Citations package: audit_citations preserved + extract/validate work for US + PRC."""

from __future__ import annotations

import json

from legal_helper.citations import (
    audit_citations,
    extract_citations,
    validate_citations,
)
from legal_helper.tools.citations import extract_citations_tool, validate_citations_tool


def test_audit_citations_preserved() -> None:
    result = audit_citations(
        "body of memo\n\n## Sources\n- Statute A — article 5 — http://example"
    )
    assert result.ok


def test_audit_citations_flags_missing_section() -> None:
    result = audit_citations("body with no sources section")
    assert not result.ok


def test_extract_prc_case_number() -> None:
    text = "本案案号 (2023)京01民终12345号 已生效。"
    cites = extract_citations(text)
    assert any(c.jurisdiction == "CN" and c.kind == "prc_case_number" for c in cites)


def test_extract_prc_statute_pinpoint() -> None:
    text = "依据《刑法》第233条 处罚。"
    cites = extract_citations(text)
    assert any(c.kind == "prc_statute_pinpoint" for c in cites)


def test_extract_us_citation() -> None:
    text = "See 410 U.S. 113 (1973)."
    cites = extract_citations(text)
    # eyecite returns at least one US citation
    assert any(c.jurisdiction == "US" for c in cites)


def test_validate_returns_results() -> None:
    text = "See (2023)京01民终12345号 and 410 U.S. 113 (1973)."
    cites = extract_citations(text)
    results = validate_citations(cites)
    assert len(results) == len(cites)


def test_extract_tool_returns_json() -> None:
    payload = extract_citations_tool.call({"text": "本案案号 (2023)京01民终12345号"})
    data = json.loads(payload)
    assert data["count"] >= 1


def test_validate_tool_returns_json() -> None:
    payload = validate_citations_tool.call({"text": "本案案号 (2023)京01民终12345号"})
    data = json.loads(payload)
    assert "results" in data


# ---- canonical GB/T 9704 full-width typography ------------------------------


def _kinds(text: str) -> dict[str, str]:
    return {c.kind: c.text for c in extract_citations(text)}


def test_extract_fullwidth_case_number() -> None:
    kinds = _kinds("本案案号（2023）京01民终12345号已生效。")
    assert kinds.get("prc_case_number") == "（2023）京01民终12345号"


def test_extract_fashi_corner_brackets() -> None:
    # 〔〕 is NOT folded by NFKC — the pattern must match it literally.
    kinds = _kinds("依据法释〔2024〕5号的规定。")
    assert "法释〔2024〕5号" in kinds.get("prc_judicial_interpretation", "")


def test_extract_statute_pinpoint_qian_numeral() -> None:
    kinds = _kinds("《民法典》第一千零八十七条规定离婚财产分割。")
    assert kinds.get("prc_statute_pinpoint") == "《民法典》第一千零八十七条"


def test_extract_statute_pinpoint_whitespace_tolerant() -> None:
    kinds = _kinds("依据《民法典》 第一千零八十七条。")
    assert "第一千零八十七条" in kinds.get("prc_statute_pinpoint", "")


def test_extract_statute_title_with_revision_suffix() -> None:
    kinds = _kinds("《中华人民共和国公司法（2023修订）》第二十条。")
    assert "第二十条" in kinds.get("prc_statute_pinpoint", "")


def test_extract_state_council_corner_brackets() -> None:
    kinds = _kinds("按照国办发〔2023〕12号执行。")
    assert kinds.get("prc_state_council") == "国办发〔2023〕12号"


def test_validate_official_case_type_taxonomy() -> None:
    # 破 (破产) is an official 代字 (法〔2015〕287号) — must NOT flag as malformed.
    # Structural validation cannot confirm authority offline, so a well-formed
    # 案号 lands on could_not_check (never a silent "verified"); grounding.py's
    # PKULaw round-trip is what promotes it to verified.
    cites = extract_citations("（2021）粤03破123号")
    results = validate_citations(cites)
    assert results and all(r.status == "could_not_check" for r in results)
    assert all(not r.ok for r in results)


def test_validate_unknown_case_marker_could_not_check() -> None:
    # Unknown marker must downgrade to could_not_check — never silently pass.
    cites = extract_citations("（2023）京01彳12345号")
    results = [r for r in validate_citations(cites) if r.citation.kind == "prc_case_number"]
    assert results
    for r in results:
        assert r.status == "could_not_check"
        assert not r.ok


def test_validate_prc_structural_pass_never_verified() -> None:
    # A structurally valid but fabricated PRC statute pinpoint (民法典 has ~1260
    # articles; 第五千条 does not exist) must NOT read as verified offline — that
    # was the exact P0 false-positive. Structural pass = could_not_check.
    cites = extract_citations("依据《民法典》第五千条。")
    results = [r for r in validate_citations(cites) if r.citation.kind == "prc_statute_pinpoint"]
    assert results
    for r in results:
        assert r.status == "could_not_check"
        assert not r.ok


def test_validate_us_statute_not_auto_verified() -> None:
    # FullLawCitation/FullJournalCitation have no offline check — a fabricated
    # statute cite must not come back "verified".
    cites = extract_citations("See 42 U.S.C. § 1983 and 123 Harv. L. Rev. 456.")
    statuses = {
        type(r.citation.raw).__name__: r.status
        for r in validate_citations(cites)
        if r.citation.jurisdiction == "US"
    }
    assert statuses.get("FullLawCitation") == "could_not_check"
    assert statuses.get("FullJournalCitation") == "could_not_check"


def test_validate_anaphoric_id_cite_still_passes() -> None:
    cites = extract_citations("See 410 U.S. 113 (1973). Id. at 116.")
    by_kind = {r.citation.kind: r for r in validate_citations(cites)}
    assert by_kind["IdCitation"].status == "verified"


def test_validate_tool_not_ok_on_could_not_check() -> None:
    payload = validate_citations_tool.call({"text": "See 42 U.S.C. § 1983."})
    data = json.loads(payload)
    assert data["ok"] is False


def test_audit_accepts_cjk_pinpoint() -> None:
    result = audit_citations(
        "备忘录正文\n\n## 资料来源\n- 《民法典》第一千零八十七条 — PKULaw\n"
        "- 法释〔2020〕12号 第三条 — PKULaw"
    )
    assert result.ok, result.warnings


def test_audit_still_flags_pinpointless_cjk_line() -> None:
    result = audit_citations("正文\n\n## 资料来源\n- 《民法典》 — PKULaw")
    assert not result.ok


def test_audit_explicit_unavailable_escape_preserved() -> None:
    result = audit_citations("正文\n\n## 资料来源\n- 某规范性文件 — 无法获得具体条款")
    assert result.ok
