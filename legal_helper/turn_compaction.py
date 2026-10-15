"""Keep a long tool loop inside the turn ceiling by clearing old tool results.

A turn used to be bounded by counting: a round cap (``*_max_iterations``) and a
cumulative tool-output cap (``turn_result_budget``) that, once spent, told the
model to stop and answer from what it had. Both traded task completion for
cost. A spreadsheet reshape that needed 40 calls stopped at 33 with the edit
half done, and the model shipped no answer at all.

The term that actually grows during a loop is the tool results already in the
transcript, and most of them are spent by the time the model is ten calls
further on. So instead of stopping the loop, this module frees that space:

- before each request, project the input size from the provider's own count
  for the previous request plus the results appended since;
- past ``turn_input_ceiling_for`` (the window share *and* the OpenAI pricing
  cliff), clear the oldest tool results down to ``CLEAR_TARGET_SHARE`` of the
  ceiling — far enough below that clearing is rare, because every pass
  invalidates the cached prompt prefix;
- a cleared result is written to ``state/tool_results/`` first and replaced by
  a stub naming the tool and the path, so the model can reopen it with
  ``read_document``. A result that cannot be saved is never cleared: this is
  lossless by construction, not a truncation.

The latest round's results are never touched (the model has not read them
yet), and deliverable-shaped results (``tool_budget.EXEMPT_TOOLS``, e.g. a
sub-agent's finished memo) are cleared only after everything else.

That is the OpenAI path, where the conversation is ours to edit. On Anthropic
the same policy runs **server-side**. Opus 5.5 binds each thinking block to
the exact transcript before it, so editing an earlier ``tool_result`` from the
client invalidates every later thinking block — silently dropped reasoning on
older accounts, a 400 on accounts created on or after 2026-08-31. Server-side
context editing (``clear_tool_uses_20250919``) is exempt from that check,
because the check compares the conversation as sent. Measured on 2026-10-07,
the server clears in sticky chunks — the same tool uses stay cleared on the
following requests, which read the new prefix from cache — removing at least
``clear_at_least`` beyond the trigger, so ``trigger = ceiling`` and
``clear_at_least = (1 − CLEAR_TARGET_SHARE) × ceiling`` reproduce "clear down
to half the ceiling". What the server cannot do is leave a note in place of
what it removed, so losslessness is kept the append-only way: each sizeable
result is saved to ``state/tool_results/`` when it arrives, and a text block
after the round's ``tool_result`` blocks names the file. Clearing removes
results, never that note.

Both providers share the ceiling, the target share and the spill directory;
the adapters live here so the policy cannot drift between them.
"""

from __future__ import annotations

import json
import os
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, Callable, Optional

from .context import estimate_tokens, turn_input_ceiling_for
from .logging_setup import log_workflow_event
from .tool_budget import EXEMPT_TOOLS, _spill

# After a clearing pass, aim the projected input at this share of the ceiling.
# Clearing just enough to slip under the ceiling would re-trigger on the next
# round and bust the prompt cache every round; halving it amortises one cache
# miss over many rounds.
CLEAR_TARGET_SHARE = 0.5
# A stub costs ~80 tokens; clearing something not much bigger buys nothing.
MIN_CLEARABLE_TOKENS = 400
# Rough cost of one inline page image. Images are the heaviest thing a render
# loop leaves behind, and are never re-sent once cleared.
IMAGE_TOKENS = 1_600

_STUB = (
    "[Earlier {name} result (~{tokens:,} tokens) was cleared from context to make "
    "room for further work. Full text saved at {path} — reopen it with "
    'read_document(path="{path}", offset=<line>, limit=<lines>) if you need it again; '
    "do not re-run the tool just to see it.{images}]"
)

# --- Anthropic: server-side clearing ------------------------------------------

CONTEXT_MANAGEMENT_BETA = "context-management-2025-06-27"
_CLEAR_TOOL_USES_TYPE = "clear_tool_uses_20250919"
# The server keeps this many of the newest tool uses when it clears. A round
# of parallel calls rarely exceeds it, so the results the model has not read
# yet survive the pass, as the client-side rule guarantees for the last round.
CLEAR_KEEP_TOOL_USES = 10
# Never cleared server-side. The deliverable-shaped results the client-side
# pass clears last cannot be ordered "last" by the server, so they are kept;
# "memory" results are meant to persist by design.
SERVER_CLEAR_EXCLUDED_TOOLS = frozenset(EXEMPT_TOOLS | {"memory"})

_POINTER = (
    "[Context note: the {name} result above is also saved at {path}. If it is later "
    'cleared from context, reopen it with read_document(path="{path}", offset=<line>, '
    "limit=<lines>) instead of re-running the tool.{images}]"
)


def anthropic_context_management(settings: Any) -> Optional[dict[str, Any]]:
    """The ``context_management`` request parameter for an Anthropic tool loop.

    On by default: it costs nothing until a request reaches the turn ceiling.
    ``LEGAL_HELPER_ANTHROPIC_CLEAR_TOOLS`` set to ``0``/``false``/``off`` turns
    it off; a number pins the trigger in tokens instead of deriving it.

    If the clearable results add up to less than ``clear_at_least`` (say the
    input is mostly attachments), the server clears nothing and the request
    keeps growing. That is the price of clearing in chunks large enough to
    amortise the cache rewrite each pass costs.
    """
    raw = (os.getenv("LEGAL_HELPER_ANTHROPIC_CLEAR_TOOLS") or "").strip().lower()
    if raw in {"0", "false", "no", "off"}:
        return None
    try:
        trigger = int(turn_input_ceiling_for(settings))
    except Exception:  # noqa: BLE001 — accounting must never break a turn
        return None
    if raw.isdigit():
        trigger = max(int(raw), 1_000)
    return {
        "edits": [
            {
                "type": _CLEAR_TOOL_USES_TYPE,
                "trigger": {"type": "input_tokens", "value": trigger},
                "clear_at_least": {
                    "type": "input_tokens",
                    "value": int((1 - CLEAR_TARGET_SHARE) * trigger),
                },
                "keep": {"type": "tool_uses", "value": CLEAR_KEEP_TOOL_USES},
                "exclude_tools": sorted(SERVER_CLEAR_EXCLUDED_TOOLS),
            }
        ]
    }


def clearing_trigger(context_management: Optional[dict[str, Any]]) -> Optional[int]:
    """The input size at which the server starts clearing, from the request param."""
    for edit in (context_management or {}).get("edits") or []:
        if edit.get("type") == _CLEAR_TOOL_USES_TYPE:
            value = (edit.get("trigger") or {}).get("value")
            return int(value) if value else None
    return None


def clearing_pointer(
    name: str,
    text: str,
    *,
    images: int = 0,
    saved_path: Optional[Path] = None,
) -> Optional[str]:
    """Save a result the server may clear; return the note that outlives it.

    ``saved_path`` is a copy already on disk — a result the tool budget cut
    to a preview was spilled in full, and that full copy is the one to name.
    Returns None for a result too small to be worth clearing, one the server
    never clears, or one that could not be saved.
    """
    if name in SERVER_CLEAR_EXCLUDED_TOOLS:
        return None
    if estimate_tokens(text) + images * IMAGE_TOKENS < MIN_CLEARABLE_TOKENS:
        return None
    path = saved_path or _spill(name, text or "(no text; image-only result)")
    if path is None:
        return None
    return _POINTER.format(
        name=name,
        path=path,
        images=(
            f" Its {images} inline image(s) cannot be reopened that way; re-render the page instead."
            if images
            else ""
        ),
    )


@dataclass
class ClearingNotes:
    """Saved copies and notes for results the server may clear — written only once it might.

    Almost no turn comes near the trigger, so nothing is written at first.
    Once the projected next request reaches ``CLEAR_TARGET_SHARE`` of the
    trigger, every sizeable result so far is saved and named in one catch-up
    note, and each later result as it arrives. A note rides in the newest
    tool-result message, so it is in place before any request large enough to
    clear, and it is never edited afterwards.
    """

    ctx: Optional[InTurnContext]
    trigger: int
    _pending: list[tuple[str, str, int, Optional[Path]]] = field(default_factory=list)
    _armed: bool = False

    def notes_for(self, results: list[tuple[str, str, int, Optional[Path]]]) -> list[str]:
        """``results``: this round's ``(name, text, images, saved_path)``, in order."""
        self._pending.extend(results)
        if not self._armed:
            if self.ctx is not None and self.ctx.last_input_tokens is not None:
                fresh = sum(estimate_tokens(text) + images * IMAGE_TOKENS for _, text, images, _ in results)
                projected = self.ctx.last_input_tokens + self.ctx._last_output + fresh
                if projected < CLEAR_TARGET_SHARE * self.trigger:
                    return []
            self._armed = True
        notes = [
            clearing_pointer(name, text, images=images, saved_path=saved)
            for name, text, images, saved in self._pending
        ]
        self._pending.clear()
        return [note for note in notes if note]


@dataclass
class _Slot:
    """One tool result in the transcript, located so it can be rewritten."""

    name: str
    tokens: int
    text: str
    images: int
    replace: Callable[[str], None]
    key: str = ""
    exempt: bool = False


@dataclass
class InTurnContext:
    """Per-turn state: the last provider count and where the new tail begins."""

    ceiling: int
    provider: str
    model: str
    _last_input: Optional[int] = None
    _last_output: int = 0
    _tail_start: int = 0
    passes: int = field(default=0)
    _cleared: set[str] = field(default_factory=set)
    # The provider's own count for the most recent request, never adjusted for
    # clearing: how full the context really was. The done event reports it.
    last_input_tokens: Optional[int] = None

    @classmethod
    def for_settings(cls, settings: Any, *, provider: str, model: str) -> Optional["InTurnContext"]:
        try:
            return cls(ceiling=int(turn_input_ceiling_for(settings)), provider=provider, model=model)
        except Exception:  # noqa: BLE001 — accounting must never break a turn
            return None

    def observe(self, input_tokens: Optional[int], output_tokens: Optional[int]) -> None:
        """Record the provider's count for the request that just finished."""
        if input_tokens:
            self._last_input = int(input_tokens)
            self._last_output = int(output_tokens or 0)
            self.last_input_tokens = int(input_tokens)

    def observe_anthropic(self, usage: Any) -> None:
        # Anthropic reports cached input in separate buckets; the request's
        # size is their sum. Only observed on this path — the clearing itself
        # is the server's (see the module docstring).
        if usage is None:
            return
        total = sum(
            int(_get(usage, k) or 0)
            for k in ("input_tokens", "cache_read_input_tokens", "cache_creation_input_tokens")
        )
        self.observe(total, _get(usage, "output_tokens"))

    def observe_openai(self, usage: Any) -> None:
        # OpenAI's cached tokens are a subset of input_tokens already.
        if usage is not None:
            self.observe(_get(usage, "input_tokens"), _get(usage, "output_tokens"))

    # ----- core -------------------------------------------------------------

    def _compact(self, slots: list[_Slot], tail: list[_Slot]) -> int:
        """Clear old results if the next request would pass the ceiling.

        Returns how many results were cleared.
        """
        if self._last_input is None:
            return 0
        projected = self._last_input + self._last_output + sum(s.tokens for s in tail)
        if projected <= self.ceiling:
            return 0
        need = projected - int(CLEAR_TARGET_SHARE * self.ceiling)
        freed = cleared = 0
        # Ordinary evidence first, oldest first; deliverables only if that
        # was not enough.
        for slot in [s for s in slots if not s.exempt] + [s for s in slots if s.exempt]:
            if freed >= need:
                break
            if slot.tokens < MIN_CLEARABLE_TOKENS:
                continue
            path = _spill(slot.name, slot.text or "(no text; image-only result)")
            if path is None:
                continue  # lossless or not at all
            stub = _STUB.format(
                name=slot.name,
                tokens=slot.tokens,
                path=path,
                images=(
                    f" {slot.images} inline image(s) were dropped; re-render the page if you need to look again."
                    if slot.images
                    else ""
                ),
            )
            slot.replace(stub)
            self._cleared.add(slot.key)
            freed += max(0, slot.tokens - estimate_tokens(stub))
            cleared += 1
        self.passes += 1
        # The next provider count reflects the cleared transcript; until then
        # assume the freed space so a second pass does not fire on stale data.
        self._last_input = max(0, self._last_input - freed)
        log_workflow_event(
            "in_turn_context_cleared",
            {
                "provider": self.provider,
                "model": self.model,
                "projected_tokens": projected,
                "ceiling": self.ceiling,
                "freed_tokens": freed,
                "results_cleared": cleared,
                "pass": self.passes,
                "short_of_target": freed < need,
            },
        )
        return cleared

    # ----- OpenAI -----------------------------------------------------------

    def before_openai_request(self, history: list[dict[str, Any]], names: dict[str, str]) -> bool:
        """Clear old ``function_call_output`` items in place. True when changed."""
        slots: list[_Slot] = []
        tail: list[_Slot] = []
        for i, item in enumerate(history):
            if not isinstance(item, dict) or item.get("type") != "function_call_output":
                continue
            if str(item.get("call_id")) in self._cleared:
                continue
            text, images = _flatten(item.get("output"))
            name = names.get(str(item.get("call_id")), "tool")

            def replace(stub: str, item: dict[str, Any] = item) -> None:
                item["output"] = stub

            slot = _Slot(
                name=name,
                tokens=estimate_tokens(text) + images * IMAGE_TOKENS,
                text=text,
                images=images,
                replace=replace,
                key=str(item.get("call_id")),
                exempt=name in EXEMPT_TOOLS,
            )
            (tail if i >= self._tail_start else slots).append(slot)
        cleared = self._compact(slots, tail)
        self._tail_start = len(history)
        return cleared > 0


def iteration_limit(requested: Optional[int], settings: Any) -> Optional[int]:
    """Resolve a tool-loop round limit; None means run until the model is done.

    ``0`` (the shipped default for every loop) and negative values mean no
    limit. A positive value is honoured, so a caller that genuinely wants one
    round — a title or a summary call — still gets exactly one.
    """
    value = requested if requested is not None else getattr(settings, "max_iterations", 0)
    try:
        value = int(value or 0)
    except (TypeError, ValueError):
        value = 0
    return value if value > 0 else None


# ----- shape helpers --------------------------------------------------------


def _get(obj: Any, key: str, default: Any = None) -> Any:
    if isinstance(obj, dict):
        return obj.get(key, default)
    return getattr(obj, key, default)


def _flatten(content: Any) -> tuple[str, int]:
    """Text of a tool result plus a count of the images riding in it."""
    if content is None:
        return "", 0
    if isinstance(content, str):
        return content, 0
    if isinstance(content, list):
        parts: list[str] = []
        images = 0
        for block in content:
            btype = _get(block, "type")
            if btype in {"image", "input_image"}:
                images += 1
            elif btype in {"text", "input_text", "output_text"}:
                parts.append(str(_get(block, "text") or ""))
            else:
                parts.append(json.dumps(block, ensure_ascii=False, default=str))
        return "\n".join(parts), images
    return str(content), 0


__all__ = [
    "InTurnContext",
    "ClearingNotes",
    "CLEAR_TARGET_SHARE",
    "CLEAR_KEEP_TOOL_USES",
    "CONTEXT_MANAGEMENT_BETA",
    "SERVER_CLEAR_EXCLUDED_TOOLS",
    "anthropic_context_management",
    "clearing_pointer",
    "clearing_trigger",
    "iteration_limit",
]
