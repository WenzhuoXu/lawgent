from __future__ import annotations

from types import SimpleNamespace

from legal_helper.providers.anthropic_provider import AnthropicProvider


class _FakeStream:
    def __init__(self, events):
        self.events = events

    def __iter__(self):
        yield from self.events


class _FakeRunner:
    def __init__(self, streams, final):
        self.streams = streams
        self.final = final

    def __iter__(self):
        yield from self.streams

    def until_done(self):
        return self.final


def _provider_for_runner(runner: _FakeRunner) -> AnthropicProvider:
    provider = AnthropicProvider.__new__(AnthropicProvider)
    provider.settings = SimpleNamespace(max_tokens=1000, max_iterations=1)
    provider.model = "claude-test"
    provider.client = SimpleNamespace(
        beta=SimpleNamespace(
            messages=SimpleNamespace(tool_runner=lambda **kwargs: runner),
        )
    )
    return provider


def test_anthropic_stream_iterates_nested_message_stream_events():
    event = SimpleNamespace(
        type="content_block_delta",
        delta=SimpleNamespace(type="text_delta", text="hello"),
    )
    final = SimpleNamespace(content=[SimpleNamespace(text="hello")], usage=None, id="msg_test")
    provider = _provider_for_runner(_FakeRunner([_FakeStream([event])], final))

    events = list(provider.stream("system", [{"role": "user", "content": "hi"}], tools=[]))

    assert [ev.kind for ev in events] == ["delta", "done"]
    assert events[0].data["text"] == "hello"
    assert events[-1].data["text"] == "hello"


def test_anthropic_stream_falls_back_to_final_message_text():
    final = SimpleNamespace(content=[SimpleNamespace(text="final only")], usage=None, id="msg_test")
    provider = _provider_for_runner(_FakeRunner([_FakeStream([])], final))

    events = list(provider.stream("system", [{"role": "user", "content": "hi"}], tools=[]))

    assert [ev.kind for ev in events] == ["delta", "done"]
    assert events[0].data["text"] == "final only"
    assert events[-1].data["text"] == "final only"


def test_ceiling_detection_reads_stop_reason():
    from legal_helper.providers.anthropic_provider import _runner_hit_ceiling

    # The SDK loop appends the assistant turn AND its tool_result message
    # before re-checking `_should_stop()`, so a final message still asking for
    # tools means the iteration ceiling ended the loop.
    assert _runner_hit_ceiling(SimpleNamespace(stop_reason="tool_use"))
    assert not _runner_hit_ceiling(SimpleNamespace(stop_reason="end_turn"))
    assert not _runner_hit_ceiling(SimpleNamespace(stop_reason="max_tokens"))


def test_forced_synthesis_after_ceiling_reissues_without_tools():
    """Parity with the OpenAI path: recover a blank ceiling exit."""
    provider = _provider_for_runner(_FakeRunner([], None))
    seen: list = []

    def fake_create(**kwargs):
        seen.append(kwargs)
        return SimpleNamespace(
            content=[SimpleNamespace(type="text", text="Recovered answer.")],
            usage=SimpleNamespace(input_tokens=9, output_tokens=4),
        )

    provider.client.beta.messages.create = fake_create
    runner = SimpleNamespace(
        _params={"messages": [{"role": "user", "content": "q"}]}
    )
    usage: dict = {}
    text = provider._forced_synthesis_after_ceiling(
        runner, system="s", betas=[], usage=usage
    )

    assert text == "Recovered answer."
    assert len(seen) == 1
    assert "tools" not in seen[0], "the recovery pass must offer no tools"
    assert seen[0]["messages"][-1]["role"] == "user"
    assert seen[0]["messages"][-1]["content"].startswith("You have reached")
    assert usage["input_tokens"] == 9


def test_forced_synthesis_failure_returns_empty_not_raises():
    provider = _provider_for_runner(_FakeRunner([], None))

    def boom(**kwargs):
        raise RuntimeError("api down")

    provider.client.beta.messages.create = boom
    text = provider._forced_synthesis_after_ceiling(
        SimpleNamespace(_params={"messages": [{"role": "user", "content": "q"}]}),
        system="s",
        betas=[],
        usage={},
    )
    assert text == ""
