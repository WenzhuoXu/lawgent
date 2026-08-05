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
