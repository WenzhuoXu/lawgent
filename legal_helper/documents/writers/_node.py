"""Locate the Node.js interpreter that goes with this Python environment.

``shutil.which("node")`` alone is not enough. Both toolchains live in the same
conda env, and when the app is started by an absolute interpreter path (a
systemd unit, a supervisor, an IDE run config) the env's ``bin/`` is never
prepended to ``PATH`` — so ``which`` misses a Node that is sitting right next
to the running Python. The PPTX writer treated that miss as "Node unavailable"
and silently downgraded to a fallback that drops tables, charts and
flowcharts, which is how decks shipped with blank slides.

Resolution order: ``LEGAL_HELPER_NODE`` → next to ``sys.executable`` → PATH.
"""

from __future__ import annotations

import os
import shutil
import sys
from functools import lru_cache
from pathlib import Path


@lru_cache(maxsize=1)
def find_node() -> str | None:
    """Return an absolute path to ``node``, or None when genuinely absent."""
    override = os.environ.get("LEGAL_HELPER_NODE", "").strip()
    if override and Path(override).is_file() and os.access(override, os.X_OK):
        return override

    # The conda env that owns this Python almost always owns Node too.
    sibling = Path(sys.executable).resolve().parent / "node"
    if sibling.is_file() and os.access(sibling, os.X_OK):
        return str(sibling)

    return shutil.which("node")


@lru_cache(maxsize=1)
def node_diagnostic() -> str:
    """Human-readable reason Node could not be found, for error messages."""
    return (
        "node not found. Looked at $LEGAL_HELPER_NODE, "
        f"{Path(sys.executable).resolve().parent / 'node'}, and $PATH. "
        "Install it in the same env as Python: `conda install -n llm nodejs`."
    )
