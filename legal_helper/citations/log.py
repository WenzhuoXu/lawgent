"""Append-only verification log.

Adapted from anthropics/claude-for-legal litigation-legal verification-log
pattern: "when you or the user verifies a flagged item — confirms a cite
against a primary source, checks a deadline against the local rule,
verifies a threshold against the current statute — record it so the next
person doesn't re-verify."

One-line-per-entry format:
``- [YYYY-MM-DD] <cite> verified by <who> against <source> — <verdict>``
"""

from __future__ import annotations

import re
from datetime import datetime, timezone
from pathlib import Path


DEFAULT_PATH = Path.home() / ".legal_helper" / "verification-log.md"
_ENTRY_RE = re.compile(r"^- \[(\d{4}-\d{2}-\d{2})\] (.+?) verified by (.+?) against (.+?) — (.+)$")


def _today() -> str:
    return datetime.now(timezone.utc).strftime("%Y-%m-%d")


def append(
    cite: str,
    source: str,
    verdict: str,
    who: str = "cite-check",
    path: Path | str | None = None,
) -> Path:
    p = Path(path) if path else DEFAULT_PATH
    p.parent.mkdir(parents=True, exist_ok=True)
    line = f"- [{_today()}] {cite.strip()} verified by {who} against {source} — {verdict}\n"
    with p.open("a", encoding="utf-8") as f:
        f.write(line)
    return p


def read_entries(path: Path | str | None = None) -> list[dict]:
    p = Path(path) if path else DEFAULT_PATH
    if not p.is_file():
        return []
    out: list[dict] = []
    for line in p.read_text(encoding="utf-8").splitlines():
        m = _ENTRY_RE.match(line)
        if m:
            out.append({
                "date": m.group(1),
                "cite": m.group(2),
                "who": m.group(3),
                "source": m.group(4),
                "verdict": m.group(5),
            })
    return out


def is_already_verified(cite: str, path: Path | str | None = None) -> bool:
    """True iff the same cite has been confirmed/verified previously."""
    target = cite.strip()
    for e in read_entries(path):
        if e["cite"] == target and (
            "confirm" in e["verdict"].lower() or "verified" in e["verdict"].lower()
        ):
            return True
    return False
