"""RAG: chunker preserves article boundaries; retrieve returns empty for missing collections."""

from __future__ import annotations

from legal_helper.rag.chunker import _est_tokens, chunk_document


def test_chunker_returns_at_least_one_chunk() -> None:
    chunks = chunk_document("Hello world.")
    assert len(chunks) >= 1


def test_chunker_propagates_metadata() -> None:
    chunks = chunk_document("一些文本", metadata={"jurisdiction": "CN", "doc": "test"})
    for c in chunks:
        assert c.metadata.get("jurisdiction") == "CN"
        assert c.metadata.get("doc") == "test"


def test_chunker_handles_prc_article_boundaries() -> None:
    text = "\n\n".join(f"第{i}条 这是第{i}条的内容。" * 30 for i in range(1, 12))
    chunks = chunk_document(text)
    # text is large enough to force multiple chunks
    assert len(chunks) >= 2


def test_est_tokens_is_cjk_aware() -> None:
    # A CJK char is ~1 token; the old flat len//4 undercounted Chinese 4x.
    zh = "民" * 814
    assert 800 <= _est_tokens(zh) <= 830
    en = "a" * 814
    assert _est_tokens(en) == 814 // 4
    mixed = "民" * 100 + "a" * 100
    assert _est_tokens(mixed) == 100 + 25


def test_chunker_splits_oversized_cjk_body() -> None:
    # 1400 CJK chars ≈ 1400 tokens: under the old estimator this looked like
    # 350 "tokens" and shipped as one oversized chunk.
    text = "中华人民共和国民用航空法总则内容" * 88  # no article boundaries
    chunks = chunk_document(text)
    assert len(chunks) >= 2
    for c in chunks:
        assert _est_tokens(c.text) <= 600


def test_chunker_emits_article_labels() -> None:
    text = "\n\n".join(f"第{i}条 这是第{i}条的内容。" * 30 for i in range(1, 12))
    chunks = chunk_document(text)
    labels = [lab for c in chunks for lab in c.metadata.get("articles", [])]
    assert "第1条" in labels and "第11条" in labels
    assert chunks[0].metadata.get("article_label") == "第1条"


def test_chunker_emits_doc_context() -> None:
    text = "# 中华人民共和国民用航空法\n\n第一条 为了维护国家的领空主权。"
    chunks = chunk_document(text, metadata={"source_name": "caacl.md"})
    assert chunks[0].metadata.get("doc_title") == "中华人民共和国民用航空法"
    assert "caacl.md" in chunks[0].metadata.get("doc_context", "")


def test_retrieve_empty_for_unknown_collection() -> None:
    from legal_helper.rag.retrieve import retrieve

    hits = retrieve("test", collection="nonexistent_collection_xyz", k=3)
    assert hits == []
