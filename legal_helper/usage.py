"""Monthly usage ledger + cost estimation (the web UI "cost center").

Every provider turn — both providers, run/tool_runner/stream paths — records
its token usage here.  Records are appended to
``state/usage/usage-YYYY-MM.jsonl``; keying files by calendar month makes the
summary reset monthly for free while keeping history queryable.

Billing semantics differ per provider and the cost math accounts for it:

* Anthropic reports ``input_tokens`` EXCLUDING the cache fields —
  ``cache_read_input_tokens`` and ``cache_creation_input_tokens`` are separate
  buckets billed at ~0.1x and 1.25x the input rate respectively.
* OpenAI reports ``input_tokens`` INCLUDING the cached portion
  (``input_tokens_details.cached_tokens``), billed at the discounted cached
  rate.

Recording must never break a chat turn: every public entry point swallows its
own errors.
"""

from __future__ import annotations

import json
import os
import threading
from contextlib import contextmanager
from contextvars import ContextVar
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Iterator, Optional

_LOCK = threading.Lock()

# Per-run cost attribution: the server/workflow layer sets chat_id / run_id /
# agent here (same pattern as logging_setup.agent_name_var) and every
# record_usage call inside that scope carries them in ``context`` — so the
# cost center can answer "what did this chat / this run cost".
usage_context_var: ContextVar[Optional[dict[str, Any]]] = ContextVar(
    "legal_helper_usage_context", default=None
)


@contextmanager
def usage_run_context(**fields: Any) -> Iterator[None]:
    """Scope usage records to a chat/run (e.g. chat_id=..., run_id=..., agent=...).

    Nested scopes merge over the outer scope; ``None`` values are dropped.
    """
    merged = dict(usage_context_var.get() or {})
    merged.update({k: v for k, v in fields.items() if v is not None})
    token = usage_context_var.set(merged or None)
    try:
        yield
    finally:
        usage_context_var.reset(token)


def _ambient_context() -> dict[str, Any]:
    """Attribution from the usage contextvar + the logging run/agent vars."""
    ctx = dict(usage_context_var.get() or {})
    try:
        from .logging_setup import agent_name_var, run_id_var

        run_id = run_id_var.get()
        if run_id and "run_id" not in ctx:
            ctx["run_id"] = run_id
        agent = agent_name_var.get()
        if agent and "agent" not in ctx:
            ctx["agent"] = agent
    except Exception:
        pass
    return ctx

# USD per 1M tokens.  ``cache_read`` / ``cache_write`` are Anthropic buckets;
# ``cached_input`` is OpenAI's discounted-cached-input rate.  Override or
# extend without a code change via LEGAL_HELPER_PRICING_JSON (same shape).
# Rates verified against official pricing pages 2026-09-11 (Anthropic:
# platform.claude.com/docs/en/about-claude/pricing; OpenAI:
# developers.openai.com/api/docs/pricing).
# Anthropic: cache read = 0.1x input, cache write (5-minute TTL) = 1.25x input.
# claude-sonnet-5 stays at 2.00/10.00 — the increase to 3.00/15.00 scheduled for
# 2026-09-01 was cancelled and the introductory rate is now standard.
DEFAULT_PRICING: dict[str, dict[str, float]] = {
    "claude-opus-5": {"input": 5.00, "output": 25.00, "cache_read": 0.50, "cache_write": 6.25},
    "claude-opus-4-8": {"input": 5.00, "output": 25.00, "cache_read": 0.50, "cache_write": 6.25},
    "claude-opus-4-7": {"input": 5.00, "output": 25.00, "cache_read": 0.50, "cache_write": 6.25},
    "claude-opus-4-6": {"input": 5.00, "output": 25.00, "cache_read": 0.50, "cache_write": 6.25},
    "claude-sonnet-5": {"input": 2.00, "output": 10.00, "cache_read": 0.20, "cache_write": 2.50},
    "claude-haiku-4-5": {"input": 1.00, "output": 5.00, "cache_read": 0.10, "cache_write": 1.25},
    # Fable/Mythos 5.1 read cache at 0.025x input (not the usual 0.1x).
    "claude-fable-5-1": {"input": 10.00, "output": 50.00, "cache_read": 0.25, "cache_write": 12.50},
    "claude-mythos-5-1": {"input": 10.00, "output": 50.00, "cache_read": 0.25, "cache_write": 12.50},
    # gpt-5.5 publishes no cache-write charge.
    "gpt-5.5": {"input": 5.00, "output": 30.00, "cached_input": 0.50},
    # gpt-5.6 family. Unlike gpt-5.5 the 5.6 family BILLS CACHE WRITES at 1.25x
    # input; the Responses API usage object does not yet report a cache-write
    # token count (input_tokens_details carries only cached_tokens), so the rate
    # sits here unused until the SDK exposes one — see the openai branch of
    # _cost_usd. Bare "gpt-5.6" routes to Sol; only named variants are used.
    "gpt-5.6-terra": {"input": 2.00, "output": 12.00, "cached_input": 0.20, "cache_write": 2.50},
    "gpt-5.6-sol": {"input": 4.00, "output": 20.00, "cached_input": 0.40, "cache_write": 5.00},
    "gpt-5.6-luna": {"input": 0.20, "output": 1.20, "cached_input": 0.02, "cache_write": 0.25},
}

# OpenAI bills a prompt whose input exceeds this threshold at 2x input (cached
# input included) and 1.5x output, for the whole request. Every current
# gpt-5.5/5.6 model carries it. Modelling it matters: through 2026-08 and
# 2026-09 roughly a quarter of gpt-5.5 and terra requests crossed the line, and
# leaving the term out under-reported the month by 21-31%.
OPENAI_LONG_CONTEXT_THRESHOLD = 272_000
OPENAI_LONG_CONTEXT_INPUT_MULT = 2.0
OPENAI_LONG_CONTEXT_OUTPUT_MULT = 1.5


def is_long_context(rec: dict[str, Any]) -> bool:
    """True when an OpenAI record crosses the long-context surcharge threshold."""
    if rec.get("provider") != "openai":
        return False
    return int(rec.get("input_tokens") or 0) > OPENAI_LONG_CONTEXT_THRESHOLD

# Used when a model has no pricing entry, so the estimate stays an estimate
# instead of silently reading as zero spend.
PROVIDER_FALLBACK_PRICING: dict[str, dict[str, float]] = {
    "anthropic": DEFAULT_PRICING["claude-opus-4-8"],
    "openai": DEFAULT_PRICING["gpt-5.6-terra"],
}


def _pricing() -> dict[str, dict[str, float]]:
    table = dict(DEFAULT_PRICING)
    raw = os.getenv("LEGAL_HELPER_PRICING_JSON")
    if raw:
        try:
            override = json.loads(raw)
            for model, rates in override.items():
                merged = dict(table.get(model, {}))
                merged.update({k: float(v) for k, v in rates.items()})
                table[model] = merged
        except Exception:
            pass
    return table


def _default_state_dir() -> Path:
    root = Path(__file__).resolve().parent.parent
    return root / os.getenv("LEGAL_HELPER_STATE_DIR", "state")


def _ledger_dir(state_dir: Optional[Path]) -> Path:
    return Path(state_dir or _default_state_dir()) / "usage"


def _month_key(dt: Optional[datetime] = None) -> str:
    return (dt or datetime.now(timezone.utc)).strftime("%Y-%m")


def _ledger_path(month: str, state_dir: Optional[Path]) -> Path:
    return _ledger_dir(state_dir) / f"usage-{month}.jsonl"


def record_usage(
    provider: str,
    model: str,
    usage: dict[str, Any],
    *,
    state_dir: Optional[Path] = None,
    context: Optional[dict[str, Any]] = None,
) -> None:
    """Append one provider turn's usage to the current month's ledger."""
    if not usage:
        return
    try:
        record: dict[str, Any] = {
            "ts": datetime.now(timezone.utc).isoformat(timespec="seconds"),
            "provider": provider,
            "model": model,
            "input_tokens": int(usage.get("input_tokens") or 0),
            "output_tokens": int(usage.get("output_tokens") or 0),
            # Anthropic buckets
            "cache_read_tokens": int(
                usage.get("cache_read_input_tokens") or usage.get("cached_input_tokens") or 0
            ),
            "cache_write_tokens": int(usage.get("cache_creation_input_tokens") or 0),
        }
        ctx = _ambient_context()
        if context:
            ctx.update(context)
        if ctx:
            record["context"] = ctx
        path = _ledger_path(_month_key(), state_dir)
        line = json.dumps(record, ensure_ascii=False) + "\n"
        with _LOCK:
            path.parent.mkdir(parents=True, exist_ok=True)
            with path.open("a", encoding="utf-8") as fh:
                fh.write(line)
    except Exception:
        # A bookkeeping failure must never fail the legal answer.
        pass


def _cost_usd(rec: dict[str, Any], pricing: dict[str, dict[str, float]]) -> float:
    rates = pricing.get(rec.get("model", "")) or PROVIDER_FALLBACK_PRICING.get(
        rec.get("provider", ""), {}
    )
    if not rates:
        return 0.0
    inp = rec.get("input_tokens", 0)
    out = rec.get("output_tokens", 0)
    cread = rec.get("cache_read_tokens", 0)
    cwrite = rec.get("cache_write_tokens", 0)
    if rec.get("provider") == "openai":
        # cached tokens are a discounted subset of input_tokens
        cached_rate = rates.get("cached_input", rates["input"] * 0.1)
        input_rate = rates["input"]
        output_rate = rates["output"]
        cwrite_rate = rates.get("cache_write")
        if is_long_context(rec):
            input_rate *= OPENAI_LONG_CONTEXT_INPUT_MULT
            cached_rate *= OPENAI_LONG_CONTEXT_INPUT_MULT
            output_rate *= OPENAI_LONG_CONTEXT_OUTPUT_MULT
            if cwrite_rate:
                cwrite_rate *= OPENAI_LONG_CONTEXT_INPUT_MULT
        cost = (
            max(inp - cread, 0) * input_rate
            + cread * cached_rate
            + out * output_rate
        )
        # gpt-5.6 family bills cache writes (1.25x input). The Responses API
        # does not report cache-write tokens yet, so cwrite stays 0 in
        # practice; once the SDK exposes the detail field the term activates.
        if cwrite_rate and cwrite:
            cost += cwrite * cwrite_rate
    else:
        cost = (
            inp * rates["input"]
            + cread * rates.get("cache_read", rates["input"] * 0.1)
            + cwrite * rates.get("cache_write", rates["input"] * 1.25)
            + out * rates["output"]
        )
    return cost / 1_000_000


def list_months(*, state_dir: Optional[Path] = None) -> list[str]:
    d = _ledger_dir(state_dir)
    if not d.is_dir():
        return []
    months = []
    for p in d.glob("usage-*.jsonl"):
        months.append(p.stem.replace("usage-", ""))
    return sorted(months, reverse=True)


def month_summary(
    month: Optional[str] = None, *, state_dir: Optional[Path] = None
) -> dict[str, Any]:
    """Aggregate one month's ledger (default: current month)."""
    month = month or _month_key()
    pricing = _pricing()
    totals = {
        "input_tokens": 0,
        "output_tokens": 0,
        "cache_read_tokens": 0,
        "cache_write_tokens": 0,
        "total_tokens": 0,
        "requests": 0,
        "long_context_requests": 0,
    }
    by_model: dict[tuple[str, str], dict[str, Any]] = {}
    cost = 0.0
    path = _ledger_path(month, state_dir)
    if path.is_file():
        with path.open(encoding="utf-8") as fh:
            for line in fh:
                line = line.strip()
                if not line:
                    continue
                try:
                    rec = json.loads(line)
                except Exception:
                    continue
                key = (rec.get("provider", "?"), rec.get("model", "?"))
                bucket = by_model.setdefault(
                    key,
                    {
                        "provider": key[0],
                        "model": key[1],
                        "input_tokens": 0,
                        "output_tokens": 0,
                        "cache_read_tokens": 0,
                        "cache_write_tokens": 0,
                        "requests": 0,
                        "long_context_requests": 0,
                        "estimated_cost_usd": 0.0,
                    },
                )
                for k in ("input_tokens", "output_tokens", "cache_read_tokens", "cache_write_tokens"):
                    v = int(rec.get(k) or 0)
                    bucket[k] += v
                    totals[k] += v
                bucket["requests"] += 1
                totals["requests"] += 1
                if is_long_context(rec):
                    bucket["long_context_requests"] += 1
                    totals["long_context_requests"] += 1
                rec_cost = _cost_usd(rec, pricing)
                bucket["estimated_cost_usd"] += rec_cost
                cost += rec_cost
    # Anthropic cache tokens are separate buckets; OpenAI cached tokens are a
    # subset of input.  Summing input+output+anthropic cache buckets gives an
    # honest "tokens processed" figure for both.
    totals["total_tokens"] = (
        totals["input_tokens"]
        + totals["output_tokens"]
        + sum(
            b["cache_read_tokens"] + b["cache_write_tokens"]
            for b in by_model.values()
            if b["provider"] != "openai"
        )
    )
    return {
        "month": month,
        "months": list_months(state_dir=state_dir),
        "totals": totals,
        "estimated_cost_usd": round(cost, 4),
        "by_model": sorted(
            (
                {**b, "estimated_cost_usd": round(b["estimated_cost_usd"], 4)}
                for b in by_model.values()
            ),
            key=lambda b: -b["estimated_cost_usd"],
        ),
        "note": "Estimated from list prices; ledger resets each calendar month.",
    }
