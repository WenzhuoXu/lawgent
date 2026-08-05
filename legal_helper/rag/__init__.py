"""Legal RAG layer — local bge-m3 embeddings + Qdrant (embedded) + legal-aware chunking."""

from .chunker import Chunk, chunk_document
from .retrieve import RetrievedChunk, retrieve

__all__ = ["Chunk", "RetrievedChunk", "chunk_document", "retrieve"]
