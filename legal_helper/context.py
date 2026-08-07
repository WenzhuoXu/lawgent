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

import re
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
# Floor for the per-turn context digest fed to planner / answer. The real budget
# is a share of the *model's* window (see ``context_budget_for``) — a constant
# here would hand a 1M-window model the same 16K digest as a 200K one, which is
# what made a 32K-token chat (3% of the window) drop turns and re-derive
# already-established analysis every turn.
DEFAULT_CONTEXT_BUDGET = 16_000
# Share of the model context window spent on the within-chat digest. The rest is
# left for the system prompt, tool surface, tool results, and the output ceiling.
DEFAULT_CONTEXT_WINDOW_FRACTION = 0.25
# Per-message cap inside the digest, as a share of the budget: one message may
# never eat more than this fraction, so a single giant paste cannot crowd out the
# conversation — but on a large budget a full prior answer survives intact.
_PER_MESSAGE_CAP_FRACTION = 0.25
_PER_MESSAGE_CAP_FLOOR_TOKENS = 4_000


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


def context_budget_for(settings: "Settings") -> int:
    """Token budget for the per-turn within-chat digest.

    Scales with the model actually serving the turn: ``fraction × window``,
    floored at ``chat_context_token_budget`` so the configured value can only
    ever raise the budget, never shrink it below what previous runs used.
    """
    try:
        fraction = float(
            getattr(settings, "chat_context_window_fraction", DEFAULT_CONTEXT_WINDOW_FRACTION)
        )
    except (TypeError, ValueError):
        fraction = DEFAULT_CONTEXT_WINDOW_FRACTION
    fraction = min(max(fraction, 0.0), 0.9)
    floor = int(getattr(settings, "chat_context_token_budget", DEFAULT_CONTEXT_BUDGET) or 0)
    return max(floor, int(fraction * context_window_for(settings)), 1_000)


def per_message_cap_for(budget_tokens: int) -> int:
    """Per-message truncation cap derived from the digest budget.

    A fixed cap truncated a 4.4K-token legal answer mid-table on a model with a
    1M window; deriving it from the budget keeps the "one paste can't eat the
    digest" guarantee without gutting prior answers.
    """
    return max(
        _PER_MESSAGE_CAP_FLOOR_TOKENS,
        int(_PER_MESSAGE_CAP_FRACTION * max(budget_tokens, 0)),
    )


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
    per_message_cap_tokens: Optional[int] = None,
) -> WindowedContext:
    """Pick the newest messages that fit under ``budget_tokens``.

    Walks newest→oldest, charging each message its (capped) token estimate, and
    stops when the next message would exceed the budget. Returns the kept set in
    chronological order plus the older messages that were dropped, so the caller
    can fold the dropped ones into the rolling summary (lossless compaction).
    """
    if per_message_cap_tokens is None:
        per_message_cap_tokens = per_message_cap_for(budget_tokens)
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


# --- verification-appendix compaction -------------------------------------
# Every substantive legal answer ends with a `资料来源与核验` / `Sources &
# Verification` markdown table. Measured on a real thread, those tables are
# 30-34% of each answer's characters — so replaying answers verbatim spends a
# third of the digest on table syntax and ✔/备注 columns. The *information*
# (which claim was established, on what pinpoint) is worth replaying; the
# rendering is not. This collapses each appendix table to one line per row,
# claim + pinpoint, link text without URLs (the body keeps its inline links).
# Storage and the user-facing answer are untouched — this is replay only.
_VERIFICATION_HEADING_RE = re.compile(
    r"^#{1,6}\s*(?:资料来源与核验|资料来源|来源与核验|Sources\s*&\s*Verification|Sources)\s*$",
    re.MULTILINE,
)
_TABLE_SEPARATOR_RE = re.compile(r"^\|(?:\s*:?-{1,}:?\s*\|)+\s*$")
_MD_LINK_RE = re.compile(r"\[([^\]]*)\]\([^)]*\)")
_CLAIM_HEADER_RE = re.compile(r"主张|claim", re.IGNORECASE)
_PINPOINT_HEADER_RE = re.compile(r"依据|pinpoint|source", re.IGNORECASE)


def _split_table_row(line: str) -> list[str]:
    return [c.strip() for c in line.strip().strip("|").split("|")]


def _compact_appendix_table(rows: list[str]) -> list[str]:
    """Collapse one markdown table to `- claim — pinpoint` lines.

    Returns the original rows unchanged if the table does not parse as a
    verification table (never lose information on an unexpected shape).
    """
    parsed = [_split_table_row(r) for r in rows if not _TABLE_SEPARATOR_RE.match(r.strip())]
    if len(parsed) < 2:
        return rows
    header, body = parsed[0], parsed[1:]
    claim_idx = next((i for i, c in enumerate(header) if _CLAIM_HEADER_RE.search(c)), None)
    pin_idx = next((i for i, c in enumerate(header) if _PINPOINT_HEADER_RE.search(c)), None)
    if claim_idx is None or pin_idx is None:
        return rows
    out: list[str] = []
    for cells in body:
        if max(claim_idx, pin_idx) >= len(cells):
            continue
        claim = _MD_LINK_RE.sub(r"\1", cells[claim_idx]).strip()
        pin = _MD_LINK_RE.sub(r"\1", cells[pin_idx]).strip()
        if not claim and not pin:
            continue
        out.append(f"- {claim} — {pin}" if claim and pin else f"- {claim or pin}")
    return out or rows


def compact_verification_appendix(text: str) -> str:
    """Compact any verification-appendix tables in an assistant answer."""
    if not text:
        return text
    match = _VERIFICATION_HEADING_RE.search(text)
    if not match:
        return text
    head, tail = text[: match.start()], text[match.start() :]
    out: list[str] = []
    table: list[str] = []
    for line in tail.splitlines():
        if line.lstrip().startswith("|"):
            table.append(line)
            continue
        if table:
            out.extend(_compact_appendix_table(table))
            table = []
        out.append(line)
    if table:
        out.extend(_compact_appendix_table(table))
    return head + "\n".join(out)


def render_recent(
    messages: list["ChatMessage"],
    per_message_cap_tokens: Optional[int] = None,
) -> str:
    if per_message_cap_tokens is None:
        per_message_cap_tokens = _PER_MESSAGE_CAP_FLOOR_TOKENS
    out: list[str] = []
    for m in messages:
        content = m.content
        if m.role == "assistant":
            content = compact_verification_appendix(content)
        out.append(f"{m.role.upper()}: {_truncate_to_tokens(content, per_message_cap_tokens)}")
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
    "context_budget_for",
    "per_message_cap_for",
    "select_recent_within_budget",
    "render_recent",
    "compact_verification_appendix",
    "should_compact",
    "WindowedContext",
    "DEFAULT_CONTEXT_BUDGET",
    "DEFAULT_CONTEXT_WINDOW_FRACTION",
]
