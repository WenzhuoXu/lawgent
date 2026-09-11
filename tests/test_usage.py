"""Cost-center ledger: recording, monthly aggregation, pricing math."""

from __future__ import annotations

import json
from datetime import datetime, timezone

import pytest

from legal_helper import usage as usage_mod
from legal_helper.usage import list_months, month_summary, record_usage


def _this_month() -> str:
    return datetime.now(timezone.utc).strftime("%Y-%m")


def test_record_and_summarize_anthropic(tmp_path):
    record_usage(
        "anthropic",
        "claude-opus-4-8",
        {
            "input_tokens": 1_000_000,
            "output_tokens": 100_000,
            "cache_read_input_tokens": 2_000_000,
            "cache_creation_input_tokens": 400_000,
        },
        state_dir=tmp_path,
    )
    summary = month_summary(state_dir=tmp_path)
    t = summary["totals"]
    assert t["input_tokens"] == 1_000_000
    assert t["output_tokens"] == 100_000
    assert t["cache_read_tokens"] == 2_000_000
    assert t["cache_write_tokens"] == 400_000
    # Anthropic cache buckets are separate from input, so they add up
    assert t["total_tokens"] == 3_500_000
    # 1M*5 + 0.1M*25 + 2M*0.5 + 0.4M*6.25 = 5 + 2.5 + 1 + 2.5 = 11.0
    assert summary["estimated_cost_usd"] == pytest.approx(11.0)
    assert summary["month"] == _this_month()


def test_record_and_summarize_openai_cached_subset(tmp_path):
    record_usage(
        "openai",
        "gpt-5.5",
        {
            # Deliberately under the 272K long-context threshold so this stays a
            # test of the base rates; the surcharge has its own test below.
            "input_tokens": 100_000,
            "output_tokens": 20_000,
            "cached_input_tokens": 40_000,
        },
        state_dir=tmp_path,
    )
    summary = month_summary(state_dir=tmp_path)
    t = summary["totals"]
    assert t["cache_read_tokens"] == 40_000
    # OpenAI cached tokens are a subset of input — not double counted
    assert t["total_tokens"] == 120_000
    assert t["long_context_requests"] == 0
    # 0.06M*5 + 0.04M*0.5 + 0.02M*30 = 0.30 + 0.02 + 0.60 = 0.92
    assert summary["estimated_cost_usd"] == pytest.approx(0.92)


def test_gpt_56_terra_pricing_math(tmp_path):
    record_usage(
        "openai",
        "gpt-5.6-terra",
        {
            "input_tokens": 100_000,
            "output_tokens": 20_000,
            "cached_input_tokens": 40_000,
        },
        state_dir=tmp_path,
    )
    summary = month_summary(state_dir=tmp_path)
    # Verified 2026-09-11: terra is 2.00 / 0.20 cached / 12.00 out.
    # 0.06M*2.00 + 0.04M*0.20 + 0.02M*12.00 = 0.12 + 0.008 + 0.24 = 0.368
    assert summary["estimated_cost_usd"] == pytest.approx(0.368)


def test_gpt_56_family_bills_cache_writes_when_reported(tmp_path):
    # The Responses API does not report cache-write tokens yet; when a record
    # carries them anyway, the 5.6-family cache_write rate (1.25x input) applies.
    record_usage(
        "openai",
        "gpt-5.6-terra",
        {"input_tokens": 100_000, "output_tokens": 0, "cache_creation_input_tokens": 10_000},
        state_dir=tmp_path,
    )
    summary = month_summary(state_dir=tmp_path)
    # 0.1M*2.00 + 0.01M*2.50 = 0.20 + 0.025
    assert summary["estimated_cost_usd"] == pytest.approx(0.225)


def test_gpt_55_never_bills_cache_writes(tmp_path):
    record_usage(
        "openai",
        "gpt-5.5",
        {"input_tokens": 100_000, "output_tokens": 0, "cache_creation_input_tokens": 10_000},
        state_dir=tmp_path,
    )
    summary = month_summary(state_dir=tmp_path)
    # gpt-5.5 has no cache_write rate: 0.1M*5.00 only
    assert summary["estimated_cost_usd"] == pytest.approx(0.5)


def test_openai_fallback_pricing_is_terra():
    assert usage_mod.PROVIDER_FALLBACK_PRICING["openai"] is usage_mod.DEFAULT_PRICING["gpt-5.6-terra"]


def test_unknown_model_falls_back_to_provider_rates(tmp_path):
    record_usage(
        "anthropic",
        "claude-experimental-99",
        {"input_tokens": 1_000_000, "output_tokens": 0},
        state_dir=tmp_path,
    )
    summary = month_summary(state_dir=tmp_path)
    # falls back to the anthropic default rate rather than pricing at $0
    assert summary["estimated_cost_usd"] > 0


def test_month_isolation_and_listing(tmp_path):
    ledger_dir = tmp_path / "usage"
    ledger_dir.mkdir()
    old = {
        "ts": "2026-01-15T00:00:00+00:00",
        "provider": "anthropic",
        "model": "claude-opus-4-8",
        "input_tokens": 500,
        "output_tokens": 100,
        "cache_read_tokens": 0,
        "cache_write_tokens": 0,
    }
    (ledger_dir / "usage-2026-01.jsonl").write_text(json.dumps(old) + "\n", encoding="utf-8")
    record_usage(
        "anthropic", "claude-opus-4-8", {"input_tokens": 42, "output_tokens": 1}, state_dir=tmp_path
    )

    current = month_summary(state_dir=tmp_path)
    assert current["totals"]["input_tokens"] == 42  # January spend not carried over

    january = month_summary("2026-01", state_dir=tmp_path)
    assert january["totals"]["input_tokens"] == 500

    assert list_months(state_dir=tmp_path) == sorted([_this_month(), "2026-01"], reverse=True)


def test_empty_usage_writes_nothing(tmp_path):
    record_usage("anthropic", "claude-opus-4-8", {}, state_dir=tmp_path)
    assert not (tmp_path / "usage").exists()
    summary = month_summary(state_dir=tmp_path)
    assert summary["totals"]["requests"] == 0
    assert summary["estimated_cost_usd"] == 0


def test_malformed_ledger_lines_are_skipped(tmp_path):
    record_usage(
        "anthropic", "claude-opus-4-8", {"input_tokens": 10, "output_tokens": 2}, state_dir=tmp_path
    )
    path = usage_mod._ledger_path(_this_month(), tmp_path)
    with path.open("a", encoding="utf-8") as fh:
        fh.write("not-json\n")
    summary = month_summary(state_dir=tmp_path)
    assert summary["totals"]["requests"] == 1
    assert summary["totals"]["input_tokens"] == 10


def _ledger_lines(tmp_path) -> list[dict]:
    path = usage_mod._ledger_path(_this_month(), tmp_path)
    return [json.loads(line) for line in path.read_text(encoding="utf-8").splitlines() if line]


def test_run_context_attribution_from_contextvars(tmp_path):
    from legal_helper.logging_setup import agent_name_var, run_id_var
    from legal_helper.usage import usage_run_context

    run_token = run_id_var.set("run-123")
    agent_token = agent_name_var.set("legal-search")
    try:
        with usage_run_context(chat_id="chat-9"):
            record_usage(
                "openai", "gpt-5.6-terra", {"input_tokens": 1}, state_dir=tmp_path
            )
    finally:
        agent_name_var.reset(agent_token)
        run_id_var.reset(run_token)

    (rec,) = _ledger_lines(tmp_path)
    assert rec["context"] == {"chat_id": "chat-9", "run_id": "run-123", "agent": "legal-search"}


def test_explicit_context_wins_over_ambient(tmp_path):
    from legal_helper.usage import usage_run_context

    with usage_run_context(chat_id="chat-1", agent="orchestrator"):
        record_usage(
            "anthropic",
            "claude-opus-4-8",
            {"input_tokens": 1},
            state_dir=tmp_path,
            context={"agent": "cite-check"},
        )

    (rec,) = _ledger_lines(tmp_path)
    assert rec["context"]["chat_id"] == "chat-1"
    assert rec["context"]["agent"] == "cite-check"


def test_nested_run_context_merges_and_restores(tmp_path):
    from legal_helper.usage import usage_context_var, usage_run_context

    with usage_run_context(chat_id="chat-1"):
        with usage_run_context(run_id="run-2"):
            assert usage_context_var.get() == {"chat_id": "chat-1", "run_id": "run-2"}
        assert usage_context_var.get() == {"chat_id": "chat-1"}
    assert usage_context_var.get() is None


def test_pricing_env_override(tmp_path, monkeypatch):
    monkeypatch.setenv(
        "LEGAL_HELPER_PRICING_JSON",
        json.dumps({"gpt-5.5": {"input": 2.0, "output": 20.0, "cached_input": 0.2}}),
    )
    record_usage(
        "openai", "gpt-5.5", {"input_tokens": 100_000, "output_tokens": 0}, state_dir=tmp_path
    )
    summary = month_summary(state_dir=tmp_path)
    assert summary["estimated_cost_usd"] == pytest.approx(0.2)


def test_openai_long_context_surcharge_is_priced():
    """>272K input bills 2x input / 1.5x output for the whole request."""
    from legal_helper.usage import DEFAULT_PRICING, _cost_usd, is_long_context

    pricing = dict(DEFAULT_PRICING)
    under = {
        "provider": "openai",
        "model": "gpt-5.6-terra",
        "input_tokens": 200_000,
        "output_tokens": 10_000,
        "cache_read_tokens": 0,
    }
    over = {**under, "input_tokens": 400_000}

    assert is_long_context(under) is False
    assert is_long_context(over) is True

    # 200k * 2.00 + 10k * 12.00 per MTok
    assert _cost_usd(under, pricing) == pytest.approx(0.4 + 0.12)
    # 400k * (2.00*2) + 10k * (12.00*1.5)
    assert _cost_usd(over, pricing) == pytest.approx(1.6 + 0.18)

    # Anthropic never carries the surcharge.
    anthropic = {
        "provider": "anthropic",
        "model": "claude-opus-5",
        "input_tokens": 400_000,
        "output_tokens": 1_000,
        "cache_read_tokens": 0,
        "cache_write_tokens": 0,
    }
    assert is_long_context(anthropic) is False
    assert _cost_usd(anthropic, pricing) == pytest.approx(2.0 + 0.025)


def test_month_summary_counts_long_context_requests(tmp_path):
    from legal_helper.usage import month_summary, record_usage

    state = tmp_path / "state"
    for tokens in (100_000, 300_000, 500_000):
        record_usage(
            "openai",
            "gpt-5.6-terra",
            {"input_tokens": tokens, "output_tokens": 100},
            state_dir=state,
        )
    summary = month_summary(state_dir=state)
    assert summary["totals"]["requests"] == 3
    assert summary["totals"]["long_context_requests"] == 2
    terra = next(b for b in summary["by_model"] if b["model"] == "gpt-5.6-terra")
    assert terra["long_context_requests"] == 2
