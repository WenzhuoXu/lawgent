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
    CLEAR_KEEP_TOOL_USES,
    CLEAR_TARGET_SHARE,
    ClearingNotes,
    InTurnContext,
    anthropic_context_management,
    clearing_trigger,
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
    names = {"memo": "cite_check_report_tool", "ev1": "legal_source_search", "ev2": "read_document"}
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


# --- Anthropic: the same policy, applied by the server ---------------------
# Editing an earlier tool_result client-side invalidates every later thinking
# block on Opus 5.5 (a 400 on accounts created on or after 2026-08-31), so the
# Anthropic path never rewrites the transcript; the server clears instead.


def _anthropic_settings(monkeypatch):
    monkeypatch.setenv("MODEL_PROVIDER", "anthropic")
    return load_settings(refresh=True)


def test_server_side_clearing_reproduces_the_client_side_policy(monkeypatch):
    from legal_helper.context import turn_input_ceiling_for
    from legal_helper.tool_budget import EXEMPT_TOOLS

    monkeypatch.delenv("LEGAL_HELPER_ANTHROPIC_CLEAR_TOOLS", raising=False)
    settings = _anthropic_settings(monkeypatch)
    cfg = anthropic_context_management(settings)
    assert cfg is not None, "server-side clearing is on by default"
    (edit,) = cfg["edits"]
    ceiling = turn_input_ceiling_for(settings)
    assert edit["type"] == "clear_tool_uses_20250919"
    assert edit["trigger"] == {"type": "input_tokens", "value": ceiling}
    # Measured: the server clears (input − trigger) + clear_at_least, so this
    # lands the transcript at CLEAR_TARGET_SHARE of the ceiling, as before.
    assert edit["clear_at_least"] == {
        "type": "input_tokens",
        "value": int((1 - CLEAR_TARGET_SHARE) * ceiling),
    }
    assert edit["keep"] == {"type": "tool_uses", "value": CLEAR_KEEP_TOOL_USES}
    # Deliverables cannot be ordered "last" by the server, so they are kept.
    assert set(EXEMPT_TOOLS) <= set(edit["exclude_tools"])
    assert clearing_trigger(cfg) == ceiling


def test_server_side_clearing_env_switch(monkeypatch):
    settings = _anthropic_settings(monkeypatch)
    monkeypatch.setenv("LEGAL_HELPER_ANTHROPIC_CLEAR_TOOLS", "off")
    assert anthropic_context_management(settings) is None
    monkeypatch.setenv("LEGAL_HELPER_ANTHROPIC_CLEAR_TOOLS", "150000")
    cfg = anthropic_context_management(settings)
    assert clearing_trigger(cfg) == 150_000
    assert cfg["edits"][0]["clear_at_least"]["value"] == 75_000


def test_clearing_notes_stay_silent_until_the_trigger_is_in_sight():
    ctx = _ctx(ceiling=100_000)
    notes = ClearingNotes(ctx, trigger=100_000)
    ctx.observe(20_000, 500)
    # Far below half the trigger: nothing saved, nothing said.
    assert notes.notes_for([("read_document", _BIG, 0, None)]) == []
    ctx.observe(49_000, 500)
    lines = notes.notes_for([("legal_source_search", _BIG, 0, None), ("inspect_xlsx", "small", 0, None)])
    # One catch-up note for the earlier result, one for the sizeable new one;
    # the small result is not worth clearing and gets none.
    assert len(lines) == 2
    assert "read_document" in lines[0] and "legal_source_search" in lines[1]
    path = lines[0].split("saved at ", 1)[1].split(". If it", 1)[0]
    with open(path, encoding="utf-8") as fh:
        assert fh.read() == _BIG  # lossless: the evidence is on disk before any clearing
    # Armed from here on: every sizeable result is named as it arrives.
    assert len(notes.notes_for([("read_document", _BIG + "x", 0, None)])) == 1


def test_clearing_notes_name_the_budget_spill_and_skip_deliverables(tmp_path):
    full = tmp_path / "full.md"
    full.write_text(_BIG, encoding="utf-8")
    notes = ClearingNotes(None, trigger=10_000)  # no accounting: annotate from the start
    lines = notes.notes_for([("pkulaw_fatiao__get_law_item_content", _BIG, 0, full), ("cite_check_report_tool", _BIG, 0, None)])
    assert len(lines) == 1
    assert str(full) in lines[0]


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
