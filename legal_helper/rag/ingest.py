"""Ingestion CLI: ``python -m legal_helper.rag.ingest --collection X --path Y``.

Sources are described per-collection in ``rag/collections/<name>.yaml``.
For ad-hoc paths, pass ``--path`` directly.
"""

from __future__ import annotations

import argparse
import hashlib
import sys
import uuid
from pathlib import Path
from typing import Iterable

import yaml

_CHUNK_NS = uuid.UUID("9b6f6a4e-7c1f-4d50-b3d2-5f7a8a4c1d11")

from .chunker import chunk_file
from .loaders import supported_suffixes
from .store import QdrantStore


_REPO_ROOT = Path(__file__).resolve().parents[2]
_COLLECTIONS_DIR = Path(__file__).resolve().parent / "collections"
_INGEST_EXTS = supported_suffixes()
_MANIFEST_CACHE: dict[Path, dict[str, dict]] = {}


def _resolve_manifest_meta(path: Path) -> dict:
    """Walk up at most 4 levels from ``path`` looking for ``manifest.yaml``;
    return the metadata entry whose ``file:`` key matches the file's path
    relative to the manifest directory. Empty dict on miss."""
    cur = path.parent
    for _ in range(4):
        candidate = cur / "manifest.yaml"
        if candidate.is_file():
            if candidate not in _MANIFEST_CACHE:
                raw = yaml.safe_load(candidate.read_text(encoding="utf-8")) or {}
                files = raw.get("files") or []
                indexed: dict[str, dict] = {}
                for entry in files:
                    rel = entry.get("file")
                    if rel:
                        indexed[rel] = {k: v for k, v in entry.items() if k != "file"}
                _MANIFEST_CACHE[candidate] = indexed
            indexed = _MANIFEST_CACHE[candidate]
            try:
                rel = str(path.relative_to(candidate.parent))
            except ValueError:
                rel = path.name
            return dict(indexed.get(rel, {}))
        if cur == cur.parent:
            break
        cur = cur.parent
    return {}


def _gather_paths(root: Path, patterns: list[str] | None = None) -> list[Path]:
    if root.is_file():
        return [root]
    if not root.is_dir():
        return []
    if patterns:
        out: list[Path] = []
        for p in patterns:
            out.extend(root.glob(p))
        return sorted({p for p in out if p.is_file()})
    return sorted(p for p in root.rglob("*") if p.is_file() and p.suffix.lower() in _INGEST_EXTS)


def _load_collection_yaml(name: str) -> dict:
    path = _COLLECTIONS_DIR / f"{name}.yaml"
    if not path.is_file():
        return {}
    return yaml.safe_load(path.read_text(encoding="utf-8")) or {}


def _chunk_id(path: Path, index: int, text: str) -> str:
    """Stable UUID5 derived from path + index + content prefix.

    Qdrant's local backend requires point IDs in UUID form, so we hash
    into the chunk namespace rather than returning a raw sha256. Same
    inputs always produce the same UUID, so re-ingest upserts cleanly.
    """
    h = hashlib.sha256()
    h.update(str(path).encode())
    h.update(f":{index}:".encode())
    h.update(text[:200].encode("utf-8", errors="ignore"))
    return str(uuid.uuid5(_CHUNK_NS, h.hexdigest()))


def ingest_paths(paths: Iterable[Path], collection: str, store: QdrantStore | None = None) -> int:
    store = store or QdrantStore()
    total = 0
    for p in paths:
        if p.suffix.lower() not in _INGEST_EXTS:
            continue
        manifest_meta = _resolve_manifest_meta(p)
        base_meta = {"collection": collection, **manifest_meta}
        try:
            chunks = chunk_file(p, metadata=base_meta)
        except Exception as exc:  # noqa: BLE001 — log and skip a bad file
            print(f"  ! {p}: load failed ({exc!s})", file=sys.stderr)
            continue
        payload: list[tuple[str, dict]] = []
        for c in chunks:
            cid = _chunk_id(p, c.metadata.get("chunk_index", 0), c.text)
            meta = {**c.metadata, "id": cid}
            payload.append((c.text, meta))
        # Chunk IDs hash the chunk text, so edited files produce new IDs and
        # upsert alone would leave the old (possibly superseded 失效) chunks
        # behind — drop this file's previous points first.
        store.delete_by_source(collection, str(p))
        added = store.upsert_chunks(collection, payload)
        total += added
        print(f"  + {p.relative_to(_REPO_ROOT) if p.is_relative_to(_REPO_ROOT) else p}  ({added} chunks)")
    return total


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description="Ingest documents into a RAG collection.")
    parser.add_argument("--collection", required=True, help="target collection name")
    parser.add_argument("--path", action="append", help="path to ingest (file or directory)")
    parser.add_argument("--seed", action="store_true", help="ingest the seed corpus declared in collections/<name>.yaml")
    parser.add_argument(
        "--source",
        action="append",
        help="named source set: ingest the seed corpus declared in collections/<source>.yaml into --collection",
    )
    parser.add_argument(
        "--rebuild",
        action="store_true",
        help="drop the collection first (full re-ingest; clears stale chunks and old vector schemas)",
    )
    args = parser.parse_args(argv)

    paths: list[Path] = []
    if args.path:
        for raw in args.path:
            p = Path(raw).expanduser().resolve()
            paths.extend(_gather_paths(p))

    seed_names = list(args.source or [])
    if args.seed:
        seed_names.append(args.collection)
    for name in seed_names:
        cfg = _load_collection_yaml(name)
        if not cfg:
            print(f"no collections/{name}.yaml found for --source/--seed", file=sys.stderr)
            return 1
        for src in cfg.get("sources", []):
            root = (_REPO_ROOT / src["path"]).resolve()
            paths.extend(_gather_paths(root, src.get("glob")))

    if not paths:
        print(f"no paths to ingest for collection={args.collection}", file=sys.stderr)
        return 1

    store = QdrantStore()
    if args.rebuild:
        store.drop_collection(args.collection)
        print(f"Dropped collection={args.collection} (rebuild)")

    print(f"Ingesting {len(paths)} file(s) into collection={args.collection}")
    total = ingest_paths(paths, args.collection, store=store)
    print(f"Done: {total} chunks indexed.")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
