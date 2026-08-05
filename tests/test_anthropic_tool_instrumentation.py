"""Stage-5 fix (b): Anthropic local-tool result instrumentation.

Asserts _instrument_local_tools emits local_tool_call_started/finished events
with the SAME field shape OpenAI already emits (openai_provider.py:485-512),
so the frontend reducer treats both providers identically.
"""

from __future__ import annotations

from anthropic.lib.tools import beta_tool

import legal_helper.logging_setup as ls
from legal_helper.providers.anthropic_provider import (
    _instrument_local_tools,
    _thinking_kwarg,
    THINKING_BUDGETS,
)


def _capture(events):
    return ls.workflow_event_sink_var.set(
        lambda rec: events.append((rec.get("event_type"), rec.get("data", {})))
    )


def test_instrumented_tool_emits_started_and_finished():
    events: list[tuple] = []
    tok = _capture(events)
    try:

        @beta_tool
        def add(a: int, b: int) -> str:
            "Add two integers."
            return str(a + b)

        wrapped = _instrument_local_tools([add], provider="anthropic", model="claude-opus-4-8")
        result = wrapped[0].call({"a": 2, "b": 3})
    finally:
        ls.workflow_event_sink_var.reset(tok)

    assert result == "5"
    kinds = [e[0] for e in events]
    assert kinds == ["local_tool_call_started", "local_tool_call_finished"]

    started = events[0][1]
    finished = events[1][1]
    # Same keys OpenAI emits.
    assert started["provider"] == "anthropic"
    assert started["tool_name"] == "add"
    assert started["arguments"] == {"a": 2, "b": 3}
    assert finished["tool_name"] == "add"
    assert finished["output_preview"] == "5"
    assert finished["output_chars"] == 1


def test_instrumented_tool_emits_error_shape_on_exception():
    events: list[tuple] = []
    tok = _capture(events)
    try:

        @beta_tool
        def boom(x: int) -> str:
            "Always fails."
            raise ValueError("nope")

        wrapped = _instrument_local_tools([boom], provider="anthropic", model="claude-opus-4-8")
        try:
            wrapped[0].call({"x": 1})
        except ValueError:
            pass
    finally:
        ls.workflow_event_sink_var.reset(tok)

    kinds = [e[0] for e in events]
    assert kinds == ["local_tool_call_started", "local_tool_call_finished"]
    assert "error" in events[1][1]


def test_dict_tool_defs_pass_through_untouched():
    hosted = {"type": "web_search_20250305", "name": "web_search"}
    out = _instrument_local_tools([hosted], provider="anthropic", model="claude-opus-4-8")
    assert out == [hosted]


def test_thinking_budget_mapping():
    assert _thinking_kwarg("none", 32000) is None
    assert _thinking_kwarg("low", 32000) == {"type": "enabled", "budget_tokens": 2048}
    # budget must stay below max_tokens with headroom
    k = _thinking_kwarg("xhigh", 4096)
    assert k is not None and k["budget_tokens"] < 4096
    assert set(THINKING_BUDGETS) == {"none", "low", "medium", "high", "xhigh"}
