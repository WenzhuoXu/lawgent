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

Both providers drive the same ``InTurnContext``; only the message shapes
differ, and those adapters live here so the policy cannot drift between them.
"""

from __future__ import annotations

import json
from dataclasses import dataclass, field
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

    def observe_anthropic(self, usage: Any) -> None:
        # Anthropic reports cached input in separate buckets; the request's
        # size is their sum.
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

    # ----- Anthropic --------------------------------------------------------

    def before_anthropic_request(self, messages: list[Any]) -> bool:
        """Clear old ``tool_result`` blocks in place. True when anything changed."""
        names = _anthropic_tool_names(messages)
        slots: list[_Slot] = []
        tail: list[_Slot] = []
        for mi, msg in enumerate(messages):
            content = _get(msg, "content")
            if _get(msg, "role") != "user" or not isinstance(content, list):
                continue
            for bi, block in enumerate(content):
                if _get(block, "type") != "tool_result":
                    continue
                if str(_get(block, "tool_use_id")) in self._cleared:
                    continue
                slot = _anthropic_slot(messages, mi, bi, block, names)
                (tail if mi >= self._tail_start else slots).append(slot)
        cleared = self._compact(slots, tail)
        self._tail_start = len(messages)
        return cleared > 0

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
    round — a planner, a summariser — still gets exactly one.
    """
    value = requested if requested is not None else getattr(settings, "max_iterations", 0)
    try:
        value = int(value or 0)
    except (TypeError, ValueError):
        value = 0
    return value if value > 0 else None


def install_on_anthropic_runner(runner: Any, ctx: Optional[InTurnContext]) -> bool:
    """Run ``ctx`` before every request the SDK tool runner sends.

    ``BaseSyncToolRunner.__run__`` appends each round's assistant turn and tool
    results to ``_params`` and then opens the next request through
    ``_handle_request``; wrapping that one method is the only point where the
    transcript is complete and not yet sent. Returns False (and the turn runs
    unmanaged) if a future SDK moves either attribute.
    """
    if ctx is None:
        return False
    original = getattr(runner, "_handle_request", None)
    if original is None or not isinstance(getattr(runner, "_params", None), dict):
        return False

    def handle_request() -> Any:
        try:
            messages = list(runner._params.get("messages") or [])
            if ctx.before_anthropic_request(messages):
                runner.set_messages_params(lambda p: {**p, "messages": messages})
        except Exception as exc:  # noqa: BLE001 — never let accounting break a turn
            log_workflow_event(
                "in_turn_context_failed",
                {"provider": ctx.provider, "model": ctx.model, "error": str(exc)[:500]},
            )
        return original()

    runner._handle_request = handle_request
    return True


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


def _anthropic_tool_names(messages: list[Any]) -> dict[str, str]:
    names: dict[str, str] = {}
    for msg in messages:
        if _get(msg, "role") != "assistant":
            continue
        content = _get(msg, "content")
        if not isinstance(content, list):
            continue
        for block in content:
            if _get(block, "type") == "tool_use":
                names[str(_get(block, "id"))] = str(_get(block, "name") or "tool")
    return names


def _anthropic_slot(
    messages: list[Any], mi: int, bi: int, block: Any, names: dict[str, str]
) -> _Slot:
    text, images = _flatten(_get(block, "content"))
    name = names.get(str(_get(block, "tool_use_id")), "tool")

    def replace(stub: str) -> None:
        # The runner's params hold the messages we were handed; rebuild the
        # block as a plain dict so an SDK object is never mutated in place.
        msg = messages[mi]
        content = list(_get(msg, "content"))
        new_block = {
            "type": "tool_result",
            "tool_use_id": _get(block, "tool_use_id"),
            "content": stub,
        }
        if _get(block, "is_error"):
            new_block["is_error"] = True
        content[bi] = new_block
        messages[mi] = {"role": _get(msg, "role"), "content": content}

    return _Slot(
        name=name,
        tokens=estimate_tokens(text) + images * IMAGE_TOKENS,
        text=text,
        images=images,
        replace=replace,
        key=str(_get(block, "tool_use_id")),
        exempt=name in EXEMPT_TOOLS,
    )


__all__ = ["InTurnContext", "CLEAR_TARGET_SHARE", "install_on_anthropic_runner", "iteration_limit"]
