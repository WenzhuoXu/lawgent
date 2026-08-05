from __future__ import annotations

import json

import yaml


def _seed_corpus(tmp_path):
    corpus = tmp_path / "caac_ccar"
    (corpus / "documents" / "ccar").mkdir(parents=True)
    (corpus / "documents" / "laws").mkdir(parents=True)

    # Live CCAR rule with full body.
    (corpus / "documents" / "ccar" / "sample.md").write_text(
        """# 民用航空安全管理规定

- Source URL: https://www.caac.gov.cn/XXGK/XXGK/MHGZ/sample.html
- CAAC category: 民航规章
- ccar_part: CCAR-398-R1

## 正文

第一条 为了规范民用航空安全管理，制定本规定。
第二条 本规定适用于民用航空生产经营单位的安全管理。
""",
        encoding="utf-8",
    )

    # Statute revision: stub body (currently 有效 per manifest) — to be
    # superseded by the new full-text revision below.
    (corpus / "documents" / "laws" / "stub.md").write_text(
        """# 中华人民共和国民用航空法（2021年版）

- Source URL: https://www.caac.gov.cn/XXGK/XXGK/FLFG/stub.html
- CAAC category: 法律法规

## 正文

附件：
中华人民共和国民用航空法.pdf
""",
        encoding="utf-8",
    )

    # New full-text revision of the same statute family (future_effective).
    (corpus / "documents" / "laws" / "full.md").write_text(
        """# 中华人民共和国民用航空法（2026年7月1日起施行）

- Source URL: https://www.caac.gov.cn/XXGK/XXGK/FLFG/full.html
- CAAC category: 法律法规

## 正文

第一百二十四条 旅客托运的行李毁灭、遗失或者损坏的，承运人应当承担责任。
""",
        encoding="utf-8",
    )

    manifest = corpus / "manifest.yaml"
    manifest.write_text(
        yaml.safe_dump(
            {
                "files": [
                    {
                        "file": "documents/ccar/sample.md",
                        "source_url": "https://www.caac.gov.cn/XXGK/XXGK/MHGZ/sample.html",
                        "jurisdiction": "CN",
                        "language": "zh",
                        "corpus_category": "ccar",
                        "legal_category": "民航规章",
                        "title": "民用航空安全管理规定",
                        "cite_prefix": "CCAR-398-R1",
                        "ccar_part": "CCAR-398-R1",
                        "validity": "有效",
                        "publish_date": "2025-11-12",
                        "file_kind": "source_page",
                    },
                    {
                        "file": "documents/laws/stub.md",
                        "source_url": "https://www.caac.gov.cn/XXGK/XXGK/FLFG/stub.html",
                        "jurisdiction": "CN",
                        "language": "zh",
                        "corpus_category": "laws",
                        "legal_category": "法律法规",
                        "title": "中华人民共和国民用航空法（2021年版）",
                        "cite_prefix": "中华人民共和国民用航空法（2021年版）",
                        "validity": "有效",
                        "publish_date": "2021-08-13",
                        "file_kind": "source_page",
                    },
                    {
                        "file": "documents/laws/full.md",
                        "source_url": "https://www.caac.gov.cn/XXGK/XXGK/FLFG/full.html",
                        "jurisdiction": "CN",
                        "language": "zh",
                        "corpus_category": "laws",
                        "legal_category": "法律法规",
                        "title": "中华人民共和国民用航空法（2026年7月1日起施行）",
                        "cite_prefix": "中华人民共和国民用航空法（2026年7月1日起施行）",
                        "publish_date": "2025-12-27",
                        "file_kind": "source_page",
                    },
                ]
            },
            allow_unicode=True,
        ),
        encoding="utf-8",
    )
    return corpus, manifest


def test_caac_local_search_and_fetch_from_staged_corpus(tmp_path, monkeypatch):
    from legal_helper.connectors import caac_local as mod

    corpus, manifest = _seed_corpus(tmp_path)
    monkeypatch.setattr(mod, "CORPUS_ROOT", corpus)
    monkeypatch.setattr(mod, "MANIFEST_PATH", manifest)

    raw = mod.caac_local_search.call(
        {"query": "安全管理", "ccar_part": "CCAR-398-R1", "article": "第二条"}
    )
    payload = json.loads(raw)
    assert payload["count"] == 1
    result = payload["results"][0]
    assert result["ccar_part"] == "CCAR-398-R1"
    assert "第二条" in result["article_text"]
    assert result["is_stub"] is False

    raw = mod.caac_local_fetch.call({"identifier": "CCAR-398-R1", "article": "第一条"})
    payload = json.loads(raw)
    assert payload["title"] == "民用航空安全管理规定"
    assert "第一条" in payload["article_text"]
    assert payload["is_stub"] is False


def test_caac_local_fetch_prefers_non_stub_when_title_has_a_stub_alternate(
    tmp_path, monkeypatch
):
    from legal_helper.connectors import caac_local as mod

    corpus, manifest = _seed_corpus(tmp_path)
    monkeypatch.setattr(mod, "CORPUS_ROOT", corpus)
    monkeypatch.setattr(mod, "MANIFEST_PATH", manifest)

    raw = mod.caac_local_fetch.call({"identifier": "中华人民共和国民用航空法"})
    payload = json.loads(raw)

    # Two entries share the title family — picked the non-stub one.
    assert payload["match_count"] == 2
    assert payload["file"] == "documents/laws/full.md"
    assert payload["is_stub"] is False
    assert payload["body_chars"] > 0
    # The stub alternate is surfaced with status info so the caller can see it.
    stub_alt = next(a for a in payload["alternates"] if a["is_stub"])
    assert stub_alt["file"] == "documents/laws/stub.md"
    assert "pdf_stub" in stub_alt["flags"]


def test_caac_local_fetch_routes_to_revision_that_contains_article(
    tmp_path, monkeypatch
):
    from legal_helper.connectors import caac_local as mod

    corpus, manifest = _seed_corpus(tmp_path)
    monkeypatch.setattr(mod, "CORPUS_ROOT", corpus)
    monkeypatch.setattr(mod, "MANIFEST_PATH", manifest)

    raw = mod.caac_local_fetch.call(
        {"identifier": "中华人民共和国民用航空法", "article": "第一百二十四条"}
    )
    payload = json.loads(raw)
    assert payload["file"] == "documents/laws/full.md"
    assert "旅客托运的行李" in payload["article_text"]


def test_caac_local_search_can_drop_pdf_stubs(tmp_path, monkeypatch):
    from legal_helper.connectors import caac_local as mod

    corpus, manifest = _seed_corpus(tmp_path)
    monkeypatch.setattr(mod, "CORPUS_ROOT", corpus)
    monkeypatch.setattr(mod, "MANIFEST_PATH", manifest)

    with_stubs = json.loads(
        mod.caac_local_search.call({"query": "民用航空法", "include_stubs": True})
    )
    without_stubs = json.loads(
        mod.caac_local_search.call({"query": "民用航空法", "include_stubs": False})
    )
    files_with = {r["file"] for r in with_stubs["results"]}
    files_without = {r["file"] for r in without_stubs["results"]}
    assert "documents/laws/stub.md" in files_with
    assert "documents/laws/stub.md" not in files_without
    assert "documents/laws/full.md" in files_without
