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
    "claude-opus-5-5": 1_000_000,
    "claude-opus-5": 1_000_000,
    "claude-opus-4-8": 1_000_000,
    "claude-opus-4-7": 1_000_000,
    "claude-opus-4-6": 1_000_000,
    "claude-sonnet-4-6": 1_000_000,
    "claude-sonnet-5": 1_000_000,
    "claude-haiku-4-5": 200_000,
    "gpt-6-astra": 1_050_000,
    "gpt-6-sol": 1_050_000,
    "gpt-6-luna": 1_050_000,
    "gpt-5.6-terra": 1_000_000,
    "gpt-5.6-sol": 1_000_000,
    "gpt-5.6-luna": 1_000_000,
    # Bare alias routes to Sol (CLAUDE.md); priced/windowed the same.
    "gpt-5.6": 1_000_000,
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
# Output allowance removed from the window before any input-side threshold is
# computed. Mirrors ``Settings.max_tokens``; used when that is unreadable.
OUTPUT_RESERVE_TOKENS = 32_000
# Headroom kept below every ceiling. A request that lands exactly on a pricing
# cliff pays the surcharge, so aim short of it rather than at it.
COMPACTION_BUFFER_TOKENS = 13_000
# How the turn ceiling is divided. Measured 2026-09-21 on this repo: a skill
# agent's fixed overhead is ~21K tokens (5.6K system prompt + 15.4K of schemas
# for 58 tools) and the orchestrator's is ~15K. Against a 259K ceiling that
# leaves ~238K, and the split below reserves ~78K for the transcript digest and
# the rest for tool results and the user's message. Tool results are not given
# a fixed share: `turn_compaction` clears the oldest ones when a request nears
# this ceiling. Most of a research turn's input is tool results, not
# transcript, so the digest takes the smaller share.
DIGEST_SHARE_OF_TURN_CEILING = 0.3
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


def effective_context_window_for(settings: "Settings") -> int:
    """Input-side window: the model's window minus this turn's output ceiling.

    A provider window is shared between input and output, and compaction can
    only yield on the input side, so the denominator for every threshold below
    has the output allowance removed first. With ``max_tokens`` at 32000 a
    1M-window model has 968K of input to work with, not 1M.
    """
    window = context_window_for(settings)
    reserve = int(getattr(settings, "max_tokens", OUTPUT_RESERVE_TOKENS) or OUTPUT_RESERVE_TOKENS)
    return max(1_000, window - min(reserve, window - 1_000))


def cost_ceiling_for(settings: "Settings") -> Optional[int]:
    """Input-token count this provider starts charging a surcharge above.

    OpenAI bills a request whose input exceeds 272K at 2x input and 1.5x
    output — for the whole request, cached tokens included. On the 2026-09
    ledger 40 of 667 requests crossed it at a mean input of 646K tokens and
    carried ~55% of the month's spend, because the only limit in the harness
    was a window fraction that sits at 750K for a 1M-window model: the pricing
    cliff and the compaction trigger were unrelated numbers that never met.
    Returns None for providers with no such cliff.
    """
    try:
        if getattr(settings, "provider", None) != "openai":
            return None
    except Exception:  # noqa: BLE001
        return None
    from .usage import OPENAI_LONG_CONTEXT_THRESHOLD

    return max(1_000, OPENAI_LONG_CONTEXT_THRESHOLD - COMPACTION_BUFFER_TOKENS)


def turn_input_ceiling_for(settings: "Settings") -> int:
    """Target maximum input tokens for one request to this model.

    The smaller of "a tier-appropriate share of the input window" and "below
    the provider's pricing cliff". This is the budget every other limit is
    carved out of, and the level at which the tool loop clears old results.
    """
    effective = effective_context_window_for(settings)
    ceiling = max(1_000, int(compaction_threshold_for(settings) * effective))
    cliff = cost_ceiling_for(settings)
    if cliff is not None:
        ceiling = min(ceiling, cliff)
    return ceiling


def context_budget_for(settings: "Settings") -> int:
    """Token budget for the per-turn within-chat digest.

    Scales with the model actually serving the turn: ``fraction × window``,
    floored at ``chat_context_token_budget`` so the configured value can only
    ever raise the budget, never shrink it below what previous runs used, and
    capped at ``DIGEST_SHARE_OF_TURN_CEILING`` of the turn ceiling so the
    digest alone cannot walk a request over the pricing cliff. Unclamped,
    0.25 × 1M handed the digest 250K tokens — most of the way to the cliff
    before a single tool had run.
    """
    try:
        fraction = float(
            getattr(settings, "chat_context_window_fraction", DEFAULT_CONTEXT_WINDOW_FRACTION)
        )
    except (TypeError, ValueError):
        fraction = DEFAULT_CONTEXT_WINDOW_FRACTION
    fraction = min(max(fraction, 0.0), 0.9)
    floor = int(getattr(settings, "chat_context_token_budget", DEFAULT_CONTEXT_BUDGET) or 0)
    share = int(DIGEST_SHARE_OF_TURN_CEILING * turn_input_ceiling_for(settings))
    return max(floor, min(int(fraction * context_window_for(settings)), share), 1_000)


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


def truncate_to_tokens(text: str, max_tokens: int, *, keep: str = "head") -> str:
    """Truncate ``text`` so its estimate is ≤ ``max_tokens`` (char-proportional).

    ``keep="tail"`` retains the end instead of the start — the right choice for
    a result whose useful part is last (a command's final output, a log).
    """
    if estimate_tokens(text) <= max_tokens:
        return text
    # Estimate is monotonic in length; binary-search the cut point.
    lo, hi = 0, len(text)
    while lo < hi:
        mid = (lo + hi + 1) // 2
        window = text[-mid:] if keep == "tail" else text[:mid]
        if estimate_tokens(window) <= max_tokens - 1:
            lo = mid
        else:
            hi = mid - 1
    if keep == "tail":
        return "…" + text[-lo:].lstrip()
    return text[:lo].rstrip() + "…"


# Back-compat alias for the original private name.
_truncate_to_tokens = truncate_to_tokens


@dataclass(frozen=True)
class CompactionDecision:
    """Why the chat layer did or did not compact, for the event log."""

    should_compact: bool
    observed_tokens: int
    estimated_tokens: int
    reported_tokens: Optional[int]
    token_source: str  # "provider_usage" | "estimate"
    ceiling_tokens: int
    cost_ceiling_tokens: Optional[int]


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


# Compaction thresholds are model-strength dependent, not universal. Measured
# on long-horizon search (arXiv 2606.29718): for strong agents, sub-agent
# *isolation* beat summarization by a wide margin (54.0% vs 35.0% on
# BrowseComp), while for weaker agents keep-latest-with-summarization won
# (44.6%). This harness already isolates specialist work in sub-agents, so a
# frontier model should ride further before folding turns into a lossy summary;
# a fast-tier model, which degrades sooner and has a smaller window, should
# summarize early. A single 0.6 for both was leaving frontier quality on the
# table and compacting fast-tier models too late.
_FAST_TIER_MODELS = frozenset(
    {
        "claude-haiku-4-5",
        "claude-haiku-3-5",
        "gpt-6-luna",
        "gpt-5.6-luna",
        "gpt-5.4-mini",
        "gpt-5.4-nano",
    }
)
FRONTIER_COMPACTION_THRESHOLD = 0.75
FAST_COMPACTION_THRESHOLD = 0.55


def is_fast_tier(model: Optional[str]) -> bool:
    """True for the cheap/short-window tier that should summarize early."""
    if not model:
        return False
    name = model.strip().lower()
    if name in _FAST_TIER_MODELS:
        return True
    return any(tok in name for tok in ("haiku", "luna", "mini", "nano"))


def compaction_threshold_for(settings: "Settings") -> float:
    """Window fraction at which to start compacting, by model tier.

    An explicit ``chat_compaction_threshold`` in config/env always wins; the
    tier default applies only when the setting is left at its sentinel.
    """
    configured = getattr(settings, "chat_compaction_threshold", None)
    if configured is not None and configured > 0:
        return float(configured)
    try:
        model = settings.model_for_provider()
    except Exception:  # noqa: BLE001 — never let tiering break a turn
        model = None
    return FAST_COMPACTION_THRESHOLD if is_fast_tier(model) else FRONTIER_COMPACTION_THRESHOLD


def compaction_decision(
    messages: Iterable["ChatMessage"],
    settings: "Settings",
    *,
    threshold: Optional[float] = None,
    summary_tokens: int = 0,
    provider_usage_tokens: Optional[int] = None,
) -> "CompactionDecision":
    """Whether to compact, and on what evidence.

    ``provider_usage_tokens`` is the input-token count the provider reported
    for the last turn of this chat. When present it is authoritative: it counts
    the system prompt, the tool schemas, attachments and tool results, none of
    which the local estimate can see, and it is already captured for the UI's
    context widget. The estimate remains the fallback and is always reported so
    the two can be compared.
    """
    if threshold is None or threshold <= 0:
        threshold = compaction_threshold_for(settings)
    estimated = summary_tokens + sum(estimate_tokens(m.content) for m in messages)
    reported = int(provider_usage_tokens or 0)
    source = "provider_usage" if reported > 0 else "estimate"
    observed = reported if reported > 0 else estimated
    # Two ceilings: the tier-derived share of the input window, and the pricing
    # cliff. A pinned `threshold` overrides only the first.
    ceiling = max(1_000, int(threshold * effective_context_window_for(settings)))
    cliff = cost_ceiling_for(settings)
    if cliff is not None:
        ceiling = min(ceiling, cliff)
    return CompactionDecision(
        should_compact=observed >= ceiling,
        observed_tokens=observed,
        estimated_tokens=estimated,
        reported_tokens=reported or None,
        token_source=source,
        ceiling_tokens=ceiling,
        cost_ceiling_tokens=cliff,
    )


def should_compact(
    messages: Iterable["ChatMessage"],
    settings: "Settings",
    *,
    threshold: Optional[float] = None,
    summary_tokens: int = 0,
    provider_usage_tokens: Optional[int] = None,
) -> bool:
    """True when this chat's context has reached its compaction ceiling.

    Compact well before the limit so quality doesn't degrade near it. See
    ``compaction_decision`` for the evidence behind the answer.
    """
    return compaction_decision(
        messages,
        settings,
        threshold=threshold,
        summary_tokens=summary_tokens,
        provider_usage_tokens=provider_usage_tokens,
    ).should_compact


__all__ = [
    "estimate_tokens",
    "truncate_to_tokens",
    "context_window_for",
    "effective_context_window_for",
    "cost_ceiling_for",
    "turn_input_ceiling_for",
    "context_budget_for",
    "per_message_cap_for",
    "select_recent_within_budget",
    "render_recent",
    "compact_verification_appendix",
    "is_fast_tier",
    "compaction_threshold_for",
    "compaction_decision",
    "should_compact",
    "CompactionDecision",
    "WindowedContext",
    "DEFAULT_CONTEXT_BUDGET",
    "DEFAULT_CONTEXT_WINDOW_FRACTION",
    "OUTPUT_RESERVE_TOKENS",
    "COMPACTION_BUFFER_TOKENS",
    "DIGEST_SHARE_OF_TURN_CEILING",
]
