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


# --- request shape: one builder for the run and stream paths -----------------


def _real_settings_provider(monkeypatch, runner, captured: dict, *, effort: str = "medium") -> AnthropicProvider:
    from legal_helper.config import load_settings

    monkeypatch.setenv("MODEL_PROVIDER", "anthropic")
    monkeypatch.delenv("LEGAL_HELPER_ANTHROPIC_CLEAR_TOOLS", raising=False)
    provider = AnthropicProvider.__new__(AnthropicProvider)
    provider.settings = load_settings(refresh=True).model_copy(update={"openai_reasoning_effort": effort})
    provider.model = "claude-opus-5-5"

    def tool_runner(**kwargs):
        captured.update(kwargs)
        return runner

    provider.client = SimpleNamespace(beta=SimpleNamespace(messages=SimpleNamespace(tool_runner=tool_runner)))
    return provider


def test_tool_runner_sends_the_same_thinking_effort_and_clearing_as_stream(monkeypatch):
    final = SimpleNamespace(content=[SimpleNamespace(type="text", text="ok")], usage=None, id="m1", stop_reason="end_turn")
    from anthropic.lib.tools import beta_tool

    @beta_tool
    def lookup(q: str) -> str:
        "Look up."
        return q

    run_kwargs: dict = {}
    provider = _real_settings_provider(monkeypatch, _FakeRunner([], final), run_kwargs, effort="high")
    provider.tool_runner("s", [{"role": "user", "content": "q"}], [lookup], max_iterations=0)

    stream_kwargs: dict = {}
    provider = _real_settings_provider(monkeypatch, _FakeRunner([_FakeStream([])], final), stream_kwargs, effort="high")
    list(provider.stream("s", [{"role": "user", "content": "q"}], tools=[lookup]))

    for kwargs in (run_kwargs, stream_kwargs):
        # tool_runner used to send neither: Sonnet 5 ran at its own default, high.
        assert kwargs["thinking"] == {"type": "adaptive", "display": "summarized"}
        assert kwargs["output_config"] == {"effort": "high"}
        # Server-side clearing replaces client-side tool_result edits.
        assert kwargs["context_management"]["edits"][0]["type"] == "clear_tool_uses_20250919"
        assert "context-management-2025-06-27" in kwargs["betas"]
        assert kwargs["cache_control"] == {"type": "ephemeral"}
    assert stream_kwargs["stream"] is True and "stream" not in run_kwargs


def test_verbosity_is_accepted_and_has_no_api_field_on_anthropic(monkeypatch):
    final = SimpleNamespace(content=[SimpleNamespace(type="text", text="ok")], usage=None, id="m1", stop_reason="end_turn")
    captured: dict = {}
    provider = _real_settings_provider(monkeypatch, _FakeRunner([], final), captured)
    provider.run("s", [{"role": "user", "content": "q"}], verbosity="low")
    assert "verbosity" not in captured and "text" not in captured


# --- tool execution: concurrent where safe, in order, append-only notes -------


class _ToolRunnerShell:
    """Just the two runner methods _install_tool_execution replaces/reads."""

    def __init__(self, content):
        self._content = content

    def _generate_tool_call_response(self):  # replaced by the install
        raise AssertionError("the SDK's serial execution should not run")

    def _get_last_assistant_message_content(self):
        return self._content


def _use(tool_id: str, name: str, **input):
    return SimpleNamespace(type="tool_use", id=tool_id, name=name, input=input)


def test_installed_execution_runs_lookups_together_and_returns_results_in_order():
    import threading

    from anthropic.lib.tools import beta_tool
    from anthropic.lib.tools._beta_functions import ToolError

    from legal_helper.providers.anthropic_provider import _install_tool_execution, _runnable_tools

    barrier = threading.Barrier(2, timeout=5)

    @beta_tool
    def flk_npc_search(query: str) -> str:
        "Search."
        barrier.wait()
        return f"hits:{query}"

    @beta_tool
    def write_docx(title: str) -> str:
        "Write."
        raise ToolError([{"type": "text", "text": "disk full"}])

    content = [
        SimpleNamespace(type="thinking", thinking=""),
        _use("tu_1", "flk_npc_search", query="民法典"),
        _use("tu_2", "flk_npc_search", query="合同编"),
        _use("tu_3", "write_docx", title="memo"),
        _use("tu_4", "no_such_tool"),
    ]
    runner = _ToolRunnerShell(content)
    assert _install_tool_execution(
        runner, _runnable_tools([flk_npc_search, write_docx]), provider="anthropic", model="m"
    )
    message = runner._generate_tool_call_response()
    blocks = message["content"]
    assert [b["tool_use_id"] for b in blocks] == ["tu_1", "tu_2", "tu_3", "tu_4"]
    assert blocks[0]["content"] == "hits:民法典" and blocks[1]["content"] == "hits:合同编"
    assert blocks[2]["is_error"] and blocks[2]["content"] == [{"type": "text", "text": "disk full"}]
    assert blocks[3]["is_error"] and "not found" in blocks[3]["content"]


def test_clearing_notes_follow_the_results_once_the_trigger_is_near():
    from anthropic.lib.tools import beta_tool

    from legal_helper.providers.anthropic_provider import _install_tool_execution, _runnable_tools
    from legal_helper.turn_compaction import ClearingNotes, InTurnContext

    body = "第五百八十二条 履行不符合约定的，应当承担违约责任。" * 120

    @beta_tool
    def read_document(path: str) -> str:
        "Read."
        return body

    ctx = InTurnContext(ceiling=10_000, provider="anthropic", model="m")
    ctx.observe(9_000, 100)  # the next request is past half the trigger
    runner = _ToolRunnerShell([_use("tu_1", "read_document", path="a.md")])
    _install_tool_execution(
        runner, _runnable_tools([read_document]), provider="anthropic", model="m", notes=ClearingNotes(ctx, 10_000)
    )
    blocks = runner._generate_tool_call_response()["content"]
    assert blocks[0]["type"] == "tool_result" and blocks[0]["content"] == body
    # The note is a separate text block AFTER the results: server-side
    # clearing removes tool results, never this, and nothing earlier is edited.
    assert blocks[-1]["type"] == "text"
    assert "read_document" in blocks[-1]["text"] and "saved at" in blocks[-1]["text"]


# --- streaming: progress and usage -----------------------------------------


def test_stream_reports_argument_progress_hosted_progress_and_last_request(monkeypatch):
    import legal_helper.providers.anthropic_provider as ap
    from legal_helper.providers.progress import ToolProgressThrottle

    ticks = iter(float(t) for t in range(1000))

    class _SteppedThrottle(ToolProgressThrottle):
        def __init__(self):
            super().__init__(interval=2.0, clock=lambda: next(ticks))

    monkeypatch.setattr(ap, "ToolProgressThrottle", _SteppedThrottle)
    tool_block = SimpleNamespace(type="tool_use", id="toolu_1", name="write_docx", input={})
    events = [
        SimpleNamespace(type="content_block_start", index=0, content_block=SimpleNamespace(type="server_tool_use", id="srv_1", name="web_search", input={})),
        SimpleNamespace(
            type="content_block_stop",
            index=0,
            content_block=SimpleNamespace(type="server_tool_use", id="srv_1", name="web_search", input={"query": "CAAC 官网"}),
        ),
        SimpleNamespace(
            type="content_block_stop",
            index=1,
            content_block=SimpleNamespace(type="web_search_tool_result", id=None, tool_use_id="srv_1", name=None, input=None),
        ),
        SimpleNamespace(type="content_block_start", index=2, content_block=tool_block),
        *[
            SimpleNamespace(type="content_block_delta", index=2, delta=SimpleNamespace(type="input_json_delta", partial_json="y" * 50))
            for _ in range(6)
        ],
        SimpleNamespace(
            type="content_block_stop",
            index=2,
            content_block=SimpleNamespace(type="tool_use", id="toolu_1", name="write_docx", input={"title": "memo"}),
        ),
    ]
    turn_usage = SimpleNamespace(input_tokens=40, output_tokens=8, cache_read_input_tokens=3_000, cache_creation_input_tokens=200)
    final = SimpleNamespace(content=[SimpleNamespace(type="text", text="ok")], usage=turn_usage, id="m1", stop_reason="end_turn")

    class _Stream(_FakeStream):
        def get_final_message(self):
            return final

    provider = _provider_for_runner(_FakeRunner([_Stream(events)], final))
    out = list(provider.stream("s", [{"role": "user", "content": "q"}], tools=[]))

    progress = [e.data for e in out if e.kind == "tool_progress"]
    assert progress and all(p["tool_name"] == "write_docx" and p["call_id"] == "toolu_1" for p in progress)
    assert progress[-1]["chars_so_far"] <= 300 and len(progress) < 6
    hosted = [e.data for e in out if e.kind == "hosted_tool_progress"]
    assert [(h["status"], h.get("query")) for h in hosted] == [("searching", "CAAC 官网"), ("completed", None)]
    assert hosted[1]["tool_id"] == "srv_1"
    done = out[-1]
    assert done.kind == "done"


def test_done_usage_carries_the_last_request_and_clearing_is_logged_once(monkeypatch):
    import legal_helper.logging_setup as ls

    def turn(n_in, n_read, n_write, cleared):
        usage = SimpleNamespace(input_tokens=n_in, output_tokens=5, cache_read_input_tokens=n_read, cache_creation_input_tokens=n_write)
        edits = [SimpleNamespace(type="clear_tool_uses_20250919", cleared_tool_uses=cleared, cleared_input_tokens=cleared * 1_000)] if cleared else []
        return SimpleNamespace(
            content=[SimpleNamespace(type="text", text="ok")],
            usage=usage,
            id=f"m{n_in}",
            stop_reason="end_turn",
            context_management=SimpleNamespace(applied_edits=edits),
        )

    turns = [turn(10, 5_000, 300, 0), turn(20, 2_000, 400, 6), turn(30, 2_400, 100, 6)]

    class _Stream(_FakeStream):
        def __init__(self, final):
            super().__init__([])
            self.final = final

        def get_final_message(self):
            return self.final

    captured: dict = {}
    provider = _real_settings_provider(monkeypatch, _FakeRunner([_Stream(t) for t in turns], turns[-1]), captured)
    events: list[tuple] = []
    token = ls.workflow_event_sink_var.set(lambda rec: events.append((rec.get("event_type"), rec.get("data", {}))))
    try:
        out = list(provider.stream("s", [{"role": "user", "content": "q"}], tools=[]))
    finally:
        ls.workflow_event_sink_var.reset(token)

    usage = out[-1].data["usage"]
    assert usage["input_tokens"] == 60 and usage["cache_read_input_tokens"] == 9_400
    # Context fill = the last request's three input buckets, not the turn sum.
    assert usage["last_request_input_tokens"] == 30 + 2_400 + 100
    cleared = [d for kind, d in events if kind == "in_turn_context_cleared"]
    # The server repeats a sticky clearing on every later request; log it once.
    assert len(cleared) == 1 and cleared[0]["server_side"] and cleared[0]["results_cleared"] == 6


def test_structured_output_reads_the_sdks_parsed_output(monkeypatch):
    """`ParsedBetaMessage` exposes `parsed_output`; reading `.parsed` returned
    None for every Anthropic structured call, the planner's included."""
    from pydantic import BaseModel

    class Route(BaseModel):
        complexity: str

    parsed_msg = SimpleNamespace(
        content=[SimpleNamespace(type="text", text='{"complexity": "simple"}')],
        usage=None,
        id="m1",
        stop_reason="end_turn",
        parsed_output=Route(complexity="simple"),
    )
    captured: dict = {}
    provider = _real_settings_provider(monkeypatch, _FakeRunner([], parsed_msg), captured)
    result = provider.run("s", [{"role": "user", "content": "q"}], structured_output=Route)
    assert result.structured_payload == {"complexity": "simple"}
    assert captured["output_format"] is Route

    # No parsed object (an SDK shape without it): the constrained text is JSON.
    text_only = SimpleNamespace(
        content=[SimpleNamespace(type="text", text='{"complexity": "complex"}')], usage=None, id="m2", stop_reason="end_turn"
    )
    provider = _real_settings_provider(monkeypatch, _FakeRunner([], text_only), {})
    assert provider.run("s", [{"role": "user", "content": "q"}], structured_output=Route).structured_payload == {
        "complexity": "complex"
    }


def test_json_object_mode_is_not_an_anthropic_output_format(monkeypatch):
    from legal_helper.providers.base import JSON_OBJECT

    msg = SimpleNamespace(
        content=[SimpleNamespace(type="text", text='{"complexity": "standard"}')], usage=None, id="m3", stop_reason="end_turn"
    )
    captured: dict = {}
    provider = _real_settings_provider(monkeypatch, _FakeRunner([], msg), captured)
    result = provider.run("s", [{"role": "user", "content": "q"}], structured_output=JSON_OBJECT)
    assert "output_format" not in captured
    assert result.structured_payload == {"complexity": "standard"}
