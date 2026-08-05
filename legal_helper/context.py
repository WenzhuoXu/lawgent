"""Within-chat context-window management for long conversations.

Cross-chat continuity lives in ``projects.py``; this module handles the other
half — keeping a **single long chat** inside the model's context window without
silently dropping legal detail.

The old behaviour was count-based: "the last 10 messages, each truncated to
3000 chars." That gutted long pasted documents (a 40-page contract became 3000
chars of context) and ignored token budgets entirely. This module replaces it
with:

- ``estimate_tokens`` — a cheap, offline token estimate (CJK-aware), so we never
  block the hot path on a ``count_tokens`` round-trip;
- ``select_recent_within_budget`` — include the newest messages that fit under a
  token budget, oldest-dropped-first, so a few huge messages don't crowd out the
  rest and small messages use the budget fully;
- ``should_compact`` — true when the accumulated transcript approaches a fraction
  of the model's window (compact early, ~0.6, not at 0.95 — the Claude Code
  rule), the signal the chat layer uses to fold older turns into the rolling
  summary before they fall out of the window.
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import TYPE_CHECKING, Iterable, Optional

if TYPE_CHECKING:  # avoid an import cycle (chat_models has no heavy deps, but keep it lazy)
    from .chat_models import ChatMessage
    from .config import Settings

# Model context windows (input tokens). Conservative; unknown models fall back
# to ``_DEFAULT_WINDOW``. Opus/Sonnet 4.x are 1M; Haiku 200K; gpt-5.x treated
# conservatively. These bound the *compaction trigger*, not the per-turn digest.
_WINDOWS = {
    "claude-opus-4-8": 1_000_000,
    "claude-opus-4-7": 1_000_000,
    "claude-opus-4-6": 1_000_000,
    "claude-sonnet-4-6": 1_000_000,
    "claude-sonnet-5": 1_000_000,
    "claude-haiku-4-5": 200_000,
    "gpt-5.6-terra": 1_000_000,
    "gpt-5.6-sol": 1_000_000,
    "gpt-5.6-luna": 1_000_000,
    "gpt-5.5": 400_000,
    "gpt-5.4": 400_000,
    "gpt-5.4-mini": 400_000,
}
_DEFAULT_WINDOW = 200_000
# Default token budget for the per-turn context digest fed to planner / answer.
DEFAULT_CONTEXT_BUDGET = 16_000
# Per-message hard cap inside the digest — generous enough to keep a clause or
# holding intact, bounded enough that one giant paste can't eat the whole budget.
_PER_MESSAGE_CAP_TOKENS = 4_000


def estimate_tokens(text: Optional[str]) -> int:
    """Cheap offline token estimate. CJK ≈ 1 token/char; other ≈ 1 token/4 chars."""
    if not text:
        return 0
    cjk = 0
    for ch in text:
        o = ord(ch)
        if 0x4E00 <= o <= 0x9FFF or 0x3040 <= o <= 0x30FF or 0xAC00 <= o <= 0xD7A3:
            cjk += 1
    other = len(text) - cjk
    return cjk + (other // 4) + 1


def context_window_for(settings: "Settings") -> int:
    model = settings.model_for_provider()
    return _WINDOWS.get(model, _DEFAULT_WINDOW)


def _truncate_to_tokens(text: str, max_tokens: int) -> str:
    """Truncate ``text`` so its estimate is ≤ ``max_tokens`` (char-proportional)."""
    if estimate_tokens(text) <= max_tokens:
        return text
    # Estimate is monotonic in length; binary-search the cut point.
    lo, hi = 0, len(text)
    while lo < hi:
        mid = (lo + hi + 1) // 2
        if estimate_tokens(text[:mid]) <= max_tokens - 1:
            lo = mid
        else:
            hi = mid - 1
    return text[:lo].rstrip() + "…"


@dataclass(frozen=True)
class WindowedContext:
    kept: list["ChatMessage"]            # oldest→newest, fit under budget
    dropped: list["ChatMessage"]         # older messages that did not fit
    used_tokens: int


def select_recent_within_budget(
    messages: Iterable["ChatMessage"],
    budget_tokens: int,
    *,
    per_message_cap_tokens: int = _PER_MESSAGE_CAP_TOKENS,
) -> WindowedContext:
    """Pick the newest messages that fit under ``budget_tokens``.

    Walks newest→oldest, charging each message its (capped) token estimate, and
    stops when the next message would exceed the budget. Returns the kept set in
    chronological order plus the older messages that were dropped, so the caller
    can fold the dropped ones into the rolling summary (lossless compaction).
    """
    msgs = list(messages)
    kept_rev: list[ChatMessage] = []
    used = 0
    cutoff = 0  # index in msgs up to which everything is dropped
    for i in range(len(msgs) - 1, -1, -1):
        m = msgs[i]
        cost = min(estimate_tokens(m.content), per_message_cap_tokens) + 4  # +role/sep overhead
        if kept_rev and used + cost > budget_tokens:
            cutoff = i + 1
            break
        kept_rev.append(m)
        used += cost
    else:
        cutoff = 0
    kept = list(reversed(kept_rev))
    dropped = msgs[:cutoff] if cutoff > 0 else []
    # Trim kept[0] if it alone is over the per-message cap (keeps digest bounded).
    return WindowedContext(kept=kept, dropped=dropped, used_tokens=used)


def render_recent(messages: list["ChatMessage"], per_message_cap_tokens: int = _PER_MESSAGE_CAP_TOKENS) -> str:
    out: list[str] = []
    for m in messages:
        out.append(f"{m.role.upper()}: {_truncate_to_tokens(m.content, per_message_cap_tokens)}")
    return "\n".join(out)


def should_compact(
    messages: Iterable["ChatMessage"],
    settings: "Settings",
    *,
    threshold: float = 0.6,
    summary_tokens: int = 0,
) -> bool:
    """True when the chat transcript approaches ``threshold`` of the window.

    Compact early (default 0.6, not 0.95) so quality doesn't degrade near the
    limit. ``summary_tokens`` accounts for the rolling summary already carried.
    """
    total = summary_tokens + sum(estimate_tokens(m.content) for m in messages)
    return total >= threshold * context_window_for(settings)


__all__ = [
    "estimate_tokens",
    "context_window_for",
    "select_recent_within_budget",
    "render_recent",
    "should_compact",
    "WindowedContext",
    "DEFAULT_CONTEXT_BUDGET",
]
