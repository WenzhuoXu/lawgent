"""Mock the OpenAI Responses API to test the function-tool loop and request shaping."""

from __future__ import annotations

import json
from types import SimpleNamespace
from unittest.mock import patch

from legal_helper.config import load_settings
from legal_helper.providers.openai_provider import OpenAIProvider


def _fc_item(call_id: str, name: str, args: dict) -> SimpleNamespace:
    return SimpleNamespace(
        type="function_call",
        call_id=call_id,
        id=call_id,
        name=name,
        arguments=json.dumps(args),
    )


def _msg_item(text: str) -> SimpleNamespace:
    return SimpleNamespace(
        type="message",
        content=[SimpleNamespace(text=text)],
    )


def _resp(items, response_id="resp_x", usage=None) -> SimpleNamespace:
    return SimpleNamespace(
        id=response_id,
        output=items,
        output_text="\n".join(getattr(c, "text", "") for it in items for c in getattr(it, "content", [])),
        usage=usage,
    )


def test_function_tool_loop_routes_local_call(monkeypatch):
    settings = load_settings(refresh=True)
    provider = OpenAIProvider(settings)

    from legal_helper.tools.documents import read_document

    calls: list = []

    def fake_create(**kwargs):
        calls.append(kwargs)
        if len(calls) == 1:
            return _resp(
                [_fc_item("call_1", "read_document", {"path": "examples/aviation/sample_aircraft_lease.md"})],
                response_id="resp_1",
                usage=SimpleNamespace(input_tokens=10, output_tokens=2, total_tokens=12),
            )
        return _resp(
            [_msg_item("IDERA: PASS\nLiability: FLAG (US$750M)\nNot legal advice.")],
            response_id="resp_2",
            usage=SimpleNamespace(input_tokens=15, output_tokens=5, total_tokens=20),
        )

    with patch.object(provider.client.responses, "create", side_effect=fake_create):
        result = provider.tool_runner(
            system="You are a test sub-agent.",
            messages=[{"role": "user", "content": "review the lease"}],
            tools=[read_document],
            max_iterations=4,
        )

    assert "IDERA" in result.text
    assert result.iterations >= 2
    assert result.response_id == "resp_2"
    # Local tool was actually invoked, and its output appended on the next turn.
    assert len(calls) == 2
    second_call_input = calls[1]["input"]
    assert any(item.get("type") == "function_call_output" for item in second_call_input)
    fco = next(item for item in second_call_input if item.get("type") == "function_call_output")
    assert fco["call_id"] == "call_1"
    # Tool actually returned the file content.
    assert "AIRCRAFT OPERATING LEASE" in fco["output"] or "IDERA" in fco["output"]


def test_hosted_web_search_call_recorded():
    settings = load_settings(refresh=True)
    provider = OpenAIProvider(settings)

    def fake_create(**kwargs):
        items = [
            SimpleNamespace(type="web_search_call", id="ws_1"),
            _msg_item("Cited FAA source confirms AD 2026-09-51."),
        ]
        return _resp(items, response_id="resp_ws", usage=SimpleNamespace(input_tokens=5, output_tokens=3))

    with patch.object(provider.client.responses, "create", side_effect=fake_create):
        result = provider.tool_runner(
            system="Find an FAA AD.",
            messages=[{"role": "user", "content": "AD 2026-09-51"}],
            tools=[{"type": "web_search"}],
            max_iterations=2,
        )

    assert any(c.name == "web_search_call" for c in result.hosted_tool_calls)
    assert "Cited FAA source" in result.text


def test_tool_loop_ceiling_forces_a_final_synthesis_pass(monkeypatch):
    """A loop cut off by max_iterations must not ship an empty answer.

    Every zero-length answer observed in production sat at
    iterations == max_iterations: the model kept asking for tools and never
    got a turn in which it had to write prose.
    """
    settings = load_settings(refresh=True)
    provider = OpenAIProvider(settings)

    from legal_helper.tools.documents import read_document

    calls: list = []

    def fake_create(**kwargs):
        calls.append(kwargs)
        # Always ask for another tool — the model never volunteers prose.
        if kwargs.get("tools"):
            return _resp(
                [_fc_item(f"call_{len(calls)}", "read_document", {"path": "README.md"})],
                response_id=f"resp_{len(calls)}",
                usage=SimpleNamespace(input_tokens=10, output_tokens=2, total_tokens=12),
            )
        # The recovery pass: no tools offered, so it must answer.
        return _resp(
            [_msg_item("Answer assembled from the material already gathered.")],
            response_id="resp_final",
            usage=SimpleNamespace(input_tokens=20, output_tokens=8, total_tokens=28),
        )

    with patch.object(provider.client.responses, "create", side_effect=fake_create):
        result = provider.tool_runner(
            system="You are a test sub-agent.",
            messages=[{"role": "user", "content": "review it"}],
            tools=[read_document],
            max_iterations=3,
        )

    assert result.text.strip(), "ceiling exit must not return an empty answer"
    assert "already gathered" in result.text
    # 3 tool-armed iterations, then exactly one tool-free recovery call.
    assert len(calls) == 4
    assert not calls[-1].get("tools")
    assert calls[-1]["input"][-1]["content"][0]["text"].startswith("You have reached")


def test_no_synthesis_pass_when_the_loop_ends_normally():
    """The recovery call is spent only on a real ceiling exit."""
    settings = load_settings(refresh=True)
    provider = OpenAIProvider(settings)

    calls: list = []

    def fake_create(**kwargs):
        calls.append(kwargs)
        return _resp(
            [_msg_item("Done in one pass.")],
            response_id="resp_1",
            usage=SimpleNamespace(input_tokens=5, output_tokens=3),
        )

    with patch.object(provider.client.responses, "create", side_effect=fake_create):
        result = provider.tool_runner(
            system="s",
            messages=[{"role": "user", "content": "q"}],
            tools=[],
            max_iterations=3,
        )

    assert result.text == "Done in one pass."
    assert len(calls) == 1


# --- request capabilities, from the 2026-10-07 live probes -------------------


def _provider_for(model: str, effort: str = "medium") -> OpenAIProvider:
    settings = load_settings(refresh=True).model_copy(
        update={"openai_model": model, "openai_reasoning_effort": effort}
    )
    return OpenAIProvider(settings)


def _capture_create(provider, responses):
    calls: list[dict] = []
    queue = list(responses)

    def fake_create(**kwargs):
        calls.append(kwargs)
        return queue.pop(0) if len(queue) > 1 else queue[0]

    return calls, patch.object(provider.client.responses, "create", side_effect=fake_create)


def test_verbosity_is_sent_only_where_the_model_takes_it():
    for model in ("gpt-6.1-sol", "gpt-6-luna"):
        provider = _provider_for(model)
        calls, patcher = _capture_create(provider, [_resp([_msg_item("x")])])
        with patcher:
            provider.run("s", [{"role": "user", "content": "q"}], verbosity="low")
        assert calls[0]["text"] == {"verbosity": "low"}

    provider = _provider_for("gpt-5.4")
    calls, patcher = _capture_create(provider, [_resp([_msg_item("x")])])
    with patcher:
        provider.run("s", [{"role": "user", "content": "q"}], verbosity="low")
    assert "text" not in calls[0]

    provider = _provider_for("gpt-6.1-sol")
    calls, patcher = _capture_create(provider, [_resp([_msg_item("x")])])
    with patcher:
        provider.run("s", [{"role": "user", "content": "q"}], verbosity="extremely-terse")
    assert "text" not in calls[0]


def test_each_model_gets_an_effort_it_accepts():
    from legal_helper.providers.openai_provider import reasoning_effort_for

    # luna takes "none" literally; omitting the parameter would run it at medium.
    assert reasoning_effort_for("gpt-6-luna", "none") == "none"
    assert reasoning_effort_for("gpt-6-luna", "minimal") == "none"
    assert reasoning_effort_for("gpt-6-luna", "max") == "max"
    # sol rejects none/minimal with a 400: the nearest level it takes is low.
    assert reasoning_effort_for("gpt-6.1-sol", "none") == "low"
    assert reasoning_effort_for("gpt-6.1-sol", "minimal") == "low"
    assert reasoning_effort_for("gpt-6.1-sol", "max") == "max"
    # A model with no recorded capabilities keeps the old rule.
    assert reasoning_effort_for("gpt-5.4", "none") is None
    assert reasoning_effort_for("gpt-5.4", "high") == "high"
    assert reasoning_effort_for("gpt-6-luna", "") is None

    provider = _provider_for("gpt-6-luna", effort="none")
    calls, patcher = _capture_create(provider, [_resp([_msg_item("x")])])
    with patcher:
        provider.run("s", [{"role": "user", "content": "q"}])
    assert calls[0]["reasoning"] == {"effort": "none"}


def test_tool_runner_records_truncation_as_stop_reason():
    provider = _provider_for("gpt-6.1-sol")
    cut = SimpleNamespace(
        id="resp_cut",
        output=[_msg_item("partial memo …")],
        output_text="partial memo …",
        usage=None,
        status="incomplete",
        incomplete_details=SimpleNamespace(reason="max_output_tokens"),
    )
    _, patcher = _capture_create(provider, [cut])
    with patcher:
        result = provider.tool_runner("s", [{"role": "user", "content": "q"}], [], max_iterations=1)
    assert result.stop_reason == "max_tokens"

    _, patcher = _capture_create(provider, [_resp([_msg_item("done")])])
    with patcher:
        result = provider.tool_runner("s", [{"role": "user", "content": "q"}], [], max_iterations=1)
    assert result.stop_reason == "stop"


def test_cut_off_function_arguments_do_not_break_the_loop():
    provider = _provider_for("gpt-6.1-sol")
    broken = SimpleNamespace(type="function_call", call_id="c1", id="c1", name="read_document", arguments='{"path": "exam')
    calls, patcher = _capture_create(
        provider, [_resp([broken], response_id="r1"), _resp([_msg_item("recovered")], response_id="r2")]
    )
    from legal_helper.tools.documents import read_document

    with patcher:
        result = provider.tool_runner("s", [{"role": "user", "content": "q"}], [read_document], max_iterations=0)
    assert result.text == "recovered"
    assert result.tool_calls[0].arguments == {}


def test_independent_calls_of_one_response_run_side_by_side_in_order():
    """Two lookups in one response overlap; their outputs keep the model's order."""
    import threading

    import legal_helper.logging_setup as ls
    from anthropic.lib.tools import beta_tool

    barrier = threading.Barrier(2, timeout=5)

    @beta_tool
    def flk_npc_search(query: str) -> str:
        "Search flk."
        barrier.wait()  # only a concurrent execution gets past this
        return f"hits for {query}"

    provider = _provider_for("gpt-6.1-sol")
    first = _resp(
        [_fc_item("c1", "flk_npc_search", {"query": "民法典"}), _fc_item("c2", "flk_npc_search", {"query": "合同法"})],
        response_id="r1",
        usage=SimpleNamespace(input_tokens=1_000, output_tokens=10, total_tokens=1_010),
    )
    last = _resp([_msg_item("ok")], response_id="r2", usage=SimpleNamespace(input_tokens=1_400, output_tokens=5, total_tokens=1_405))
    calls, patcher = _capture_create(provider, [first, last])
    events: list[tuple] = []
    token = ls.workflow_event_sink_var.set(lambda rec: events.append((rec.get("event_type"), rec.get("data", {}))))
    try:
        with patcher:
            result = provider.tool_runner("s", [{"role": "user", "content": "q"}], [flk_npc_search], max_iterations=0)
    finally:
        ls.workflow_event_sink_var.reset(token)

    outputs = [i for i in calls[1]["input"] if i.get("type") == "function_call_output"]
    assert [o["call_id"] for o in outputs] == ["c1", "c2"]
    assert outputs[0]["output"] == "hits for 民法典" and outputs[1]["output"] == "hits for 合同法"
    finished = [d for kind, d in events if kind == "local_tool_call_finished"]
    # Events from the worker threads reached the caller's sink, in call order.
    assert [d["output_preview"] for d in finished] == ["hits for 民法典", "hits for 合同法"]
    assert all("duration_ms" in d and d["calls_in_response"] == 2 for d in finished)
    # Turn totals for billing; the last request alone for context fill.
    assert result.usage["input_tokens"] == 2_400
    assert result.usage["last_request_input_tokens"] == 1_400


# --- streaming: progress while a call is still being written -----------------


class _FakeStream:
    def __init__(self, events):
        self.events = events

    def __enter__(self):
        return iter(self.events)

    def __exit__(self, *args):
        return False


def _completed(items, response_id="resp_s", usage=None):
    return SimpleNamespace(
        type="response.completed",
        response=SimpleNamespace(id=response_id, output=items, usage=usage, status="completed"),
    )


def test_stream_reports_argument_progress_and_hosted_search_progress(monkeypatch):
    import legal_helper.providers.openai_provider as op
    from legal_helper.providers.progress import ToolProgressThrottle

    ticks = iter(float(t) for t in range(1000))

    class _SteppedThrottle(ToolProgressThrottle):
        def __init__(self):
            super().__init__(interval=2.0, clock=lambda: next(ticks))

    monkeypatch.setattr(op, "ToolProgressThrottle", _SteppedThrottle)
    provider = _provider_for("gpt-6.1-sol")
    fc_added = SimpleNamespace(
        type="response.output_item.added",
        item=SimpleNamespace(type="function_call", id="fc_1", call_id="call_1", name="write_docx"),
    )
    deltas = [
        SimpleNamespace(type="response.function_call_arguments.delta", item_id="fc_1", delta="x" * 10, snapshot="x" * (10 * (i + 1)))
        for i in range(6)
    ]
    ws_item = SimpleNamespace(
        type="web_search_call",
        id="ws_1",
        status="completed",
        action=SimpleNamespace(type="search", query="CAAC official website", queries=None, url=None),
    )
    first_events = [
        SimpleNamespace(type="response.web_search_call.in_progress", item_id="ws_1"),
        SimpleNamespace(type="response.web_search_call.searching", item_id="ws_1"),
        SimpleNamespace(type="response.output_item.done", item=ws_item),
        fc_added,
        *deltas,
        SimpleNamespace(
            type="response.output_item.done",
            item=SimpleNamespace(type="function_call", id="fc_1", call_id="call_1", name="write_docx"),
        ),
        _completed(
            [ws_item, SimpleNamespace(type="function_call", id="fc_1", call_id="call_1", name="write_docx", arguments="{}")],
            response_id="r1",
            usage=SimpleNamespace(
                input_tokens=900,
                output_tokens=60,
                total_tokens=960,
                input_tokens_details=SimpleNamespace(cached_tokens=600, cache_write_tokens=250),
            ),
        ),
    ]
    second_events = [
        SimpleNamespace(type="response.output_text.delta", delta="Done."),
        _completed([], response_id="r2", usage=SimpleNamespace(input_tokens=1_200, output_tokens=5, total_tokens=1_205)),
    ]
    streams = iter([_FakeStream(first_events), _FakeStream(second_events)])
    with patch.object(provider.client.responses, "stream", side_effect=lambda **kw: next(streams)):
        events = list(provider.stream("s", [{"role": "user", "content": "q"}], tools=[]))

    progress = [e.data for e in events if e.kind == "tool_progress"]
    assert progress, "a long argument stream must report progress"
    assert all(p["tool_name"] == "write_docx" and p["call_id"] == "call_1" for p in progress)
    assert [p["chars_so_far"] for p in progress] == sorted(p["chars_so_far"] for p in progress)
    assert len(progress) < len(deltas)  # throttled, not one per delta

    hosted = [e.data for e in events if e.kind == "hosted_tool_progress"]
    assert [h["status"] for h in hosted] == ["searching", "completed"]
    assert hosted[1]["query"] == "CAAC official website"
    # The searching event arrives before the response completes, not after.
    kinds = [e.kind for e in events]
    assert kinds.index("hosted_tool_progress") < kinds.index("hosted_tool_call")
    hosted_call = next(e.data for e in events if e.kind == "hosted_tool_call")
    assert hosted_call["arguments"] == {"query": "CAAC official website"}

    done = events[-1]
    assert done.kind == "done" and done.data["stop_reason"] == "stop"
    usage = done.data["usage"]
    assert usage["input_tokens"] == 2_100
    assert usage["cached_input_tokens"] == 600
    assert usage["cache_write_input_tokens"] == 250
    assert usage["last_request_input_tokens"] == 1_200
