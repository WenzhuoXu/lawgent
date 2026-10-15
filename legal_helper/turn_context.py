"""What a tool running inside the author's loop knows about the turn.

The author answers every turn in one tool loop. Some of its tools need the turn
itself, not just their arguments: `run_workflow` hands its specialists the chat
digest, the attachments, the cancel signal and the run record, `load_pack`
adds a pack to the turn's settings, and `load_skill` reads the turn's active
domain packs. Tool functions take only the model's arguments, so the executor
opens a `TurnContext` for the loop and the tools read it here. Worker threads
see it because both providers run tools under a copy of the caller's context
(`providers/tool_exec.py`).
"""

from __future__ import annotations

import contextvars
import threading
from contextlib import contextmanager
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, Iterator, Optional


@dataclass
class TurnContext:
    settings: Any
    user_message: str = ""
    user_language: str = "Chinese"
    #: The chat digest: every specialist's cacheable request prefix.
    context_block: str = ""
    #: The rolling summary, which changes every turn.
    context_tail: str = ""
    attachments: list[Path] = field(default_factory=list)
    cancel_event: Optional[threading.Event] = None
    run_store: Any = None
    #: Bundles a paused run already finished: task id → {skill_name, title, text}.
    resume_tasks: dict[str, dict[str, str]] = field(default_factory=dict)
    #: Methods this turn used — loaded by the author or run by a specialist.
    methods_used: set[str] = field(default_factory=set)
    _lock: threading.Lock = field(default_factory=threading.Lock, repr=False)

    def note_method(self, name: str) -> None:
        with self._lock:
            self.methods_used.add(name)

    def cancelled(self) -> bool:
        return self.cancel_event is not None and self.cancel_event.is_set()


turn_context_var: contextvars.ContextVar[Optional[TurnContext]] = contextvars.ContextVar(
    "turn_context", default=None
)


def current_turn() -> Optional[TurnContext]:
    return turn_context_var.get()


@contextmanager
def open_turn(ctx: TurnContext) -> Iterator[TurnContext]:
    token = turn_context_var.set(ctx)
    try:
        yield ctx
    finally:
        turn_context_var.reset(token)


__all__ = ["TurnContext", "current_turn", "open_turn", "turn_context_var"]
