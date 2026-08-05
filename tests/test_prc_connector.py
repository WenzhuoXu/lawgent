from __future__ import annotations

import json

import httpx

from legal_helper.connectors import prc


class _Response:
    def __init__(self, payload):
        self._payload = payload

    def raise_for_status(self):
        return None

    def json(self):
        return self._payload


def _patch_flk(monkeypatch, handler):
    """Route prc.request_with_retry into a handler(method, url, params, json_body)."""

    def fake(method, url, *, params=None, json_body=None, headers=None, **kwargs):
        return handler(method, url, params, json_body)

    monkeypatch.setattr(prc, "request_with_retry", fake)


def test_flk_npc_search_uses_current_law_search_api(monkeypatch):
    calls = []

    def handler(method, url, params, body):
        calls.append((url, body))
        return _Response(
            {
                "code": 200,
                "rows": [
                    {
                        "bbbs": "ff808081729d1efe01729d50b5c500bf",
                        "title": "中华人民共和国<em class='highlight'>民法典</em>",
                        "flxz": "法律",
                        "zdjgName": "全国人民代表大会",
                        "gbrq": "2020-05-28",
                        "sxrq": "2021-01-01",
                        "sxx": 3,
                    }
                ],
            }
        )

    _patch_flk(monkeypatch, handler)

    raw = prc.flk_npc_search.call({"query": "民法典", "level": "law", "per_page": 3})
    payload = json.loads(raw)

    assert calls[0][0] == "https://flk.npc.gov.cn/law-search/search/list"
    assert calls[0][1]["searchContent"] == "民法典"
    assert calls[0][1]["searchRange"] == 1
    assert calls[0][1]["searchType"] == 1
    assert calls[0][1]["pageSize"] == 3
    assert calls[0][1]["flfgCodeId"]
    assert payload["count"] == 1
    assert payload["level_filter_applied"] is True
    assert payload["results"][0]["doc_id"] == "ff808081729d1efe01729d50b5c500bf"
    assert payload["results"][0]["title"] == "中华人民共和国民法典"
    assert payload["results"][0]["status"] == "有效"


def test_flk_npc_search_falls_back_to_fuzzy_when_exact_empty(monkeypatch):
    calls = []

    def handler(method, url, params, body):
        calls.append(body["searchType"])
        rows = [] if body["searchType"] == 1 else [{"bbbs": "doc", "title": "结果"}]
        return _Response({"code": 200, "rows": rows})

    _patch_flk(monkeypatch, handler)

    payload = json.loads(prc.flk_npc_search.call({"query": "个人信息", "per_page": 1}))

    assert calls == [1, 2]
    assert payload["count"] == 1
    assert payload["results"][0]["title"] == "结果"


def test_flk_npc_search_rule_level_never_silently_widens(monkeypatch):
    """flk.npc.gov.cn has no 部门规章 bucket (verified against the live
    enumData endpoint) — level='rule' must say so instead of quietly
    searching all levels as if the filter applied."""
    sent_codes = []

    def handler(method, url, params, body):
        sent_codes.append(body["flfgCodeId"])
        return _Response({"code": 200, "rows": [{"bbbs": "x", "title": "某法"}]})

    _patch_flk(monkeypatch, handler)

    payload = json.loads(prc.flk_npc_search.call({"query": "一般规定", "level": "rule"}))

    assert sent_codes[0] == []
    assert payload["level_filter_applied"] is False
    assert "pkulaw_law_search" in payload["note"]


def test_flk_npc_search_supported_levels_map_to_enumdata_codes(monkeypatch):
    seen = {}

    def handler(method, url, params, body):
        seen["codes"] = body["flfgCodeId"]
        return _Response({"code": 200, "rows": []})

    _patch_flk(monkeypatch, handler)

    prc.flk_npc_search.call({"query": "监察", "level": "supervision_regulation"})
    assert seen["codes"] == [220]

    prc.flk_npc_search.call({"query": "条例", "level": "local_regulation"})
    assert 230 in seen["codes"] and 290 in seen["codes"]


def test_flk_npc_search_error_contract(monkeypatch):
    def handler(method, url, params, body):
        raise httpx.ConnectError("boom")

    _patch_flk(monkeypatch, handler)

    payload = json.loads(prc.flk_npc_search.call({"query": "民法典"}))
    assert "error" in payload
    assert payload["query"] == "民法典"


def test_flk_npc_fetch_returns_outline_and_history(monkeypatch):
    def handler(method, url, params, body):
        assert url == "https://flk.npc.gov.cn/law-search/search/flfgDetails"
        assert params == {"bbbs": "abc123"}
        return _Response(
            {
                "code": 200,
                "data": {
                    "bbbs": "abc123",
                    "title": "中华人民共和国民用航空法",
                    "flxz": "法律",
                    "zdjgName": "全国人民代表大会常务委员会",
                    "gbrq": "2025-12-27",
                    "sxrq": "2026-07-01",
                    "sxx": 3,
                    "lsyg": [
                        {"bbbs": "abc123", "title": "中华人民共和国民用航空法",
                         "gbrq": "2025-12-27", "highLight": True},
                        {"bbbs": "old456", "title": "中华人民共和国民用航空法",
                         "gbrq": "2021-04-29", "highLight": False},
                    ],
                    "xgzl": [{"title": "中华人民共和国主席令（第六十五号）", "busiType": "主席令"}],
                },
            }
        )

    _patch_flk(monkeypatch, handler)

    payload = json.loads(prc.flk_npc_fetch.call({"doc_id": "abc123"}))
    assert payload["status"] == "有效"
    assert payload["effective_date"] == "2026-07-01"
    assert len(payload["history"]) == 2
    assert payload["history"][0]["is_this_version"] is True
    assert payload["related_instruments"][0]["type"] == "主席令"
    # flk never serves body text publicly — the fetch is an outline companion.
    assert payload["body_text"] == ""
    assert "pkulaw_fatiao" in payload["note"]


def test_flk_npc_fetch_error_contract(monkeypatch):
    _patch_flk(monkeypatch, lambda m, u, p, b: _Response({"code": 500, "msg": "内部错误"}))
    payload = json.loads(prc.flk_npc_fetch.call({"doc_id": "abc123"}))
    assert "error" in payload
    assert payload["msg"] == "内部错误"

    payload = json.loads(prc.flk_npc_fetch.call({"doc_id": ""}))
    assert "error" in payload
