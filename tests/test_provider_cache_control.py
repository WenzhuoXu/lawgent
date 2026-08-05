"""Anthropic prompt-cache breakpoints, per-turn usage accumulation, and the
OpenAI output ceiling — all against mocked SDK surfaces."""

from __future__ import annotations

import json
from types import SimpleNamespace
from unittest.mock import patch

from legal_helper.config import load_settings
from legal_helper.providers.anthropic_provider import (
    AnthropicProvider,
    _cached_system,
    _tools_with_cache_breakpoint,
)
from legal_helper.providers.openai_provider import OpenAIProvider
from legal_helper.usage import month_summary


def _usage(inp=0, out=0, cread=0, cwrite=0) -> SimpleNamespace:
    return SimpleNamespace(
        input_tokens=inp,
        output_tokens=out,
        cache_read_input_tokens=cread,
        cache_creation_input_tokens=cwrite,
    )


def _text_block(text: str) -> SimpleNamespace:
    return SimpleNamespace(type="text", text=text)


def _tool_use_block(name: str, block_id: str) -> SimpleNamespace:
    return SimpleNamespace(type="tool_use", name=name, input={"q": "x"}, id=block_id, text=None)


class _FakeMessageRunner:
    """Non-streaming tool runner: yields one message per model turn."""

    def __init__(self, messages):
        self.messages = messages

    def __iter__(self):
        yield from self.messages

    def until_done(self):
        return self.messages[-1]


class _FakeTurnStream:
    def __init__(self, events, final_message):
        self.events = events
        self.final_message = final_message

    def __iter__(self):
        yield from self.events

    def get_final_message(self):
        return self.final_message


class _FakeStreamRunner:
    def __init__(self, streams):
        self.streams = streams

    def __iter__(self):
        yield from self.streams

    def until_done(self):
        return self.streams[-1].final_message


def _provider(tmp_path, runner, captured: dict) -> AnthropicProvider:
    provider = AnthropicProvider.__new__(AnthropicProvider)
    provider.settings = SimpleNamespace(max_tokens=1000, max_iterations=3, state_dir=tmp_path)
    provider.model = "claude-test"

    def tool_runner(**kwargs):
        captured.update(kwargs)
        return runner

    provider.client = SimpleNamespace(
        beta=SimpleNamespace(messages=SimpleNamespace(tool_runner=tool_runner))
    )
    return provider


# ----- cache_control breakpoints -------------------------------------------


def test_cached_system_wraps_prompt_with_ephemeral_breakpoint():
    assert _cached_system("You are a PRC legal assistant.") == [
        {
            "type": "text",
            "text": "You are a PRC legal assistant.",
            "cache_control": {"type": "ephemeral"},
        }
    ]
    # Empty prompts pass through untouched (an empty text block would 400).
    assert _cached_system("") == ""


def test_tools_breakpoint_annotates_last_dict_tool_without_mutating_input():
    web = {"type": "web_search_20250305", "name": "web_search"}
    tools = [{"name": "a"}, web]
    out = _tools_with_cache_breakpoint(tools)
    assert out[-1]["cache_control"] == {"type": "ephemeral"}
    assert "cache_control" not in web  # original never mutated
    assert out[0] is tools[0]  # non-last tools untouched
    assert _tools_with_cache_breakpoint([]) == []


def test_tools_breakpoint_copies_beta_function_tool():
    from anthropic.lib.tools._beta_functions import BetaFunctionTool

    def lookup(q: str) -> str:
        """Look up a statute."""
        return q

    tool = BetaFunctionTool(lookup)
    out = _tools_with_cache_breakpoint([tool])
    assert out[0] is not tool
    assert out[0].to_dict()["cache_control"] == {"type": "ephemeral"}
    assert tool._cache_control is None  # shared tool object never mutated
    assert out[0].call({"q": "民法典"}) == "民法典"  # copy still executes


def test_run_and_stream_kwargs_carry_cache_breakpoints(tmp_path):
    final = SimpleNamespace(content=[_text_block("ok")], usage=_usage(1, 1), id="m1")
    captured: dict = {}
    provider = _provider(tmp_path, _FakeMessageRunner([final]), captured)
    provider.run("sys prompt", [{"role": "user", "content": "hi"}], tools=[{"name": "t"}])
    assert captured["system"][0]["cache_control"] == {"type": "ephemeral"}
    assert captured["tools"][-1]["cache_control"] == {"type": "ephemeral"}

    captured_stream: dict = {}
    stream_runner = _FakeStreamRunner([_FakeTurnStream([], final)])
    provider2 = _provider(tmp_path, stream_runner, captured_stream)
    list(provider2.stream("sys prompt", [{"role": "user", "content": "hi"}], tools=[{"name": "t"}]))
    assert captured_stream["system"][0]["cache_control"] == {"type": "ephemeral"}
    assert captured_stream["tools"][-1]["cache_control"] == {"type": "ephemeral"}


# ----- per-turn usage + tool_use accumulation ------------------------------


def test_run_accumulates_usage_and_tool_calls_across_turns(tmp_path):
    turn1 = SimpleNamespace(
        content=[_tool_use_block("search_article", "tu_1")],
        usage=_usage(inp=100, out=10, cread=5000, cwrite=200),
        id="m1",
    )
    turn2 = SimpleNamespace(
        content=[_text_block("答案")],
        usage=_usage(inp=40, out=25, cread=5200),
        id="m2",
        stop_reason="end_turn",
    )
    provider = _provider(tmp_path, _FakeMessageRunner([turn1, turn2]), {})

    result = provider.run("sys", [{"role": "user", "content": "hi"}], tools=[])

    # Cache reads are verified through usage.cache_read_input_tokens.
    assert result.usage["cache_read_input_tokens"] == 10_200
    assert result.usage["cache_creation_input_tokens"] == 200
    assert result.usage["input_tokens"] == 140
    assert result.usage["output_tokens"] == 35
    # Intermediate turns' tool_use blocks are no longer dropped.
    assert [c.name for c in result.tool_calls] == ["search_article"]
    assert result.text == "答案"

    # The ledger sees the accumulated totals (cache buckets now populate).
    summary = month_summary(state_dir=tmp_path)
    assert summary["totals"]["cache_read_tokens"] == 10_200
    assert summary["totals"]["cache_write_tokens"] == 200
    assert summary["totals"]["input_tokens"] == 140


def test_stream_accumulates_usage_per_turn_without_double_count(tmp_path):
    delta = SimpleNamespace(
        type="content_block_delta", delta=SimpleNamespace(type="text_delta", text="hello")
    )
    turn1_final = SimpleNamespace(content=[], usage=_usage(inp=50, cread=3000), id="m1")
    turn2_final = SimpleNamespace(
        content=[_text_block("hello")], usage=_usage(inp=20, out=8, cread=3100), id="m2"
    )
    runner = _FakeStreamRunner(
        [_FakeTurnStream([], turn1_final), _FakeTurnStream([delta], turn2_final)]
    )
    provider = _provider(tmp_path, runner, {})

    events = list(provider.stream("sys", [{"role": "user", "content": "hi"}], tools=[]))

    done = events[-1]
    assert done.kind == "done"
    # Per-turn sums; the final message (turn 2) is not counted twice.
    assert done.data["usage"]["cache_read_input_tokens"] == 6_100
    assert done.data["usage"]["input_tokens"] == 70
    assert done.data["usage"]["output_tokens"] == 8


# ----- OpenAI output ceiling ------------------------------------------------


def test_openai_run_passes_max_output_tokens():
    settings = load_settings(refresh=True)
    provider = OpenAIProvider(settings)
    captured: list[dict] = []

    def fake_create(**kwargs):
        captured.append(kwargs)
        return SimpleNamespace(
            id="resp_1",
            output=[SimpleNamespace(type="message", content=[SimpleNamespace(text="done")])],
            output_text="done",
            usage=None,
        )

    with patch.object(provider.client.responses, "create", side_effect=fake_create):
        provider.tool_runner("sys", [{"role": "user", "content": "hi"}], [], max_iterations=1)

    assert captured[0]["max_output_tokens"] == settings.max_tokens


def test_openai_stream_passes_max_output_tokens():
    settings = load_settings(refresh=True)
    provider = OpenAIProvider(settings)
    captured: list[dict] = []

    class _FakeCM:
        def __enter__(self):
            final = SimpleNamespace(id="resp_1", output=[], usage=None)
            return iter([SimpleNamespace(type="response.completed", response=final)])

        def __exit__(self, *args):
            return False

    def fake_stream(**kwargs):
        captured.append(kwargs)
        return _FakeCM()

    with patch.object(provider.client.responses, "stream", side_effect=fake_stream):
        list(provider.stream("sys", [{"role": "user", "content": "hi"}], []))

    assert captured[0]["max_output_tokens"] == settings.max_tokens
