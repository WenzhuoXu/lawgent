"""国家法律法规数据库 source (cn.flk), offline: search, full-text passages, official text."""

from __future__ import annotations

import io

import pytest

from legal_helper.research.base import SourceUnavailable
from legal_helper.research.sources import cn


class _Response:
    def __init__(self, payload=None, content=b"", ctype="application/json"):
        self._payload = payload
        self.content = content
        self.headers = {"content-type": ctype}

    def json(self):
        return self._payload


def _docx_bytes(paragraphs: list[str]) -> bytes:
    import docx

    d = docx.Document()
    for p in paragraphs:
        d.add_paragraph(p)
    buf = io.BytesIO()
    d.save(buf)
    return buf.getvalue()


def _route(monkeypatch, handler):
    def fake(method, url, *, params=None, json_body=None, headers=None, **kwargs):
        return handler(method, url, params or {}, json_body or {})

    monkeypatch.setattr(cn, "request_with_retry", fake)


ROW = {
    "bbbs": "abc123", "title": "中华人民共和国<em class='highlight'>民用航空法</em>", "flxz": "法律",
    "zdjgName": "全国人民代表大会常务委员会", "gbrq": "2025-12-27", "sxrq": "2026-07-01", "sxx": 3, "score": "9.1",
}


def test_title_search_tries_exact_then_fuzzy(monkeypatch):
    calls = []

    def handler(method, url, params, body):
        calls.append(body)
        rows = [ROW] if body.get("searchType") == 2 else []
        return _Response({"code": 200, "rows": rows})

    _route(monkeypatch, handler)
    hits = cn.FlkSource().search("民用航空法", filters={"level": "法律", "status": "in_force"})
    assert [c["searchType"] for c in calls] == [1, 2]
    assert calls[0]["searchRange"] == 1 and calls[0]["sxx"] == [3]
    assert calls[0]["flfgCodeId"] == cn._LEVEL_CODES["law"]
    assert hits[0].ref == "cn.flk:abc123"
    assert hits[0].title == "中华人民共和国民用航空法"
    assert hits[0].status == "有效"


def test_fulltext_search_attaches_hit_passages(monkeypatch):
    def handler(method, url, params, body):
        if url.endswith("/hitDisplay"):
            return _Response({"code": 200, "data": {"contentHitDisplay": ["第一百二十九条　……<em class='highlight'>随身</em>……"]}})
        assert body["searchRange"] == 2
        return _Response({"code": 200, "rows": [ROW]})

    _route(monkeypatch, handler)
    hits = cn.FlkSource().search("随身携带物品", mode="fulltext")
    assert hits[0].snippet.startswith("第一百二十九条")
    assert "<em" not in hits[0].snippet


def test_fetch_reads_the_official_word_file(monkeypatch):
    docx = _docx_bytes(["中华人民共和国民用航空法", "第一条　为了维护国家的领空主权，制定本法。", "第一百二十九条　旅客非托运行李……"])

    def handler(method, url, params, body):
        if url.endswith("/flfgDetails"):
            return _Response({"code": 200, "data": {
                "title": "中华人民共和国民用航空法", "flxz": "法律", "gbrq": "2025-12-27", "sxrq": "2026-07-01", "sxx": 3,
                "lsyg": [{"bbbs": "abc123", "gbrq": "2025-12-27", "highLight": True},
                         {"bbbs": "old456", "gbrq": "2021-04-29", "highLight": False}],
            }})
        if url.endswith("/download/pc"):
            assert params["format"] == "docx"
            return _Response({"code": 200, "data": {"url": "https://flkoss.example/x.docx", "urlIn": "http://172.16.0.1/x"}})
        assert url == "https://flkoss.example/x.docx"
        return _Response(content=docx, ctype="application/vnd.openxmlformats-officedocument.wordprocessingml.document")

    _route(monkeypatch, handler)
    doc = cn.FlkSource().fetch("abc123")
    assert "第一百二十九条" in doc.text
    assert doc.metadata["status"] == "有效"
    assert [v["ref"] for v in doc.metadata["versions"]] == ["cn.flk:abc123", "cn.flk:old456"]
    assert doc.metadata["text_format"] == "docx"


def test_database_error_is_unavailable_not_empty(monkeypatch):
    _route(monkeypatch, lambda m, u, p, b: _Response({"code": 500, "msg": "内部错误"}))
    with pytest.raises(SourceUnavailable, match="500"):
        cn.FlkSource().search("民法典")
