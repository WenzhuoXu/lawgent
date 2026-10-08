"""Files the chat serves and lists: uploads, deliverables, working directories."""

from __future__ import annotations

from pathlib import Path

from fastapi.testclient import TestClient

from legal_helper.config import load_settings


def _env(tmp_path, monkeypatch):
    monkeypatch.setenv("LEGAL_HELPER_OUTPUTS_DIR", str(tmp_path / "out"))
    monkeypatch.setenv("LEGAL_HELPER_STATE_DIR", str(tmp_path / "state"))
    return load_settings(refresh=True)


def test_uploads_are_served_to_their_own_chat_only(tmp_path, monkeypatch):
    _env(tmp_path, monkeypatch)
    from legal_helper.server import app

    client = TestClient(app)
    chat_a = client.post("/api/chats", json={}).json()
    chat_b = client.post("/api/chats", json={}).json()
    up = client.post(
        f"/api/chats/{chat_a['id']}/upload",
        files={"file": ("合同.txt", "甲方：某某".encode("utf-8"), "text/plain")},
    ).json()

    ok = client.get(f"/api/chats/{chat_a['id']}/uploads/{up['id']}/{up['filename']}")
    assert ok.status_code == 200
    assert ok.content.decode("utf-8") == "甲方：某某"

    assert client.get(f"/api/chats/{chat_b['id']}/uploads/{up['id']}/{up['filename']}").status_code == 404
    traversal = client.get(f"/api/chats/{chat_a['id']}/uploads/{up['id']}/..%2F..%2Fsecret.txt")
    assert traversal.status_code == 404


def test_working_directories_are_not_registered_as_deliverables(tmp_path, monkeypatch):
    _env(tmp_path, monkeypatch)
    from legal_helper.chat_store import ChatStore
    from legal_helper.config import current_settings
    from legal_helper.server import _scan_new_artifacts

    store = ChatStore(current_settings())
    chat = store.create_chat()
    artifact_dir = tmp_path / "out" / "chat_artifacts" / chat.id
    (artifact_dir / "_decks" / "proj" / "svg_output").mkdir(parents=True)
    (artifact_dir / "_decks" / "proj" / "svg_output" / "p01.svg").write_text("<svg/>")
    (artifact_dir / ".cache").mkdir()
    (artifact_dir / ".cache" / "x.json").write_text("{}")
    (artifact_dir / "汇报.pptx").write_bytes(b"pptx")

    found = [a["filename"] for a in _scan_new_artifacts(store, chat.id, set(), artifact_dir)]
    assert found == ["汇报.pptx"]


def test_generated_inventory_lists_newest_first_with_lineage(tmp_path, monkeypatch):
    settings = _env(tmp_path, monkeypatch)
    from legal_helper.chat_store import ChatStore
    from legal_helper.documents.lineage import record_derivation
    from legal_helper.server import _chat_generated_inventory, _chat_outputs_dir

    store = ChatStore(settings)
    chat = store.create_chat()
    out = _chat_outputs_dir(chat.id)
    v1, v2 = out / "workbook_v1.xlsx", out / "workbook_v2.xlsx"
    v1.write_bytes(b"1")
    v2.write_bytes(b"2")
    store.add_artifact(chat.id, v1.name, str(v1), "", origin="generated")
    store.add_artifact(chat.id, v2.name, str(v2), "", origin="generated")
    record_derivation(v2, [v1], "reshape_xlsx", state_dir=settings.state_dir)

    inventory = _chat_generated_inventory(store, chat.id)
    assert [entry["name"] for entry in inventory] == ["workbook_v2.xlsx", "workbook_v1.xlsx"]
    assert [Path(p).name for p in inventory[0]["derived_from"]] == ["workbook_v1.xlsx"]

    from legal_helper.workflow import _render_available_files

    listing = _render_available_files([], inventory)
    assert "workbook_v2.xlsx" in listing and "(from workbook_v1.xlsx)" in listing
    assert listing.index("- workbook_v2.xlsx") < listing.index("- workbook_v1.xlsx")


def test_event_writes_wait_out_another_writer(tmp_path, monkeypatch):
    """Three concurrent runs used to die with "database is locked": every
    streamed event is a write, and the default 5 s busy timeout ran out."""
    import sqlite3
    import threading
    import time

    import legal_helper.chat_store as chat_store_mod

    settings = _env(tmp_path, monkeypatch)
    monkeypatch.setattr(chat_store_mod, "_BUSY_TIMEOUT_SECONDS", 4.0)
    store = chat_store_mod.ChatStore(settings)
    chat = store.create_chat()

    locked = threading.Event()

    def hold_the_write_lock() -> None:
        blocker = sqlite3.connect(store.db_path, timeout=1)
        blocker.execute("BEGIN IMMEDIATE")
        locked.set()
        time.sleep(1.5)
        blocker.commit()
        blocker.close()

    holder = threading.Thread(target=hold_the_write_lock)
    holder.start()
    assert locked.wait(5)
    started = time.monotonic()
    event = chat_store_mod.ChatStore(settings).add_event(chat.id, "progress", {"n": 1})
    assert event.event_type == "progress"
    assert time.monotonic() - started >= 1.0  # it waited instead of failing
    holder.join()
