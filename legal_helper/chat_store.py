"""SQLite persistence for the web chat UI."""

from __future__ import annotations

import json
import shutil
import sqlite3
import uuid
from pathlib import Path
from typing import Any, Iterable, Optional

from .chat_models import (
    ArtifactRecord,
    ChatMessage,
    ChatRun,
    ChatSession,
    ChatSettings,
    WorkflowEvent,
    utc_now,
)
from .config import Settings, current_settings


def _json_loads(raw: str | None, default: Any) -> Any:
    if not raw:
        return default
    try:
        return json.loads(raw)
    except Exception:
        return default


def _row_to_session(row: sqlite3.Row) -> ChatSession:
    keys = row.keys()
    return ChatSession(
        id=row["id"],
        title=row["title"],
        created_at=row["created_at"],
        updated_at=row["updated_at"],
        archived=bool(row["archived"]),
        settings=ChatSettings(**_json_loads(row["settings_json"], {})),
        memory_summary=row["memory_summary"] or "",
        last_response_id=row["last_response_id"],
        # project_id is added by the ProjectStore migration; read defensively so
        # ChatStore works even if ProjectStore.init_db has not run on this DB.
        project_id=(row["project_id"] if "project_id" in keys else None),
    )


def _row_to_message(row: sqlite3.Row) -> ChatMessage:
    return ChatMessage(
        id=row["id"],
        chat_id=row["chat_id"],
        role=row["role"],
        content=row["content"] or "",
        created_at=row["created_at"],
        metadata=_json_loads(row["metadata_json"], {}),
    )


def _row_to_event(row: sqlite3.Row) -> WorkflowEvent:
    return WorkflowEvent(
        id=row["id"],
        chat_id=row["chat_id"],
        created_at=row["created_at"],
        event_type=row["event_type"],
        agent_name=row["agent_name"] or "orchestrator",
        run_id=row["run_id"],
        parent_run_id=row["parent_run_id"],
        data=_json_loads(row["data_json"], {}),
    )


def _row_to_run(row: sqlite3.Row) -> ChatRun:
    return ChatRun(
        id=row["id"],
        chat_id=row["chat_id"],
        user_message_id=row["user_message_id"],
        assistant_message_id=row["assistant_message_id"],
        status=row["status"],
        created_at=row["created_at"],
        updated_at=row["updated_at"],
        finished_at=row["finished_at"],
        error=row["error"],
    )


def _row_to_artifact(row: sqlite3.Row) -> ArtifactRecord:
    keys = row.keys()
    return ArtifactRecord(
        id=row["id"],
        chat_id=row["chat_id"],
        message_id=row["message_id"],
        filename=row["filename"],
        path=row["path"],
        url=row["url"],
        origin=row["origin"] if "origin" in keys else "generated",
        created_at=row["created_at"],
    )


class ChatStore:
    def __init__(self, settings: Optional[Settings] = None, db_path: Optional[Path] = None) -> None:
        self.settings = settings or current_settings()
        self.db_path = db_path or (self.settings.state_dir / "chat_sessions.sqlite3")
        self.db_path.parent.mkdir(parents=True, exist_ok=True)
        if db_path is None:
            self._migrate_legacy_output_db()
        self.init_db()

    def _migrate_legacy_output_db(self) -> None:
        legacy = self.settings.outputs_dir / "chat_sessions.sqlite3"
        if legacy.resolve() == self.db_path.resolve() or not legacy.exists():
            return
        if self.db_path.exists():
            if self._chat_count(self.db_path) == 0 and self._chat_count(legacy) > 0:
                self._move_db_family(self.db_path, self._unique_state_path("chat_sessions.empty.sqlite3"))
            else:
                self._move_db_family(legacy, self._unique_state_path("chat_sessions.legacy-output.sqlite3"))
                return
        self._move_db_family(legacy, self.db_path)

    def _unique_state_path(self, filename: str) -> Path:
        target = self.settings.state_dir / filename
        if not target.exists():
            return target
        stem = target.stem
        suffix = target.suffix
        for index in range(1, 1000):
            candidate = target.with_name(f"{stem}-{index}{suffix}")
            if not candidate.exists():
                return candidate
        return target.with_name(f"{stem}-{uuid.uuid4().hex[:8]}{suffix}")

    def _chat_count(self, path: Path) -> int:
        try:
            con = sqlite3.connect(path)
            try:
                row = con.execute("SELECT count(*) FROM chats").fetchone()
            finally:
                con.close()
            return int(row[0]) if row else 0
        except Exception:
            return 0

    def _move_db_family(self, source: Path, target: Path) -> None:
        target.parent.mkdir(parents=True, exist_ok=True)
        shutil.move(str(source), str(target))
        for suffix in ("-wal", "-shm"):
            source_sidecar = Path(f"{source}{suffix}")
            if source_sidecar.exists():
                shutil.move(str(source_sidecar), str(target.parent / f"{target.name}{suffix}"))

    def _connect(self) -> sqlite3.Connection:
        con = sqlite3.connect(self.db_path)
        con.row_factory = sqlite3.Row
        con.execute("PRAGMA journal_mode=WAL")
        con.execute("PRAGMA foreign_keys=ON")
        return con

    def init_db(self) -> None:
        with self._connect() as con:
            con.executescript(
                """
                CREATE TABLE IF NOT EXISTS chats (
                    id TEXT PRIMARY KEY,
                    title TEXT NOT NULL,
                    created_at TEXT NOT NULL,
                    updated_at TEXT NOT NULL,
                    archived INTEGER NOT NULL DEFAULT 0,
                    settings_json TEXT NOT NULL,
                    memory_summary TEXT NOT NULL DEFAULT '',
                    last_response_id TEXT,
                    project_id TEXT
                );
                CREATE TABLE IF NOT EXISTS messages (
                    id TEXT PRIMARY KEY,
                    chat_id TEXT NOT NULL REFERENCES chats(id) ON DELETE CASCADE,
                    role TEXT NOT NULL,
                    content TEXT NOT NULL,
                    created_at TEXT NOT NULL,
                    metadata_json TEXT NOT NULL DEFAULT '{}'
                );
                CREATE INDEX IF NOT EXISTS idx_messages_chat_created
                    ON messages(chat_id, created_at);
                CREATE TABLE IF NOT EXISTS workflow_events (
                    id TEXT PRIMARY KEY,
                    chat_id TEXT NOT NULL REFERENCES chats(id) ON DELETE CASCADE,
                    created_at TEXT NOT NULL,
                    event_type TEXT NOT NULL,
                    agent_name TEXT NOT NULL DEFAULT 'orchestrator',
                    run_id TEXT,
                    parent_run_id TEXT,
                    data_json TEXT NOT NULL DEFAULT '{}'
                );
                CREATE INDEX IF NOT EXISTS idx_events_chat_created
                    ON workflow_events(chat_id, created_at);
                CREATE TABLE IF NOT EXISTS chat_runs (
                    id TEXT PRIMARY KEY,
                    chat_id TEXT NOT NULL REFERENCES chats(id) ON DELETE CASCADE,
                    user_message_id TEXT NOT NULL REFERENCES messages(id) ON DELETE CASCADE,
                    assistant_message_id TEXT NOT NULL REFERENCES messages(id) ON DELETE CASCADE,
                    status TEXT NOT NULL,
                    created_at TEXT NOT NULL,
                    updated_at TEXT NOT NULL,
                    finished_at TEXT,
                    error TEXT
                );
                CREATE INDEX IF NOT EXISTS idx_runs_chat_status
                    ON chat_runs(chat_id, status, updated_at);
                CREATE TABLE IF NOT EXISTS artifacts (
                    id TEXT PRIMARY KEY,
                    chat_id TEXT NOT NULL REFERENCES chats(id) ON DELETE CASCADE,
                    message_id TEXT REFERENCES messages(id) ON DELETE SET NULL,
                    filename TEXT NOT NULL,
                    path TEXT NOT NULL,
                    url TEXT NOT NULL,
                    origin TEXT NOT NULL DEFAULT 'generated',
                    created_at TEXT NOT NULL
                );
                """
            )
            self._ensure_artifact_origin_column(con)

    def _ensure_artifact_origin_column(self, con: sqlite3.Connection) -> None:
        """Backfill the `origin` column for databases created before it existed."""
        cols = {r["name"] for r in con.execute("PRAGMA table_info(artifacts)").fetchall()}
        if "origin" not in cols:
            con.execute("ALTER TABLE artifacts ADD COLUMN origin TEXT NOT NULL DEFAULT 'generated'")

    def default_settings(self) -> ChatSettings:
        return ChatSettings(
            provider=self.settings.provider,
            model=self.settings.model_for_provider(),
            reasoning_effort=self.settings.openai_reasoning_effort,
            enable_web_search=self.settings.enable_web_search,
            enable_web_fetch=self.settings.enable_web_fetch,
        )

    def create_chat(self, title: str = "New chat", settings: Optional[ChatSettings] = None) -> ChatSession:
        chat_id = uuid.uuid4().hex[:16]
        now = utc_now()
        chat_settings = settings or self.default_settings()
        with self._connect() as con:
            con.execute(
                """
                INSERT INTO chats
                    (id, title, created_at, updated_at, settings_json)
                VALUES (?, ?, ?, ?, ?)
                """,
                (chat_id, title, now, now, chat_settings.model_dump_json()),
            )
        return self.get_chat(chat_id)

    def list_chats(self, include_archived: bool = False) -> list[ChatSession]:
        where = "" if include_archived else "WHERE archived = 0"
        with self._connect() as con:
            rows = con.execute(
                f"SELECT * FROM chats {where} ORDER BY updated_at DESC"
            ).fetchall()
        return [_row_to_session(r) for r in rows]

    def get_chat(self, chat_id: str) -> ChatSession:
        with self._connect() as con:
            row = con.execute("SELECT * FROM chats WHERE id = ?", (chat_id,)).fetchone()
        if row is None:
            raise KeyError(chat_id)
        return _row_to_session(row)

    def update_chat(
        self,
        chat_id: str,
        *,
        title: Optional[str] = None,
        archived: Optional[bool] = None,
        settings: Optional[ChatSettings] = None,
        memory_summary: Optional[str] = None,
        last_response_id: Optional[str] = None,
    ) -> ChatSession:
        current = self.get_chat(chat_id)
        fields: dict[str, Any] = {"updated_at": utc_now()}
        if title is not None:
            fields["title"] = title
        if archived is not None:
            fields["archived"] = 1 if archived else 0
        if settings is not None:
            fields["settings_json"] = settings.model_dump_json()
        if memory_summary is not None:
            fields["memory_summary"] = memory_summary
        if last_response_id is not None:
            fields["last_response_id"] = last_response_id
        assignments = ", ".join(f"{k} = ?" for k in fields)
        with self._connect() as con:
            con.execute(
                f"UPDATE chats SET {assignments} WHERE id = ?",
                (*fields.values(), current.id),
            )
        return self.get_chat(chat_id)

    def delete_chat(self, chat_id: str) -> None:
        with self._connect() as con:
            con.execute("DELETE FROM chats WHERE id = ?", (chat_id,))

    def add_message(
        self,
        chat_id: str,
        role: str,
        content: str,
        metadata: Optional[dict[str, Any]] = None,
    ) -> ChatMessage:
        msg_id = uuid.uuid4().hex[:16]
        now = utc_now()
        with self._connect() as con:
            con.execute(
                """
                INSERT INTO messages
                    (id, chat_id, role, content, created_at, metadata_json)
                VALUES (?, ?, ?, ?, ?, ?)
                """,
                (msg_id, chat_id, role, content, now, json.dumps(metadata or {}, ensure_ascii=False)),
            )
            con.execute("UPDATE chats SET updated_at = ? WHERE id = ?", (now, chat_id))
        return self.get_message(msg_id)

    def update_message(self, message_id: str, content: str, metadata: Optional[dict[str, Any]] = None) -> ChatMessage:
        fields: dict[str, Any] = {"content": content}
        if metadata is not None:
            fields["metadata_json"] = json.dumps(metadata, ensure_ascii=False)
        assignments = ", ".join(f"{k} = ?" for k in fields)
        with self._connect() as con:
            con.execute(f"UPDATE messages SET {assignments} WHERE id = ?", (*fields.values(), message_id))
        return self.get_message(message_id)

    def merge_message_metadata(self, message_id: str, updates: dict[str, Any]) -> ChatMessage:
        current = self.get_message(message_id)
        metadata = {**current.metadata, **updates}
        return self.update_message(message_id, current.content, metadata)

    def get_message(self, message_id: str) -> ChatMessage:
        with self._connect() as con:
            row = con.execute("SELECT * FROM messages WHERE id = ?", (message_id,)).fetchone()
        if row is None:
            raise KeyError(message_id)
        return _row_to_message(row)

    def list_messages(self, chat_id: str, limit: Optional[int] = None) -> list[ChatMessage]:
        sql = "SELECT * FROM messages WHERE chat_id = ? ORDER BY created_at ASC"
        params: tuple[Any, ...] = (chat_id,)
        if limit is not None:
            sql = (
                "SELECT * FROM (SELECT * FROM messages WHERE chat_id = ? "
                "ORDER BY created_at DESC LIMIT ?) ORDER BY created_at ASC"
            )
            params = (chat_id, limit)
        with self._connect() as con:
            rows = con.execute(sql, params).fetchall()
        return [_row_to_message(r) for r in rows]

    def add_event(self, chat_id: str, event_type: str, data: dict[str, Any]) -> WorkflowEvent:
        event_id = uuid.uuid4().hex[:16]
        now = utc_now()
        with self._connect() as con:
            con.execute(
                """
                INSERT INTO workflow_events
                    (id, chat_id, created_at, event_type, agent_name, run_id, parent_run_id, data_json)
                VALUES (?, ?, ?, ?, ?, ?, ?, ?)
                """,
                (
                    event_id,
                    chat_id,
                    now,
                    event_type,
                    data.get("agent") or data.get("agent_name") or "orchestrator",
                    data.get("run_id"),
                    data.get("parent_run_id"),
                    json.dumps(data, ensure_ascii=False, default=str),
                ),
            )
        return self.get_event(event_id)

    def get_event(self, event_id: str) -> WorkflowEvent:
        with self._connect() as con:
            row = con.execute("SELECT * FROM workflow_events WHERE id = ?", (event_id,)).fetchone()
        if row is None:
            raise KeyError(event_id)
        return _row_to_event(row)

    def list_events(self, chat_id: str, limit: int = 300) -> list[WorkflowEvent]:
        with self._connect() as con:
            rows = con.execute(
                """
                SELECT * FROM (
                    SELECT * FROM workflow_events WHERE chat_id = ?
                    ORDER BY created_at DESC LIMIT ?
                ) ORDER BY created_at ASC
                """,
                (chat_id, limit),
            ).fetchall()
        return [_row_to_event(r) for r in rows]

    def create_run(self, chat_id: str, user_message_id: str, assistant_message_id: str) -> ChatRun:
        run_id = uuid.uuid4().hex[:16]
        now = utc_now()
        with self._connect() as con:
            con.execute(
                """
                INSERT INTO chat_runs
                    (id, chat_id, user_message_id, assistant_message_id, status, created_at, updated_at)
                VALUES (?, ?, ?, ?, ?, ?, ?)
                """,
                (run_id, chat_id, user_message_id, assistant_message_id, "running", now, now),
            )
            con.execute("UPDATE chats SET updated_at = ? WHERE id = ?", (now, chat_id))
        return self.get_run(run_id)

    def get_run(self, run_id: str) -> ChatRun:
        with self._connect() as con:
            row = con.execute("SELECT * FROM chat_runs WHERE id = ?", (run_id,)).fetchone()
        if row is None:
            raise KeyError(run_id)
        return _row_to_run(row)

    def update_run(
        self,
        run_id: str,
        *,
        status: Optional[str] = None,
        error: Optional[str] = None,
        finished: bool = False,
    ) -> ChatRun:
        fields: dict[str, Any] = {"updated_at": utc_now()}
        if status is not None:
            fields["status"] = status
        if error is not None:
            fields["error"] = error
        if finished:
            fields["finished_at"] = fields["updated_at"]
        assignments = ", ".join(f"{k} = ?" for k in fields)
        with self._connect() as con:
            con.execute(f"UPDATE chat_runs SET {assignments} WHERE id = ?", (*fields.values(), run_id))
        return self.get_run(run_id)

    def list_runs(self, chat_id: str, statuses: Optional[Iterable[str]] = None) -> list[ChatRun]:
        params: list[Any] = [chat_id]
        status_sql = ""
        if statuses:
            status_list = list(statuses)
            placeholders = ", ".join("?" for _ in status_list)
            status_sql = f" AND status IN ({placeholders})"
            params.extend(status_list)
        with self._connect() as con:
            rows = con.execute(
                f"""
                SELECT * FROM chat_runs
                WHERE chat_id = ?{status_sql}
                ORDER BY created_at ASC
                """,
                tuple(params),
            ).fetchall()
        return [_row_to_run(r) for r in rows]

    def list_active_runs(self, chat_id: str) -> list[ChatRun]:
        return self.list_runs(chat_id, statuses=["running"])

    def has_active_run(self, chat_id: str) -> bool:
        return bool(self.list_active_runs(chat_id))

    def mark_orphaned_runs(self, message: str = "Interrupted by server restart.") -> int:
        now = utc_now()
        with self._connect() as con:
            cur = con.execute(
                """
                UPDATE chat_runs
                SET status = 'error', error = ?, updated_at = ?, finished_at = ?
                WHERE status = 'running'
                """,
                (message, now, now),
            )
            return cur.rowcount

    def add_artifact(
        self,
        chat_id: str,
        filename: str,
        path: str,
        url: str,
        *,
        message_id: Optional[str] = None,
        origin: str = "generated",
    ) -> ArtifactRecord:
        artifact_id = uuid.uuid4().hex[:16]
        now = utc_now()
        with self._connect() as con:
            con.execute(
                """
                INSERT INTO artifacts
                    (id, chat_id, message_id, filename, path, url, origin, created_at)
                VALUES (?, ?, ?, ?, ?, ?, ?, ?)
                """,
                (artifact_id, chat_id, message_id, filename, path, url, origin, now),
            )
        return self.get_artifact(artifact_id)

    def get_artifact(self, artifact_id: str) -> ArtifactRecord:
        with self._connect() as con:
            row = con.execute("SELECT * FROM artifacts WHERE id = ?", (artifact_id,)).fetchone()
        if row is None:
            raise KeyError(artifact_id)
        return _row_to_artifact(row)

    def list_artifacts(self, chat_id: str) -> list[ArtifactRecord]:
        with self._connect() as con:
            rows = con.execute(
                "SELECT * FROM artifacts WHERE chat_id = ? ORDER BY created_at ASC",
                (chat_id,),
            ).fetchall()
        return [_row_to_artifact(r) for r in rows]

    def existing_artifact_paths(self, chat_id: str) -> set[str]:
        return {a.path for a in self.list_artifacts(chat_id)}

    def first_nonempty_assistant_message(self, chat_id: str) -> Optional[ChatMessage]:
        messages = [m for m in self.list_messages(chat_id) if m.role == "assistant" and m.content.strip()]
        return messages[-1] if messages else None
