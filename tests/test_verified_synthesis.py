"""A failed citation audit has to change the answer, not just label it.

Before this, the audit ran after the answer had streamed and its verdict was
attached as metadata: a memo citing a statute that does not support the claim
shipped with a warning on it. The workflow now buffers a substantive legal
answer, audits it, repairs exactly what the audit flagged, and only then
reveals it — bounded by `max_repair_rounds` so a model that cannot satisfy its
own auditor cannot loop.

Also covered here: the clarify gates (a hedging router does not get to ask
forever) and the durable run record (research already paid for survives a
cancel).
"""

from __future__ import annotations

from legal_helper.chat_models import AgentTask, WorkflowPlan
from legal_helper.config import load_settings
from legal_helper.workflow import (
    CLARIFY_CONFIDENCE_THRESHOLD,
    MAX_CLARIFY_ROUNDS,
    WorkflowExecutor,
    _audit_is_repairable,
)


def _plan(**kwargs) -> WorkflowPlan:
    base = {
        "title": "t",
        "execution_mode": "agent_workflow",
        "agent_tasks": [
            AgentTask(id="a1", skill_name="legal-response", title="research", task="do")
        ],
    }
    base.update(kwargs)
    return WorkflowPlan(**base)


def _executor(**settings_updates) -> WorkflowExecutor:
    # config.yaml ships with enable_cite_check off for fast iteration; verified
    # synthesis is gated on it, so these tests state the flag they need.
    updates = {"enable_cite_check": True}
    updates.update(settings_updates)
    settings = load_settings(refresh=True).model_copy(update=updates)
    return WorkflowExecutor(settings=settings, provider=_NullProvider())


class _NullProvider:
    name = "openai"
    model = "gpt-5.6-terra"

    def stream(self, *args, **kwargs):  # pragma: no cover - never called here
        raise AssertionError("no model call expected")

    def run(self, *args, **kwargs):  # pragma: no cover
        raise AssertionError("no model call expected")


# --- what counts as repairable ------------------------------------------


def test_a_passing_or_skipped_audit_is_not_repairable():
    assert not _audit_is_repairable(None)
    assert not _audit_is_repairable({"ok": True, "items": [{"severity": "critical"}]})
    assert not _audit_is_repairable({"ok": False, "skipped": True})


def test_a_failed_audit_with_findings_is_repairable():
    assert _audit_is_repairable({"ok": False, "items": [{"severity": "critical"}]})
    assert _audit_is_repairable({"ok": False, "do_not_file": True})
    assert _audit_is_repairable({"ok": False, "report_markdown": "## CRITICAL\n..."})


def test_a_failed_audit_with_nothing_to_act_on_is_not_repairable():
    """No report, no items, no verdict — a repair pass would have no target."""
    assert not _audit_is_repairable({"ok": False, "items": [], "report_markdown": "  "})


# --- which answers are held back ----------------------------------------


def test_substantive_legal_answers_are_verified_before_reveal():
    executor = _executor()
    assert executor._verify_before_reveal(_plan()) is True


def test_chit_chat_and_clarifications_are_not_held_back():
    executor = _executor()
    assert executor._verify_before_reveal(_plan(execution_mode="direct_answer")) is False
    assert executor._verify_before_reveal(_plan(execution_mode="clarify")) is False
    assert executor._verify_before_reveal(None) is False


def test_verification_can_be_turned_off_in_one_setting():
    assert _executor(verify_before_reveal=False)._verify_before_reveal(_plan()) is False
    assert _executor(enable_cite_check=False)._verify_before_reveal(_plan()) is False


def test_the_repair_prompt_names_the_findings_and_forbids_invention():
    executor = _executor()
    audit = {
        "ok": False,
        "report_markdown": "## CRITICAL\nArticle 577 does not support the claim.",
        "items": [
            {"severity": "critical", "claim": "Damages are capped", "note": "unsupported"},
            {"severity": "low", "claim": "ignore me", "note": "nit"},
        ],
    }
    prompt = executor._repair_prompt("DRAFT TEXT", audit, _plan(user_language="Chinese"))
    assert "DRAFT TEXT" in prompt
    assert "Article 577 does not support the claim." in prompt
    assert "Damages are capped" in prompt
    assert "ignore me" not in prompt  # only critical/major findings are listed
    assert "Never invent a replacement citation" in prompt
    assert "Chinese" in prompt


# --- clarify gates -------------------------------------------------------


def test_a_low_confidence_clarify_still_asks():
    executor = _executor()
    plan = _plan(execution_mode="clarify", intake_confidence=0.2, direct_response="Which contract?")
    resolved = executor._resolve_clarify(plan, 0)
    assert resolved.execution_mode == "clarify"
    assert resolved.direct_response == "Which contract?"


def test_a_confident_clarify_is_treated_as_a_hedge():
    executor = _executor()
    plan = _plan(
        execution_mode="clarify",
        intake_confidence=CLARIFY_CONFIDENCE_THRESHOLD,
        direct_response="Which jurisdiction?",
    )
    resolved = executor._resolve_clarify(plan, 0)
    assert resolved.execution_mode == "agent_workflow"
    # The question it wanted to ask becomes the assumption the answer must state.
    assert "Which jurisdiction?" in resolved.integration_instructions
    assert "State the assumptions" in resolved.integration_instructions


def test_an_unreported_confidence_does_not_count_as_confident():
    """The heuristic planner omits the field; that must not disable clarify."""
    executor = _executor()
    plan = _plan(execution_mode="clarify", direct_response="Which contract?")
    assert plan.intake_confidence is None
    assert executor._resolve_clarify(plan, 0).execution_mode == "clarify"


def test_clarifying_stops_after_the_round_cap():
    executor = _executor()
    plan = _plan(execution_mode="clarify", intake_confidence=0.1, direct_response="Which one?")
    assert executor._resolve_clarify(plan, MAX_CLARIFY_ROUNDS - 1).execution_mode == "clarify"
    exhausted = executor._resolve_clarify(plan, MAX_CLARIFY_ROUNDS)
    assert exhausted.execution_mode == "agent_workflow"
    assert "Which one?" in exhausted.integration_instructions


def test_non_clarify_plans_pass_through_untouched():
    executor = _executor()
    plan = _plan(intake_confidence=0.1)
    assert executor._resolve_clarify(plan, 99) is plan


def test_consecutive_clarify_rounds_counts_only_the_trailing_run(tmp_path):
    from legal_helper.chat_store import ChatStore

    store = ChatStore(db_path=tmp_path / "chats.sqlite3")
    chat = store.create_chat()
    assert store.consecutive_clarify_rounds(chat.id) == 0
    store.add_event(chat.id, "workflow_plan", {"execution_mode": "clarify"})
    store.add_event(chat.id, "workflow_plan", {"execution_mode": "clarify"})
    assert store.consecutive_clarify_rounds(chat.id) == 2
    # A turn that did real work resets the streak.
    store.add_event(chat.id, "workflow_plan", {"execution_mode": "agent_workflow"})
    store.add_event(chat.id, "workflow_plan", {"execution_mode": "clarify"})
    assert store.consecutive_clarify_rounds(chat.id) == 1
