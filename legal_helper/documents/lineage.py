"""Which generated file was made from which.

Generated files used to carry no lineage, so the model could not tell the
latest version of a deliverable from an older sibling: in one chat an edit
started from a superseded workbook, its own diff showed 4 changed cells, and
the file it replaced differed from the delivered one by 72 cells and three
rows. Every tool that writes a file from other files records one line here;
an edit that targets a file with a newer descendant can then say so.

The record is append-only JSON lines under the state directory, keyed by
absolute path. Writing it is best-effort: lineage must never fail a tool.
"""

from __future__ import annotations

import json
import threading
import time
from pathlib import Path
from typing import Iterable, Optional

_LOCK = threading.Lock()


def _ledger(state_dir: Optional[Path] = None) -> Path:
    if state_dir is None:
        from ..config import current_settings

        state_dir = current_settings().state_dir
    return Path(state_dir) / "lineage.jsonl"


def _key(path: Path | str) -> str:
    try:
        return str(Path(path).expanduser().resolve())
    except OSError:
        return str(path)


def record_derivation(
    output: Path | str,
    sources: Iterable[Path | str],
    tool: str,
    *,
    state_dir: Optional[Path] = None,
) -> None:
    """Record that ``output`` was produced by ``tool`` from ``sources``."""
    entry = {
        "output": _key(output),
        "sources": [_key(s) for s in sources if s],
        "tool": tool,
        "at": time.time(),
    }
    try:
        ledger = _ledger(state_dir)
        ledger.parent.mkdir(parents=True, exist_ok=True)
        with _LOCK, ledger.open("a", encoding="utf-8") as fh:
            fh.write(json.dumps(entry, ensure_ascii=False) + "\n")
    except Exception:  # noqa: BLE001 — lineage is advisory
        return


def _entries(state_dir: Optional[Path] = None) -> list[dict]:
    try:
        ledger = _ledger(state_dir)
        if not ledger.is_file():
            return []
        lines = ledger.read_text(encoding="utf-8").splitlines()
    except Exception:  # noqa: BLE001
        return []
    out: list[dict] = []
    for line in lines:
        try:
            out.append(json.loads(line))
        except ValueError:
            continue
    return out


def derived_from(path: Path | str, *, state_dir: Optional[Path] = None) -> list[str]:
    """The sources the latest write of ``path`` was made from (may be empty)."""
    target = _key(path)
    for entry in reversed(_entries(state_dir)):
        if entry.get("output") == target:
            return list(entry.get("sources") or [])
    return []


def descendants(path: Path | str, *, state_dir: Optional[Path] = None) -> list[str]:
    """Every existing file transitively derived from ``path``, oldest first."""
    entries = _entries(state_dir)
    children: dict[str, list[str]] = {}
    for entry in entries:
        for src in entry.get("sources") or []:
            out = entry.get("output")
            if out and out not in children.setdefault(src, []):
                children[src].append(out)
    seen: list[str] = []
    frontier = [_key(path)]
    while frontier:
        current = frontier.pop(0)
        for child in children.get(current, []):
            if child not in seen and child != _key(path):
                seen.append(child)
                frontier.append(child)
    return [p for p in seen if Path(p).is_file()]


def newer_descendants(path: Path | str, *, state_dir: Optional[Path] = None) -> list[str]:
    """Descendants of ``path`` modified after it — i.e. later versions of it."""
    try:
        base_mtime = Path(path).stat().st_mtime
    except OSError:
        return []
    out: list[str] = []
    for child in descendants(path, state_dir=state_dir):
        try:
            if Path(child).stat().st_mtime > base_mtime:
                out.append(child)
        except OSError:
            continue
    return out


def stale_source_warning(path: Path | str, *, state_dir: Optional[Path] = None) -> str:
    """A one-line warning when ``path`` has a newer descendant, else ``""``."""
    newer = newer_descendants(path, state_dir=state_dir)
    if not newer:
        return ""
    names = ", ".join(Path(p).name for p in newer[-3:])
    return (
        f"WARNING: {Path(path).name} has a newer version derived from it ({names}). "
        "Editing this file starts from the older version; open the latest one "
        "unless the user asked for this exact file."
    )


__all__ = [
    "derived_from",
    "descendants",
    "newer_descendants",
    "record_derivation",
    "stale_source_warning",
]
