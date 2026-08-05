"""Qdrant (embedded) wrapper. File-backed under ``<state_dir>/qdrant/``.

Hybrid schema (``hybrid-v1``): named dense vector ``dense`` (bge-m3) +
named sparse vector ``sparse`` (lexical BM25-style, IDF applied by Qdrant)
fused at query time via the Query API's RRF. Collections created before
this schema (single unnamed dense vector) are still searchable — the store
detects them and falls back to dense-only search.
"""

from __future__ import annotations

import json
import uuid
from dataclasses import dataclass
from pathlib import Path
from typing import Any

from .embeddings import (
    Embedder,
    LexicalSparseEncoder,
    default_embedder,
    default_sparse_encoder,
)

_META_FILENAME = "collections_meta.json"
_SCHEMA_HYBRID = "hybrid-v1"
_SCHEMA_LEGACY = "legacy-dense"


def default_store_path() -> Path:
    """Anchor to the configured ``state_dir`` (not ``Path.cwd()``), so the
    store resolves identically no matter where the process was launched."""
    from ..config import load_settings

    return load_settings().state_dir / "qdrant"


@dataclass
class StoredChunk:
    id: str
    text: str
    score: float
    metadata: dict[str, Any]


class QdrantStore:
    """Thin wrapper over qdrant-client's local persistent client."""

    def __init__(
        self,
        path: Path | None = None,
        embedder: Embedder | None = None,
        sparse_encoder: LexicalSparseEncoder | None = None,
    ) -> None:
        from qdrant_client import QdrantClient

        if path is None:
            path = default_store_path()
        path.mkdir(parents=True, exist_ok=True)
        self._path = path
        self._client = QdrantClient(path=str(path))
        self._embedder = embedder or default_embedder()
        self._sparse = sparse_encoder or default_sparse_encoder()
        self._dim = self._embedder.dim

    # -- per-collection meta (embedder identity + schema) -------------------

    def _meta_path(self) -> Path:
        return self._path / _META_FILENAME

    def _read_meta(self) -> dict[str, dict[str, Any]]:
        try:
            return json.loads(self._meta_path().read_text(encoding="utf-8"))
        except (OSError, ValueError):
            return {}

    def _write_meta(self, meta: dict[str, dict[str, Any]]) -> None:
        self._meta_path().write_text(
            json.dumps(meta, ensure_ascii=False, indent=1), encoding="utf-8"
        )

    def _check_embedder(self, name: str) -> None:
        """Refuse to mix embedding spaces: querying vectors produced by a
        different model returns silently wrong results, not errors."""
        entry = self._read_meta().get(name)
        if not entry:
            return
        if entry.get("embedder") != self._embedder.name or entry.get("dim") != self._dim:
            raise ValueError(
                f"collection {name!r} was ingested with embedder "
                f"{entry.get('embedder')!r} (dim={entry.get('dim')}); current "
                f"embedder is {self._embedder.name!r} (dim={self._dim}). "
                "Re-ingest with --rebuild or switch back to the original embedder."
            )

    def _schema(self, name: str) -> str:
        entry = self._read_meta().get(name)
        if entry and entry.get("schema"):
            return entry["schema"]
        # Legacy collections predate the meta sidecar: inspect the config.
        info = self._client.get_collection(name)
        vectors = info.config.params.vectors
        return _SCHEMA_HYBRID if isinstance(vectors, dict) and "dense" in vectors else _SCHEMA_LEGACY

    def ensure_collection(self, name: str) -> None:
        from qdrant_client.http import models as qm

        existing = {c.name for c in self._client.get_collections().collections}
        if name in existing:
            self._check_embedder(name)
            return
        self._client.create_collection(
            collection_name=name,
            vectors_config={
                "dense": qm.VectorParams(size=self._dim, distance=qm.Distance.COSINE)
            },
            sparse_vectors_config={
                "sparse": qm.SparseVectorParams(modifier=qm.Modifier.IDF)
            },
        )
        meta = self._read_meta()
        meta[name] = {
            "embedder": self._embedder.name,
            "dim": self._dim,
            "sparse": self._sparse.name,
            "schema": _SCHEMA_HYBRID,
        }
        self._write_meta(meta)

    def upsert_chunks(self, collection: str, chunks: list[tuple[str, dict[str, Any]]]) -> int:
        """Insert chunks; each chunk is (text, metadata).

        If metadata carries ``doc_context`` (set by the chunker), it is
        prepended to the text for both dense and sparse encoding — SAC-style
        document grounding — while the stored payload text stays the
        original chunk, so quoted passages remain verbatim.
        """
        if not chunks:
            return 0
        from qdrant_client.http import models as qm

        self.ensure_collection(collection)
        embed_texts = [
            f"{meta['doc_context']}\n{text}" if meta.get("doc_context") else text
            for text, meta in chunks
        ]
        dense = self._embedder.encode(embed_texts)
        hybrid = self._schema(collection) == _SCHEMA_HYBRID
        sparse = self._sparse.encode(embed_texts) if hybrid else [None] * len(chunks)
        points = []
        for dvec, svec, (text, meta) in zip(dense, sparse, chunks):
            pid = meta.get("id") or str(uuid.uuid4())
            payload = {"text": text, **meta}
            if hybrid:
                vector: Any = {
                    "dense": dvec,
                    "sparse": qm.SparseVector(indices=svec[0], values=svec[1]),
                }
            else:
                vector = dvec
            points.append(qm.PointStruct(id=pid, vector=vector, payload=payload))
        self._client.upsert(collection_name=collection, points=points)
        return len(points)

    def _build_filter(self, filters: dict[str, Any] | None):
        if not filters:
            return None
        from qdrant_client.http import models as qm

        return qm.Filter(
            must=[
                qm.FieldCondition(key=k, match=qm.MatchValue(value=v))
                for k, v in filters.items()
            ]
        )

    def search(
        self,
        collection: str,
        query: str,
        k: int = 6,
        filters: dict[str, Any] | None = None,
    ) -> list[StoredChunk]:
        """Hybrid dense+sparse search fused with RRF; dense-only on legacy
        collections. Scores are RRF ranks for hybrid, cosine for legacy."""
        from qdrant_client.http import models as qm

        existing = {c.name for c in self._client.get_collections().collections}
        if collection not in existing:
            return []
        self._check_embedder(collection)
        vec = self._embedder.encode([query])[0]
        qfilter = self._build_filter(filters)

        if self._schema(collection) == _SCHEMA_HYBRID:
            s_idx, s_val = self._sparse.encode([query])[0]
            # Over-fetch each leg so RRF fuses a real candidate pool, not
            # two already-truncated lists.
            prefetch_k = max(k * 2, k + 4)
            resp = self._client.query_points(
                collection_name=collection,
                prefetch=[
                    qm.Prefetch(query=vec, using="dense", limit=prefetch_k, filter=qfilter),
                    qm.Prefetch(
                        query=qm.SparseVector(indices=s_idx, values=s_val),
                        using="sparse",
                        limit=prefetch_k,
                        filter=qfilter,
                    ),
                ],
                query=qm.FusionQuery(fusion=qm.Fusion.RRF),
                limit=k,
                with_payload=True,
            )
        else:
            resp = self._client.query_points(
                collection_name=collection,
                query=vec,
                limit=k,
                query_filter=qfilter,
                with_payload=True,
            )
        hits = resp.points if hasattr(resp, "points") else resp
        out: list[StoredChunk] = []
        for h in hits:
            payload = h.payload or {}
            text = payload.get("text", "")
            meta = {key: v for key, v in payload.items() if key != "text"}
            out.append(StoredChunk(id=str(h.id), text=text, score=float(h.score), metadata=meta))
        return out

    def delete_by_source(self, collection: str, source_path: str) -> None:
        """Remove every chunk ingested from ``source_path`` so re-ingesting
        a changed file cannot leave stale (e.g. superseded 失效) chunks
        co-retrievable with the current text."""
        from qdrant_client.http import models as qm

        if collection not in self.collections():
            return
        self._client.delete(
            collection_name=collection,
            points_selector=qm.FilterSelector(
                filter=self._build_filter({"source_path": source_path})
            ),
        )

    def drop_collection(self, name: str) -> None:
        if name in self.collections():
            self._client.delete_collection(name)
        meta = self._read_meta()
        if meta.pop(name, None) is not None:
            self._write_meta(meta)

    def collections(self) -> list[str]:
        try:
            return [c.name for c in self._client.get_collections().collections]
        except Exception:
            return []
