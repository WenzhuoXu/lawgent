"""Disk cache of fetched documents, so a statute is downloaded once a while.

A fetch is usually followed by several reads of the same document (outline,
then article 9, then a search inside it), each a separate tool call. The
first call stores the converted text under ``state/legal_docs/docs``; later
calls within the freshness window read it back. Legislation is refreshed
after three days (it is amended), judgments are kept for thirty.
"""

from __future__ import annotations

import hashlib
import json
import threading
import time
from collections import OrderedDict
from dataclasses import asdict
from pathlib import Path
from typing import Optional

from .base import LegalDoc

LEGISLATION_TTL = 3 * 24 * 3600
CASE_TTL = 30 * 24 * 3600

_MEMORY: "OrderedDict[str, tuple[float, LegalDoc]]" = OrderedDict()
_LOCK = threading.Lock()
_MEMORY_MAX = 64


def _root() -> Path:
    from ..config import load_settings

    root = Path(load_settings().state_dir) / "legal_docs" / "docs"
    root.mkdir(parents=True, exist_ok=True)
    return root


def _path(ref: str) -> Path:
    source, _, native = ref.partition(":")
    digest = hashlib.sha1(native.encode("utf-8")).hexdigest()[:24]
    d = _root() / (source.replace("/", "_") or "unknown")
    d.mkdir(parents=True, exist_ok=True)
    return d / f"{digest}.json"


def _ttl(doc: LegalDoc) -> float:
    if doc.ttl is not None:
        return doc.ttl
    return CASE_TTL if doc.kind == "case" else LEGISLATION_TTL


def get(ref: str, *, language: Optional[str] = None) -> Optional[LegalDoc]:
    key = f"{ref}|{language or ''}"
    now = time.time()
    with _LOCK:
        hit = _MEMORY.get(key)
        if hit and hit[0] > now:
            _MEMORY.move_to_end(key)
            return hit[1]
    p = _path(key)
    if not p.exists():
        return None
    try:
        raw = json.loads(p.read_text(encoding="utf-8"))
        expires = float(raw.pop("_expires", 0))
        if expires <= now:
            return None
        doc = LegalDoc(**raw)
    except (OSError, ValueError, TypeError):
        return None
    with _LOCK:
        _MEMORY[key] = (expires, doc)
        while len(_MEMORY) > _MEMORY_MAX:
            _MEMORY.popitem(last=False)
    return doc


def put(doc: LegalDoc, *, language: Optional[str] = None) -> None:
    key = f"{doc.ref}|{language or ''}"
    expires = time.time() + _ttl(doc)
    with _LOCK:
        _MEMORY[key] = (expires, doc)
        while len(_MEMORY) > _MEMORY_MAX:
            _MEMORY.popitem(last=False)
    try:
        payload = asdict(doc)
        payload["_expires"] = expires
        _path(key).write_text(json.dumps(payload, ensure_ascii=False), encoding="utf-8")
    except (OSError, TypeError, ValueError):
        pass  # the cache is an optimisation; a failed write never fails a fetch
