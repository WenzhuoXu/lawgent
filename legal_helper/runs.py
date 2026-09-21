"""Durable record of one workflow run: phase artifacts and a resumable snapshot.

A run used to exist only in memory and in the event log. A crash, a cancel or a
provider outage discarded everything — including research that had already been
paid for. For a legal research run that is 30-odd PKULaw and CourtListener calls
of work, which is the expensive part of the turn.

Two things are written under ``state/runs/<run_id>/``:

- ``artifacts/NN-<phase>.md`` — the output of each phase as it completes: the
  plan, each specialist's research bundle, the synthesised draft, the citation
  audit, any repair pass. Inspectable while the run is still going, and the
  audit trail the PRC AI-agent rules require is the same file set.
- ``snapshot.json`` — plan, per-task status and output, failures, the current
  phase, and why the run stopped. ``resumable_outputs`` reads the completed
  task outputs back so a retry skips the research it already has.

The snapshot is rewritten on every transition rather than appended, so a
half-written file can only ever cost the newest transition; ``events.jsonl``
beside it keeps the ordered history.
"""

from __future__ import annotations

import json
import re
import threading
from dataclasses import asdict, dataclass, field
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Literal, Optional

RunStatus = Literal["running", "complete", "paused", "failed", "cancelled"]
TaskStatus = Literal["pending", "active", "complete", "failed"]

# Why a run stopped short. "deadlock" and "error_threshold" are the two the
# scheduler can detect on its own; the rest come from outside.
PauseReason = Literal[
    "cancelled", "error", "deadlock", "error_threshold", "provider_unavailable"
]

_SAFE = re.compile(r"[^A-Za-z0-9._-]+")


def _now() -> str:
    return datetime.now(timezone.utc).isoformat()


def _safe(name: str, *, limit: int = 60) -> str:
    return (_SAFE.sub("-", name).strip("-") or "unnamed")[:limit]


@dataclass
class TaskRecord:
    id: str
    skill_name: str = ""
    title: str = ""
    status: TaskStatus = "pending"
    artifact: Optional[str] = None
    error: Optional[str] = None
    tokens: int = 0
    updated_at: str = field(default_factory=_now)


@dataclass
class RunSnapshot:
    run_id: str
    chat_id: Optional[str] = None
    status: RunStatus = "running"
    phase: str = "routing"
    title: str = ""
    created_at: str = field(default_factory=_now)
    updated_at: str = field(default_factory=_now)
    pause_reason: Optional[str] = None
    error: Optional[str] = None
    plan: Optional[dict[str, Any]] = None
    tasks: dict[str, TaskRecord] = field(default_factory=dict)
    artifacts: list[str] = field(default_factory=list)
    repair_rounds: int = 0

    def to_json(self) -> dict[str, Any]:
        data = asdict(self)
        data["tasks"] = {k: asdict(v) if not isinstance(v, dict) else v for k, v in self.tasks.items()}
        return data

    @classmethod
    def from_json(cls, data: dict[str, Any]) -> "RunSnapshot":
        tasks = {
            key: TaskRecord(**value)
            for key, value in (data.get("tasks") or {}).items()
            if isinstance(value, dict)
        }
        known = {f for f in cls.__dataclass_fields__}  # type: ignore[attr-defined]
        kwargs = {k: v for k, v in data.items() if k in known and k != "tasks"}
        return cls(tasks=tasks, **kwargs)


class RunStore:
    """Filesystem home for one run. Every write is best-effort.

    Durability must never be able to fail a turn: a full disk or a read-only
    mount degrades the run to the old in-memory behaviour instead of raising in
    the middle of a legal answer. Failures are visible in the event log.
    """

    def __init__(
        self,
        settings: Any,
        run_id: str,
        *,
        chat_id: Optional[str] = None,
        title: str = "",
        create: bool = True,
    ) -> None:
        self.run_id = run_id
        self._lock = threading.Lock()
        self._step = 0
        self.root = Path(getattr(settings, "state_dir", Path("state"))) / "runs" / _safe(run_id)
        self.artifacts_dir = self.root / "artifacts"
        self.snapshot_path = self.root / "snapshot.json"
        self.events_path = self.root / "events.jsonl"
        self.snapshot = RunSnapshot(run_id=run_id, chat_id=chat_id, title=title)
        self.enabled = True
        if not create:
            # Reading a run must not bring one into existence: `load_run` used
            # to leave an empty directory behind for every 404.
            self.enabled = self.snapshot_path.is_file()
            return
        try:
            self.artifacts_dir.mkdir(parents=True, exist_ok=True)
        except Exception:  # noqa: BLE001
            self.enabled = False

    # --- writes ---------------------------------------------------------
    def write_artifact(self, phase: str, content: str, *, title: str = "") -> Optional[str]:
        """Persist one phase's output. Returns the relative path written."""
        if not self.enabled or not content:
            return None
        with self._lock:
            self._step += 1
            rel = f"artifacts/{self._step:02d}-{_safe(phase)}.md"
        try:
            target = self.root / rel
            header = f"# {title or phase}\n\n" if (title or phase) else ""
            target.write_text(header + content, encoding="utf-8")
        except Exception:  # noqa: BLE001
            return None
        self.snapshot.artifacts.append(rel)
        return rel

    def append_event(self, event_type: str, data: Optional[dict[str, Any]] = None) -> None:
        if not self.enabled:
            return
        try:
            with self.events_path.open("a", encoding="utf-8") as handle:
                handle.write(
                    json.dumps(
                        {"ts": _now(), "event_type": event_type, "data": data or {}},
                        ensure_ascii=False,
                        default=str,
                    )
                    + "\n"
                )
        except Exception:  # noqa: BLE001
            pass

    def save(self) -> None:
        if not self.enabled:
            return
        self.snapshot.updated_at = _now()
        try:
            tmp = self.snapshot_path.with_suffix(".json.tmp")
            tmp.write_text(
                json.dumps(self.snapshot.to_json(), ensure_ascii=False, indent=2, default=str),
                encoding="utf-8",
            )
            tmp.replace(self.snapshot_path)
        except Exception:  # noqa: BLE001
            pass

    # --- transitions ----------------------------------------------------
    def set_phase(self, phase: str) -> None:
        self.snapshot.phase = phase
        self.append_event("phase", {"phase": phase})
        self.save()

    def record_plan(self, plan: Any) -> None:
        try:
            self.snapshot.plan = plan.model_dump() if hasattr(plan, "model_dump") else dict(plan)
        except Exception:  # noqa: BLE001
            self.snapshot.plan = None
        self.snapshot.title = getattr(plan, "title", "") or self.snapshot.title
        for task in getattr(plan, "agent_tasks", []) or []:
            self.snapshot.tasks[task.id] = TaskRecord(
                id=task.id, skill_name=task.skill_name, title=task.title
            )
        if self.snapshot.plan is not None:
            self.write_artifact(
                "plan",
                json.dumps(self.snapshot.plan, ensure_ascii=False, indent=2, default=str),
                title=f"Plan — {self.snapshot.title}",
            )
        self.append_event("plan_recorded", {"task_count": len(self.snapshot.tasks)})
        self.save()

    def task_started(self, task_id: str) -> None:
        record = self.snapshot.tasks.setdefault(task_id, TaskRecord(id=task_id))
        record.status = "active"
        record.updated_at = _now()
        self.save()

    def task_finished(
        self,
        task_id: str,
        output: str,
        *,
        skill_name: str = "",
        title: str = "",
        error: Optional[str] = None,
    ) -> None:
        record = self.snapshot.tasks.setdefault(task_id, TaskRecord(id=task_id))
        record.skill_name = skill_name or record.skill_name
        record.title = title or record.title
        record.status = "failed" if error else "complete"
        record.error = error
        record.updated_at = _now()
        if output:
            record.artifact = self.write_artifact(
                f"{task_id}-{record.skill_name or 'task'}",
                output,
                title=record.title or task_id,
            )
            record.tokens = len(output)
        self.append_event(
            "task_finished", {"task_id": task_id, "status": record.status, "error": error}
        )
        self.save()

    def finish(
        self,
        status: RunStatus,
        *,
        pause_reason: Optional[str] = None,
        error: Optional[str] = None,
    ) -> None:
        self.snapshot.status = status
        self.snapshot.pause_reason = pause_reason
        self.snapshot.error = error
        for record in self.snapshot.tasks.values():
            # A task left "active" by a dead process is not running any more.
            if record.status == "active":
                record.status = "pending"
        self.append_event("run_finished", {"status": status, "pause_reason": pause_reason})
        self.save()

    # --- reads ----------------------------------------------------------
    def resumable_outputs(self) -> dict[str, str]:
        """Completed task outputs from this run's artifacts, by task id."""
        out: dict[str, str] = {}
        for task_id, record in self.snapshot.tasks.items():
            if record.status != "complete" or not record.artifact:
                continue
            try:
                text = (self.root / record.artifact).read_text(encoding="utf-8")
            except Exception:  # noqa: BLE001
                continue
            # Drop the "# title" header the artifact was written with.
            body = text.split("\n\n", 1)[1] if text.startswith("# ") and "\n\n" in text else text
            out[task_id] = body
        return out


def load_run(settings: Any, run_id: str) -> Optional[RunStore]:
    """Reopen a previous run, or None when it has no readable snapshot."""
    store = RunStore(settings, run_id, create=False)
    try:
        data = json.loads(store.snapshot_path.read_text(encoding="utf-8"))
    except Exception:  # noqa: BLE001
        return None
    store.snapshot = RunSnapshot.from_json(data)
    store._step = len(store.snapshot.artifacts)
    return store


def list_runs(settings: Any, *, chat_id: Optional[str] = None, limit: int = 50) -> list[dict[str, Any]]:
    """Recent run snapshots, newest first."""
    root = Path(getattr(settings, "state_dir", Path("state"))) / "runs"
    if not root.is_dir():
        return []
    out: list[dict[str, Any]] = []
    for path in sorted(root.glob("*/snapshot.json"), key=lambda p: p.stat().st_mtime, reverse=True):
        try:
            data = json.loads(path.read_text(encoding="utf-8"))
        except Exception:  # noqa: BLE001
            continue
        if chat_id and data.get("chat_id") != chat_id:
            continue
        out.append(data)
        if len(out) >= limit:
            break
    return out


__all__ = [
    "RunStore",
    "RunSnapshot",
    "TaskRecord",
    "load_run",
    "list_runs",
]
