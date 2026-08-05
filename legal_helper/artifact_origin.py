"""In-process registry of artifact provenance keyed by resolved file path.

The artifact scan (`server._scan_new_artifacts`) walks the outputs dir after a
turn and cannot, from the bytes alone, tell whether a file was *generated* by a
`write_*` document tool or *fetched* from an external URL. Tools that produce a
non-default origin record it here before the scan runs; the scan looks each
path up and falls back to ``"generated"``.

Kept deliberately tiny and dependency-free to avoid import cycles between the
tools layer and the server.
"""

from __future__ import annotations

import threading
from pathlib import Path
from typing import Literal

Origin = Literal["generated", "fetched", "uploaded"]

DEFAULT_ORIGIN: Origin = "generated"

_lock = threading.Lock()
_origins: dict[str, Origin] = {}


def _key(path: str | Path) -> str:
    return str(Path(path).resolve())


def record_origin(path: str | Path, origin: Origin) -> None:
    """Remember that ``path`` was produced with the given ``origin``."""
    with _lock:
        _origins[_key(path)] = origin


def origin_for(path: str | Path) -> Origin:
    """Return the recorded origin for ``path``, or the default if unknown."""
    with _lock:
        return _origins.get(_key(path), DEFAULT_ORIGIN)


__all__ = ["Origin", "DEFAULT_ORIGIN", "record_origin", "origin_for"]
