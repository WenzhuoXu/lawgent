"""A failed citation audit has to change the answer, not just label it.

Before this, the audit ran after the answer had streamed and its verdict was
attached as metadata: a memo citing a statute that does not support the claim
shipped with a warning on it. The author's answer is buffered, audited,
repaired for exactly what the audit flagged, and only then revealed — bounded
by `max_repair_rounds` so a model that cannot satisfy its own auditor cannot
loop. Whether an answer is audited is decided from the answer itself.
"""

from __future__ import annotations

from legal_helper.config import load_settings
from legal_helper.providers import StreamEvent
from legal_helper.workflow import (
    WorkflowExecutor,
    _audit_is_repairable,
    _infer_cite_check_stakes,
    _should_run_cite_check,
)


class _ScriptedProvider:
    """Answers each stream call with the next scripted text."""

    name = "openai"
    model = "gpt-6.1-sol"

    def __init__(self, *texts: str) -> None:
        self.texts = list(texts)
        self.calls: list[dict] = []

    def stream(self, system, messages, tools=(), *, max_iterations=None, **kwargs):
        self.calls.append({"system": system, "messages": messages})
        text = self.texts.pop(0)
        yield StreamEvent("delta", {"text": text})
        yield StreamEvent("done", {"text": text, "usage": {"input_tokens": 1, "output_tokens": 1}})

    def run(self, *args, **kwargs):  # pragma: no cover
        raise AssertionError("no one-shot call expected")


def _executor(provider=None, **settings_updates) -> WorkflowExecutor:
    # config.yaml ships with enable_cite_check off for fast iteration; verified
    # synthesis is gated on it, so these tests state the flag they need.
    updates = {"enable_cite_check": True}
    updates.update(settings_updates)
    settings = load_settings(refresh=True).model_copy(update=updates)
    return WorkflowExecutor(settings=settings, provider=provider or _ScriptedProvider())


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


# --- what is held back and audited --------------------------------------


def test_answers_are_held_back_whenever_the_audit_is_on():
    assert _executor()._verify_before_reveal() is True


def test_verification_can_be_turned_off_in_one_setting():
    assert _executor(verify_before_reveal=False)._verify_before_reveal() is False
    assert _executor(enable_cite_check=False)._verify_before_reveal() is False


def test_the_audit_runs_on_answers_that_state_law_and_skips_the_rest():
    assert _should_run_cite_check("根据《民法典》第577条，违约方应当承担违约责任。")
    assert _should_run_cite_check("Under Article 17 of the Montreal Convention the carrier is liable.")
    assert not _should_run_cite_check("你好！有什么可以帮你？")
    assert not _should_run_cite_check("正确答案是 C。否定 C 后论证不成立。")
    assert not _should_run_cite_check("")


def test_stakes_escalate_on_filing_methods_not_on_brief():
    assert _infer_cite_check_stakes({"brief"}, "An internal memo.") == "internal"
    assert _infer_cite_check_stakes({"litigation-analysis"}, "An internal memo.") == "filed"
    assert _infer_cite_check_stakes(set(), "Draft 民事起诉状 for filing with the court") == "filed"


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
    prompt = executor._repair_prompt("DRAFT TEXT", audit, "Chinese")
    assert "DRAFT TEXT" in prompt
    assert "Article 577 does not support the claim." in prompt
    assert "Damages are capped" in prompt
    assert "ignore me" not in prompt  # only critical/major findings are listed
    assert "Never invent a replacement citation" in prompt
    assert "Chinese" in prompt


def test_a_flagged_answer_is_repaired_before_the_reader_sees_it(monkeypatch):
    provider = _ScriptedProvider(
        "根据《民法典》第999条，赔偿以合同价款为限。",
        "根据《民法典》第584条，赔偿以可预见的损失为限。",
    )
    executor = _executor(provider)
    audits = iter(
        [
            {"ok": False, "items": [{"severity": "critical", "claim": "第999条"}], "report_markdown": "x"},
            {"ok": True, "items": []},
        ]
    )

    def fake_audit(self, *, turn, final_text, cancel_event):
        yield StreamEvent("citation_audit", next(audits))

    monkeypatch.setattr(WorkflowExecutor, "_run_cite_check_after_summary", fake_audit)
    events = list(executor.stream("违约赔偿有没有上限？"))
    shown = "".join(e.data.get("text", "") for e in events if e.kind == "delta")
    assert "第584条" in shown and "第999条" not in shown
    assert [e.data.get("ok") for e in events if e.kind == "citation_audit"] == [True]
    assert len(provider.calls) == 2  # the draft and one repair
