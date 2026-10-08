"""Progress events for work the model is doing before any tool runs.

Before a `write_docx` call the user saw nothing for p50 30 s / p90 163 s: the
model was streaming the document into the call's arguments, and both providers
dropped those deltas until the call was complete. Hosted web searches on the
OpenAI path surfaced only at `response.completed`, after every search in the
response had already finished.

Two event kinds fill those gaps. Both are transient previews, like
`reasoning_delta`: they describe progress, not results, and nothing downstream
should treat them as a tool call.

- ``tool_progress`` — ``{tool_name, call_id, chars_so_far, agent}``: a local
  tool call whose arguments are still being written. Throttled per call to one
  event per ``PROGRESS_INTERVAL_S``, and silent for a call that finishes
  streaming inside the first interval, so ordinary small calls emit nothing.
- ``hosted_tool_progress`` — ``{tool_name, tool_id, status, query?, url?,
  provider, model, agent}`` with ``status`` ``"searching"`` or ``"completed"``:
  a provider-hosted search or fetch, reported when the provider reports it.
"""

from __future__ import annotations

import time
from dataclasses import dataclass, field
from typing import Any, Callable, Optional

from ..logging_setup import agent_name_var
from .base import StreamEvent

# Same cadence as the specialist answer preview (`workflow._emit_preview`):
# frequent enough to read as live, rare enough not to flood N parallel agents.
PROGRESS_INTERVAL_S = 0.25


@dataclass
class _Call:
    tool_name: str
    call_id: str
    started_at: float
    last_emit: float
    chars: int = 0


@dataclass
class ToolProgressThrottle:
    """Turns streamed argument deltas into throttled ``tool_progress`` events."""

    interval: float = PROGRESS_INTERVAL_S
    clock: Callable[[], float] = time.monotonic
    _calls: dict[Any, _Call] = field(default_factory=dict)

    def open(self, key: Any, tool_name: str, call_id: Optional[str]) -> None:
        now = self.clock()
        self._calls[key] = _Call(tool_name=tool_name or "tool", call_id=str(call_id or key), started_at=now, last_emit=now)

    def update(self, key: Any, *, chars_so_far: Optional[int] = None, added: int = 0) -> Optional[StreamEvent]:
        """Record progress; return an event when one is due, else None."""
        call = self._calls.get(key)
        if call is None:
            return None
        call.chars = int(chars_so_far) if chars_so_far is not None else call.chars + int(added)
        now = self.clock()
        if now - call.last_emit < self.interval:
            return None
        call.last_emit = now
        return StreamEvent(
            "tool_progress",
            {
                "tool_name": call.tool_name,
                "call_id": call.call_id,
                "chars_so_far": call.chars,
                "agent": agent_name_var.get(),
            },
        )

    def close(self, key: Any) -> None:
        self._calls.pop(key, None)


def hosted_progress_event(
    *,
    provider: str,
    model: str,
    tool_name: str,
    tool_id: Optional[str],
    status: str,
    query: Optional[str] = None,
    url: Optional[str] = None,
) -> StreamEvent:
    data: dict[str, Any] = {
        "provider": provider,
        "model": model,
        "tool_name": tool_name,
        "tool_id": tool_id,
        "status": status,
        "agent": agent_name_var.get(),
    }
    if query:
        data["query"] = query
    if url:
        data["url"] = url
    return StreamEvent("hosted_tool_progress", data)


__all__ = ["PROGRESS_INTERVAL_S", "ToolProgressThrottle", "hosted_progress_event"]
