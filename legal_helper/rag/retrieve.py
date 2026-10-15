"""Public retrieve entry point used by tools/rag_tools.py.

Pipeline: hybrid dense+sparse RRF search (see ``store``) over-retrieves a
3-4x candidate pool, then a local cross-encoder (see ``rerank``) reorders
the pool and the top ``k`` survive. Everything runs locally, and the models
are released when the search finishes (``models_held``).
"""

from __future__ import annotations

import gc
import sys
import threading
from contextlib import contextmanager
from dataclasses import dataclass
from pathlib import Path
from typing import Any, Iterator

from .embeddings import release_default_embedder
from .rerank import release_reranker, rerank_enabled, rerank_order
from .store import QdrantStore, default_store_path

_OVERFETCH = 4  # candidate pool = k * _OVERFETCH, capped
_POOL_CAP = 32

# Searches currently inside ``models_held``; the last one out releases.
_HOLDERS = 0
_HOLDERS_LOCK = threading.Lock()


@contextmanager
def models_held() -> Iterator[None]:
    """Keep the embedder and reranker loaded only while a search runs.

    Both are 568M-parameter fp32 models that sentence-transformers puts on
    the GPU when there is one: kept between searches, they held ~6 GiB of a
    long-running server's GPU memory while it sat idle. Scopes nest and
    overlap: the last one to exit releases, so a tool searching several
    collections loads once and one chat's search never unloads a model
    another chat is using. The price is a reload per search, ~5 s for both
    models (measured 2026-10-12, RTX PRO 6000, weights in page cache).
    """
    global _HOLDERS
    with _HOLDERS_LOCK:
        _HOLDERS += 1
    try:
        yield
    finally:
        with _HOLDERS_LOCK:
            _HOLDERS -= 1
            if _HOLDERS == 0:
                _release_models()


def _release_models() -> None:
    dropped = release_default_embedder()
    dropped = release_reranker() or dropped
    if not dropped:
        return
    # A loaded model holds reference cycles: without a collection its
    # weights stay allocated and empty_cache returns only the activations
    # (measured: 5.0 GiB still held without it, 0.7 GiB with it).
    gc.collect()
    torch = sys.modules.get("torch")
    if torch is not None:
        torch.cuda.empty_cache()


@dataclass
class RetrievedChunk:
    text: str
    score: float
    metadata: dict[str, Any]


# One client per resolved path: the embedded Qdrant backend takes an
# exclusive file lock, and a single-slot cache would silently ignore a
# differing ``path`` argument once any store existed.
_STORES: dict[Path, QdrantStore] = {}


def _store(path: Path | None = None) -> QdrantStore:
    resolved = (path or default_store_path()).resolve()
    if resolved not in _STORES:
        _STORES[resolved] = QdrantStore(path=resolved)
    return _STORES[resolved]


def retrieve(
    query: str,
    collection: str,
    k: int = 6,
    filters: dict[str, Any] | None = None,
    store_path: Path | None = None,
    rerank: bool | None = None,
) -> list[RetrievedChunk]:
    """Top-k retrieval over a collection. Returns empty if the collection
    has not been ingested yet.

    ``rerank=None`` follows the ``LEGAL_HELPER_RAG_RERANK`` env default
    (on); pass ``False`` to skip the cross-encoder for latency-critical
    callers. The models are released on return unless the caller holds
    them across several calls with ``models_held``."""
    use_rerank = rerank_enabled() if rerank is None else rerank
    pool_k = min(k * _OVERFETCH, _POOL_CAP) if use_rerank else k
    with models_held():
        hits = _store(store_path).search(collection, query, k=pool_k, filters=filters)
        if use_rerank and len(hits) > 1:
            out = []
            for i, score in rerank_order(query, [h.text for h in hits], k):
                h = hits[i]
                # Cross-encoder relevance replaces the RRF rank score so pooled
                # multi-collection results stay sortable; fallback keeps the
                # store score.
                out.append(
                    RetrievedChunk(
                        text=h.text,
                        score=h.score if score is None else score,
                        metadata=h.metadata,
                    )
                )
            return out
    hits = hits[:k]
    return [RetrievedChunk(text=h.text, score=h.score, metadata=h.metadata) for h in hits]
