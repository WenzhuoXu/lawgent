"""Local cross-encoder reranking for the retrieve pipeline.

Model choice: ``BAAI/bge-reranker-v2-m3`` — the cross-encoder companion to
our bge-m3 embedder. It is the smallest reranker with strong zh+en quality
(568M params, ~2.3 GB weights on first download, then fully local), which
matters because the primary corpora are PRC/CAAC texts. It loads via
``sentence-transformers`` (already a dependency), lazily, off the module
import path — skills that never rerank pay nothing. Retrieval stays local:
no provider API is ever in this path (CLAUDE.md contract).

Controls:
- ``LEGAL_HELPER_RAG_RERANK=0`` disables reranking globally;
- ``LEGAL_HELPER_RAG_RERANKER=<model>`` swaps the model (e.g. to evaluate
  ``Qwen3-Reranker-0.6B`` against the golden set before making it default);
- ``set_reranker(...)`` injects a custom/stub implementation (tests).
"""

from __future__ import annotations

import os
import sys
from typing import Protocol, Sequence

_DEFAULT_MODEL = "BAAI/bge-reranker-v2-m3"


class Reranker(Protocol):
    name: str

    def score(self, query: str, texts: Sequence[str]) -> list[float]:
        ...


class CrossEncoderReranker:
    """Lazily-loaded sentence-transformers CrossEncoder."""

    def __init__(self, model_name: str | None = None) -> None:
        self.name = model_name or os.getenv("LEGAL_HELPER_RAG_RERANKER", _DEFAULT_MODEL)
        self._model = None

    def _load(self) -> None:
        if self._model is not None:
            return
        from sentence_transformers import CrossEncoder

        self._model = CrossEncoder(self.name)

    def score(self, query: str, texts: Sequence[str]) -> list[float]:
        if not texts:
            return []
        self._load()
        assert self._model is not None
        out = self._model.predict([(query, t) for t in texts])
        return out.tolist() if hasattr(out, "tolist") else [float(v) for v in out]


_RERANKER: Reranker | None = None


def rerank_enabled() -> bool:
    return os.getenv("LEGAL_HELPER_RAG_RERANK", "1").lower() not in ("0", "false", "no")


def get_reranker() -> Reranker:
    global _RERANKER
    if _RERANKER is None:
        _RERANKER = CrossEncoderReranker()
    return _RERANKER


def set_reranker(reranker: Reranker | None) -> None:
    """Inject a reranker (or ``None`` to restore the lazy default)."""
    global _RERANKER
    _RERANKER = reranker


def rerank_order(query: str, texts: Sequence[str], k: int) -> list[tuple[int, float | None]]:
    """``(index, score)`` pairs of ``texts`` in reranked order, truncated
    to ``k``. Scores are cross-encoder relevance — comparable across calls,
    so callers that pool candidates from several collections can sort on
    them safely (RRF ranks are not comparable that way).

    Degrades gracefully: any failure (weights unavailable offline, OOM, …)
    returns the incoming order with ``None`` scores so retrieval keeps
    working without the quality boost rather than erroring the tool call.
    """
    if not texts:
        return []
    try:
        scores = get_reranker().score(query, texts)
        order = sorted(range(len(texts)), key=lambda i: scores[i], reverse=True)
        return [(i, float(scores[i])) for i in order[:k]]
    except Exception as exc:  # noqa: BLE001 — reranking is best-effort
        print(f"rag.rerank: falling back to fusion order ({exc!s})", file=sys.stderr)
        return [(i, None) for i in range(min(len(texts), k))]
