"""CLI unification: both CLI commands must ride the WorkflowExecutor path.

The REPL keeps an in-process transcript + rolling summary (so follow-ups like
"expand section 2" keep their context), and the CLI surface must start the
same author turn as the server surface for an identical prompt.
"""

from __future__ import annotations

from types import SimpleNamespace

from rich.console import Console

from legal_helper.config import load_settings
from legal_helper.logging_setup import workflow_event_sink_var
from legal_helper.providers import StreamEvent
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


class AuthorMockProvider:
    """Answers every author turn with one fixed text."""

    name = "mock"
    model = "mock-advanced"

    def __init__(self, final_text: str = "FINAL ANSWER") -> None:
        self.final_text = final_text

    def stream(self, system, messages, tools=(), *, max_iterations=None, **kwargs):
        yield StreamEvent("delta", {"text": self.final_text})
        yield StreamEvent("done", {"text": self.final_text})


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


def test_cli_and_server_paths_start_the_same_author_turn(tmp_path):
    """Parity lock: the CLI turn and the server-path engine (a direct
    WorkflowExecutor.stream call) must start the same author turn for an
    identical prompt."""
    settings = load_settings(refresh=True).model_copy(update={"outputs_dir": tmp_path / "out"})
    prompt = "旅客机上撕毁护照怎么办？"

    def _author_events(records: list[dict]) -> list[dict]:
        return [r["data"] for r in records if r["event_type"] == "author_started"]

    # CLI surface.
    cli_session = _ChatSession(
        settings,
        executor=WorkflowExecutor(settings=settings, provider=AuthorMockProvider()),
    )
    console = Console(file=open(tmp_path / "console.txt", "w", encoding="utf-8"))
    cli_records = _capture_workflow_events(lambda: cli_session.turn(console, prompt))

    # Server surface engine: the exact call server.py's produce() makes.
    server_executor = WorkflowExecutor(settings=settings, provider=AuthorMockProvider())
    server_records = _capture_workflow_events(
        lambda: list(
            server_executor.stream(prompt, attachments=[], recent_messages=[], memory_summary="")
        )
    )

    cli_author = _author_events(cli_records)
    assert len(cli_author) == 1
    assert cli_author == _author_events(server_records)
    assert [m.content for m in cli_session.history if m.role == "assistant"] == ["FINAL ANSWER"]
