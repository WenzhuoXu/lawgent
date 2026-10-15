from __future__ import annotations

import json
import threading
import time
from pathlib import Path

import pytest
from fastapi.testclient import TestClient

import legal_helper.multi_agent as multi_agent_mod
import legal_helper.providers as providers_mod
from legal_helper.chat_models import ChatSettings
from legal_helper.chat_store import ChatStore
from legal_helper.citations import audit_citations
from legal_helper.config import load_settings
from legal_helper.errors import OUT_OF_MONEY_MESSAGE
from legal_helper.logging_setup import tool_call_id_var, transient_event_sink_var, workflow_event_sink_var
from legal_helper.multi_agent import WorkflowRejected, run_workflow_tool, validate_tasks
from legal_helper.providers import StreamEvent
from legal_helper.turn_context import TurnContext, open_turn
from legal_helper.workflow import (
    WorkflowExecutor,
    write_followup_docx,
    write_followup_pdf,
)


class StubProvider:
    """A provider for tests that never reach a model: building and failing."""

    name = "mock"
    model = "mock-advanced"

    def __init__(self, exc: Exception | None = None) -> None:
        self.exc = exc

    def stream(self, *args, **kwargs):
        if self.exc:
            raise self.exc
        yield from ()


class CapturingStreamProvider(StubProvider):
    def __init__(self, final_text: str = "FINAL ANSWER") -> None:
        super().__init__()
        self.final_text = final_text
        self.stream_calls: list[dict] = []

    def stream(self, system, messages, tools=(), *, max_iterations=None, **kwargs):
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


def _tool_names(tools) -> set[str]:
    return {
        (t.get("name") if isinstance(t, dict) else getattr(t, "name", None))
        or (t.get("type") if isinstance(t, dict) else None)
        for t in tools
    }


def test_chat_store_persists_messages_and_memory(tmp_path):
    settings = load_settings(refresh=True).model_copy(update={"outputs_dir": tmp_path / "out"})
    store = ChatStore(settings, tmp_path / "chat.sqlite3")
    chat = store.create_chat(
        settings=ChatSettings(provider="openai", model="gpt-6.1-sol", reasoning_effort="high")
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


def test_workflow_builds_the_chat_model_and_the_fast_cite_check_model(tmp_path, monkeypatch):
    settings = load_settings(refresh=True).model_copy(
        update={
            "provider": "anthropic",
            "anthropic_model": "claude-opus-5-5",
            "anthropic_fast_model": "claude-sonnet-5",
            "outputs_dir": tmp_path / "out",
        }
    )
    calls: list[tuple[bool, str]] = []

    def fake_build_provider(settings_arg, *, fast=False):
        calls.append((fast, settings_arg.model_for_provider(fast=fast)))
        return StubProvider()

    import legal_helper.workflow as workflow_mod

    monkeypatch.setattr(workflow_mod, "build_provider", fake_build_provider)

    executor = WorkflowExecutor(settings=settings)

    # The primary, then one provider per role from `Settings.role_profiles`
    # that differs from it: cite-check on the fast tier. The author's default
    # row is the chat's own model and effort, so it reuses the primary.
    assert calls == [(False, "claude-opus-5-5"), (True, "claude-sonnet-5")]
    assert executor.author_provider is executor.provider


def test_the_author_turn_offers_web_search_and_the_document_tools(tmp_path):
    settings = load_settings(refresh=True).model_copy(
        update={"provider": "openai", "outputs_dir": tmp_path / "out", "enable_web_search": True}
    )
    provider = CapturingStreamProvider(final_text="Final answer.")

    events = list(WorkflowExecutor(settings=settings, provider=provider).stream("Please create the finished file."))

    assert len(provider.stream_calls) == 1
    call = provider.stream_calls[0]
    assert {
        "web_search",
        "fetch_url_to_artifact",
        "read_document",
        "write_pdf",
        "write_docx",
        "write_xlsx",
        "create_deck",
        "inspect_xlsx",
        "edit_xlsx_cells",
        "inspect_pptx",
        "edit_pptx_text",
        "render_pptx_slides",
    } <= _tool_names(call["tools"])
    assert "Please create the finished file." in json.dumps(call["message"], ensure_ascii=False)
    assert events[-1].kind == "done"
    assert events[-1].data["text"] == "Final answer."


def test_out_of_money_stream_message(tmp_path):
    settings = load_settings(refresh=True).model_copy(update={"outputs_dir": tmp_path / "out"})
    provider = StubProvider(exc=RuntimeError("insufficient_quota: billing hard limit"))
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


def test_uploads_are_chat_scoped(tmp_path, monkeypatch):
    monkeypatch.setenv("LEGAL_HELPER_OUTPUTS_DIR", str(tmp_path / "out"))
    monkeypatch.setenv("LEGAL_HELPER_STATE_DIR", str(tmp_path / "state"))
    load_settings(refresh=True)

    from legal_helper.server import _is_chat_upload_path, app

    client = TestClient(app)
    chat_a = client.post("/api/chats", json={}).json()
    chat_b = client.post("/api/chats", json={}).json()

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
    captured_available: list[list[str]] = []

    class FakeExecutor:
        def __init__(self, settings):
            self.settings = settings

        def stream(self, user_message, *, attachments=None, available_files=None, **kwargs):
            captured.append([str(p) for p in attachments or []])
            captured_available.append(
                [str(entry.get("path", "")) for entry in available_files or []]
            )
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
    # A prior upload stays reachable on later turns — but by reference, not by
    # re-inlining its bytes into every subsequent prompt. Inlining the whole
    # upload history is what shipped 33 identical screenshots on 66 consecutive
    # provider calls; the model now opens what it needs with `read_document`.
    assert uploaded_path not in captured[-1]
    assert uploaded_path in captured_available[-1]


def test_named_prior_upload_is_inlined_again(tmp_path, monkeypatch):
    """Naming a previous upload in the message re-inlines that one file."""
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
    monkeypatch.setattr(server_mod, "summarize_title_with_fast_model", lambda *args, **kwargs: "t")

    client = TestClient(server_mod.app)
    chat = client.post("/api/chats", json={}).json()
    upload = client.post(
        f"/api/chats/{chat['id']}/upload",
        files={"file": ("lease_agreement.docx", b"x", "application/octet-stream")},
    )
    uploaded_path = upload.json()["path"]

    with client.stream(
        "POST",
        f"/api/chats/{chat['id']}/messages/stream",
        json={"message": "Check clause 7 of lease_agreement.docx", "attachments": []},
    ) as resp:
        assert resp.status_code == 200
        "".join(resp.iter_text())

    deadline = time.time() + 2
    while time.time() < deadline and not captured:
        time.sleep(0.01)
    assert captured
    assert uploaded_path in captured[-1]


# ---------------------------------------------------------------------------
# run_workflow: validation, parallel specialists, fail-soft degradation.
# ---------------------------------------------------------------------------


def _task(n: int, method: str, depends_on: list[str] | None = None) -> dict:
    word = {1: "one", 2: "two", 3: "three", 4: "four"}[n]
    return {
        "id": f"task_{n}",
        "title": word.title(),
        "method": method,
        "task": f"Task {word}.",
        "depends_on": depends_on or [],
    }


def test_validate_tasks_rejects_what_cannot_run():
    tasks = [_task(1, "brief", ["task_2"]), _task(2, "compliance-check", ["task_1"])]
    with pytest.raises(WorkflowRejected, match="cycle"):
        validate_tasks(tasks, max_tasks=3)
    with pytest.raises(WorkflowRejected, match="At most 3"):
        validate_tasks([_task(n, "brief") for n in range(1, 5)], max_tasks=3)
    with pytest.raises(WorkflowRejected, match="unknown method"):
        validate_tasks([_task(1, "cite-check")], max_tasks=3)
    with pytest.raises(WorkflowRejected, match="not another task"):
        validate_tasks([_task(1, "brief", ["task_9"])], max_tasks=3)

    tasks[0]["depends_on"] = []
    assert [t.depends_on for t in validate_tasks(tasks, max_tasks=3)] == [[], ["task_1"]]


class SpecialistProvider:
    """A specialist's model: answers ``BUNDLE <task>`` unless ``fail`` says otherwise.

    ``fail(task, attempt)`` returns the exception to raise on that attempt, or
    None. Attempts are counted per task across every provider built.
    """

    name = "mock"
    model = "mock-specialist"
    _lock = threading.Lock()

    def __init__(self, attempts: dict[str, int], fail=None) -> None:
        self.attempts = attempts
        self.fail = fail

    def stream(self, system, messages, tools=(), *, max_iterations=None, **kwargs):
        content = messages[-1]["content"]
        if not isinstance(content, str):
            content = json.dumps(content, ensure_ascii=False)
        task = "two" if "Task two." in content else "one"
        with self._lock:
            attempt = self.attempts.get(task, 0)
            self.attempts[task] = attempt + 1
        exc = self.fail(task, attempt) if self.fail else None
        if exc is not None:
            raise exc
        text = f"BUNDLE {task.upper()}"
        yield StreamEvent("delta", {"text": text})
        yield StreamEvent("done", {"text": text})


def _call_run_workflow(settings, monkeypatch, fail=None):
    """Call `run_workflow` as the author would, with two specialists."""
    attempts: dict[str, int] = {}
    monkeypatch.setattr(
        providers_mod, "build_provider", lambda *args, **kwargs: SpecialistProvider(attempts, fail)
    )
    monkeypatch.setattr(multi_agent_mod, "_RETRY_BACKOFF_SECONDS", 0.01)
    events: list[tuple[str, dict]] = []
    previews: list[tuple[str, dict]] = []
    tokens = (
        workflow_event_sink_var.set(lambda record: events.append((record["event_type"], record["data"]))),
        transient_event_sink_var.set(lambda kind, data: previews.append((kind, data))),
        tool_call_id_var.set("call_wf"),
    )
    try:
        turn = TurnContext(settings=settings, user_message="Answer the question.", user_language="English")
        with open_turn(turn):
            result = run_workflow_tool(settings).call(
                {
                    "objective": "Answer the question.",
                    "tasks": [_task(1, "brief"), _task(2, "compliance-check")],
                }
            )
    finally:
        tool_call_id_var.reset(tokens[2])
        transient_event_sink_var.reset(tokens[1])
        workflow_event_sink_var.reset(tokens[0])
    return result, events, previews, attempts


def _workflow_settings(tmp_path, **updates):
    base = {"outputs_dir": tmp_path / "out", "state_dir": tmp_path / "state"}
    base.update(updates)
    return load_settings(refresh=True).model_copy(update=base)


def test_run_workflow_returns_the_bundles_and_names_its_call_on_every_event(tmp_path, monkeypatch):
    result, events, previews, _ = _call_run_workflow(_workflow_settings(tmp_path), monkeypatch)

    assert result.startswith("Workflow finished: 2 of 2 specialists returned research")
    assert "## bundle task_1" in result and "BUNDLE ONE" in result
    assert "## bundle task_2" in result and "BUNDLE TWO" in result
    assert "Open gaps" not in result

    started = [data for kind, data in events if kind == "agent_task_started"]
    assert sorted(data["task_id"] for data in started) == ["task_1", "task_2"]
    assert all(data["parent_call_id"] == "call_wf" for data in started)
    workflow = [data for kind, data in events if kind == "workflow_started"]
    assert workflow and workflow[0]["parent_call_id"] == "call_wf"

    # Specialist previews are transient: they reach a watching client, never the log.
    deltas = [data for kind, data in previews if kind == "agent_delta"]
    assert deltas
    for data in deltas:
        assert data["task_id"] in {"task_1", "task_2"}
        assert data["parent_call_id"] == "call_wf"
        assert data["phase"] in {"answer", "reasoning", "research"}
    assert all("tail" in data for data in deltas if data["phase"] == "answer")
    assert "agent_delta" not in [kind for kind, _ in events]


def test_specialists_respect_the_concurrency_cap(tmp_path, monkeypatch):
    settings = _workflow_settings(tmp_path, max_concurrent_agents=1)
    _, events, _, _ = _call_run_workflow(settings, monkeypatch)
    lifecycle = [
        (kind, data["task_id"])
        for kind, data in events
        if kind in {"agent_task_started", "agent_task_finished"}
    ]
    assert lifecycle == [
        ("agent_task_started", "task_1"),
        ("agent_task_finished", "task_1"),
        ("agent_task_started", "task_2"),
        ("agent_task_finished", "task_2"),
    ]


_RATE_LIMIT = "Error code: 429 - rate_limit_error: tokens per minute"


def test_a_rate_limit_lowers_concurrency_and_retries_the_task(tmp_path, monkeypatch):
    settings = _workflow_settings(tmp_path, provider="openai", max_concurrent_agents="auto")
    result, events, _, attempts = _call_run_workflow(
        settings,
        monkeypatch,
        fail=lambda task, attempt: RuntimeError(_RATE_LIMIT) if task == "two" and attempt == 0 else None,
    )
    kinds = [kind for kind, _ in events]
    assert "agent_task_rate_limited" in kinds
    assert "workflow_agent_concurrency_reduced" in kinds
    assert attempts["two"] == 2
    assert "BUNDLE ONE" in result and "BUNDLE TWO" in result


def test_one_failed_specialist_degrades_to_an_open_gap(tmp_path, monkeypatch):
    result, events, _, attempts = _call_run_workflow(
        _workflow_settings(tmp_path),
        monkeypatch,
        fail=lambda task, attempt: RuntimeError("connector exploded") if task == "two" else None,
    )
    kinds = [kind for kind, _ in events]

    # One retry after a transient failure, then the task degrades.
    assert attempts["two"] == 2
    assert {"agent_task_retried", "agent_task_failed", "workflow_degraded"} <= set(kinds)
    assert "BUNDLE ONE" in result
    assert "## bundle task_2" not in result
    # Failed coverage is declared to the author as an open gap, not dropped.
    assert "Open gaps" in result
    assert "the specialist FAILED — connector exploded" in result


def test_every_specialist_failing_returns_only_open_gaps(tmp_path, monkeypatch):
    result, events, _, _ = _call_run_workflow(
        _workflow_settings(tmp_path),
        monkeypatch,
        fail=lambda task, attempt: RuntimeError("connector exploded"),
    )
    assert result.startswith("Workflow finished: 0 of 2 specialists returned research")
    assert "(no bundle completed)" in result
    assert result.count("the specialist FAILED") == 2
    assert "workflow_degraded" not in [kind for kind, _ in events]


def test_a_persistent_rate_limit_degrades_instead_of_aborting(tmp_path, monkeypatch):
    settings = _workflow_settings(tmp_path, provider="openai", max_concurrent_agents="auto")
    result, events, _, _ = _call_run_workflow(
        settings,
        monkeypatch,
        fail=lambda task, attempt: RuntimeError(_RATE_LIMIT) if task == "two" else None,
    )
    kinds = [kind for kind, _ in events]
    assert "agent_task_rate_limited" in kinds
    assert "workflow_agent_concurrency_reduced" in kinds
    assert "agent_task_failed" in kinds
    assert "BUNDLE ONE" in result
    assert "Open gaps" in result and "the specialist FAILED" in result


# ---------------------------------------------------------------------------
# Project context reaches the digest every request this turn carries.
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


def test_project_context_reaches_specialists_through_the_digest_not_the_system_prompt(tmp_path):
    """The project block lives in the cached request prefix (the chat digest);
    in the system prompt it invalidated the system cache on every
    project-memory update."""
    settings = load_settings(refresh=True)
    from legal_helper.agent import SkillAgent
    from legal_helper.projects import ProjectStore, active_project
    from legal_helper.workflow import _build_context_block

    project = ProjectStore(settings).create_project(
        "Route launch memo", brief="Jurisdiction pin: PRC law first, US comparative."
    )
    provider = CapturingStreamProvider(final_text="bundle")
    agent = SkillAgent("brief", provider, settings)
    with active_project(project.id):
        assert "Jurisdiction pin" not in agent._system_prompt()
        digest = _build_context_block("", [])
        list(agent.stream("Research the question.", cache_prefix=digest))
    message = provider.stream_calls[-1]["message"]
    assert isinstance(message, list)
    assert "Jurisdiction pin: PRC law first" in json.dumps(message, ensure_ascii=False)


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

    executor = WorkflowExecutor(settings=settings, provider=StubProvider())
    final_text = (
        "根据《民法典》第一千零八十七条，离婚时应当妥善处理。\n\n"
        "## 资料来源\n- 《民法典》第一千零八十七条"
    )
    events = list(
        executor._run_cite_check_after_summary(
            turn=TurnContext(settings=settings), final_text=final_text, cancel_event=None
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

    executor = WorkflowExecutor(settings=settings, provider=StubProvider())
    final_text = "The compliance answer is straightforward and needs no authority."
    events = list(
        executor._run_cite_check_after_summary(
            turn=TurnContext(settings=settings), final_text=final_text, cancel_event=None
        )
    )
    audit = [ev for ev in events if ev.kind == "citation_audit"][-1]
    assert audit.data["ok"] is True
    assert audit.data["skip_reason"] == "no citations detected in final answer"


def test_author_and_deck_rows_of_the_role_table_take_effect(tmp_path, monkeypatch):
    """The table listed author and deck rows that nothing read; an override now applies."""
    settings = load_settings(refresh=True).model_copy(
        update={
            "provider": "openai",
            "openai_reasoning_effort": "medium",
            "outputs_dir": tmp_path / "out",
            "role_profiles": {"author": {"tier": "main", "effort": "high"}, "deck": {"tier": "main", "effort": "low"}},
        }
    )
    built: list[str] = []

    def fake_build_provider(settings_arg, *, fast=False):
        built.append(settings_arg.openai_reasoning_effort)
        return StubProvider()

    import legal_helper.workflow as workflow_mod

    monkeypatch.setattr(workflow_mod, "build_provider", fake_build_provider)
    executor = WorkflowExecutor(settings=settings)
    assert executor.author_provider is not executor.provider
    assert built[-1] == "high"

    import legal_helper.agent as agent_mod
    from legal_helper.tools import decks

    seen = {}

    class FakeAgent:
        def __init__(self, skill, provider, agent_settings):
            seen["effort"] = agent_settings.openai_reasoning_effort
            seen["outputs"] = agent_settings.outputs_dir

        def execute(self, task):
            return "done"

    monkeypatch.setattr(agent_mod, "SkillAgent", FakeAgent)
    monkeypatch.setattr(providers_mod, "build_provider", fake_build_provider)
    deck_settings = settings.model_copy(update={"outputs_dir": tmp_path / "out" / "_decks" / "d1"})
    assert decks.run_deck_agent("task", deck_settings) == "done"
    assert seen == {"effort": "low", "outputs": tmp_path / "out" / "_decks" / "d1"}
