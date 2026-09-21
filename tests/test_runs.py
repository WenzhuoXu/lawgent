"""A run that stops short must not lose the research it already paid for.

A legal research run is 30-odd PKULaw / CourtListener calls before it writes a
word. Before this, a cancel or a provider outage discarded all of it: the run
existed only in memory and in the event log. Now each phase's output lands in
``state/runs/<run_id>/artifacts/`` and a snapshot records what finished, so the
next attempt reuses the completed bundles.
"""

from __future__ import annotations

import json

from legal_helper.chat_models import AgentTask, WorkflowPlan
from legal_helper.config import load_settings
from legal_helper.runs import RunStore, list_runs, load_run


def _plan() -> WorkflowPlan:
    return WorkflowPlan(
        title="ET route filing",
        agent_tasks=[
            AgentTask(id="a1", skill_name="legal-response", title="ET aviation law", task="research"),
            AgentTask(id="a2", skill_name="brief", title="CN outbound", task="research"),
        ],
    )


def _settings(tmp_path):
    return load_settings(refresh=True).model_copy(update={"state_dir": tmp_path / "state"})


def test_a_run_records_its_plan_and_each_bundle(tmp_path):
    settings = _settings(tmp_path)
    store = RunStore(settings, "run-1", chat_id="chat-1")
    store.record_plan(_plan())
    store.task_started("a1")
    store.task_finished("a1", "ET findings with pinpoints", skill_name="legal-response")
    store.task_finished("a2", "", skill_name="brief", error="rate limited")
    store.finish("paused", pause_reason="cancelled")

    snapshot = json.loads(store.snapshot_path.read_text(encoding="utf-8"))
    assert snapshot["status"] == "paused"
    assert snapshot["pause_reason"] == "cancelled"
    assert snapshot["tasks"]["a1"]["status"] == "complete"
    assert snapshot["tasks"]["a2"]["status"] == "failed"
    assert snapshot["tasks"]["a2"]["error"] == "rate limited"
    # The plan and the finished bundle are both on disk, numbered in order.
    written = sorted(p.name for p in store.artifacts_dir.glob("*.md"))
    assert written[0].startswith("01-plan")
    assert any("a1-legal-response" in name for name in written)
    assert "ET findings with pinpoints" in (store.root / snapshot["tasks"]["a1"]["artifact"]).read_text(
        encoding="utf-8"
    )


def test_only_completed_bundles_are_resumable(tmp_path):
    settings = _settings(tmp_path)
    store = RunStore(settings, "run-2")
    store.record_plan(_plan())
    store.task_finished("a1", "ET findings", skill_name="legal-response")
    store.task_finished("a2", "", skill_name="brief", error="failed")
    store.finish("paused", pause_reason="cancelled")

    reopened = load_run(settings, "run-2")
    assert reopened is not None
    resumable = reopened.resumable_outputs()
    assert set(resumable) == {"a1"}
    # The title header the artifact was written with is not part of the bundle.
    assert resumable["a1"].strip() == "ET findings"


def test_a_task_left_active_by_a_dead_process_becomes_pending(tmp_path):
    """Nothing is running after the process is gone, so nothing stays active."""
    settings = _settings(tmp_path)
    store = RunStore(settings, "run-3")
    store.record_plan(_plan())
    store.task_started("a1")
    store.finish("failed", pause_reason="error", error="boom")
    assert store.snapshot.tasks["a1"].status == "pending"


def test_runs_are_listed_newest_first_and_filtered_by_chat(tmp_path):
    settings = _settings(tmp_path)
    for index, chat in enumerate(("chat-a", "chat-b", "chat-a")):
        store = RunStore(settings, f"run-{index}", chat_id=chat)
        store.record_plan(_plan())
        store.finish("complete")

    everything = list_runs(settings)
    assert len(everything) == 3
    just_a = list_runs(settings, chat_id="chat-a")
    assert {r["chat_id"] for r in just_a} == {"chat-a"}
    assert len(just_a) == 2
    assert list_runs(settings, limit=1) == everything[:1]


def test_load_run_returns_none_for_an_unknown_run(tmp_path):
    assert load_run(_settings(tmp_path), "never-existed") is None


def test_durability_failures_never_raise(tmp_path):
    """A read-only state dir degrades to in-memory, it does not break a turn."""
    settings = _settings(tmp_path)
    store = RunStore(settings, "run-x")
    store.enabled = False  # as if the directory could not be created
    store.record_plan(_plan())
    store.task_finished("a1", "text")
    store.finish("complete")
    assert not store.snapshot_path.exists()
    assert store.resumable_outputs() == {}
