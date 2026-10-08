"""The user is released at `done`; summary and title follow in the background.

The rolling summary and the chat title used to run inside the turn's stream
after `done` (p50 8 s, p90 63 s), and the composer stayed disabled until the
response body ended. These pin the replacement: the body ends at `done`, the
bookkeeping runs on a per-chat serial worker, and what it produces is persisted.
"""

from __future__ import annotations

import threading
import time

from fastapi.testclient import TestClient

from legal_helper.chat_store import ChatStore
from legal_helper.config import load_settings
from legal_helper.providers.base import StreamEvent


def _server(tmp_path, monkeypatch, events):
    import legal_helper.server as server_mod

    settings = load_settings(refresh=True).model_copy(update={"outputs_dir": tmp_path / "out"})
    store = ChatStore(settings, tmp_path / "chat.sqlite3")
    monkeypatch.setattr(server_mod, "_store", lambda: store)
    monkeypatch.setattr(server_mod, "_settings_for_chat", lambda chat_settings, chat_id=None: settings)

    class FakeExecutor:
        def __init__(self, settings):
            self.settings = settings

        def stream(self, *args, **kwargs):
            yield from events

    monkeypatch.setattr(server_mod, "WorkflowExecutor", FakeExecutor)
    return server_mod, store


def _wait(predicate, timeout=3.0):
    deadline = time.time() + timeout
    while time.time() < deadline:
        if predicate():
            return True
        time.sleep(0.01)
    return False


def test_the_stream_ends_at_done_while_housekeeping_is_still_running(tmp_path, monkeypatch):
    server_mod, store = _server(
        tmp_path,
        monkeypatch,
        [StreamEvent("delta", {"text": "Answer."}), StreamEvent("done", {"text": "Answer."})],
    )
    release = threading.Event()
    started = threading.Event()

    def slow_summary(settings, messages, previous=""):
        started.set()
        release.wait(5)
        return "## Facts\n- summarised"

    monkeypatch.setattr(server_mod, "summarize_with_fast_model", slow_summary)
    monkeypatch.setattr(server_mod, "summarize_title_with_fast_model", lambda *a, **k: "Defective Goods Remedy")
    chat = store.create_chat()

    received: list[str] = []
    finished = threading.Event()

    def consume():
        with TestClient(server_mod.app).stream(
            "POST", f"/api/chats/{chat.id}/messages/stream", json={"message": "remedies?"}
        ) as resp:
            for line in resp.iter_lines():
                if line.startswith("event:"):
                    received.append(line.split(":", 1)[1].strip())
        finished.set()

    consumer = threading.Thread(target=consume, daemon=True)
    consumer.start()
    try:
        # The body ends while the summariser is still blocked: the client is free.
        assert finished.wait(5), "the stream stayed open behind the summariser"
        assert received[-1] == "done"
        assert started.wait(5)
        assert store.get_chat(chat.id).memory_summary == ""
    finally:
        release.set()
    # Persistence is what a reload reads, and it still happens.
    assert _wait(lambda: store.get_chat(chat.id).memory_summary == "## Facts\n- summarised")
    assert _wait(lambda: store.get_chat(chat.id).title == "Defective Goods Remedy")
    assert _wait(lambda: not server_mod.chat_housekeeping_pending(chat.id))
    messages = store.list_messages(chat.id)
    assert messages[-1].content == "Answer." and messages[-1].metadata["run_status"] == "complete"


def test_housekeeping_is_serial_per_chat_and_each_job_sees_the_last_summary():
    import legal_helper.server as server_mod

    log: list[str] = []
    active = {"now": 0, "max": 0}
    lock = threading.Lock()
    summary = {"value": ""}

    def job(n):
        def run():
            with lock:
                active["now"] += 1
                active["max"] = max(active["max"], active["now"])
            previous = summary["value"]
            time.sleep(0.02)
            summary["value"] = previous + str(n)
            log.append(f"job{n}")
            with lock:
                active["now"] -= 1

        return run

    for n in range(5):
        server_mod._submit_chat_housekeeping("chat-serial", job(n))
    assert _wait(lambda: not server_mod.chat_housekeeping_pending("chat-serial"))
    assert log == [f"job{n}" for n in range(5)]
    assert active["max"] == 1
    assert summary["value"] == "01234"  # each built on the one before it


def test_a_failing_job_does_not_stop_the_chats_queue():
    import legal_helper.server as server_mod

    ran: list[str] = []

    def boom():
        raise RuntimeError("summariser down")

    server_mod._submit_chat_housekeeping("chat-fail", boom)
    server_mod._submit_chat_housekeeping("chat-fail", lambda: ran.append("after"))
    assert _wait(lambda: ran == ["after"])


def test_turn_usage_reads_openai_cache_and_the_last_request(tmp_path, monkeypatch):
    usage = {
        "input_tokens": 3_640_000,
        "output_tokens": 9_000,
        "cached_input_tokens": 3_100_000,
        "cache_write_input_tokens": 41_000,
        "last_request_input_tokens": 212_000,
    }
    server_mod, store = _server(
        tmp_path,
        monkeypatch,
        [StreamEvent("delta", {"text": "Long answer."}), StreamEvent("done", {"text": "Long answer.", "usage": usage})],
    )
    monkeypatch.setattr(server_mod, "summarize_with_fast_model", lambda *a, **k: "s")
    monkeypatch.setattr(server_mod, "summarize_title_with_fast_model", lambda *a, **k: "t")
    chat = store.create_chat()
    with TestClient(server_mod.app).stream(
        "POST", f"/api/chats/{chat.id}/messages/stream", json={"message": "q"}
    ) as resp:
        names = [line.split(":", 1)[1].strip() for line in resp.iter_lines() if line.startswith("event:")]

    events = {e.event_type: e.data for e in store.list_events(chat.id)}
    payload = events["turn_usage"]
    # 0 of 460 OpenAI turns showed a cache hit when only Anthropic names were read.
    assert payload["cache_read_tokens"] == 3_100_000
    assert payload["cache_write_tokens"] == 41_000
    # Context fill is the last request, not 3.64M summed over the loop…
    assert payload["context_tokens"] == 212_000
    # …so a turn well under the 259K ceiling is not reported as compacting.
    assert "context_compaction" not in names
    assert _wait(lambda: not server_mod.chat_housekeeping_pending(chat.id))
