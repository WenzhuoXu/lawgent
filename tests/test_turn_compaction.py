"""A long tool loop is kept under the turn ceiling by clearing, never by stopping.

The round caps and the cumulative tool-output cap are gone; these tests pin
what replaced them: old tool results are saved to disk and stubbed once a
request would cross the ceiling, the latest round is never touched, the loop
runs past the old limits, and nothing is cleared that could not be saved.
"""

from __future__ import annotations

import json
from types import SimpleNamespace
from unittest.mock import patch

from legal_helper.config import load_settings
from legal_helper.turn_compaction import (
    CLEAR_TARGET_SHARE,
    InTurnContext,
    install_on_anthropic_runner,
    iteration_limit,
)

_BIG = "第一条 合同当事人应当遵循诚实信用原则。" * 200  # ~4K tokens


def _ctx(ceiling: int = 10_000) -> InTurnContext:
    return InTurnContext(ceiling=ceiling, provider="test", model="m")


def _fco(call_id: str, text: str = _BIG) -> dict:
    return {"type": "function_call_output", "call_id": call_id, "output": text}


def test_zero_or_unset_round_limit_means_unlimited():
    settings = load_settings(refresh=True)
    assert iteration_limit(0, settings) is None
    assert iteration_limit(None, settings) is None  # shipped default is 0 too
    assert iteration_limit(-1, settings) is None
    assert iteration_limit(1, settings) == 1  # single-shot callers keep one round


def test_nothing_is_cleared_below_the_ceiling():
    ctx = _ctx()
    history = [_fco("a"), _fco("b")]
    assert not ctx.before_openai_request(history, {"a": "t", "b": "t"})  # no count yet
    ctx.observe(3_000, 100)
    history.append(_fco("c", "small"))
    assert not ctx.before_openai_request(history, {})
    assert all(item["output"] in (_BIG, "small") for item in history)


def test_old_results_are_saved_and_stubbed_past_the_ceiling():
    ctx = _ctx()
    history = [{"role": "user", "content": "go"}, _fco("a"), _fco("b"), _fco("c")]
    names = {"a": "legal_source_search", "b": "read_document", "c": "inspect_xlsx"}
    ctx.before_openai_request(history, names)  # first request: establishes the tail
    ctx.observe(9_500, 400)
    history.append(_fco("d"))  # the round the model has not read yet

    assert ctx.before_openai_request(history, names)

    cleared = [i for i in history[1:4] if i["output"] != _BIG]
    assert cleared, "nothing was cleared past the ceiling"
    stub = cleared[0]["output"]
    assert "read_document" in stub and "legal_source_search" in stub
    path = stub.split("saved at ", 1)[1].split(" — ", 1)[0]
    with open(path, encoding="utf-8") as fh:
        assert fh.read() == _BIG  # lossless: the evidence is on disk
    assert history[-1]["output"] == _BIG  # the newest round is never touched
    # Oldest first.
    assert history[1]["output"] != _BIG


def test_clearing_aims_well_below_the_ceiling_so_it_is_rare():
    ctx = _ctx(ceiling=20_000)
    history = [_fco(str(i)) for i in range(6)]
    ctx.before_openai_request(history, {})
    ctx.observe(19_000, 500)
    history.append(_fco("new"))
    ctx.before_openai_request(history, {})
    # The projection (old count + output + the new ~4K round) now sits at or
    # below the target share, not merely under the ceiling.
    assert ctx._last_input + ctx._last_output + 4_000 <= int(CLEAR_TARGET_SHARE * 20_000)
    # And the next round, without a new provider count, does not clear again.
    history.append(_fco("newer", "small"))
    before = [i["output"] for i in history]
    assert not ctx.before_openai_request(history, {})
    assert [i["output"] for i in history] == before


def test_deliverables_are_cleared_only_after_ordinary_evidence():
    ctx = _ctx()
    history = [_fco("memo"), _fco("ev1"), _fco("ev2")]
    names = {"memo": "run_skill", "ev1": "legal_source_search", "ev2": "read_document"}
    ctx.before_openai_request(history, names)
    ctx.observe(9_900, 200)
    history.append(_fco("tail", "x"))
    assert ctx.before_openai_request(history, names)
    # The oldest result is the memo, but ordinary evidence goes first and was
    # enough to reach the target.
    assert history[0]["output"] == _BIG
    assert history[1]["output"] != _BIG and history[2]["output"] != _BIG


def test_a_result_that_cannot_be_saved_is_not_cleared(monkeypatch):
    import legal_helper.turn_compaction as tc

    monkeypatch.setattr(tc, "_spill", lambda name, text: None)
    ctx = _ctx()
    history = [_fco("a"), _fco("b")]
    ctx.before_openai_request(history, {})
    ctx.observe(9_900, 100)
    history.append(_fco("c"))
    assert not ctx.before_openai_request(history, {})
    assert all(i["output"] == _BIG for i in history)


def test_anthropic_tool_results_are_rewritten_as_plain_dicts():
    ctx = _ctx()
    tool_use = SimpleNamespace(type="tool_use", id="tu_1", name="read_document", input={})
    messages = [
        {"role": "user", "content": "go"},
        {"role": "assistant", "content": [tool_use]},
        {"role": "user", "content": [{"type": "tool_result", "tool_use_id": "tu_1", "content": _BIG}]},
    ]
    ctx.before_anthropic_request(messages)
    ctx.observe(9_800, 300)
    tu2 = SimpleNamespace(type="tool_use", id="tu_2", name="inspect_xlsx", input={})
    messages += [
        {"role": "assistant", "content": [tu2]},
        {"role": "user", "content": [{"type": "tool_result", "tool_use_id": "tu_2", "content": _BIG}]},
    ]
    assert ctx.before_anthropic_request(messages)
    block = messages[2]["content"][0]
    assert block["tool_use_id"] == "tu_1" and "read_document" in block["content"]
    assert set(block) == {"type", "tool_use_id", "content"}  # nothing internal leaks to the API
    assert messages[4]["content"][0]["content"] == _BIG


def test_the_anthropic_hook_rewrites_params_before_each_request():
    ctx = _ctx()
    sent: list[list] = []

    class _Runner:
        def __init__(self):
            self._params = {"messages": []}

        def set_messages_params(self, fn):
            self._params = fn(self._params)

        def _handle_request(self):
            sent.append(list(self._params["messages"]))
            return "item"

    runner = _Runner()
    assert install_on_anthropic_runner(runner, ctx)
    tu = SimpleNamespace(type="tool_use", id="tu_1", name="read_document", input={})
    runner._params["messages"] = [
        {"role": "assistant", "content": [tu]},
        {"role": "user", "content": [{"type": "tool_result", "tool_use_id": "tu_1", "content": _BIG}]},
    ]
    assert runner._handle_request() == "item"
    ctx.observe(9_900, 100)
    tu2 = SimpleNamespace(type="tool_use", id="tu_2", name="read_document", input={})
    runner._params["messages"] += [
        {"role": "assistant", "content": [tu2]},
        {"role": "user", "content": [{"type": "tool_result", "tool_use_id": "tu_2", "content": _BIG}]},
    ]
    runner._handle_request()
    assert sent[-1][1]["content"][0]["content"] != _BIG
    assert sent[-1][3]["content"][0]["content"] == _BIG


def test_openai_loop_runs_past_the_old_caps_and_resends_a_cleared_history():
    """30 tool rounds, a ceiling crossing mid-way, and still a finished answer."""
    from legal_helper.providers.openai_provider import OpenAIProvider
    from legal_helper.tools.documents import read_document

    settings = load_settings(refresh=True)
    provider = OpenAIProvider(settings)
    calls: list[dict] = []
    rounds = 30
    level = [0]

    def fake_create(**kwargs):
        calls.append(kwargs)
        n = len(calls)
        # Input grows 20K a round and crosses the ceiling mid-loop; a re-sent
        # (cleared) history reports a small input again, as the API would.
        level[0] = 20_000 if (n > 1 and "previous_response_id" not in kwargs) else level[0] + 20_000
        usage = SimpleNamespace(input_tokens=level[0], output_tokens=200, total_tokens=0)
        if n <= rounds:
            item = SimpleNamespace(
                type="function_call",
                call_id=f"call_{n}",
                id=f"fc_{n}",
                name="read_document",
                arguments=json.dumps({"path": "examples/aviation/sample_aircraft_lease.md"}),
            )
            return SimpleNamespace(id=f"resp_{n}", output=[item], output_text="", usage=usage)
        msg = SimpleNamespace(type="message", content=[SimpleNamespace(text="Done.")])
        return SimpleNamespace(id=f"resp_{n}", output=[msg], output_text="Done.", usage=usage)

    with patch.object(provider.client.responses, "create", side_effect=fake_create):
        result = provider.tool_runner(
            system="s",
            messages=[{"role": "user", "content": "go"}],
            tools=[read_document],
            max_iterations=0,
        )

    assert result.text == "Done."
    assert len(calls) == rounds + 1
    resent = [c for c in calls[1:] if "previous_response_id" not in c]
    assert resent, "a crossing never re-sent the cleared history"
    items = resent[0]["input"]
    assert any(i.get("type") == "item_reference" for i in items)
    assert any(
        i.get("type") == "function_call_output" and "cleared from context" in str(i.get("output"))
        for i in items
    )
    # Chaining resumes after the pass.
    later = calls[calls.index(resent[0]) + 1 :]
    assert later and "previous_response_id" in later[0]
