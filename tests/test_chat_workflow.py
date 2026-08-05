from __future__ import annotations

import json
import threading
import time
from pathlib import Path

from fastapi.testclient import TestClient

from legal_helper.chat_models import ChatMessage, ChatSettings, utc_now
from legal_helper.chat_store import ChatStore
from legal_helper.citations import audit_citations
from legal_helper.config import load_settings
from legal_helper.errors import OUT_OF_MONEY_MESSAGE
from legal_helper.providers import RunResult, StreamEvent
from legal_helper.workflow import (
    GENERAL_TASK_SKILL,
    WorkflowExecutor,
    write_followup_docx,
    write_followup_pdf,
)


class PlannerProvider:
    name = "mock"
    model = "mock-advanced"
    supports_hosted_web_search = False
    supports_file_search = False
    supports_structured_outputs = False

    def __init__(self, text: str | None = None, exc: Exception | None = None) -> None:
        self.text = text
        self.exc = exc
        self.run_calls = 0

    def run(self, *args, **kwargs):
        self.run_calls += 1
        if self.exc:
            raise self.exc
        return RunResult(text=self.text or "{}", provider=self.name, model=self.model)

    def stream(self, *args, **kwargs):
        yield from ()

    def tool_runner(self, *args, **kwargs):
        return self.run(*args, **kwargs)


class CapturingPlannerProvider(PlannerProvider):
    def __init__(self, text: str | None = None, exc: Exception | None = None) -> None:
        super().__init__(text=text, exc=exc)
        self.prompts: list[str] = []

    def run(self, system=None, messages=None, *args, **kwargs):
        if messages:
            self.prompts.append(messages[-1]["content"])
        return super().run(system=system, messages=messages, *args, **kwargs)


class SequentialPlannerProvider(PlannerProvider):
    def __init__(self, texts: list[str]) -> None:
        super().__init__(text=None)
        self.texts = texts

    def run(self, *args, **kwargs):
        self.run_calls += 1
        text = self.texts.pop(0) if self.texts else "{}"
        return RunResult(text=text, provider=self.name, model=self.model)


class StreamingPlannerProvider(PlannerProvider):
    def __init__(self, text: str, final_text: str = "FINAL ANSWER") -> None:
        super().__init__(text=text)
        self.final_text = final_text
        self.integration_messages: list[str] = []

    def stream(self, system, messages, tools=(), *, max_iterations=None):
        self.integration_messages.append(messages[-1]["content"])
        yield StreamEvent("delta", {"text": self.final_text})
        yield StreamEvent("done", {"text": self.final_text})


class CapturingStreamProvider(PlannerProvider):
    def __init__(self, text: str, final_text: str = "FINAL ANSWER") -> None:
        super().__init__(text=text)
        self.final_text = final_text
        self.stream_calls: list[dict] = []

    def stream(self, system, messages, tools=(), *, max_iterations=None):
        self.stream_calls.append(
            {
                "system": system,
                "message": messages[-1]["content"],
                "tools": list(tools),
                "max_iterations": max_iterations,
            }
        )
        yield StreamEvent("delta", {"text": self.final_text})
        yield StreamEvent("done", {"text": self.final_text})


class SubAgentStreamProvider(PlannerProvider):
    def __init__(self, text: str) -> None:
        super().__init__(text="{}")
        self.stream_text = text

    def stream(self, *args, **kwargs):
        yield StreamEvent("delta", {"text": self.stream_text})
        yield StreamEvent("done", {"text": self.stream_text})


class RateLimitOnceSubAgentProvider(PlannerProvider):
    _lock = threading.Lock()
    _attempts: dict[str, int] = {}

    @classmethod
    def reset(cls) -> None:
        with cls._lock:
            cls._attempts = {}

    def stream(self, system, messages, tools=(), *, max_iterations=None):
        task = messages[-1]["content"]
        with self._lock:
            attempt = self._attempts.get(task, 0)
            self._attempts[task] = attempt + 1
        if "Task two." in task and attempt == 0:
            raise RuntimeError("Error code: 429 - rate_limit_error: tokens per minute")
        text = f"SUB AGENT OUTPUT: {task}"
        yield StreamEvent("delta", {"text": text})
        yield StreamEvent("done", {"text": text})


def test_chat_store_persists_messages_and_memory(tmp_path):
    settings = load_settings(refresh=True).model_copy(update={"outputs_dir": tmp_path / "out"})
    store = ChatStore(settings, tmp_path / "chat.sqlite3")
    chat = store.create_chat(
        settings=ChatSettings(provider="openai", model="gpt-5.5", reasoning_effort="high")
    )
    store.add_message(chat.id, "user", "Analyze passport tearing on board.")
    store.add_message(chat.id, "assistant", "Analysis result.")
    store.update_chat(chat.id, memory_summary="Prior analysis: passport tearing.")

    loaded = store.get_chat(chat.id)
    messages = store.list_messages(chat.id)
    assert loaded.memory_summary == "Prior analysis: passport tearing."
    assert [m.role for m in messages] == ["user", "assistant"]


def test_chat_store_tracks_active_and_finished_runs(tmp_path):
    settings = load_settings(refresh=True).model_copy(update={"outputs_dir": tmp_path / "out"})
    store = ChatStore(settings, tmp_path / "chat.sqlite3")
    chat = store.create_chat()
    user = store.add_message(chat.id, "user", "Analyze passport tearing on board.")
    assistant = store.add_message(chat.id, "assistant", "", {"run_status": "running"})

    run = store.create_run(chat.id, user.id, assistant.id)

    assert store.has_active_run(chat.id)
    assert store.list_active_runs(chat.id)[0].assistant_message_id == assistant.id

    store.update_message(assistant.id, "Analysis result.", {"run_status": "complete"})
    store.update_run(run.id, status="complete", finished=True)

    assert not store.has_active_run(chat.id)
    assert store.list_runs(chat.id)[0].status == "complete"


def test_chat_store_cancelled_runs_are_not_active(tmp_path):
    settings = load_settings(refresh=True).model_copy(update={"outputs_dir": tmp_path / "out"})
    store = ChatStore(settings, tmp_path / "chat.sqlite3")
    chat = store.create_chat()
    user = store.add_message(chat.id, "user", "Analyze passport tearing on board.")
    assistant = store.add_message(chat.id, "assistant", "", {"run_status": "running"})
    run = store.create_run(chat.id, user.id, assistant.id)

    store.update_run(run.id, status="cancelled", error="Cancelled by user.", finished=True)

    loaded = store.get_run(run.id)
    assert loaded.status == "cancelled"
    assert loaded.finished_at
    assert not store.has_active_run(chat.id)


def test_chat_store_marks_orphaned_runs_as_error(tmp_path):
    settings = load_settings(refresh=True).model_copy(update={"outputs_dir": tmp_path / "out"})
    store = ChatStore(settings, tmp_path / "chat.sqlite3")
    chat = store.create_chat()
    user = store.add_message(chat.id, "user", "Analyze passport tearing on board.")
    assistant = store.add_message(chat.id, "assistant", "", {"run_status": "running"})
    run = store.create_run(chat.id, user.id, assistant.id)

    assert store.mark_orphaned_runs("Server restarted.") == 1

    loaded = store.get_run(run.id)
    assert loaded.status == "error"
    assert loaded.error == "Server restarted."
    assert loaded.finished_at


def test_workflow_builds_primary_and_low_effort_models(tmp_path, monkeypatch):
    settings = load_settings(refresh=True).model_copy(
        update={
            "provider": "anthropic",
            "anthropic_model": "claude-sonnet-4-6",
            "anthropic_fast_model": "claude-haiku-4-5",
            "outputs_dir": tmp_path / "out",
        }
    )
    calls: list[tuple[bool, str]] = []

    def fake_build_provider(settings_arg, *, fast=False):
        calls.append((fast, settings_arg.model_for_provider(fast=fast)))
        return PlannerProvider(
            """
            {
              "title": "Direct",
              "user_language": "English",
              "execution_mode": "direct_answer",
              "direct_response": "ok",
              "routing_reason": "test",
              "agent_tasks": []
            }
            """
        )

    import legal_helper.workflow as workflow_mod

    monkeypatch.setattr(workflow_mod, "build_provider", fake_build_provider)

    WorkflowExecutor(settings=settings)

    # Primary, planner (fast), cite-check (fast). The integration provider
    # reuses the primary when _integration_settings leaves settings unchanged.
    assert calls == [
        (False, "claude-sonnet-4-6"),
        (True, "claude-haiku-4-5"),
        (True, "claude-haiku-4-5"),
    ]


def test_llm_planner_accepts_variable_agent_count(tmp_path):
    settings = load_settings(refresh=True).model_copy(update={"outputs_dir": tmp_path / "out"})
    provider = PlannerProvider(
        """
        {
          "title": "Complex passport workflow",
          "user_language": "Chinese",
          "issue_decomposition": ["treaty", "operations", "risk", "drafting"],
          "agent_tasks": [
            {"id": "task_1", "skill_name": "brief", "title": "Treaty", "task": "Research treaty sources.", "depends_on": []},
            {"id": "task_2", "skill_name": "compliance-check", "title": "Ops", "task": "Analyze operations.", "depends_on": []},
            {"id": "task_3", "skill_name": "legal-risk-assessment", "title": "Risk", "task": "Assess risk.", "depends_on": []},
            {"id": "task_4", "skill_name": "legal-response", "title": "SOP", "task": "Draft SOP.", "depends_on": ["task_1", "task_2"]}
          ],
          "integration_instructions": "Integrate in Chinese.",
          "citation_requirements": "Use pinpoint citations."
        }
        """
    )
    plan = WorkflowExecutor(settings=settings, provider=provider).plan("护照撕毁复杂问题")
    assert plan.execution_mode == "agent_workflow"
    # Plans larger than MAX_SPECIALISTS (3) are capped: task_4 is dropped and
    # the survivors keep their ids/deps.
    assert len(plan.agent_tasks) == 3
    deps_by_id = {task.id: task.depends_on for task in plan.agent_tasks}
    assert deps_by_id["task_1"] == []
    assert deps_by_id["task_2"] == []
    assert deps_by_id["task_3"] == []
    assert "task_4" not in deps_by_id


def test_planner_removes_redundant_final_synthesis_task_and_preserves_deps(tmp_path):
    settings = load_settings(refresh=True).model_copy(update={"outputs_dir": tmp_path / "out"})
    provider = PlannerProvider(
        """
        {
          "title": "Passport workflow",
          "user_language": "Chinese",
          "issue_decomposition": ["sources", "ops", "final"],
          "agent_tasks": [
            {"id": "task_1", "skill_name": "brief", "title": "Sources", "task": "Research sources.", "depends_on": []},
            {"id": "task_2", "skill_name": "compliance-check", "title": "Ops", "task": "Analyze operations.", "depends_on": ["task_1"]},
            {"id": "task_3", "skill_name": "legal-response", "title": "Draft final Chinese legal analysis", "task": "Prepare the final answer in Chinese and synthesize all prior research.", "depends_on": ["task_1", "task_2"]}
          ],
          "integration_instructions": "Integrate in Chinese.",
          "citation_requirements": "Use pinpoint citations."
        }
        """
    )
    plan = WorkflowExecutor(settings=settings, provider=provider).plan("旅客机上撕毁护照怎么办？")
    assert [task.id for task in plan.agent_tasks] == ["task_1", "task_2"]
    deps_by_id = {task.id: task.depends_on for task in plan.agent_tasks}
    assert deps_by_id["task_1"] == []
    assert deps_by_id["task_2"] == ["task_1"]
    assert "Final synthesis requirements moved" in plan.integration_instructions


def test_planner_removes_observed_synthesis_and_qc_tasks(tmp_path):
    """Regression: matches the exact mis-planned 6-agent run from 2026-05-12.

    Reproduces the planner output that produced parallel "Source verification
    and citation quality control" + "Chinese final legal analysis draft"
    sub-agents alongside four legitimate specialists. Both extras must be
    dropped (orchestrator owns synthesis and citation audit), and the
    remaining specialists must keep their natural depends_on edges.
    """
    settings = load_settings(refresh=True).model_copy(update={"outputs_dir": tmp_path / "out"})
    provider = PlannerProvider(
        """
        {
          "title": "Passport tearing onboard",
          "user_language": "Chinese",
          "issue_decomposition": ["sources", "authority", "ops", "risk", "qc", "draft"],
          "agent_tasks": [
            {"id": "task_1", "skill_name": "brief", "title": "International source mapping on travel documents", "task": "Research relevant instruments.", "depends_on": []},
            {"id": "task_2", "skill_name": "legal-response", "title": "Aircraft commander and crew authority", "task": "Analyze commander authority.", "depends_on": []},
            {"id": "task_3", "skill_name": "compliance-check", "title": "Airline operational compliance workflow", "task": "Define onboard workflow.", "depends_on": []},
            {"id": "task_4", "skill_name": "legal-risk-assessment", "title": "Risk, liability, and passenger-rights assessment", "task": "Assess carrier risk.", "depends_on": []},
            {"id": "task_5", "skill_name": "brief", "title": "Source verification and citation quality control", "task": "Verify all primary legal source references likely to be used in the final answer.", "depends_on": ["task_1", "task_2", "task_3", "task_4"]},
            {"id": "task_6", "skill_name": "legal-response", "title": "Chinese final legal analysis draft", "task": "Using the outputs of the source, authority, compliance, risk, and verification tasks, draft a Chinese-language legal analysis.", "depends_on": ["task_1", "task_2", "task_3", "task_4", "task_5"]}
          ],
          "integration_instructions": "Integrate in Chinese.",
          "citation_requirements": "Use pinpoint citations."
        }
        """
    )
    plan = WorkflowExecutor(settings=settings, provider=provider).plan("旅客机上撕毁护照怎么办？")
    ids = [task.id for task in plan.agent_tasks]
    # task_5 (qc) and task_6 (synthesis) are stripped, then the
    # MAX_SPECIALISTS=3 cap drops task_4.
    assert ids == ["task_1", "task_2", "task_3"]
    assert all(task.depends_on == [] for task in plan.agent_tasks)
    assert "Final synthesis requirements moved" in plan.integration_instructions


def test_router_replans_generic_claude_task_placeholders(tmp_path):
    settings = load_settings(refresh=True).model_copy(update={"outputs_dir": tmp_path / "out"})
    bad_router_plan = """
    {
      "title": "Passport tearing onboard",
      "user_language": "Chinese",
      "execution_mode": "agent_workflow",
      "routing_reason": "needs research",
      "issue_decomposition": ["Task 1", "Task 2"],
      "agent_tasks": [
        {"id": "task_1", "skill_name": "brief", "title": "Task 1", "task": "source_verification", "depends_on": []},
        {"id": "task_2", "skill_name": "brief", "title": "Task 2", "task": "comparative_legal_research", "depends_on": []}
      ],
      "integration_instructions": "Integrate.",
      "citation_requirements": "Use citations."
    }
    """
    good_planner_plan = """
    {
      "title": "Passport tearing onboard",
      "user_language": "Chinese",
      "execution_mode": "agent_workflow",
      "routing_reason": "needs research",
      "issue_decomposition": ["treaty", "operations"],
      "agent_tasks": [
        {"id": "task_1", "skill_name": "brief", "title": "Treaty and ICAO source research", "task": "Research Tokyo Convention, Chicago Convention Annex 9, and ICAO/IATA source support for passenger destruction of travel documents on board. Answer in English with pinpoint citations.", "depends_on": []},
        {"id": "task_2", "skill_name": "compliance-check", "title": "Operational compliance workflow", "task": "Analyze airline and crew onboard response, reporting, evidence preservation, handoff, and limits. Answer in English with source support.", "depends_on": []}
      ],
      "integration_instructions": "Integrate in Chinese.",
      "citation_requirements": "Use pinpoint citations."
    }
    """
    provider = SequentialPlannerProvider([bad_router_plan, good_planner_plan])

    plan = WorkflowExecutor(settings=settings, provider=provider).plan("旅客机上撕毁护照怎么办？")

    assert provider.run_calls == 2
    assert [task.title for task in plan.agent_tasks] == [
        "Treaty and ICAO source research",
        "Operational compliance workflow",
    ]


def test_stream_forwards_transient_subagent_previews_not_answer_text(tmp_path, monkeypatch):
    settings = load_settings(refresh=True).model_copy(update={"outputs_dir": tmp_path / "out"})
    plan_json = """
    {
      "title": "Parallel workflow",
      "user_language": "English",
      "issue_decomposition": ["one", "two"],
      "agent_tasks": [
        {"id": "task_1", "skill_name": "brief", "title": "One", "task": "Task one.", "depends_on": []},
        {"id": "task_2", "skill_name": "compliance-check", "title": "Two", "task": "Task two.", "depends_on": []}
      ],
      "integration_instructions": "Integrate.",
      "citation_requirements": "Use citations."
    }
    """
    provider = StreamingPlannerProvider(plan_json, final_text="FINAL ONLY")

    import legal_helper.workflow as workflow_mod

    monkeypatch.setattr(
        workflow_mod,
        "build_provider",
        lambda settings: SubAgentStreamProvider("SUB AGENT OUTPUT"),
    )

    events = list(WorkflowExecutor(settings=settings, provider=provider).stream("test"))
    assert [ev.kind for ev in events].count("done") == 1
    assert events[-1].kind == "done"
    # Sub-agent output must NOT leak into the saved answer — final is the
    # integration text only, never the sub-agent stream.
    assert events[-1].data["text"] == "FINAL ONLY"
    assert all("text" not in ev.data for ev in events if ev.kind == "agent_task_finished")
    assert provider.integration_messages

    # Sub-agent live previews (agent_delta) ARE forwarded now (throttled tail),
    # but they are transient: they carry a rolling `tail` + task attribution and
    # never accumulate into the assistant answer.
    agent_deltas = [ev for ev in events if ev.kind == "agent_delta"]
    for ev in agent_deltas:
        assert "task_id" in ev.data
        assert "tail" in ev.data
        assert ev.data.get("phase") in {"answer", "reasoning"}
    assert "SUB AGENT OUTPUT" in provider.integration_messages[-1]


def test_stream_limits_parallel_subagents_to_configured_cap(tmp_path, monkeypatch):
    settings = load_settings(refresh=True).model_copy(
        update={"outputs_dir": tmp_path / "out", "max_concurrent_agents": 1}
    )
    plan_json = """
    {
      "title": "Throttled workflow",
      "user_language": "English",
      "issue_decomposition": ["one", "two"],
      "agent_tasks": [
        {"id": "task_1", "skill_name": "brief", "title": "One", "task": "Task one.", "depends_on": []},
        {"id": "task_2", "skill_name": "compliance-check", "title": "Two", "task": "Task two.", "depends_on": []}
      ],
      "integration_instructions": "Integrate.",
      "citation_requirements": "Use citations."
    }
    """
    provider = StreamingPlannerProvider(plan_json, final_text="FINAL ONLY")

    import legal_helper.workflow as workflow_mod

    monkeypatch.setattr(
        workflow_mod,
        "build_provider",
        lambda settings: SubAgentStreamProvider("SUB AGENT OUTPUT"),
    )

    events = list(WorkflowExecutor(settings=settings, provider=provider).stream("test"))
    task_lifecycle = [
        (ev.kind, ev.data.get("task_id") or ev.data.get("id"))
        for ev in events
        if ev.kind in {"agent_task_started", "agent_task_finished"}
    ]

    assert task_lifecycle == [
        ("agent_task_started", "task_1"),
        ("agent_task_finished", "task_1"),
        ("agent_task_started", "task_2"),
        ("agent_task_finished", "task_2"),
    ]


def test_stream_reduces_concurrency_and_retries_after_rate_limit(tmp_path, monkeypatch):
    settings = load_settings(refresh=True).model_copy(
        update={"provider": "openai", "outputs_dir": tmp_path / "out", "max_concurrent_agents": "auto"}
    )
    plan_json = """
    {
      "title": "Adaptive workflow",
      "user_language": "English",
      "issue_decomposition": ["one", "two"],
      "agent_tasks": [
        {"id": "task_1", "skill_name": "brief", "title": "One", "task": "Task one.", "depends_on": []},
        {"id": "task_2", "skill_name": "compliance-check", "title": "Two", "task": "Task two.", "depends_on": []}
      ],
      "integration_instructions": "Integrate.",
      "citation_requirements": "Use citations."
    }
    """
    provider = StreamingPlannerProvider(plan_json, final_text="FINAL ONLY")

    import legal_helper.workflow as workflow_mod

    RateLimitOnceSubAgentProvider.reset()
    monkeypatch.setattr(workflow_mod, "build_provider", lambda settings: RateLimitOnceSubAgentProvider())

    events = list(WorkflowExecutor(settings=settings, provider=provider).stream("test"))
    kinds = [ev.kind for ev in events]
    task_2_starts = [
        ev for ev in events if ev.kind == "agent_task_started" and ev.data.get("id") == "task_2"
    ]

    assert "agent_task_rate_limited" in kinds
    assert "agent_concurrency_reduced" in kinds
    assert len(task_2_starts) == 2
    assert events[-1].kind == "done"
    assert events[-1].data["text"] == "FINAL ONLY"
    assert "Task one." in provider.integration_messages[-1]
    assert "Task two." in provider.integration_messages[-1]


def test_stream_direct_answer_skips_subagents_and_integration(tmp_path, monkeypatch):
    settings = load_settings(refresh=True).model_copy(update={"outputs_dir": tmp_path / "out"})
    plan_json = """
    {
      "title": "Greeting",
      "user_language": "English",
      "execution_mode": "direct_answer",
      "direct_response": "Hi, how can I help today?",
      "routing_reason": "Simple greeting.",
      "issue_decomposition": [],
      "agent_tasks": [],
      "integration_instructions": "",
      "citation_requirements": ""
    }
    """
    provider = StreamingPlannerProvider(plan_json, final_text="SHOULD NOT STREAM")

    import legal_helper.workflow as workflow_mod

    monkeypatch.setattr(
        workflow_mod,
        "build_provider",
        lambda settings, **kwargs: (_ for _ in ()).throw(AssertionError("sub-agent provider should not be built")),
    )

    events = list(WorkflowExecutor(settings=settings, provider=provider).stream("hello"))
    kinds = [ev.kind for ev in events]
    assert provider.run_calls == 1
    assert provider.integration_messages == []
    assert "agent_task_started" not in kinds
    # Direct answers still get a citation_audit event, but it must be a
    # skipped/auto-pass payload (no cite-check sub-agent was built — the
    # monkeypatched build_provider above would have raised).
    assert kinds.count("citation_audit") == 1
    audit = next(ev.data for ev in events if ev.kind == "citation_audit")
    assert audit.get("skip_reason")
    assert events[-1].kind == "done"
    assert events[-1].data["text"] == "Hi, how can I help today?"


def test_stream_clarify_skips_subagents_and_integration(tmp_path, monkeypatch):
    settings = load_settings(refresh=True).model_copy(update={"outputs_dir": tmp_path / "out"})
    plan_json = """
    {
      "title": "Clarify document review",
      "user_language": "English",
      "execution_mode": "clarify",
      "direct_response": "Which agreement or clause would you like me to review?",
      "routing_reason": "The request lacks the document or issue to analyze.",
      "issue_decomposition": [],
      "agent_tasks": [],
      "integration_instructions": "",
      "citation_requirements": ""
    }
    """
    provider = StreamingPlannerProvider(plan_json, final_text="SHOULD NOT STREAM")

    import legal_helper.workflow as workflow_mod

    monkeypatch.setattr(
        workflow_mod,
        "build_provider",
        lambda settings, **kwargs: (_ for _ in ()).throw(AssertionError("sub-agent provider should not be built")),
    )

    events = list(WorkflowExecutor(settings=settings, provider=provider).stream("review this"))
    kinds = [ev.kind for ev in events]
    assert provider.run_calls == 1
    assert provider.integration_messages == []
    assert "agent_task_started" not in kinds
    # Clarify turns also emit a single skipped citation_audit event.
    assert kinds.count("citation_audit") == 1
    audit = next(ev.data for ev in events if ev.kind == "citation_audit")
    assert audit.get("skip_reason")
    assert events[-1].kind == "done"
    assert events[-1].data["text"] == "Which agreement or clause would you like me to review?"


def test_direct_plan_with_accidental_tasks_is_normalized_to_zero(tmp_path):
    settings = load_settings(refresh=True).model_copy(update={"outputs_dir": tmp_path / "out"})
    provider = PlannerProvider(
        """
        {
          "title": "Simple status",
          "user_language": "English",
          "execution_mode": "direct_answer",
          "direct_response": "Yes, I can export that as a PDF.",
          "routing_reason": "Simple capability response.",
          "issue_decomposition": ["unneeded"],
          "agent_tasks": [
            {"id": "task_1", "skill_name": "brief", "title": "Unneeded", "task": "Do work.", "depends_on": []}
          ],
          "integration_instructions": "",
          "citation_requirements": ""
        }
        """
    )
    plan = WorkflowExecutor(settings=settings, provider=provider).plan("can you export pdf?")
    assert plan.execution_mode == "direct_answer"
    assert plan.agent_tasks == []
    assert plan.issue_decomposition == []
    assert plan.direct_response == "Yes, I can export that as a PDF."


def test_router_prompt_gets_chat_context_for_history_followups(tmp_path):
    settings = load_settings(refresh=True).model_copy(update={"outputs_dir": tmp_path / "out"})
    provider = CapturingPlannerProvider(
        """
        {
          "title": "Export prior findings",
          "user_language": "English",
          "execution_mode": "direct_answer",
          "direct_response": "I will export the prior findings.",
          "routing_reason": "The request references prior findings.",
          "issue_decomposition": [],
          "agent_tasks": [],
          "integration_instructions": "",
          "citation_requirements": ""
        }
        """
    )
    recent = [
        ChatMessage(id="u", chat_id="c", role="user", content="Analyze passport tearing on board.", created_at=utc_now()),
        ChatMessage(
            id="a",
            chat_id="c",
            role="assistant",
            content="Detailed prior findings about passport tearing.",
            created_at=utc_now(),
        ),
    ]
    WorkflowExecutor(settings=settings, provider=provider).plan(
        "generate a document with your findings",
        recent_messages=recent,
        memory_summary="Prior analysis: passenger tearing a passport onboard.",
    )
    assert "Prior analysis: passenger tearing a passport onboard." in provider.prompts[0]
    assert "Detailed prior findings" in provider.prompts[0]


def test_router_failure_greeting_uses_local_direct_fallback(tmp_path):
    settings = load_settings(refresh=True).model_copy(update={"outputs_dir": tmp_path / "out"})
    provider = PlannerProvider(exc=RuntimeError("Invalid schema for response_format 'WorkflowPlan'"))
    plan = WorkflowExecutor(settings=settings, provider=provider).plan("how are you today?")
    assert plan.execution_mode == "direct_answer"
    assert plan.agent_tasks == []
    assert "ready to help" in plan.direct_response


def test_router_failure_nonlegal_search_request_uses_single_general_task(tmp_path):
    settings = load_settings(refresh=True).model_copy(update={"outputs_dir": tmp_path / "out"})
    provider = PlannerProvider(exc=RuntimeError("planner unavailable"))

    plan = WorkflowExecutor(settings=settings, provider=provider).plan(
        "Find good noise cancelling headphones under $200 today."
    )

    assert plan.execution_mode == "agent_workflow"
    assert provider.run_calls == 2
    assert [task.skill_name for task in plan.agent_tasks] == [GENERAL_TASK_SKILL]
    assert plan.agent_tasks[0].depends_on == []
    assert plan.agent_tasks[0].title == "General answer"


def test_stream_general_task_is_single_threaded_and_search_capable(tmp_path, monkeypatch):
    settings = load_settings(refresh=True).model_copy(
        update={"provider": "openai", "outputs_dir": tmp_path / "out", "enable_web_search": True}
    )
    plan_json = f"""
    {{
      "title": "Headphone search",
      "user_language": "English",
      "execution_mode": "agent_workflow",
      "routing_reason": "Needs current non-legal product research.",
      "issue_decomposition": ["general search"],
      "agent_tasks": [
        {{"id": "task_1", "skill_name": "{GENERAL_TASK_SKILL}", "title": "General answer", "task": "Search for current headphone options under $200.", "depends_on": []}}
      ],
      "integration_instructions": "Answer plainly with sources.",
      "citation_requirements": ""
    }}
    """
    provider = CapturingStreamProvider(plan_json, final_text="Final general answer.")

    import legal_helper.workflow as workflow_mod

    monkeypatch.setattr(workflow_mod, "build_provider", lambda settings: provider)

    events = list(WorkflowExecutor(settings=settings, provider=provider).stream("Find headphones under $200 today."))
    task_lifecycle = [
        (ev.kind, ev.data.get("task_id") or ev.data.get("id"))
        for ev in events
        if ev.kind in {"agent_task_started", "agent_task_finished"}
    ]

    # Direct-LLM shortcut: a single general-answer plan skips sub-agent
    # dispatch and the tools-empty integration step entirely. The model gets
    # the user's verbatim message + tool surface in one call.
    assert task_lifecycle == []
    direct_call = provider.stream_calls[-1]
    tool_names = {
        (t.get("name") if isinstance(t, dict) else getattr(t, "name", None))
        or (t.get("type") if isinstance(t, dict) else None)
        for t in direct_call["tools"]
    }
    assert "web_search" in tool_names
    assert "fetch_url_to_artifact" in tool_names
    assert "read_document" in tool_names
    assert "write_pdf" in tool_names
    assert "write_docx" in tool_names
    assert "write_xlsx" in tool_names
    assert "write_pptx" in tool_names
    assert "inspect_pptx" in tool_names
    assert "render_pptx_slides" in tool_names
    assert "general-purpose assistant" in direct_call["system"]
    assert "Find headphones under $200 today." in direct_call["message"]
    assert events[-1].data["text"] == "Final general answer."


def test_final_synthesis_has_document_io_tools(tmp_path, monkeypatch):
    settings = load_settings(refresh=True).model_copy(update={"outputs_dir": tmp_path / "out"})
    plan_json = """
    {
      "title": "Document rewrite",
      "user_language": "English",
      "execution_mode": "agent_workflow",
      "routing_reason": "Needs a final authored answer.",
      "issue_decomposition": ["source support"],
      "agent_tasks": [
        {"id": "task_1", "skill_name": "brief", "title": "Source support", "task": "Research the supporting legal points. coverage: source-support", "depends_on": []}
      ],
      "integration_instructions": "Answer and create a deliverable if useful.",
      "citation_requirements": ""
    }
    """
    provider = CapturingStreamProvider(plan_json, final_text="Final answer.")
    import legal_helper.workflow as workflow_mod

    monkeypatch.setattr(workflow_mod, "build_provider", lambda settings, fast=False: provider)

    events = list(WorkflowExecutor(settings=settings, provider=provider).stream("Please create the finished file."))

    final_call = provider.stream_calls[-1]
    tool_names = {
        (t.get("name") if isinstance(t, dict) else getattr(t, "name", None))
        or (t.get("type") if isinstance(t, dict) else None)
        for t in final_call["tools"]
    }
    assert {
        "write_pdf",
        "write_docx",
        "write_xlsx",
        "write_pptx",
        "inspect_xlsx",
        "edit_xlsx_cells",
        "inspect_pptx",
        "edit_pptx_text",
        "render_pptx_slides",
        "read_document",
        "fetch_url_to_artifact",
    } <= tool_names
    assert events[-1].data["text"] == "Final answer."


def test_out_of_money_stream_message(tmp_path):
    settings = load_settings(refresh=True).model_copy(update={"outputs_dir": tmp_path / "out"})
    provider = PlannerProvider(exc=RuntimeError("insufficient_quota: billing hard limit"))
    events = list(WorkflowExecutor(settings=settings, provider=provider).stream("test"))
    assert any(ev.kind == "delta" and ev.data.get("text") == OUT_OF_MONEY_MESSAGE for ev in events)
    assert any(ev.kind == "error" and ev.data.get("message") == OUT_OF_MONEY_MESSAGE for ev in events)


def test_server_chat_turn_persists_after_stream_response_is_not_consumed(tmp_path, monkeypatch):
    settings = load_settings(refresh=True).model_copy(update={"outputs_dir": tmp_path / "out"})

    import legal_helper.server as server_mod

    store = ChatStore(settings, tmp_path / "chat.sqlite3")
    monkeypatch.setattr(server_mod, "_store", lambda: store)
    monkeypatch.setattr(server_mod, "_settings_for_chat", lambda chat_settings, chat_id=None: settings)
    monkeypatch.setattr(server_mod, "summarize_with_fast_model", lambda *args, **kwargs: "summary")

    class FakeExecutor:
        def __init__(self, settings):
            self.settings = settings

        def stream(self, *args, **kwargs):
            yield StreamEvent("delta", {"text": "Background answer."})
            yield StreamEvent("done", {"text": "Background answer."})

    monkeypatch.setattr(server_mod, "WorkflowExecutor", FakeExecutor)

    chat = store.create_chat()
    server_mod._stream_chat_turn(chat.id, server_mod.ChatStreamRequest(message="test"))

    deadline = time.time() + 2
    while time.time() < deadline and store.has_active_run(chat.id):
        time.sleep(0.01)
    while time.time() < deadline and store.get_chat(chat.id).memory_summary != "summary":
        time.sleep(0.01)

    messages = store.list_messages(chat.id)
    assert [m.role for m in messages] == ["user", "assistant"]
    assert messages[-1].content == "Background answer."
    assert messages[-1].metadata["run_status"] == "complete"
    assert store.get_chat(chat.id).memory_summary == "summary"
    assert not store.has_active_run(chat.id)


def test_server_generates_chat_title_with_fast_model(tmp_path, monkeypatch):
    settings = load_settings(refresh=True).model_copy(update={"outputs_dir": tmp_path / "out"})

    import legal_helper.server as server_mod

    store = ChatStore(settings, tmp_path / "chat.sqlite3")
    monkeypatch.setattr(server_mod, "_store", lambda: store)
    monkeypatch.setattr(server_mod, "_settings_for_chat", lambda chat_settings, chat_id=None: settings)
    monkeypatch.setattr(server_mod, "summarize_with_fast_model", lambda *args, **kwargs: "summary")
    monkeypatch.setattr(
        server_mod,
        "summarize_title_with_fast_model",
        lambda *args, **kwargs: "Passport Damage Review",
    )

    class FakeExecutor:
        def __init__(self, settings):
            self.settings = settings

        def stream(self, *args, **kwargs):
            yield StreamEvent("delta", {"text": "Analysis result."})
            yield StreamEvent("done", {"text": "Analysis result."})

    monkeypatch.setattr(server_mod, "WorkflowExecutor", FakeExecutor)

    chat = store.create_chat()
    server_mod._stream_chat_turn(chat.id, server_mod.ChatStreamRequest(message="Please analyze this incident."))

    deadline = time.time() + 2
    while time.time() < deadline and store.get_chat(chat.id).title == "New chat":
        time.sleep(0.01)

    assert store.get_chat(chat.id).title == "Passport Damage Review"


def test_server_does_not_replace_edited_chat_title(tmp_path, monkeypatch):
    settings = load_settings(refresh=True).model_copy(update={"outputs_dir": tmp_path / "out"})

    import legal_helper.server as server_mod

    store = ChatStore(settings, tmp_path / "chat.sqlite3")
    monkeypatch.setattr(server_mod, "_store", lambda: store)
    monkeypatch.setattr(server_mod, "_settings_for_chat", lambda chat_settings, chat_id=None: settings)
    monkeypatch.setattr(server_mod, "summarize_with_fast_model", lambda *args, **kwargs: "summary")
    monkeypatch.setattr(server_mod, "summarize_title_with_fast_model", lambda *args, **kwargs: "Generated Title")

    class FakeExecutor:
        def __init__(self, settings):
            self.settings = settings

        def stream(self, *args, **kwargs):
            yield StreamEvent("delta", {"text": "Analysis result."})
            yield StreamEvent("done", {"text": "Analysis result."})

    monkeypatch.setattr(server_mod, "WorkflowExecutor", FakeExecutor)

    chat = store.create_chat(title="Manual Title")
    server_mod._stream_chat_turn(chat.id, server_mod.ChatStreamRequest(message="Please analyze this incident."))

    deadline = time.time() + 2
    while time.time() < deadline and store.has_active_run(chat.id):
        time.sleep(0.01)

    assert store.get_chat(chat.id).title == "Manual Title"


def test_citation_audit_requires_pinpoints():
    weak = "## Sources\n- ICAO Annex 9: https://example.test"
    strong = "## Sources\n- ICAO Annex 9, Standard 5.18, page 2: https://example.test"
    assert not audit_citations(weak).ok
    assert audit_citations(strong).ok


def test_followup_pdf_writer(tmp_path):
    settings = load_settings(refresh=True).model_copy(update={"outputs_dir": tmp_path / "out"})
    artifact = write_followup_pdf(settings, "# Analysis\n\nBody", "Legal Analysis")
    assert artifact["filename"].endswith(".pdf")
    assert Path(artifact["path"]).is_file()


def test_followup_docx_writer(tmp_path):
    settings = load_settings(refresh=True).model_copy(update={"outputs_dir": tmp_path / "out"})
    artifact = write_followup_docx(settings, "# Analysis\n\nBody", "Legal Analysis")
    assert artifact["filename"].endswith(".docx")
    assert Path(artifact["path"]).is_file()


def test_legacy_document_export_mode_is_demoted_to_agent_workflow(tmp_path):
    """A planner that still emits `document_export` (cached prompt, stale build,
    older model) is no longer special-cased — the plan normalizer demotes it
    to `agent_workflow` so the model decides via the `write_pdf` / `write_docx`
    tools instead of a keyword router."""
    settings = load_settings(refresh=True).model_copy(update={"outputs_dir": tmp_path / "out"})
    provider = PlannerProvider(
        """
        {
          "title": "Export PDF",
          "user_language": "Chinese",
          "execution_mode": "document_export",
          "direct_response": "",
          "artifact_format": "pdf",
          "routing_reason": "User wants the previous answer exported as a PDF.",
          "issue_decomposition": [],
          "agent_tasks": []
        }
        """
    )
    executor = WorkflowExecutor(settings=settings, provider=provider)
    plan = executor.plan("生成pdf")
    assert plan.execution_mode == "agent_workflow"


def test_cancel_chat_runs_endpoint_marks_active_run_cancelled(tmp_path, monkeypatch):
    settings = load_settings(refresh=True).model_copy(update={"outputs_dir": tmp_path / "out"})

    import legal_helper.server as server_mod

    store = ChatStore(settings, tmp_path / "chat.sqlite3")
    monkeypatch.setattr(server_mod, "_store", lambda: store)

    chat = store.create_chat()
    user = store.add_message(chat.id, "user", "Analyze passport tearing on board.")
    assistant = store.add_message(chat.id, "assistant", "", {"run_status": "running"})
    run = store.create_run(chat.id, user.id, assistant.id)

    client = TestClient(server_mod.app)
    response = client.post(f"/api/chats/{chat.id}/runs/cancel")

    assert response.status_code == 200
    assert response.json()["count"] == 1
    loaded = store.get_run(run.id)
    message = store.get_message(assistant.id)
    assert loaded.status == "cancelled"
    assert loaded.finished_at
    assert message.metadata["run_status"] == "cancelled"
    assert not store.has_active_run(chat.id)
    assert store.list_events(chat.id)[0].event_type == "run_cancelled"


def test_message_export_records_tool_activity(tmp_path, monkeypatch):
    settings = load_settings(refresh=True).model_copy(update={"outputs_dir": tmp_path / "out"})

    import legal_helper.server as server_mod

    store = ChatStore(settings, tmp_path / "chat.sqlite3")
    monkeypatch.setattr(server_mod, "_store", lambda: store)
    monkeypatch.setattr(server_mod, "_settings_for_chat", lambda chat_settings, chat_id=None: settings)

    chat = store.create_chat()
    msg = store.add_message(chat.id, "assistant", "# Analysis\n\nBody")

    client = TestClient(server_mod.app)
    response = client.post(
        f"/api/chats/{chat.id}/messages/{msg.id}/export",
        json={"title": "Legal Analysis"},
    )

    assert response.status_code == 200
    assert response.json()["filename"].endswith(".pdf")
    events = store.list_events(chat.id)
    assert [event.event_type for event in events] == [
        "local_tool_call_started",
        "local_tool_call_finished",
    ]
    assert events[0].data["tool_name"] == "write_pdf"


def test_artifact_scan_stays_inside_chat_output_dir(tmp_path, monkeypatch):
    monkeypatch.setenv("LEGAL_HELPER_OUTPUTS_DIR", str(tmp_path / "out"))
    settings = load_settings(refresh=True)
    store = ChatStore(settings, tmp_path / "chat.sqlite3")
    chat_a = store.create_chat()
    chat_b = store.create_chat()

    dir_a = settings.outputs_dir / "chat_artifacts" / chat_a.id
    dir_b = settings.outputs_dir / "chat_artifacts" / chat_b.id
    dir_a.mkdir(parents=True)
    dir_b.mkdir(parents=True)
    file_a = dir_a / "a.pdf"
    file_b = dir_b / "b.pdf"
    file_a.write_text("a", encoding="utf-8")
    file_b.write_text("b", encoding="utf-8")

    from legal_helper.server import _scan_new_artifacts

    records = list(_scan_new_artifacts(store, chat_a.id, set(), dir_a))

    assert [record["filename"] for record in records] == ["a.pdf"]
    assert records[0]["chat_id"] == chat_a.id
    assert records[0]["url"] == f"/api/chats/{chat_a.id}/artifacts/{records[0]['id']}"
    assert store.list_artifacts(chat_b.id) == []


def test_visible_artifacts_exclude_app_state_and_other_chats(tmp_path, monkeypatch):
    monkeypatch.setenv("LEGAL_HELPER_OUTPUTS_DIR", str(tmp_path / "out"))
    monkeypatch.setenv("LEGAL_HELPER_STATE_DIR", str(tmp_path / "state"))
    settings = load_settings(refresh=True)
    store = ChatStore(settings, tmp_path / "chat.sqlite3")
    chat_a = store.create_chat()
    chat_b = store.create_chat()

    good_dir = settings.outputs_dir / "chat_artifacts" / chat_a.id
    other_dir = settings.outputs_dir / "chat_artifacts" / chat_b.id
    good_dir.mkdir(parents=True)
    other_dir.mkdir(parents=True)
    good = good_dir / "analysis.pdf"
    wrong_chat_file = other_dir / "other.pdf"
    state_db = settings.state_dir / "chat_sessions.sqlite3"
    good.write_text("pdf", encoding="utf-8")
    wrong_chat_file.write_text("pdf", encoding="utf-8")
    state_db.write_text("sqlite", encoding="utf-8")

    visible = store.add_artifact(chat_a.id, "analysis.pdf", str(good), "")
    store.add_artifact(chat_a.id, "chat_sessions.sqlite3", str(state_db), "")
    store.add_artifact(chat_a.id, "other.pdf", str(wrong_chat_file), "")

    from legal_helper.server import _list_visible_artifacts

    records = _list_visible_artifacts(store, chat_a.id)

    assert records == [visible]


def test_default_chat_store_migrates_legacy_db_out_of_outputs(tmp_path):
    settings = load_settings(refresh=True).model_copy(
        update={"outputs_dir": tmp_path / "out", "state_dir": tmp_path / "state"}
    )
    settings.outputs_dir.mkdir(parents=True)
    settings.state_dir.mkdir(parents=True, exist_ok=True)
    legacy = settings.outputs_dir / "chat_sessions.sqlite3"
    old_store = ChatStore(settings, legacy)
    chat = old_store.create_chat(title="Legacy")

    store = ChatStore(settings)

    assert not legacy.exists()
    assert store.db_path == settings.state_dir / "chat_sessions.sqlite3"
    assert store.get_chat(chat.id).title == "Legacy"


def test_uploads_are_chat_scoped_and_required(tmp_path, monkeypatch):
    monkeypatch.setenv("LEGAL_HELPER_OUTPUTS_DIR", str(tmp_path / "out"))
    monkeypatch.setenv("LEGAL_HELPER_STATE_DIR", str(tmp_path / "state"))
    load_settings(refresh=True)

    from legal_helper.server import _is_chat_upload_path, app

    client = TestClient(app)
    chat_a = client.post("/api/chats", json={}).json()
    chat_b = client.post("/api/chats", json={}).json()

    missing_chat = client.post(
        "/api/upload",
        files={"file": ("loose.txt", b"loose", "text/plain")},
    )
    assert missing_chat.status_code == 400

    upload = client.post(
        f"/api/chats/{chat_a['id']}/upload",
        files={"file": ("../brief.txt", b"hello", "text/plain")},
    )
    assert upload.status_code == 200
    payload = upload.json()
    path = Path(payload["path"])

    assert payload["chat_id"] == chat_a["id"]
    assert path.is_file()
    assert "chat_uploads" in path.parts
    assert chat_a["id"] in path.parts
    assert path.name == "brief.txt"
    assert _is_chat_upload_path(path, chat_a["id"])
    assert not _is_chat_upload_path(path, chat_b["id"])

    wrong_chat = client.post(
        f"/api/chats/{chat_b['id']}/messages/stream",
        json={"message": "Use this upload", "attachments": [str(path)]},
    )
    assert wrong_chat.status_code == 400


def test_followup_turn_inherits_same_chat_uploads(tmp_path, monkeypatch):
    monkeypatch.setenv("LEGAL_HELPER_OUTPUTS_DIR", str(tmp_path / "out"))
    monkeypatch.setenv("LEGAL_HELPER_STATE_DIR", str(tmp_path / "state"))
    load_settings(refresh=True)

    import legal_helper.server as server_mod

    captured: list[list[str]] = []

    class FakeExecutor:
        def __init__(self, settings):
            self.settings = settings

        def stream(self, user_message, *, attachments=None, **kwargs):
            captured.append([str(p) for p in attachments or []])
            yield StreamEvent("delta", {"text": "ok"})
            yield StreamEvent("done", {"text": "ok"})

    monkeypatch.setattr(server_mod, "WorkflowExecutor", FakeExecutor)
    monkeypatch.setattr(server_mod, "summarize_with_fast_model", lambda *args, **kwargs: "summary")
    monkeypatch.setattr(server_mod, "summarize_title_with_fast_model", lambda *args, **kwargs: "Uploaded file follow-up")

    client = TestClient(server_mod.app)
    chat = client.post("/api/chats", json={}).json()
    upload = client.post(
        f"/api/chats/{chat['id']}/upload",
        files={"file": ("prior.docx", b"not a real docx", "application/vnd.openxmlformats-officedocument.wordprocessingml.document")},
    )
    assert upload.status_code == 200
    uploaded_path = upload.json()["path"]

    with client.stream(
        "POST",
        f"/api/chats/{chat['id']}/messages/stream",
        json={"message": "Use the Word document I uploaded earlier.", "attachments": []},
    ) as resp:
        assert resp.status_code == 200
        "".join(resp.iter_text())

    deadline = time.time() + 2
    while time.time() < deadline and not captured:
        time.sleep(0.01)
    assert captured
    assert uploaded_path in captured[-1]

# ---------------------------------------------------------------------------
# Plan fidelity: coverage-dedup must merge the dropped mandate, not lose it.
# ---------------------------------------------------------------------------


def test_coverage_dedup_rewrites_merge_target_with_dropped_mandate(tmp_path):
    settings = load_settings(refresh=True).model_copy(update={"outputs_dir": tmp_path / "out"})
    provider = PlannerProvider(
        """
        {
          "title": "Tax treaty analysis",
          "user_language": "English",
          "execution_mode": "agent_workflow",
          "routing_reason": "multi-issue",
          "issue_decomposition": ["treaty", "filings"],
          "agent_tasks": [
            {"id": "task_1", "skill_name": "brief", "title": "Treaty research", "task": "Research the bilateral tax treaty and withholding rules. coverage: tax-treaty, withholding", "depends_on": []},
            {"id": "task_2", "skill_name": "compliance-check", "title": "Withholding filings", "task": "Check dividend withholding filings with the tax bureau. coverage: withholding, filings", "depends_on": ["task_1"]}
          ],
          "integration_instructions": "Integrate.",
          "citation_requirements": "Use pinpoint citations."
        }
        """
    )
    plan = WorkflowExecutor(settings=settings, provider=provider).plan(
        "Analyze the treaty withholding position."
    )
    assert [task.id for task in plan.agent_tasks] == ["task_1"]
    body = plan.agent_tasks[0].task
    # The dropped task's instructions were appended, with attribution.
    assert "Check dividend withholding filings with the tax bureau." in body
    assert "Withholding filings" in body and "task_2" in body
    # Exactly one coverage line, carrying the union of both tasks' tokens.
    coverage_lines = [
        line for line in body.splitlines() if line.lower().startswith("coverage:")
    ]
    assert coverage_lines == ["coverage: filings, tax-treaty, withholding"]


def test_normalize_plan_breaks_dependency_cycles_but_keeps_downstream_edges(tmp_path):
    settings = load_settings(refresh=True).model_copy(update={"outputs_dir": tmp_path / "out"})
    provider = PlannerProvider(
        """
        {
          "title": "Cyclic plan",
          "user_language": "English",
          "execution_mode": "agent_workflow",
          "routing_reason": "multi-issue",
          "issue_decomposition": ["a", "b", "c"],
          "agent_tasks": [
            {"id": "task_1", "skill_name": "brief", "title": "Statute research", "task": "Research the governing statutes. coverage: statutes", "depends_on": ["task_2"]},
            {"id": "task_2", "skill_name": "compliance-check", "title": "Filing duties", "task": "Identify the filing duties. coverage: filings", "depends_on": ["task_1"]},
            {"id": "task_3", "skill_name": "legal-risk-assessment", "title": "Exposure ranking", "task": "Rank the exposure using the statute findings. coverage: exposure", "depends_on": ["task_1"]}
          ],
          "integration_instructions": "Integrate.",
          "citation_requirements": "Use pinpoint citations."
        }
        """
    )
    plan = WorkflowExecutor(settings=settings, provider=provider).plan(
        "Assess the compliance exposure."
    )
    deps_by_id = {task.id: task.depends_on for task in plan.agent_tasks}
    # Cycle members lose only the intra-cycle edges…
    assert deps_by_id["task_1"] == []
    assert deps_by_id["task_2"] == []
    # …while the merely-downstream task keeps its legitimate dependency.
    assert deps_by_id["task_3"] == ["task_1"]


# ---------------------------------------------------------------------------
# Fail-soft degradation: one failed specialist must not abort the whole run.
# ---------------------------------------------------------------------------


class FailTaskTwoSubAgentProvider(PlannerProvider):
    _lock = threading.Lock()
    attempts: dict[str, int] = {}

    @classmethod
    def reset(cls) -> None:
        with cls._lock:
            cls.attempts = {}

    def stream(self, system, messages, tools=(), *, max_iterations=None):
        task = messages[-1]["content"]
        with self._lock:
            self.attempts[task] = self.attempts.get(task, 0) + 1
        if "Task two." in task:
            raise RuntimeError("connector exploded")
        text = f"SUB AGENT OUTPUT: {task}"
        yield StreamEvent("delta", {"text": text})
        yield StreamEvent("done", {"text": text})


_TWO_TASK_PLAN_JSON = """
{
  "title": "Parallel workflow",
  "user_language": "English",
  "issue_decomposition": ["one", "two"],
  "agent_tasks": [
    {"id": "task_1", "skill_name": "brief", "title": "One", "task": "Task one.", "depends_on": []},
    {"id": "task_2", "skill_name": "compliance-check", "title": "Two", "task": "Task two.", "depends_on": []}
  ],
  "integration_instructions": "Integrate.",
  "citation_requirements": "Use citations."
}
"""


def test_stream_degrades_to_completed_bundles_when_one_specialist_fails(tmp_path, monkeypatch):
    settings = load_settings(refresh=True).model_copy(update={"outputs_dir": tmp_path / "out"})
    provider = StreamingPlannerProvider(_TWO_TASK_PLAN_JSON, final_text="FINAL ONLY")

    import legal_helper.workflow as workflow_mod

    FailTaskTwoSubAgentProvider.reset()
    monkeypatch.setattr(workflow_mod, "build_provider", lambda settings: FailTaskTwoSubAgentProvider())
    monkeypatch.setattr(workflow_mod, "_SPECIALIST_RETRY_BACKOFF_SECONDS", 0.01)

    events = list(WorkflowExecutor(settings=settings, provider=provider).stream("test"))
    kinds = [ev.kind for ev in events]

    # Transient-failure retry ran exactly once, then the task degraded.
    failed_attempts = [n for task, n in FailTaskTwoSubAgentProvider.attempts.items() if "Task two." in task]
    assert failed_attempts == [2]
    assert "agent_task_retried" in kinds
    assert "agent_task_failed" in kinds
    assert "workflow_degraded" in kinds
    assert "error" not in kinds
    # The run still completed on the surviving bundle.
    assert events[-1].kind == "done"
    assert events[-1].data["text"] == "FINAL ONLY"
    integration = provider.integration_messages[-1]
    assert "SUB AGENT OUTPUT: Task one." in integration
    # Failed coverage is declared to the integration turn, not silently dropped.
    assert "SPECIALIST FAILURES" in integration
    assert "bundle:task_2" not in integration


def test_stream_errors_out_when_every_specialist_fails(tmp_path, monkeypatch):
    settings = load_settings(refresh=True).model_copy(update={"outputs_dir": tmp_path / "out"})
    provider = StreamingPlannerProvider(_TWO_TASK_PLAN_JSON, final_text="FINAL ONLY")

    import legal_helper.workflow as workflow_mod

    class AlwaysFailingSubAgentProvider(PlannerProvider):
        def stream(self, system, messages, tools=(), *, max_iterations=None):
            raise RuntimeError("connector exploded")
            yield  # pragma: no cover

    monkeypatch.setattr(workflow_mod, "build_provider", lambda settings: AlwaysFailingSubAgentProvider())
    monkeypatch.setattr(workflow_mod, "_SPECIALIST_RETRY_BACKOFF_SECONDS", 0.01)

    events = list(WorkflowExecutor(settings=settings, provider=provider).stream("test"))
    errors = [ev for ev in events if ev.kind == "error"]
    assert errors and "All specialist tasks failed" in errors[-1].data["message"]
    assert provider.integration_messages == []


def test_stream_degrades_after_exhausted_rate_limit_instead_of_aborting(tmp_path, monkeypatch):
    settings = load_settings(refresh=True).model_copy(
        update={"provider": "openai", "outputs_dir": tmp_path / "out", "max_concurrent_agents": "auto"}
    )
    provider = StreamingPlannerProvider(_TWO_TASK_PLAN_JSON, final_text="FINAL ONLY")

    import legal_helper.workflow as workflow_mod

    class AlwaysRateLimitedTaskTwoProvider(PlannerProvider):
        def stream(self, system, messages, tools=(), *, max_iterations=None):
            task = messages[-1]["content"]
            if "Task two." in task:
                raise RuntimeError("Error code: 429 - rate_limit_error: tokens per minute")
            text = f"SUB AGENT OUTPUT: {task}"
            yield StreamEvent("delta", {"text": text})
            yield StreamEvent("done", {"text": text})

    monkeypatch.setattr(workflow_mod, "build_provider", lambda settings: AlwaysRateLimitedTaskTwoProvider())

    events = list(WorkflowExecutor(settings=settings, provider=provider).stream("test"))
    kinds = [ev.kind for ev in events]
    assert "agent_task_rate_limited" in kinds
    assert "agent_concurrency_reduced" in kinds
    assert "agent_task_failed" in kinds
    assert events[-1].kind == "done"
    assert events[-1].data["text"] == "FINAL ONLY"
    integration = provider.integration_messages[-1]
    assert "SUB AGENT OUTPUT: Task one." in integration
    assert "SPECIALIST FAILURES" in integration


# ---------------------------------------------------------------------------
# Project context must reach the planner context block and specialists.
# ---------------------------------------------------------------------------


def test_build_context_block_includes_active_project_context(tmp_path):
    settings = load_settings(refresh=True)
    from legal_helper.projects import ProjectStore, active_project
    from legal_helper.workflow import _build_context_block

    project = ProjectStore(settings).create_project(
        "Route launch memo", brief="Jurisdiction pin: PRC law first, US comparative."
    )
    with active_project(project.id):
        block = _build_context_block("", [])
    assert "Jurisdiction pin: PRC law first" in block
    assert _build_context_block("", []) == ""


def test_skill_agent_system_prompt_includes_active_project_context(tmp_path):
    settings = load_settings(refresh=True)
    from legal_helper.agent import SkillAgent
    from legal_helper.projects import ProjectStore, active_project

    project = ProjectStore(settings).create_project(
        "Route launch memo", brief="Jurisdiction pin: PRC law first, US comparative."
    )
    agent = SkillAgent("brief", PlannerProvider("{}"), settings)
    with active_project(project.id):
        prompt = agent._system_prompt()
    assert "Jurisdiction pin: PRC law first" in prompt
    assert "Jurisdiction pin" not in agent._system_prompt()


# ---------------------------------------------------------------------------
# Zero-extraction cite-check bypass: citation-shaped text defeats auto-pass.
# ---------------------------------------------------------------------------


def test_citation_surface_signals_detects_prc_typography_and_urls():
    from legal_helper.workflow import _citation_surface_signals

    assert "title_brackets" in _citation_surface_signals("依据《民法典》的规定处理。")
    assert "cjk_pinpoint" in _citation_surface_signals("第一千零八十七条规定了原则。")
    assert "document_number" in _citation_surface_signals("见法释〔2024〕5号。")
    assert "document_number" in _citation_surface_signals("（2023）京01民终12345号")
    assert "url" in _citation_surface_signals("See https://example.com/statute")
    assert "sources_heading" in _citation_surface_signals("结论。\n\n## 资料来源\n- 条目")
    assert _citation_surface_signals("你好，很高兴帮忙。The weather is nice.") == []
    assert _citation_surface_signals("") == []


def test_cite_check_runs_despite_zero_extraction_when_signals_present(tmp_path, monkeypatch):
    settings = load_settings(refresh=True).model_copy(
        update={"outputs_dir": tmp_path / "out", "enable_cite_check": True}
    )

    import legal_helper.workflow as workflow_mod

    class FakeCiteCheckAgent:
        instantiated = 0

        def __init__(self, skill_name, provider, agent_settings):
            assert skill_name == "cite-check"
            type(self).instantiated += 1

        def stream(self, task_text, attachments=None):
            yield StreamEvent(
                "delta",
                {"text": "# Citation Verification Report\n\nChecked 1 of 1 citations. All verified."},
            )
            yield StreamEvent("done", {"text": ""})

    monkeypatch.setattr(workflow_mod, "SkillAgent", FakeCiteCheckAgent)
    # Force the zero-extraction path regardless of extractor strength.
    monkeypatch.setattr(workflow_mod, "extract_citations", lambda text: [])

    executor = WorkflowExecutor(settings=settings, provider=PlannerProvider("{}"))
    final_text = (
        "根据《民法典》第一千零八十七条，离婚时应当妥善处理。\n\n"
        "## 资料来源\n- 《民法典》第一千零八十七条"
    )
    events = list(
        executor._run_cite_check_after_summary(
            plan=None, final_text=final_text, attachments=None, cancel_event=None
        )
    )
    audit = [ev for ev in events if ev.kind == "citation_audit"][-1]
    assert FakeCiteCheckAgent.instantiated == 1
    assert audit.data["skipped"] is False
    assert "Checked 1 of 1 citations" in audit.data["report_markdown"]


def test_cite_check_still_auto_passes_without_surface_signals(tmp_path, monkeypatch):
    settings = load_settings(refresh=True).model_copy(
        update={"outputs_dir": tmp_path / "out", "enable_cite_check": True}
    )

    import legal_helper.workflow as workflow_mod

    def _fail_if_instantiated(*args, **kwargs):
        raise AssertionError("cite-check sub-agent must not spin up on a signal-free answer")

    monkeypatch.setattr(workflow_mod, "SkillAgent", _fail_if_instantiated)
    monkeypatch.setattr(workflow_mod, "extract_citations", lambda text: [])

    executor = WorkflowExecutor(settings=settings, provider=PlannerProvider("{}"))
    final_text = "The compliance answer is straightforward and needs no authority."
    events = list(
        executor._run_cite_check_after_summary(
            plan=None, final_text=final_text, attachments=None, cancel_event=None
        )
    )
    audit = [ev for ev in events if ev.kind == "citation_audit"][-1]
    assert audit.data["ok"] is True
    assert audit.data["skip_reason"] == "no citations detected in final answer"
