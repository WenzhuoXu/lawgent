"""Expose RAG retrieve as a model-callable function tool."""

import json
from typing import Optional

from anthropic import beta_tool

from ..rag import retrieve
from ..rag.retrieve import _store


# Common label aliases. Each key fans out to one or more real Qdrant
# collection names. ``aviation`` is the most-requested alias because skills
# routinely ask for "aviation" while the corpus is split into per-source
# collections (``aviation_treaties``, ``icao_doc``, …).
_COLLECTION_ALIASES: dict[str, tuple[str, ...]] = {
    "aviation": ("aviation_treaties", "icao_doc", "caac_ccar"),
}


def _available_collections() -> list[str]:
    try:
        return sorted(_store().collections())
    except Exception:  # noqa: BLE001
        return []


def _resolve_collections(requested: str, available: list[str]) -> list[str]:
    """Map the model-supplied collection label to real, existing collections.

    Order of precedence:
      1. Exact match against an existing collection.
      2. Alias from ``_COLLECTION_ALIASES`` (intersected with what exists).
      3. Empty list (caller surfaces an informative error).
    """
    if requested in available:
        return [requested]
    aliased = [c for c in _COLLECTION_ALIASES.get(requested, ()) if c in available]
    return aliased


@beta_tool
def retrieve_legal(
    query: str,
    collection: str = "general",
    k: int = 6,
    filters_json: Optional[str] = None,
) -> str:
    """Semantic retrieval over the local RAG store.

    Prefer this when the user's question is about content that should be in
    your indexed corpora (statutes, playbooks, prior memos, treaty texts).
    Returns the top ``k`` chunks with their source path / URL and score.

    Args:
        query: Free-text query, in any supported language.
        collection: Collection name. Use ``aviation`` for the aviation pack;
            other ingested collections appear in the ``available_collections``
            field of error responses.
        k: Top-k hits to return (1-12).
        filters_json: Optional JSON string of {metadata_key: value} filters
            (e.g. ``{"jurisdiction": "CN"}``).
    """
    k = max(1, min(int(k), 12))
    filters = None
    if filters_json:
        try:
            filters = json.loads(filters_json)
        except json.JSONDecodeError:
            return json.dumps({"error": f"invalid filters_json: {filters_json}"})

    available = _available_collections()
    targets = _resolve_collections(collection, available)
    if not targets:
        return json.dumps(
            {
                "error": (
                    f"collection {collection!r} is not ingested in the local "
                    "Qdrant store; pass one of the names in "
                    "``available_collections`` (or run "
                    "``python -m legal_helper.rag.ingest`` to populate it)."
                ),
                "collection": collection,
                "available_collections": available,
                "query": query,
            },
            ensure_ascii=False,
        )

    all_hits = []
    try:
        for target in targets:
            for h in retrieve(query=query, collection=target, k=k, filters=filters):
                all_hits.append((target, h))
    except Exception as e:  # noqa: BLE001
        return json.dumps(
            {"error": f"retrieve failed: {e!s}", "collection": collection, "query": query}
        )

    all_hits.sort(key=lambda x: x[1].score, reverse=True)
    all_hits = all_hits[:k]

    return json.dumps(
        {
            "collection": collection,
            "resolved_collections": targets,
            "query": query,
            "count": len(all_hits),
            "results": [
                {
                    "score": h.score,
                    "collection": src,
                    "text": h.text,
                    "source_name": h.metadata.get("source_name"),
                    "source_path": h.metadata.get("source_path"),
                    "url": h.metadata.get("url") or h.metadata.get("source_url"),
                    "jurisdiction": h.metadata.get("jurisdiction"),
                    "metadata": h.metadata,
                }
                for src, h in all_hits
            ],
        },
        ensure_ascii=False,
    )
