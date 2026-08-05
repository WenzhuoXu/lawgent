from __future__ import annotations

import json

from legal_helper.citations.grounding import ground_answer


_ANSWER = (
    'The statute provides that "no person may operate an aircraft" without a '
    "certificate ([FAA](https://example.gov/14cfr91)). The court also said "
    '"safety is paramount" in its ruling ([Case](https://example.gov/case1)). '
    'A third source claims "this exact phrase is invented" '
    "([Bad](https://example.gov/bad)). And an uncited quote: "
    '"floating quotation with no link".'
)

_SOURCES = {
    "https://example.gov/14cfr91": "Sec 91.3 ... no person may operate an aircraft without a certificate ...",
    "https://example.gov/case1": "The tribunal emphasized that safety is paramount in all operations.",
    "https://example.gov/bad": "This page is about something else entirely; the phrase is absent.",
}


def _fetch(url: str) -> dict:
    if url in _SOURCES:
        return {"body_text": _SOURCES[url], "extractor": "text", "url": url}
    return {"body_text": "", "error": "404"}


def test_ground_answer_classifies_quotes():
    report = ground_answer(_ANSWER, fetch=_fetch)
    by_quote = {v["quote"]: v["status"] for v in report["verdicts"]}

    assert by_quote["no person may operate an aircraft"] == "exact"
    assert by_quote["safety is paramount"] == "exact"
    assert by_quote["this exact phrase is invented"] == "not_found"
    assert by_quote["floating quotation with no link"] == "no_citation"

    assert report["summary"]["exact"] == 2
    assert report["summary"]["not_found"] == 1
    assert report["summary"]["no_citation"] == 1
    # grounding score = grounded / checkable = 2 / 3
    assert abs(report["grounding_score"] - (2 / 3)) < 1e-6


def test_ground_answer_handles_unfetchable_source():
    answer = 'It states "phrase here" ([X](https://example.gov/missing)).'
    report = ground_answer(answer, fetch=_fetch)
    statuses = [v["status"] for v in report["verdicts"]]
    assert statuses == ["could_not_check"]
    assert report["grounding_score"] is None  # nothing checkable


def test_ground_answer_tool_is_json():
    from legal_helper.tools.citations import ground_answer_tool

    # No network: empty/triviai text → valid JSON, no quotes.
    out = json.loads(ground_answer_tool.call({"text": "No quotes here, just prose."}))
    assert out["quote_count"] == 0
    assert out["grounding_score"] is None


# ---- elided quotes (……) -----------------------------------------------------

_MFD_1087 = (
    "第一千零八十七条 离婚时，夫妻的共同财产由双方协议处理；协议不成的，"
    "由人民法院根据财产的具体情况，按照照顾子女、女方和无过错方权益的原则判决。"
)


def test_roundtrip_elided_quote_exact():
    from legal_helper.citations.groundedness import roundtrip_quote

    quote = "离婚时，夫妻的共同财产由双方协议处理……按照照顾子女、女方和无过错方权益的原则判决。"
    result = roundtrip_quote(quote, _MFD_1087)
    assert result.status == "exact"
    assert "elided" in (result.reason or "")


def test_roundtrip_elided_quote_near_modulo_punct():
    from legal_helper.citations.groundedness import roundtrip_quote

    # segment punctuation differs from the source → near, not not_found
    quote = "离婚时夫妻的共同财产由双方协议处理……按照照顾子女、女方和无过错方权益的原则判决"
    result = roundtrip_quote(quote, _MFD_1087)
    assert result.status in {"exact", "near"}


def test_roundtrip_elided_quote_out_of_order_not_found():
    from legal_helper.citations.groundedness import roundtrip_quote

    quote = "按照照顾子女、女方和无过错方权益的原则判决……离婚时，夫妻的共同财产由双方协议处理"
    result = roundtrip_quote(quote, _MFD_1087)
    assert result.status == "not_found"


def test_roundtrip_ascii_ellipsis_also_elides():
    from legal_helper.citations.groundedness import roundtrip_quote

    source = (
        "No person shall be held to answer for a capital, or otherwise infamous "
        "crime, unless on a presentment or indictment of a grand jury."
    )
    quote = "No person shall be held to answer ... unless on a presentment or indictment of a grand jury."
    result = roundtrip_quote(quote, source)
    assert result.status in {"exact", "near"}


# ---- PRC-grounded round-trip (PKULaw-style refs, no URL) --------------------

_CN_ANSWER = (
    "关于离婚财产分割："
    "“离婚时，夫妻的共同财产由双方协议处理……按照照顾子女、女方和无过错方权益的原则判决。”"
    "（《民法典》第一千零八十七条）\n\n"
    "## 资料来源\n"
    "- 《民法典》第一千零八十七条 — 婚姻家庭编\n"
)


def _cn_fetch(ref: str) -> dict:
    assert not ref.startswith("http"), f"web fetch attempted for {ref!r}"
    if "第一千零八十七条" in ref:
        return {
            "body_text": _MFD_1087,
            "extractor": "pkulaw_fatiao",
            "connector": "pkulaw_fatiao",
            "in_force_status": "in_force",
        }
    return {"body_text": "", "error": "not found"}


def test_ground_answer_roundtrips_prc_statute_ref():
    report = ground_answer(_CN_ANSWER, fetch=_cn_fetch)
    assert report["citation_count"] >= 1
    statuses = [v["status"] for v in report["verdicts"]]
    assert statuses == ["exact"]
    assert report["grounding_score"] == 1.0
    # the quote was checked against the statute ref, not a URL
    assert "《民法典》" in report["verdicts"][0]["url"]


def test_ground_answer_trust_tiers():
    report = ground_answer(_CN_ANSWER, fetch=_cn_fetch)
    trust = {t["ref"]: t for t in report["trust"]}
    tag = trust["《民法典》第一千零八十七条"]
    assert tag["tier"] == "connector_verified"
    assert tag["connector"] == "pkulaw_fatiao"
    assert tag["tag"] == "[connector-verified: PKULaw]"


def test_ground_answer_unfetched_ref_stays_model_only():
    # Cite present but no quote anchored to it → never fetched → "[verify]".
    text = "结论依据《公司法》第二十条。\n\n## 资料来源\n- 《公司法》第二十条\n"
    report = ground_answer(text, fetch=_cn_fetch)
    trust = {t["ref"]: t for t in report["trust"]}
    tag = trust["《公司法》第二十条"]
    assert tag["tier"] == "model_only"
    assert tag["tag"] == "[verify]"


def test_ground_answer_temporality_flag():
    def stale_fetch(ref: str) -> dict:
        return {
            "body_text": _MFD_1087,
            "connector": "pkulaw_fatiao",
            "in_force_status": "revised",
        }

    report = ground_answer(_CN_ANSWER, fetch=stale_fetch)
    assert report["temporality_flags"]
    flag = report["temporality_flags"][0]
    assert flag["status"] == "revised"
    assert "《民法典》" in flag["ref"]


# ---- prc_aware_fetch dispatch (offline paths only) --------------------------


def test_parse_statute_ref():
    from legal_helper.citations.grounding import _parse_statute_ref

    assert _parse_statute_ref("《民法典》第一千零八十七条") == ("民法典", "第一千零八十七条")
    assert _parse_statute_ref("《中华人民共和国公司法（2023修订）》第二十条") == (
        "中华人民共和国公司法（2023修订）",
        "第二十条",
    )
    assert _parse_statute_ref("（2023）京01民终12345号") is None


def test_prc_aware_fetch_rejects_unroutable_ref_without_network():
    from legal_helper.citations.grounding import prc_aware_fetch

    out = prc_aware_fetch("（2023）京01民终12345号")
    assert out["body_text"] == ""
    assert "pkulaw_case_search" in out["error"]


def test_in_force_status_mapping():
    from legal_helper.citations.grounding import _in_force_status

    assert _in_force_status("已废止") == "repealed"
    assert _in_force_status("已修改") == "revised"
    assert _in_force_status("现行有效") == "in_force"
    assert _in_force_status("尚未生效") == "not_yet_effective"
    assert _in_force_status(None) is None


# ---- Sources-section trust annotation ---------------------------------------


def test_annotate_sources_with_trust():
    from legal_helper.citations.report import TrustTag, annotate_sources_with_trust

    text = (
        "正文……\n\n## 资料来源\n"
        "- 《民法典》第一千零八十七条 — 婚姻家庭编\n"
        "- 《公司法》第二十条 — 法人人格否认\n"
        "- 《刑法》第二百三十三条 [PKULaw]\n"
    )
    tags = [
        TrustTag("《民法典》第一千零八十七条", "connector_verified", "pkulaw_fatiao"),
        TrustTag("《公司法》第二十条", "model_only"),
        TrustTag("《刑法》第二百三十三条", "model_only"),
    ]
    out = annotate_sources_with_trust(text, tags)
    lines = out.splitlines()
    assert any("第一千零八十七条" in l and "[connector-verified: PKULaw]" in l for l in lines)
    assert any("《公司法》" in l and l.rstrip().endswith("[verify]") for l in lines)
    # an entry that already carries a provenance tag is left untouched
    assert any(l.rstrip().endswith("[PKULaw]") for l in lines)
    # the body above the Sources section is unchanged
    assert out.startswith("正文……")


def test_connector_verified_tag_recognized_as_provenance():
    from legal_helper.citations.provenance import inspect_tag

    assert inspect_tag("connector-verified: PKULaw").kind == "retrieval"


def test_ground_answer_tool_reports_trust():
    from legal_helper.tools.citations import ground_answer_tool

    # No quotes → nothing fetched → offline; cite still gets a model-only tier.
    out = json.loads(
        ground_answer_tool.call(
            {
                "text": "依据《民法典》第一千零八十七条。\n\n## 资料来源\n- 《民法典》第一千零八十七条\n",
                "annotate_sources": True,
            }
        )
    )
    assert out["quote_count"] == 0
    tiers = {t["ref"]: t["tier"] for t in out["trust"]}
    assert tiers["《民法典》第一千零八十七条"] == "model_only"
    assert "[verify]" in out["annotated_text"]
