"""Trajectory failure taxonomy: event classification and run profiles."""

from __future__ import annotations

from legal_helper.citations.trajectory import (
    Category,
    Layer,
    classify_event,
    profile,
)


def test_progress_events_are_not_failures():
    for event in (
        "citation_audit_started",
        "citation_audit_finished",
        "workflow_plan_created",
        "agent_task_finished",
    ):
        assert classify_event(event) is None


def test_failure_events_carry_layer_and_category():
    sub = classify_event("workflow_degraded_missing_specialists")
    assert sub is not None
    assert sub.layer is Layer.PROCEDURAL
    assert sub.category is Category.DELEGATION

    sub = classify_event("citation_audit_failed")
    assert sub is not None
    assert sub.layer is Layer.SUBSTANTIVE
    assert sub.category is Category.CITATION


def test_profile_separates_layers_and_flags_right_answer_wrong_reason():
    # Procedural failures only: the answer may look right, the chain was not.
    p = profile(
        [
            "workflow_plan_fallback",
            "workflow_degraded_missing_specialists",
            "citation_audit_finished",  # progress, ignored
        ]
    )
    assert p["total"] == 2
    assert p["by_layer"] == {"procedural": 2}
    assert p["right_answer_wrong_reason"] is True

    # Add a substantive failure and the flag clears — the content is wrong too.
    p = profile(["workflow_plan_fallback", "citation_audit_failed"])
    assert p["by_layer"] == {"procedural": 1, "substantive": 1}
    assert p["right_answer_wrong_reason"] is False


def test_profile_accepts_event_dicts_and_pairs():
    events = [
        {"event_type": "agent_task_failed"},
        {"event": "agent_task_failed"},
        ("workflow_plan_fallback", {"reason": "x"}),
    ]
    # event_type is not one of the recognised keys, so only 2 classify.
    p = profile(events)
    assert p["by_subclass"] == {
        "delegation.task_failed": 1,
        "planning.fallback": 1,
    }
    assert p["dominant_subclass"] in {"delegation.task_failed", "planning.fallback"}


def test_workflow_events_are_tagged_with_their_subclass(tmp_path, monkeypatch):
    from legal_helper.logging_setup import log_workflow_event

    record = log_workflow_event("agent_task_failed", {"task": "t1"})
    assert record["failure"]["subclass"] == "delegation.task_failed"
    assert record["failure"]["layer"] == "procedural"

    record = log_workflow_event("agent_task_finished", {"task": "t1"})
    assert "failure" not in record
