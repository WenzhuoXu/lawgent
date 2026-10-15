"""Loguru text log + JSONL turn and event logs shared by the author and sub-agents."""

from __future__ import annotations

import contextvars
import json
import re
import time
import uuid
from contextlib import contextmanager
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Callable, Iterator, Optional

from loguru import logger

from .config import Settings, current_settings


_CONFIGURED = False

# Run-scoped context: the author's turn sets `run_id` (its agent name is
# "orchestrator"); sub-agents set their own while preserving `parent_run_id`.
run_id_var: contextvars.ContextVar[Optional[str]] = contextvars.ContextVar("run_id", default=None)
parent_run_id_var: contextvars.ContextVar[Optional[str]] = contextvars.ContextVar(
    "parent_run_id", default=None
)
agent_name_var: contextvars.ContextVar[str] = contextvars.ContextVar(
    "agent_name", default="orchestrator"
)
workflow_event_sink_var: contextvars.ContextVar[Optional[Callable[[dict[str, Any]], None]]] = (
    contextvars.ContextVar("workflow_event_sink", default=None)
)
# Previews that reach a watching client but are never persisted: a
# specialist's rolling output tail ticks up to four times a second, and a tool
# running inside the author's loop cannot yield to the turn's generator.
transient_event_sink_var: contextvars.ContextVar[Optional[Callable[[str, dict[str, Any]], None]]] = (
    contextvars.ContextVar("transient_event_sink", default=None)
)
# The id of the tool call whose body is running (set by both providers), so a
# tool that spawns work can name itself as the parent of that work.
tool_call_id_var: contextvars.ContextVar[Optional[str]] = contextvars.ContextVar(
    "tool_call_id", default=None
)
# Inside a workflow specialist: its task id and the `run_workflow` call that
# started it. Every event the specialist emits carries both, which is how a
# client nests a sub-agent's tool calls under the call that spawned it.
task_id_var: contextvars.ContextVar[Optional[str]] = contextvars.ContextVar("task_id", default=None)
parent_call_id_var: contextvars.ContextVar[Optional[str]] = contextvars.ContextVar(
    "parent_call_id", default=None
)


@contextmanager
def sub_agent_scope(task_id: str) -> Iterator[None]:
    """Tag what a tool's sub-agent emits with ``task_id`` and the running call.

    A deck or procedure sub-agent runs inside one tool call; tagged, its tool
    calls and progress nest under that call instead of reaching the client as
    top-level calls of their own that finish while the tool is still running.
    Inside a workflow specialist the specialist's own tags stay, so the work
    still nests under the specialist that asked for it.
    """
    if task_id_var.get() is not None:
        yield
        return
    task_token = task_id_var.set(task_id)
    parent_token = parent_call_id_var.set(tool_call_id_var.get())
    try:
        yield
    finally:
        parent_call_id_var.reset(parent_token)
        task_id_var.reset(task_token)


# `token(?!s\b)`: an access token is a secret, a token *count* (input_tokens,
# total_tokens) is telemetry — redacting the counts blanked every usage figure
# in the event stream.
_SECRET_KEY_RE = re.compile(r"(api[_-]?key|authorization|secret|token(?!s\b)|bearer)", re.IGNORECASE)


def _redact(value: Any) -> Any:
    if isinstance(value, dict):
        return {k: ("***REDACTED***" if _SECRET_KEY_RE.search(k) else _redact(v)) for k, v in value.items()}
    if isinstance(value, list):
        return [_redact(v) for v in value]
    if isinstance(value, str) and len(value) > 32 and value.startswith(("sk-", "anthropic-", "Bearer ")):
        return "***REDACTED***"
    return value


def setup_logging(settings: Optional[Settings] = None) -> None:
    global _CONFIGURED
    if _CONFIGURED:
        return
    s = settings or current_settings()
    s.logs_dir.mkdir(parents=True, exist_ok=True)
    logger.remove()
    logger.add(
        s.logs_dir / "legal_helper.log",
        rotation="1 day",
        retention="14 days",
        enqueue=False,
        level="INFO",
    )
    _CONFIGURED = True


def new_run_id() -> str:
    return uuid.uuid4().hex[:16]


def jsonl_path(settings: Optional[Settings] = None) -> Path:
    s = settings or current_settings()
    return s.logs_dir / f"turns-{datetime.now(timezone.utc):%Y-%m-%d}.jsonl"


def event_jsonl_path(settings: Optional[Settings] = None) -> Path:
    s = settings or current_settings()
    return s.logs_dir / f"events-{datetime.now(timezone.utc):%Y-%m-%d}.jsonl"


def log_turn(record: dict[str, Any]) -> None:
    """Append a single turn record to today's JSONL log."""
    setup_logging()
    record = dict(record)
    record.setdefault("ts", datetime.now(timezone.utc).isoformat())
    record.setdefault("run_id", run_id_var.get())
    record.setdefault("parent_run_id", parent_run_id_var.get())
    record.setdefault("agent", agent_name_var.get())
    record = _redact(record)
    path = jsonl_path()
    path.parent.mkdir(parents=True, exist_ok=True)
    with path.open("a", encoding="utf-8") as f:
        f.write(json.dumps(record, ensure_ascii=False, default=str) + "\n")


def _with_task_context(data: dict[str, Any]) -> dict[str, Any]:
    """``data`` plus the specialist task it came from, when it came from one."""
    task_id = task_id_var.get()
    parent = parent_call_id_var.get()
    if task_id is None and parent is None:
        return data
    out = dict(data)
    if task_id is not None:
        out.setdefault("task_id", task_id)
    if parent is not None:
        out.setdefault("parent_call_id", parent)
    return out


def emit_transient(kind: str, data: dict[str, Any]) -> None:
    """Send a preview event to a watching client, if any. Never persisted."""
    sink = transient_event_sink_var.get()
    if sink is None:
        return
    try:
        sink(kind, _with_task_context(data))
    except Exception:  # noqa: BLE001 — a preview must never break a run
        logger.exception("transient event sink failed")


def log_workflow_event(event_type: str, data: Optional[dict[str, Any]] = None) -> dict[str, Any]:
    """Append a small event record for UI/activity timelines and debugging.

    Turn logs capture the complete provider request/response after a turn
    finishes. Event logs capture what is happening inside the turn: the
    workflow and skill lifecycle, local tool calls, hosted search calls, and
    verification markers.
    """
    setup_logging()
    record = {
        "ts": datetime.now(timezone.utc).isoformat(),
        "event_type": event_type,
        "run_id": run_id_var.get(),
        "parent_run_id": parent_run_id_var.get(),
        "agent": agent_name_var.get(),
        "data": _with_task_context(data or {}),
    }
    # Failure/degradation events carry their trajectory subclass so a run's
    # failure profile can be aggregated after the fact without re-parsing prose.
    # Tagging happens here rather than at ~30 call sites so the vocabulary
    # cannot drift away from the events that actually fire.
    try:
        from .citations.trajectory import classify_event

        subclass = classify_event(event_type)
        if subclass is not None:
            record["failure"] = {
                "subclass": subclass.key,
                "layer": subclass.layer.value,
                "category": subclass.category.value,
            }
    except Exception:  # noqa: BLE001 — observability must never break a run
        pass
    record = _redact(record)
    path = event_jsonl_path()
    path.parent.mkdir(parents=True, exist_ok=True)
    with path.open("a", encoding="utf-8") as f:
        f.write(json.dumps(record, ensure_ascii=False, default=str) + "\n")

    sink = workflow_event_sink_var.get()
    if sink is not None:
        try:
            sink(record)
        except Exception:
            logger.exception("workflow event sink failed")
    return record


class TurnTimer:
    """Context manager that measures latency for a single provider round-trip."""

    def __init__(self) -> None:
        self.start = 0.0
        self.latency_ms = 0

    def __enter__(self) -> "TurnTimer":
        self.start = time.perf_counter()
        return self

    def __exit__(self, exc_type, exc, tb) -> None:
        self.latency_ms = int((time.perf_counter() - self.start) * 1000)
