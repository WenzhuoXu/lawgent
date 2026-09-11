"""Project context management — durable cross-chat context for long work.

A **Project** groups many chats and long-running executions under one shared,
persistent context. It borrows the model that makes Claude Code effective at
long work:

- a **brief** (the ``CLAUDE.md`` equivalent) — stable, hand-authored context +
  instructions that are re-injected into the orchestrator on every turn, so
  they survive per-chat compaction;
- a structured, append-only **memory** of typed entries (fact / decision /
  task / open_question / glossary / artifact / risk / source), each scored by
  salience so the most load-bearing ones rank into the context window;
- a rolling **summary** distilled from the project's chats;
- **compaction** that folds memory + chat summaries into the summary when the
  project's accumulated context grows large, keeping the high-fidelity core.

``ProjectStore`` persists all of this in the same SQLite database as
``ChatStore`` (``state/chat_sessions.sqlite3``) and adds a ``project_id``
foreign key to ``chats``. The orchestrator reads ``build_context_block`` each
turn; specialist sub-agents and the CLI/server write back via the project
memory tools.
"""

from __future__ import annotations

import re
import sqlite3
import uuid
from contextlib import contextmanager
from contextvars import ContextVar
from pathlib import Path
from typing import Any, Iterator, Optional

from .chat_models import Project, ProjectMemory, utc_now
from .config import Settings, current_settings

_SALIENCE_MIN, _SALIENCE_MAX = 1, 5

# Request-scoped active project. The server/CLI set this around an
# orchestrator run so ``_build_parent_addendum`` (agent.py) injects the
# project context block and the project memory tools know which project to
# write to — without threading a project id through every call site.
_ACTIVE_PROJECT_ID: ContextVar[Optional[str]] = ContextVar(
    "legal_helper_active_project_id", default=None
)


def active_project_id() -> Optional[str]:
    return _ACTIVE_PROJECT_ID.get()


@contextmanager
def active_project(project_id: Optional[str]) -> Iterator[None]:
    token = _ACTIVE_PROJECT_ID.set(project_id)
    try:
        yield
    finally:
        _ACTIVE_PROJECT_ID.reset(token)


def active_project_context_block() -> str:
    """The active project's context block, or '' when no project is active."""
    pid = active_project_id()
    if not pid:
        return ""
    try:
        return ProjectStore().build_context_block(pid)
    except Exception:  # noqa: BLE001 — context injection must never break a run
        return ""


def slugify(name: str) -> str:
    slug = re.sub(r"[^a-z0-9]+", "-", (name or "").strip().lower()).strip("-")
    return slug or "project"


def _row_to_project(row: sqlite3.Row) -> Project:
    import json

    return Project(
        id=row["id"],
        name=row["name"],
        slug=row["slug"],
        brief=row["brief"] or "",
        summary=row["summary"] or "",
        jurisdiction=row["jurisdiction"],
        domain_packs=json.loads(row["domain_packs_json"] or "[]"),
        status=row["status"],
        created_at=row["created_at"],
        updated_at=row["updated_at"],
        metadata=json.loads(row["metadata_json"] or "{}"),
    )


def _row_to_memory(row: sqlite3.Row) -> ProjectMemory:
    import json

    return ProjectMemory(
        id=row["id"],
        project_id=row["project_id"],
        kind=row["kind"],
        title=row["title"],
        body=row["body"] or "",
        status=row["status"],
        salience=row["salience"],
        source_chat_id=row["source_chat_id"],
        tags=json.loads(row["tags_json"] or "[]"),
        created_at=row["created_at"],
        updated_at=row["updated_at"],
        superseded_by=row["superseded_by"],
    )


class ProjectStore:
    def __init__(self, settings: Optional[Settings] = None, db_path: Optional[Path] = None) -> None:
        self.settings = settings or current_settings()
        self.db_path = db_path or (self.settings.state_dir / "chat_sessions.sqlite3")
        self.db_path.parent.mkdir(parents=True, exist_ok=True)
        self.init_db()

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
                CREATE TABLE IF NOT EXISTS projects (
                    id TEXT PRIMARY KEY,
                    name TEXT NOT NULL,
                    slug TEXT NOT NULL UNIQUE,
                    brief TEXT NOT NULL DEFAULT '',
                    summary TEXT NOT NULL DEFAULT '',
                    jurisdiction TEXT,
                    domain_packs_json TEXT NOT NULL DEFAULT '[]',
                    status TEXT NOT NULL DEFAULT 'active',
                    created_at TEXT NOT NULL,
                    updated_at TEXT NOT NULL,
                    metadata_json TEXT NOT NULL DEFAULT '{}'
                );
                CREATE TABLE IF NOT EXISTS project_memory (
                    id TEXT PRIMARY KEY,
                    project_id TEXT NOT NULL REFERENCES projects(id) ON DELETE CASCADE,
                    kind TEXT NOT NULL,
                    title TEXT NOT NULL,
                    body TEXT NOT NULL DEFAULT '',
                    status TEXT NOT NULL DEFAULT 'open',
                    salience INTEGER NOT NULL DEFAULT 3,
                    source_chat_id TEXT,
                    tags_json TEXT NOT NULL DEFAULT '[]',
                    created_at TEXT NOT NULL,
                    updated_at TEXT NOT NULL,
                    superseded_by TEXT
                );
                CREATE INDEX IF NOT EXISTS idx_pmem_project
                    ON project_memory(project_id, status, salience);
                """
            )
            # chats may not exist yet (ChatStore.init_db creates it) — guard.
            self._ensure_chat_project_column(con)

    def _ensure_chat_project_column(self, con: sqlite3.Connection) -> None:
        row = con.execute(
            "SELECT name FROM sqlite_master WHERE type='table' AND name='chats'"
        ).fetchone()
        if row is None:
            return
        cols = {r["name"] for r in con.execute("PRAGMA table_info(chats)").fetchall()}
        if "project_id" not in cols:
            con.execute("ALTER TABLE chats ADD COLUMN project_id TEXT")

    # ----- projects -------------------------------------------------------
    def create_project(
        self,
        name: str,
        *,
        slug: Optional[str] = None,
        brief: str = "",
        jurisdiction: Optional[str] = None,
        domain_packs: Optional[list[str]] = None,
    ) -> Project:
        import json

        project_id = uuid.uuid4().hex[:16]
        now = utc_now()
        final_slug = self._unique_slug(slug or slugify(name))
        with self._connect() as con:
            con.execute(
                """
                INSERT INTO projects
                    (id, name, slug, brief, summary, jurisdiction, domain_packs_json,
                     status, created_at, updated_at, metadata_json)
                VALUES (?, ?, ?, ?, '', ?, ?, 'active', ?, ?, '{}')
                """,
                (
                    project_id, name, final_slug, brief, jurisdiction,
                    json.dumps(domain_packs or []), now, now,
                ),
            )
        return self.get_project(project_id)

    def _unique_slug(self, base: str) -> str:
        base = slugify(base)
        with self._connect() as con:
            taken = {r["slug"] for r in con.execute("SELECT slug FROM projects").fetchall()}
        if base not in taken:
            return base
        for i in range(2, 1000):
            candidate = f"{base}-{i}"
            if candidate not in taken:
                return candidate
        return f"{base}-{uuid.uuid4().hex[:6]}"

    def get_project(self, project_id: str) -> Project:
        with self._connect() as con:
            row = con.execute("SELECT * FROM projects WHERE id = ?", (project_id,)).fetchone()
        if row is None:
            raise KeyError(project_id)
        return _row_to_project(row)

    def get_project_by_slug(self, slug: str) -> Optional[Project]:
        with self._connect() as con:
            row = con.execute("SELECT * FROM projects WHERE slug = ?", (slug,)).fetchone()
        return _row_to_project(row) if row else None

    def resolve(self, ref: str) -> Optional[Project]:
        """Resolve a project by id or slug (slug preferred for human input)."""
        by_slug = self.get_project_by_slug(ref)
        if by_slug:
            return by_slug
        try:
            return self.get_project(ref)
        except KeyError:
            return None

    def list_projects(self, include_archived: bool = False) -> list[Project]:
        where = "" if include_archived else "WHERE status = 'active'"
        with self._connect() as con:
            rows = con.execute(
                f"SELECT * FROM projects {where} ORDER BY updated_at DESC"
            ).fetchall()
        return [_row_to_project(r) for r in rows]

    def update_project(
        self,
        project_id: str,
        *,
        name: Optional[str] = None,
        brief: Optional[str] = None,
        summary: Optional[str] = None,
        jurisdiction: Optional[str] = None,
        domain_packs: Optional[list[str]] = None,
        status: Optional[str] = None,
    ) -> Project:
        import json

        self.get_project(project_id)  # existence check
        fields: dict[str, Any] = {"updated_at": utc_now()}
        if name is not None:
            fields["name"] = name
        if brief is not None:
            fields["brief"] = brief
        if summary is not None:
            fields["summary"] = summary
        if jurisdiction is not None:
            fields["jurisdiction"] = jurisdiction
        if domain_packs is not None:
            fields["domain_packs_json"] = json.dumps(domain_packs)
        if status is not None:
            fields["status"] = status
        assignments = ", ".join(f"{k} = ?" for k in fields)
        with self._connect() as con:
            con.execute(
                f"UPDATE projects SET {assignments} WHERE id = ?",
                (*fields.values(), project_id),
            )
        return self.get_project(project_id)

    def archive_project(self, project_id: str) -> Project:
        return self.update_project(project_id, status="archived")

    def delete_project(self, project_id: str) -> None:
        with self._connect() as con:
            con.execute("UPDATE chats SET project_id = NULL WHERE project_id = ?", (project_id,))
            con.execute("DELETE FROM projects WHERE id = ?", (project_id,))

    # ----- chat <-> project ----------------------------------------------
    def assign_chat(self, chat_id: str, project_id: Optional[str]) -> None:
        with self._connect() as con:
            con.execute("UPDATE chats SET project_id = ? WHERE id = ?", (project_id, chat_id))

    def project_id_for_chat(self, chat_id: str) -> Optional[str]:
        try:
            with self._connect() as con:
                row = con.execute("SELECT project_id FROM chats WHERE id = ?", (chat_id,)).fetchone()
        except sqlite3.OperationalError:
            # The chats table belongs to ChatStore's schema; on a DB that only
            # ProjectStore has initialized it does not exist yet. No linkage,
            # not an error — project context is best-effort.
            return None
        return row["project_id"] if row and row["project_id"] else None

    def list_project_chats(self, project_id: str) -> list[dict[str, Any]]:
        with self._connect() as con:
            rows = con.execute(
                "SELECT id, title, updated_at, memory_summary FROM chats "
                "WHERE project_id = ? ORDER BY updated_at DESC",
                (project_id,),
            ).fetchall()
        return [dict(r) for r in rows]

    # ----- memory ---------------------------------------------------------
    def add_memory(
        self,
        project_id: str,
        kind: str,
        title: str,
        body: str = "",
        *,
        salience: int = 3,
        status: str = "open",
        source_chat_id: Optional[str] = None,
        tags: Optional[list[str]] = None,
    ) -> ProjectMemory:
        import json

        self.get_project(project_id)  # existence check
        mem_id = uuid.uuid4().hex[:16]
        now = utc_now()
        salience = max(_SALIENCE_MIN, min(int(salience), _SALIENCE_MAX))
        with self._connect() as con:
            con.execute(
                """
                INSERT INTO project_memory
                    (id, project_id, kind, title, body, status, salience,
                     source_chat_id, tags_json, created_at, updated_at)
                VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
                """,
                (
                    mem_id, project_id, kind, title, body, status, salience,
                    source_chat_id, json.dumps(tags or []), now, now,
                ),
            )
            con.execute("UPDATE projects SET updated_at = ? WHERE id = ?", (now, project_id))
        return self.get_memory(mem_id)

    def get_memory(self, mem_id: str) -> ProjectMemory:
        with self._connect() as con:
            row = con.execute("SELECT * FROM project_memory WHERE id = ?", (mem_id,)).fetchone()
        if row is None:
            raise KeyError(mem_id)
        return _row_to_memory(row)

    def list_memory(
        self,
        project_id: str,
        *,
        kind: Optional[str] = None,
        status: Optional[str] = None,
        limit: Optional[int] = None,
    ) -> list[ProjectMemory]:
        clauses = ["project_id = ?"]
        params: list[Any] = [project_id]
        if kind:
            clauses.append("kind = ?")
            params.append(kind)
        if status:
            clauses.append("status = ?")
            params.append(status)
        sql = (
            f"SELECT * FROM project_memory WHERE {' AND '.join(clauses)} "
            "ORDER BY salience DESC, updated_at DESC"
        )
        if limit is not None:
            sql += " LIMIT ?"
            params.append(limit)
        with self._connect() as con:
            rows = con.execute(sql, tuple(params)).fetchall()
        return [_row_to_memory(r) for r in rows]

    def update_memory(
        self,
        mem_id: str,
        *,
        title: Optional[str] = None,
        body: Optional[str] = None,
        status: Optional[str] = None,
        salience: Optional[int] = None,
        superseded_by: Optional[str] = None,
    ) -> ProjectMemory:
        self.get_memory(mem_id)
        fields: dict[str, Any] = {"updated_at": utc_now()}
        if title is not None:
            fields["title"] = title
        if body is not None:
            fields["body"] = body
        if status is not None:
            fields["status"] = status
        if salience is not None:
            fields["salience"] = max(_SALIENCE_MIN, min(int(salience), _SALIENCE_MAX))
        if superseded_by is not None:
            fields["superseded_by"] = superseded_by
        assignments = ", ".join(f"{k} = ?" for k in fields)
        with self._connect() as con:
            con.execute(
                f"UPDATE project_memory SET {assignments} WHERE id = ?",
                (*fields.values(), mem_id),
            )
        return self.get_memory(mem_id)

    def supersede_memory(self, mem_id: str, new_mem_id: Optional[str] = None) -> ProjectMemory:
        return self.update_memory(mem_id, status="superseded", superseded_by=new_mem_id)

    def delete_memory(self, mem_id: str) -> None:
        with self._connect() as con:
            con.execute("DELETE FROM project_memory WHERE id = ?", (mem_id,))

    def search_memory(
        self, project_id: str, query: str, *, kind: Optional[str] = None, limit: int = 8
    ) -> list[ProjectMemory]:
        """Keyword-scored retrieval over a project's memory (open + done)."""
        terms = [t for t in re.findall(r"[\w一-鿿]+", (query or "").lower()) if len(t) > 1]
        candidates = [
            m for m in self.list_memory(project_id, kind=kind)
            if m.status != "superseded"
        ]
        if not terms:
            return candidates[:limit]
        scored: list[tuple[int, ProjectMemory]] = []
        for m in candidates:
            hay = f"{m.title} {m.body} {' '.join(m.tags)}".lower()
            score = sum(hay.count(t) for t in terms)
            score += m.salience  # tie-break toward important entries
            if score:
                scored.append((score, m))
        scored.sort(key=lambda x: x[0], reverse=True)
        return [m for _s, m in scored[:limit]]

    # ----- context assembly ----------------------------------------------
    def build_context_block(
        self,
        project_id: str,
        *,
        max_chars: int = 6000,
        max_entries: int = 14,
    ) -> str:
        """Assemble the orchestrator-facing ``# Project context`` block.

        Order: brief (always, in full — it is the CLAUDE.md-equivalent), then
        the rolling summary, then the highest-salience open decisions / facts /
        open questions / tasks. Budgeted to ``max_chars``.
        """
        try:
            project = self.get_project(project_id)
        except KeyError:
            return ""
        lines: list[str] = [f"# Project context — {project.name}"]
        if project.jurisdiction or project.domain_packs:
            meta = []
            if project.jurisdiction:
                meta.append(f"jurisdiction **{project.jurisdiction}**")
            if project.domain_packs:
                meta.append(f"packs {project.domain_packs}")
            lines.append("(" + ", ".join(meta) + ")")
        if project.brief.strip():
            lines.append("\n## Project brief (standing instructions)\n" + project.brief.strip())
        if project.summary.strip():
            lines.append("\n## Cross-chat summary\n" + project.summary.strip())

        # Salient open memory, grouped by kind for scanability.
        open_mem = [
            m for m in self.list_memory(project_id, status="open", limit=max_entries)
        ]
        if open_mem:
            groups: dict[str, list[ProjectMemory]] = {}
            for m in open_mem:
                groups.setdefault(m.kind, []).append(m)
            lines.append("\n## Durable memory (highest-salience open items)")
            for kind in ("decision", "fact", "open_question", "task", "risk", "glossary",
                         "artifact", "source"):
                items = groups.get(kind) or []
                if not items:
                    continue
                lines.append(f"**{kind}**:")
                for m in items:
                    body = (m.body or "").strip().replace("\n", " ")
                    suffix = f" — {body}" if body else ""
                    lines.append(f"- [s{m.salience}] {m.title}{suffix}")
        block = "\n".join(lines)
        if len(block) > max_chars:
            block = block[: max_chars - 1] + "…"
        return block

    # ----- summary / compaction ------------------------------------------
    def set_summary(self, project_id: str, summary: str) -> Project:
        return self.update_project(project_id, summary=summary[:8000])

    def _local_compact(self, project: Project, chat_summaries: list[str]) -> str:
        """Deterministic fallback digest when no model is available."""
        parts: list[str] = []
        if project.summary.strip():
            parts.append(project.summary.strip())
        for s in chat_summaries:
            s = " ".join((s or "").split())
            if s:
                parts.append(s)
        decisions = self.list_memory(project.id, kind="decision", status="open", limit=8)
        if decisions:
            parts.append("Key decisions: " + "; ".join(d.title for d in decisions))
        joined = "\n".join(parts)
        return joined[-8000:]

    def compact(self, project_id: str, settings: Optional[Settings] = None) -> Project:
        """Fold chat summaries + memory into the rolling project summary.

        Best-effort model distillation (cheap/fast provider) with a deterministic
        local fallback, mirroring ``memory.summarize_with_fast_model``. Low-salience
        ``done`` tasks are pruned to keep the durable memory lean.
        """
        project = self.get_project(project_id)
        chat_summaries = [
            c.get("memory_summary") or "" for c in self.list_project_chats(project_id)
        ]
        new_summary = self._local_compact(project, chat_summaries)
        settings = settings or self.settings
        try:
            from .providers import build_provider

            provider = build_provider(settings, fast=True)
            decisions = self.list_memory(project_id, kind="decision", limit=12)
            mem_blob = "\n".join(f"- ({m.kind}) {m.title}: {m.body}" for m in decisions)
            transcript = "\n\n".join(s for s in chat_summaries if s.strip())[:6000]
            # Delta, not rewrite. The cross-chat summary is the longest-lived
            # context in the product, so wholesale re-summarization erodes it
            # fastest (ACE's context collapse). Ask only for what changed and
            # merge deterministically; the existing summary is always the base.
            from .memory import apply_delta, parse_delta, parse_summary, render_summary

            sections = parse_summary(project.summary)
            prompt = (
                "Below is a legal matter's durable project memory and the newest "
                "material. Emit ONLY the changes, one per line, in exactly this form:\n"
                "  + Section | new durable fact\n"
                "  - Section | start of an existing bullet that is now wrong or resolved\n"
                "Valid sections: Goals, Facts, Conclusions, Artifacts, Open. "
                "Do not restate bullets that are still correct — they are kept "
                "automatically. Drop chit-chat. Write bullet text in whichever "
                "language best preserves fidelity to the matter, and keep Chinese "
                "legal terms and citations exactly as written. If nothing changed, "
                "output nothing.\n\n"
                f"Current project memory:\n{project.summary or '(empty)'}\n\n"
                f"Project brief:\n{project.brief[:1500] or '(none)'}\n\n"
                f"Key memory entries:\n{mem_blob or '(none)'}\n\n"
                f"Recent chat summaries:\n{transcript or '(none)'}"
            )
            result = provider.run(
                system=(
                    "You maintain a legal matter's durable memory as an append-only "
                    "playbook. You emit only deltas — never a rewritten summary."
                ),
                messages=[{"role": "user", "content": prompt}],
                tools=[],
                max_iterations=1,
            )
            ops = parse_delta(result.text)
            if ops:
                new_summary = render_summary(apply_delta(sections, ops))[:8000]
            elif any(sections.values()):
                new_summary = render_summary(sections)[:8000]
            elif result.text.strip():
                new_summary = result.text.strip()[:8000]
        except Exception:
            pass  # keep the local fallback

        # Prune low-salience completed tasks to keep durable memory lean.
        for m in self.list_memory(project_id, kind="task", status="done"):
            if m.salience <= 2:
                self.delete_memory(m.id)
        return self.set_summary(project_id, new_summary)


def context_for_chat(
    chat_id: str, store: Optional[ProjectStore] = None
) -> tuple[Optional[Project], str]:
    """Convenience: resolve a chat's project and its context block (or empty)."""
    store = store or ProjectStore()
    project_id = store.project_id_for_chat(chat_id)
    if not project_id:
        return None, ""
    try:
        project = store.get_project(project_id)
    except KeyError:
        return None, ""
    return project, store.build_context_block(project_id)


__all__ = [
    "ProjectStore",
    "slugify",
    "context_for_chat",
    "active_project",
    "active_project_id",
    "active_project_context_block",
]
