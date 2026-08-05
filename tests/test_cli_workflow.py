"""CLI unification: both CLI commands must ride the WorkflowExecutor path.

The REPL keeps an in-process transcript + rolling summary (so follow-ups like
"expand section 2" keep their context), and the CLI surface must produce the
same plan-normalization events as the server surface for an identical prompt.
"""

from __future__ import annotations

from types import SimpleNamespace

from rich.console import Console

from legal_helper.config import load_settings
from legal_helper.logging_setup import workflow_event_sink_var
from legal_helper.providers import RunResult, StreamEvent
from legal_helper.workflow import WorkflowExecutor

import legal_helper.cli as cli_mod
from legal_helper.cli import _RECENT_WINDOW, _ChatSession, _stream_workflow_turn


class FakeExecutor:
    """Captures every stream() call so tests can assert the CLI's inputs."""

    def __init__(self, settings=None):
        self.settings = settings
        self.provider = SimpleNamespace(model="fake-model")
        self.calls: list[dict] = []

    def stream(self, message, *, attachments=None, recent_messages=None, memory_summary="", **kwargs):
        self.calls.append(
            {
                "message": message,
                "attachments": [str(p) for p in (attachments or [])],
                "recent_messages": list(recent_messages or []),
                "memory_summary": memory_summary,
            }
        )
        text = f"answer to: {message}"
        yield StreamEvent("delta", {"text": text})
        yield StreamEvent("done", {"text": text})


class ErrorExecutor(FakeExecutor):
    def stream(self, message, **kwargs):
        yield StreamEvent("error", {"message": "provider exploded"})


class WorkflowMockProvider:
    """Planner returns a plan that needs normalization; stream() is the answer."""

    name = "mock"
    model = "mock-advanced"
    supports_hosted_web_search = False
    supports_file_search = False
    supports_structured_outputs = False

    def __init__(self, plan_json: str, final_text: str = "FINAL ANSWER") -> None:
        self.plan_json = plan_json
        self.final_text = final_text

    def run(self, *args, **kwargs):
        return RunResult(text=self.plan_json, provider=self.name, model=self.model)

    def tool_runner(self, *args, **kwargs):
        return self.run(*args, **kwargs)

    def stream(self, system, messages, tools=(), *, max_iterations=None):
        yield StreamEvent("delta", {"text": self.final_text})
        yield StreamEvent("done", {"text": self.final_text})


# One real specialist + a redundant final-synthesis task: normalization must
# strip task_2, leaving a single-task plan (single-pass shortcut, no sub-agent
# provider needed).
_NORMALIZABLE_PLAN_JSON = """
{
  "title": "Passport workflow",
  "user_language": "English",
  "execution_mode": "agent_workflow",
  "routing_reason": "needs research",
  "issue_decomposition": ["sources", "final"],
  "agent_tasks": [
    {"id": "task_1", "skill_name": "brief", "title": "Sources", "task": "Research the governing sources and cite them.", "depends_on": []},
    {"id": "task_2", "skill_name": "legal-response", "title": "Draft final Chinese legal analysis", "task": "Prepare the final answer in Chinese and synthesize all prior research.", "depends_on": ["task_1"]}
  ],
  "integration_instructions": "Integrate in Chinese.",
  "citation_requirements": "Use pinpoint citations."
}
"""


def _capture_workflow_events(fn):
    records: list[dict] = []
    token = workflow_event_sink_var.set(lambda record: records.append(record))
    try:
        fn()
    finally:
        workflow_event_sink_var.reset(token)
    return records


def test_chat_session_passes_history_to_workflow_executor(tmp_path):
    settings = load_settings(refresh=True).model_copy(update={"outputs_dir": tmp_path / "out"})
    executor = FakeExecutor()
    session = _ChatSession(settings, executor=executor)
    console = Console(file=open(tmp_path / "console.txt", "w", encoding="utf-8"))

    final_text, ok = session.turn(console, "first question")
    assert ok and final_text == "answer to: first question"
    assert executor.calls[0]["recent_messages"] == []
    assert executor.calls[0]["memory_summary"] == ""

    session.turn(console, "expand section 2")
    recent = executor.calls[1]["recent_messages"]
    assert [(m.role, m.content) for m in recent] == [
        ("user", "first question"),
        ("assistant", "answer to: first question"),
    ]


def test_chat_session_rolls_memory_summary_once_window_filled(tmp_path, monkeypatch):
    settings = load_settings(refresh=True).model_copy(update={"outputs_dir": tmp_path / "out"})
    monkeypatch.setattr(
        cli_mod,
        "summarize_with_fast_model",
        lambda settings, messages, previous="": f"summary of {len(messages)} messages",
    )
    executor = FakeExecutor()
    session = _ChatSession(settings, executor=executor)
    console = Console(file=open(tmp_path / "console.txt", "w", encoding="utf-8"))

    turns = _RECENT_WINDOW // 2  # each turn appends user + assistant
    for i in range(turns):
        session.turn(console, f"question {i}")

    # No summary while the transcript still fits in the recent window…
    assert all(call["memory_summary"] == "" for call in executor.calls)
    assert session.memory_summary == f"summary of {_RECENT_WINDOW} messages"

    # …and the rolled summary rides along on the next turn (which then
    # re-rolls it over the grown transcript).
    rolled = session.memory_summary
    session.turn(console, "one more")
    assert executor.calls[-1]["memory_summary"] == rolled
    assert len(executor.calls[-1]["recent_messages"]) == _RECENT_WINDOW
    assert session.memory_summary == f"summary of {_RECENT_WINDOW + 2} messages"


def test_chat_session_keeps_user_message_but_no_summary_on_error(tmp_path, monkeypatch):
    settings = load_settings(refresh=True).model_copy(update={"outputs_dir": tmp_path / "out"})

    def _fail(*args, **kwargs):
        raise AssertionError("must not summarize an errored turn")

    monkeypatch.setattr(cli_mod, "summarize_with_fast_model", _fail)
    session = _ChatSession(settings, executor=ErrorExecutor())
    console = Console(file=open(tmp_path / "console.txt", "w", encoding="utf-8"))

    final_text, ok = _stream_workflow_turn(console, session.executor, "boom")
    assert final_text is None and ok is False

    # Fill the window so only the errored-turn gate can block summarization.
    for i in range(_RECENT_WINDOW):
        session._record("user" if i % 2 == 0 else "assistant", f"msg {i}")

    final_text, ok = session.turn(console, "boom")
    assert final_text is None and ok is False
    assert (session.history[-1].role, session.history[-1].content) == ("user", "boom")
    assert session.memory_summary == ""


def test_cmd_run_routes_through_workflow_executor(tmp_path, monkeypatch, capsys):
    created: list[FakeExecutor] = []

    def _factory(settings=None):
        executor = FakeExecutor(settings)
        created.append(executor)
        return executor

    monkeypatch.setattr(cli_mod, "WorkflowExecutor", _factory)

    rc = cli_mod.main(["run", "--task", "Summarize the filing duties."])

    assert rc == 0
    assert created and created[0].calls[0]["message"] == "Summarize the filing duties."
    assert "answer to: Summarize the filing duties." in capsys.readouterr().out


def test_cli_and_server_paths_emit_identical_plan_normalization_events(tmp_path):
    """Parity lock: the CLI turn and the server-path engine (a direct
    WorkflowExecutor.stream call) must emit the same plan-normalization
    workflow events for an identical prompt."""
    settings = load_settings(refresh=True).model_copy(update={"outputs_dir": tmp_path / "out"})
    prompt = "旅客机上撕毁护照怎么办？"

    def _norm_events(records: list[dict]) -> list[tuple[str, dict]]:
        return [
            (r["event_type"], r["data"])
            for r in records
            if r["event_type"].startswith("workflow_plan")
        ]

    # CLI surface.
    cli_session = _ChatSession(
        settings,
        executor=WorkflowExecutor(settings=settings, provider=WorkflowMockProvider(_NORMALIZABLE_PLAN_JSON)),
    )
    console = Console(file=open(tmp_path / "console.txt", "w", encoding="utf-8"))
    cli_records = _capture_workflow_events(lambda: cli_session.turn(console, prompt))

    # Server surface engine: the exact call server.py's produce() makes.
    server_executor = WorkflowExecutor(
        settings=settings, provider=WorkflowMockProvider(_NORMALIZABLE_PLAN_JSON)
    )
    server_records = _capture_workflow_events(
        lambda: list(
            server_executor.stream(prompt, attachments=[], recent_messages=[], memory_summary="")
        )
    )

    cli_norm = _norm_events(cli_records)
    server_norm = _norm_events(server_records)
    assert cli_norm == server_norm
    # And normalization genuinely ran on both surfaces (synthesis task stripped).
    normalized = [data for etype, data in cli_norm if etype == "workflow_plan_normalized"]
    assert normalized and normalized[0]["removed_final_synthesis_tasks"] == ["task_2"]
    assert [m.content for m in cli_session.history if m.role == "assistant"] == ["FINAL ANSWER"]
