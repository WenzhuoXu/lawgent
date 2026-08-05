"""Hybrid RAG: sparse+dense RRF, rerank hook, safe re-ingest, store anchoring.

Every test runs against a per-test temp Qdrant path (never the live
``state/qdrant`` — a running server may hold its lock) and a deterministic
hash embedder, so nothing here downloads or loads model weights.
"""

from __future__ import annotations

import hashlib
from pathlib import Path

import pytest

from legal_helper.rag import rerank as rerank_mod
from legal_helper.rag.embeddings import LexicalSparseEncoder
from legal_helper.rag.store import QdrantStore, default_store_path


class FakeEmbedder:
    """Deterministic 1-hot hash embedding — intentionally lexically blind,
    so any exact-term recall in these tests is attributable to the sparse
    leg, not embedding luck."""

    name = "fake-hash-v1"
    dim = 8

    def encode(self, texts: list[str]) -> list[list[float]]:
        out = []
        for t in texts:
            vec = [0.0] * self.dim
            vec[hashlib.sha256(t.encode()).digest()[0] % self.dim] = 1.0
            out.append(vec)
        return out


class KeywordReranker:
    name = "fake-keyword-reranker"

    def __init__(self, keyword: str) -> None:
        self.keyword = keyword

    def score(self, query: str, texts) -> list[float]:
        return [1.0 if self.keyword in t else 0.0 for t in texts]


@pytest.fixture()
def store(tmp_path: Path) -> QdrantStore:
    return QdrantStore(path=tmp_path / "qdrant", embedder=FakeEmbedder())


_STATUTES = [
    ("第10条 国务院民用航空主管部门对全国民用航空活动实施统一监督管理。", {"jurisdiction": "CN"}),
    ("第44条 民用航空器优先权,是指债权人依照本法规定,向民用航空器所有人提出赔偿请求。", {"jurisdiction": "CN"}),
    ("第75条 民用航空器应当按照空中交通管制单位指定的航路和飞行高度飞行。", {"jurisdiction": "CN"}),
    ("General provisions on civil aviation administration apply nationwide.", {"jurisdiction": "US"}),
    ("Operators shall comply with air traffic control route clearances.", {"jurisdiction": "US"}),
]


def _seed(store: QdrantStore, collection: str = "statutes") -> None:
    store.upsert_chunks(
        collection,
        [(text, {"source_path": f"/corpus/{i}.md", **meta}) for i, (text, meta) in enumerate(_STATUTES)],
    )


def test_sparse_encoder_tokenizes_cjk_and_terms() -> None:
    enc = LexicalSparseEncoder()
    toks = enc.tokenize("第44条 CCAR-121部")
    assert "44" in toks and "第" in toks and "条" in toks
    assert "ccar-121" in toks
    assert "民用" in enc.tokenize("民用航空")  # CJK bigrams
    # Deterministic output.
    assert enc.encode(["第44条"]) == enc.encode(["第44条"])


def test_hybrid_exact_article_lookup(store: QdrantStore) -> None:
    _seed(store)
    hits = store.search("statutes", "第44条", k=2)
    assert hits, "hybrid search returned nothing"
    assert any("第44条" in h.text for h in hits), (
        "exact article number must be retrievable via the sparse leg even "
        "with a lexically-blind dense embedder"
    )


def test_hybrid_search_respects_filters(store: QdrantStore) -> None:
    _seed(store)
    hits = store.search("statutes", "aviation administration", k=5, filters={"jurisdiction": "US"})
    assert hits
    assert all(h.metadata.get("jurisdiction") == "US" for h in hits)


def test_collection_meta_persisted_and_embedder_mismatch_rejected(tmp_path: Path) -> None:
    path = tmp_path / "qdrant"
    store = QdrantStore(path=path, embedder=FakeEmbedder())
    _seed(store)
    meta = store._read_meta()["statutes"]
    assert meta["embedder"] == "fake-hash-v1" and meta["dim"] == 8

    store._client.close()

    class OtherEmbedder(FakeEmbedder):
        name = "other-embedder"

    other = QdrantStore(path=path, embedder=OtherEmbedder())
    with pytest.raises(ValueError, match="fake-hash-v1"):
        other.search("statutes", "第44条", k=2)


def test_delete_by_source_and_drop(store: QdrantStore) -> None:
    _seed(store)
    total = store._client.count("statutes").count
    store.delete_by_source("statutes", "/corpus/1.md")
    assert store._client.count("statutes").count == total - 1
    store.drop_collection("statutes")
    assert "statutes" not in store.collections()
    assert "statutes" not in store._read_meta()


def test_default_store_path_is_anchored_to_state_dir(tmp_path: Path) -> None:
    # conftest points LEGAL_HELPER_STATE_DIR at a tmp dir; the store must
    # follow settings.state_dir, never Path.cwd().
    from legal_helper.config import load_settings

    assert default_store_path() == load_settings().state_dir / "qdrant"
    assert default_store_path() != Path.cwd() / "state" / "qdrant"


def test_safe_reingest_leaves_no_orphans(tmp_path: Path, monkeypatch) -> None:
    from legal_helper.rag import ingest as ingest_mod

    store = QdrantStore(path=tmp_path / "qdrant", embedder=FakeEmbedder())
    doc = tmp_path / "law.md"
    doc.write_text(
        "# 测试法\n\n" + "\n\n".join(f"第{i}条 原始条文内容。" * 30 for i in range(1, 6)),
        encoding="utf-8",
    )
    ingest_mod.ingest_paths([doc], "reingest", store=store)
    first = store._client.count("reingest").count
    assert first > 0

    doc.write_text("# 测试法\n\n第1条 修订后的条文内容。", encoding="utf-8")
    ingest_mod.ingest_paths([doc], "reingest", store=store)
    second = store._client.count("reingest").count
    assert second < first, "re-ingesting a shrunk file must drop stale chunks"
    hits = store.search("reingest", "原始条文", k=5)
    assert all("原始" not in h.text for h in hits), "superseded text must not co-retrieve"


def test_ingest_cli_rebuild_and_source(tmp_path: Path, monkeypatch, capsys) -> None:
    from legal_helper.rag import ingest as ingest_mod

    store = QdrantStore(path=tmp_path / "qdrant", embedder=FakeEmbedder())
    monkeypatch.setattr(ingest_mod, "QdrantStore", lambda: store)
    doc = tmp_path / "a.md"
    doc.write_text("第1条 内容。", encoding="utf-8")

    assert ingest_mod.main(["--collection", "cli", "--path", str(doc)]) == 0
    assert ingest_mod.main(["--collection", "cli", "--path", str(doc), "--rebuild"]) == 0
    out = capsys.readouterr().out
    assert "Dropped collection=cli" in out

    # --source pulls the seed set of another collection yaml (the CLAUDE.md
    # ``--collection aviation --source caac_ccar`` invocation shape).
    assert ingest_mod.main(["--collection", "cli", "--source", "general"]) == 0
    assert ingest_mod.main(["--collection", "cli", "--source", "no_such_yaml"]) == 1


def test_retrieve_rerank_uses_injected_reranker(tmp_path: Path, monkeypatch) -> None:
    import importlib

    # ``legal_helper.rag.retrieve`` the module — the package re-exports the
    # ``retrieve`` function under the same name.
    retrieve_mod = importlib.import_module("legal_helper.rag.retrieve")

    monkeypatch.setattr("legal_helper.rag.store.default_embedder", FakeEmbedder)
    path = tmp_path / "qdrant"
    store = retrieve_mod._store(path)
    _seed(store)

    rerank_mod.set_reranker(KeywordReranker("空中交通"))
    try:
        hits = retrieve_mod.retrieve("航路", collection="statutes", k=3, store_path=path, rerank=True)
        assert hits and "空中交通" in hits[0].text
        assert hits[0].score == 1.0  # cross-encoder score replaces the RRF rank
    finally:
        rerank_mod.set_reranker(None)


def test_retrieve_rerank_env_kill_switch(monkeypatch) -> None:
    monkeypatch.setenv("LEGAL_HELPER_RAG_RERANK", "0")
    assert rerank_mod.rerank_enabled() is False
    monkeypatch.setenv("LEGAL_HELPER_RAG_RERANK", "1")
    assert rerank_mod.rerank_enabled() is True


def test_rerank_order_degrades_gracefully() -> None:
    class BrokenReranker:
        name = "broken"

        def score(self, query, texts):
            raise RuntimeError("weights unavailable")

    rerank_mod.set_reranker(BrokenReranker())
    try:
        order = rerank_mod.rerank_order("q", ["a", "b", "c"], k=2)
        assert order == [(0, None), (1, None)]
    finally:
        rerank_mod.set_reranker(None)


def test_recall_at_3_exact_article_golden_set(store: QdrantStore) -> None:
    """Miniature recall@k gate: every exact-term query must recall its
    article in the top 3 — the regime dense-only retrieval failed in."""
    _seed(store)
    golden = {"第10条": "第10条", "第44条": "第44条", "第75条": "第75条"}
    for query, expected in golden.items():
        hits = store.search("statutes", query, k=3)
        assert any(expected in h.text for h in hits), f"recall@3 miss for {query}"
