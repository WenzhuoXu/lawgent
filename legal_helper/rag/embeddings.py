"""Embedder abstractions. Default to local bge-m3 (multilingual zh+en) for
dense vectors plus a dependency-free BM25-style lexical encoder for the
sparse side of hybrid retrieval."""

from __future__ import annotations

import hashlib
import re
from collections import Counter
from typing import Protocol


class Embedder(Protocol):
    name: str
    dim: int

    def encode(self, texts: list[str]) -> list[list[float]]:
        ...


class BgeM3Embedder:
    """BAAI/bge-m3, loaded lazily via sentence-transformers.

    Multilingual (100+ langs, strong on Chinese), 8K context, 1024-dim
    dense vectors. First call downloads ~2 GB of weights to the
    HuggingFace cache; subsequent calls are local. We use
    ``sentence-transformers`` rather than ``FlagEmbedding`` because the
    latter pulls in ``peft`` and other deps that conflict with the
    repo's pinned ``transformers`` major version. The dense output is
    equivalent for retrieval purposes.
    """

    name = "BAAI/bge-m3"
    dim = 1024

    def __init__(self) -> None:
        self._model = None

    def _load(self) -> None:
        if self._model is not None:
            return
        from sentence_transformers import SentenceTransformer

        self._model = SentenceTransformer(self.name)

    def encode(self, texts: list[str]) -> list[list[float]]:
        if not texts:
            return []
        self._load()
        assert self._model is not None
        out = self._model.encode(
            texts,
            normalize_embeddings=True,
            convert_to_numpy=True,
        )
        return out.tolist() if hasattr(out, "tolist") else [list(v) for v in out]


class OpenAIEmbedder:
    """text-embedding-3-large (3072 dim). Used only when explicitly configured."""

    name = "openai/text-embedding-3-large"
    dim = 3072

    def __init__(self) -> None:
        self._client = None

    def _load(self) -> None:
        if self._client is not None:
            return
        from openai import OpenAI

        self._client = OpenAI()

    def encode(self, texts: list[str]) -> list[list[float]]:
        if not texts:
            return []
        self._load()
        assert self._client is not None
        resp = self._client.embeddings.create(model="text-embedding-3-large", input=texts)
        return [d.embedding for d in resp.data]


class LexicalSparseEncoder:
    """CJK-aware lexical sparse encoder for Qdrant hybrid retrieval.

    Chosen over bge-m3's ``lexical_weights`` output because that path needs
    ``FlagEmbedding`` (see BgeM3Embedder note) and over fastembed's
    ``Qdrant/bm25`` because that model has no CJK segmentation. This is the
    classic hashed-BM25 recipe: Latin words + CJK character uni/bigrams
    (bigrams approximate Chinese word segmentation, so 第44条 / 案号 / CCAR
    part numbers match exactly), term-frequency values, token hashed to a
    uint32 index. IDF is applied server-side via Qdrant's
    ``Modifier.IDF`` on the sparse vector config — no corpus statistics
    are kept client-side, so it is fully local and deterministic.
    """

    name = "lexical-bm25-cjk-v1"

    _WORD_RE = re.compile(r"[a-z0-9]+(?:[._§-][a-z0-9]+)*")
    _CJK_RE = re.compile("[㐀-䶿一-鿿豈-﫿]")

    def tokenize(self, text: str) -> list[str]:
        text = text.lower()
        tokens = self._WORD_RE.findall(text)
        cjk_chars = self._CJK_RE.findall(text)
        tokens.extend(cjk_chars)
        # Bigrams only within contiguous CJK runs.
        for run in re.findall(f"{self._CJK_RE.pattern}+", text):
            tokens.extend(run[i : i + 2] for i in range(len(run) - 1))
        return tokens

    @staticmethod
    def _index(token: str) -> int:
        return int.from_bytes(hashlib.blake2b(token.encode(), digest_size=4).digest(), "big")

    def encode(self, texts: list[str]) -> list[tuple[list[int], list[float]]]:
        """Per text: (indices, values) with term-frequency values."""
        out: list[tuple[list[int], list[float]]] = []
        for text in texts:
            counts: Counter[int] = Counter(self._index(t) for t in self.tokenize(text))
            indices = sorted(counts)
            out.append((indices, [float(counts[i]) for i in indices]))
        return out


_DEFAULT_EMBEDDER: Embedder | None = None
_DEFAULT_SPARSE: LexicalSparseEncoder | None = None


def default_sparse_encoder() -> LexicalSparseEncoder:
    global _DEFAULT_SPARSE
    if _DEFAULT_SPARSE is None:
        _DEFAULT_SPARSE = LexicalSparseEncoder()
    return _DEFAULT_SPARSE


def default_embedder() -> Embedder:
    global _DEFAULT_EMBEDDER
    if _DEFAULT_EMBEDDER is None:
        _DEFAULT_EMBEDDER = BgeM3Embedder()
    return _DEFAULT_EMBEDDER


def get_embedder(name: str | None = None) -> Embedder:
    if name in (None, "", "bge-m3", "bge_m3", "BAAI/bge-m3"):
        return default_embedder()
    if name in ("openai", "text-embedding-3-large"):
        return OpenAIEmbedder()
    raise ValueError(f"unknown embedder: {name}")
