"""Auto-detect which domain packs a user prompt belongs to.

Each pack declares its trigger vocabulary in ``pack.yaml`` under ``markers``
(English + Chinese). This module reads those markers and returns the list of
pack names that should auto-activate for a given prompt. Multiple packs can
activate from the same prompt (e.g. "Can a hospital lease a medevac
helicopter?" → both ``aviation`` and ``healthcare`` once both packs ship).

Detection is layered, not exclusive: explicit user-set ``active_domain_packs``
always win and are kept; detected packs are added on top.
"""

from __future__ import annotations

import re
from typing import Iterable

from .pack import DomainPack, available_packs, load_pack


# Cache packs so detect_packs() doesn't re-parse pack.yaml on every call.
_PACK_CACHE: dict[str, DomainPack] = {}
_WORD_RE_CACHE: dict[str, re.Pattern[str]] = {}


def _packs() -> list[DomainPack]:
    out: list[DomainPack] = []
    for name in available_packs():
        if name not in _PACK_CACHE:
            try:
                _PACK_CACHE[name] = load_pack(name)
            except Exception:  # noqa: BLE001 — soft-fail per pack
                continue
        out.append(_PACK_CACHE[name])
    return out


def _english_re(markers: tuple[str, ...]) -> re.Pattern[str] | None:
    if not markers:
        return None
    key = "|".join(markers)
    if key not in _WORD_RE_CACHE:
        # Word-boundary aware; multi-word markers like "cape town" handled via
        # explicit alternation. We escape each marker and require a word-edge
        # transition before and after to avoid partial-word hits.
        parts = "|".join(re.escape(m) for m in markers)
        _WORD_RE_CACHE[key] = re.compile(rf"(?<![A-Za-z0-9])(?:{parts})(?![A-Za-z0-9])", re.IGNORECASE)
    return _WORD_RE_CACHE[key]


def _pack_matches(pack: DomainPack, text: str, lowered: str) -> bool:
    en = _english_re(pack.markers_english)
    if en is not None and en.search(lowered):
        return True
    for marker in pack.markers_chinese:
        if marker in text:
            return True
    return False


def detect_packs(text: str) -> list[str]:
    """Return pack names whose markers fire against ``text``."""
    if not text:
        return []
    lowered = text.lower()
    matched: list[str] = []
    for pack in _packs():
        if _pack_matches(pack, text, lowered):
            matched.append(pack.name)
    return matched


def merge_active_packs(
    explicit: Iterable[str],
    text: str,
) -> tuple[list[str], list[str]]:
    """Combine explicit and auto-detected packs.

    Returns ``(merged, newly_detected)`` so callers can log / surface the
    auto-additions without changing the explicit set.
    """
    explicit_list = list(dict.fromkeys(explicit or []))
    detected = [p for p in detect_packs(text) if p not in explicit_list]
    return explicit_list + detected, detected


def clear_cache() -> None:
    """Force a reload of pack.yaml on the next call (tests + hot edits)."""
    _PACK_CACHE.clear()
    _WORD_RE_CACHE.clear()
