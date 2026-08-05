"""Public retrieve entry point used by tools/rag_tools.py.

Pipeline: hybrid dense+sparse RRF search (see ``store``) over-retrieves a
3-4x candidate pool, then a local cross-encoder (see ``rerank``) reorders
the pool and the top ``k`` survive. Everything runs locally.
"""

from __future__ import annotations

from dataclasses import dataclass
from pathlib import Path
from typing import Any

from .rerank import rerank_enabled, rerank_order
from .store import QdrantStore, default_store_path

_OVERFETCH = 4  # candidate pool = k * _OVERFETCH, capped
_POOL_CAP = 32


@dataclass
class RetrievedChunk:
    text: str
    score: float
    metadata: dict[str, Any]


# One client per resolved path: the embedded Qdrant backend takes an
# exclusive file lock, and the old single-slot cache silently ignored a
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
    callers."""
    use_rerank = rerank_enabled() if rerank is None else rerank
    pool_k = min(k * _OVERFETCH, _POOL_CAP) if use_rerank else k
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
