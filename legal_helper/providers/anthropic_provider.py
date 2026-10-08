"""Anthropic provider — Claude via `client.beta.messages.tool_runner`."""

from __future__ import annotations

import copy
import json
import time
from typing import Any, Iterable, Iterator, Optional

import httpx
from anthropic import Anthropic
from anthropic.lib.tools._beta_functions import (
    BetaBuiltinFunctionTool,
    BetaFunctionTool,
    ToolError,
)

from ..attachments import needs_anthropic_files_beta
from ..config import Settings
from ..logging_setup import TurnTimer, agent_name_var, log_turn, log_workflow_event
from ..tool_budget import apply_result_budget, budget_log_fields
from ..turn_compaction import (
    CONTEXT_MANAGEMENT_BETA,
    ClearingNotes,
    InTurnContext,
    anthropic_context_management,
    clearing_trigger,
    iteration_limit,
)
from ..tools.multimodal import split_inline_images, to_anthropic_tool_content
from ..usage import record_usage
from .base import JSON_OBJECT, Message, RunResult, StreamEvent, Tool, ToolCallRecord
from .progress import ToolProgressThrottle, hosted_progress_event
from .tool_exec import run_tool_calls


# The shared reasoning-effort dial mapped onto Anthropic's ``output_config.effort``.
# Every offered Claude model (Opus 4.7+, Sonnet 5) rejects manual
# ``thinking: {type: "enabled", budget_tokens}`` with a 400, so depth is set by
# adaptive thinking plus effort. "none" has no Anthropic equivalent — Opus 5.5
# cannot disable thinking at all — so it maps to the lowest effort instead.
ANTHROPIC_EFFORTS: dict[str, str] = {
    "none": "low",
    "low": "low",
    "medium": "medium",
    "high": "high",
    "xhigh": "xhigh",
    "max": "max",
}

# What the reasoning stream carries. Opus 5.5 and Sonnet 5 default to
# "omitted": thinking blocks arrive with empty text, so the UI's reasoning pane
# stayed blank on both. "summarized" returns a readable summary — and on Opus
# 5.5 the short notes it writes between tool calls, which it now puts in
# thinking blocks rather than text. Display changes visibility only; thinking
# is billed the same under every setting.
THINKING_DISPLAY = "summarized"


def _thinking_kwargs(effort: str) -> dict[str, Any]:
    """Build the Anthropic ``thinking`` / ``output_config`` params from the dial.

    Adaptive thinking is sent for every level, "none" included: both offered
    models run adaptive when ``thinking`` is omitted, so sending it explicitly
    changes nothing except that ``display`` can be set.
    """
    level = (effort or "none").lower()
    return {
        "thinking": {"type": "adaptive", "display": THINKING_DISPLAY},
        "output_config": {"effort": ANTHROPIC_EFFORTS.get(level, "medium")},
    }


WEB_SEARCH_TOOL = {"type": "web_search_20250305", "name": "web_search"}
WEB_FETCH_TOOL = {"type": "web_fetch_20250910", "name": "web_fetch"}
WEB_BETAS = ["web-search-2025-03-05", "web-fetch-2025-09-10"]
FILES_API_BETA = "files-api-2025-04-14"
CACHE_EPHEMERAL: dict[str, str] = {"type": "ephemeral"}
# The system prompt and the tool schemas are identical across every turn on a
# route, but the next turn often starts 5–60 minutes later: past the default
# TTL, inside this one. A 1-hour write costs 2x the input rate (5-minute: 1.25x)
# and pays for itself with a single read in that window. Longer TTLs must come
# first in the prefix, which tools → system → messages already gives us.
STABLE_PREFIX_CACHE: dict[str, str] = {"type": "ephemeral", "ttl": "1h"}
# Per-request limit on cache_control breakpoints; a fifth is a 400.
MAX_CACHE_BREAKPOINTS = 4


def _cached_system(system: str) -> Any:
    """System prompt as a text block carrying a cache_control breakpoint.

    Prompt caching is a prefix match and the prefix renders tools -> system ->
    messages, so this single breakpoint caches the stable tool schemas + system
    prompt together; volatile per-turn content stays after it in ``messages``.
    Cached reads bill ~0.05x the input rate on Opus 5.5, so every tool-runner
    iteration and follow-up chat turn stops re-billing the legal system prompt.
    """
    if not system:
        return system
    return [{"type": "text", "text": system, "cache_control": dict(STABLE_PREFIX_CACHE)}]


def _tools_with_cache_breakpoint(tools: list[Any]) -> list[Any]:
    """Copy of *tools* with a cache_control breakpoint on the last definition.

    Keeps the tool list independently cacheable across agents that share tools
    but differ in system prompt. Non-mutating: shared tool objects are never
    annotated in place (a mid-list annotation elsewhere would burn one of the
    four allowed breakpoints per request).
    """
    if not tools:
        return tools
    out = list(tools)
    last = out[-1]
    if isinstance(last, dict):
        if "cache_control" not in last:
            out[-1] = {**last, "cache_control": dict(STABLE_PREFIX_CACHE)}
    elif isinstance(last, BetaFunctionTool) and getattr(last, "_cache_control", None) is None:
        annotated = copy.copy(last)
        annotated._cache_control = dict(STABLE_PREFIX_CACHE)
        out[-1] = annotated
    return out


def _has_cache_marker(obj: Any) -> bool:
    if isinstance(obj, dict):
        return obj.get("cache_control") is not None
    return getattr(obj, "_cache_control", None) is not None or getattr(obj, "cache_control", None) is not None


def _cache_breakpoints(tools: list[Any], system: Any, messages: list[Any]) -> int:
    """Explicit breakpoints a request already carries."""
    count = sum(_has_cache_marker(t) for t in tools or [])
    if isinstance(system, list):
        count += sum(_has_cache_marker(b) for b in system)
    for message in messages or []:
        content = message.get("content") if isinstance(message, dict) else getattr(message, "content", None)
        if not isinstance(content, list):
            continue
        for block in content:
            count += _has_cache_marker(block)
            nested = block.get("content") if isinstance(block, dict) else None
            if isinstance(nested, list):
                count += sum(_has_cache_marker(b) for b in nested)
    return count


def _automatic_cache_kwarg(
    tools: list[Any], system: Any, messages: list[Any], *, loops: bool = True
) -> dict[str, Any]:
    """Top-level ``cache_control``: a breakpoint that follows the conversation.

    The explicit breakpoints sit on the tools, the system prompt and the chat
    digest, so every local-tool round inside a loop was re-read uncached; the
    automatic one moves to the last block of each request, making each round's
    prefix a read on the next. Default 5-minute TTL — rounds start well inside
    it, and an automatic entry may not outlive the explicit ones before it.

    Only where there is a next round to read it (``loops``): on a one-shot
    call — router, planner, summary, title — it would land after content
    unique to that call and bill the write premium on bytes never read back.
    Also skipped when the explicit breakpoints already fill the four slots.
    """
    if not loops or _cache_breakpoints(tools, system, messages) >= MAX_CACHE_BREAKPOINTS:
        return {}
    return {"cache_control": dict(CACHE_EPHEMERAL)}


def _instrument_local_tools(tools: list[Any], *, provider: str, model: str) -> list[Any]:
    """Wrap local ``BetaFunctionTool`` objects so each execution emits
    ``local_tool_call_started`` / ``local_tool_call_finished`` log events with
    the SAME shape OpenAI already emits.

    This is the fallback path. Normally ``_install_tool_execution`` replaces
    the runner's own tool execution and these wrappers are never called; if a
    future SDK moves that hook, the runner calls the tools itself, serially,
    and the wrappers keep logging and result budgets working.
    """
    out: list[Any] = []
    for tool in tools:
        if not isinstance(tool, BetaFunctionTool):
            out.append(tool)
            continue
        wrapped = copy.copy(tool)
        inner_call = tool.call
        tool_name = getattr(tool, "name", "tool")

        def make_call(_inner_call, _name):
            def instrumented_call(input: object) -> Any:
                args = input if isinstance(input, dict) else {"input": input}
                log_workflow_event(
                    "local_tool_call_started",
                    {"provider": provider, "model": model, "tool_name": _name, "arguments": args},
                )
                try:
                    result = _inner_call(input)
                except Exception as exc:  # emit an error-shaped finished event, then re-raise
                    log_workflow_event(
                        "local_tool_call_finished",
                        {
                            "provider": provider,
                            "model": model,
                            "tool_name": _name,
                            "output_preview": f'{{"error": {exc!r}}}'[:1200],
                            "output_chars": 0,
                            "error": str(exc),
                        },
                    )
                    raise
                # Tools that render pages return their images inline via the
                # __inline_images__ protocol; expand them into tool_result
                # content blocks so the model actually SEES what it produced.
                # Log the text half only — base64 must never hit the log.
                output_str, images = split_inline_images(result)
                # Image-carrying results keep their protocol intact; text-only
                # results are cut to the tool's declared model-facing budget.
                budgeted = (
                    apply_result_budget(_name, output_str) if not images else None
                )
                log_workflow_event(
                    "local_tool_call_finished",
                    {
                        "provider": provider,
                        "model": model,
                        "tool_name": _name,
                        "output_preview": output_str[:1200],
                        "output_chars": len(output_str),
                        **({"inline_images": len(images)} if images else {}),
                        **budget_log_fields(budgeted),
                    },
                )
                if images:
                    return to_anthropic_tool_content(result)
                return budgeted.text if budgeted is not None else result

            return instrumented_call

        # Bind the instrumented call onto the copied instance.
        object.__setattr__(wrapped, "call", make_call(inner_call, tool_name))
        out.append(wrapped)
    return out


def _runnable_tools(tools: list[Any]) -> dict[str, Any]:
    """The tools this process executes, by name (hosted tools run server-side)."""
    return {
        tool.name: tool
        for tool in tools
        if isinstance(tool, (BetaFunctionTool, BetaBuiltinFunctionTool))
    }


def _execute_tool_uses(
    uses: list[Any],
    runnable: dict[str, Any],
    *,
    provider: str,
    model: str,
    notes: Optional[ClearingNotes] = None,
) -> dict[str, Any]:
    """Run one response's tool_use blocks; return the user message carrying results.

    Mirrors the SDK runner's own loop — an unknown tool, a ``ToolError`` and
    any other exception all become ``is_error`` results the model can read —
    with three differences: independent read-only calls run side by side
    (``tool_exec``), results are logged and budgeted here rather than in a
    wrapper, and with server-side clearing on, a note naming each sizeable
    result's saved copy follows the results, where clearing cannot reach it.
    """
    names = [str(getattr(u, "name", "") or "") for u in uses]
    durations = [0] * len(uses)
    blocks: list[dict[str, Any]] = [{} for _ in uses]
    clearable: list[tuple[str, str, int, Any]] = []

    def before(i: int) -> None:
        args = getattr(uses[i], "input", None)
        log_workflow_event(
            "local_tool_call_started",
            {
                "provider": provider,
                "model": model,
                "tool_name": names[i],
                "arguments": args if isinstance(args, dict) else {"input": args},
            },
        )

    def work(i: int) -> tuple[str, Any]:
        tool = runnable.get(names[i])
        started = time.monotonic()
        try:
            if tool is None:
                return "missing", None
            try:
                return "ok", tool.call(getattr(uses[i], "input", None))
            except ToolError as exc:
                return "tool_error", exc
            except Exception as exc:  # noqa: BLE001 — returned to the model, as the SDK does
                return "error", exc
        finally:
            durations[i] = int((time.monotonic() - started) * 1000)

    def after(i: int, outcome: tuple[str, Any]) -> None:
        kind, value = outcome
        name = names[i]
        block: dict[str, Any] = {"type": "tool_result", "tool_use_id": getattr(uses[i], "id", None)}
        extra: dict[str, Any] = {}
        if kind == "ok":
            output_str, images = split_inline_images(value)
            # Image-carrying results keep their protocol intact; text-only
            # results are cut to the tool's declared model-facing budget.
            budgeted = apply_result_budget(name, output_str) if not images else None
            block["content"] = (
                to_anthropic_tool_content(value)
                if images
                else (budgeted.text if budgeted is not None else value)
            )
            extra = {**({"inline_images": len(images)} if images else {}), **budget_log_fields(budgeted)}
            clearable.append(
                (
                    name,
                    budgeted.text if budgeted is not None else output_str,
                    len(images),
                    budgeted.spill_path if budgeted is not None else None,
                )
            )
        else:
            if kind == "missing":
                block["content"] = f"Error: Tool '{name}' not found"
                output_str = block["content"]
            elif kind == "tool_error":
                block["content"] = value.content
                output_str = f'{{"error": {str(value)!r}}}'
            else:
                block["content"] = repr(value)
                output_str = f'{{"error": {value!r}}}'
            block["is_error"] = True
            extra = {"error": str(value) if value is not None else block["content"]}
        log_workflow_event(
            "local_tool_call_finished",
            {
                "provider": provider,
                "model": model,
                "tool_name": name,
                "output_preview": output_str[:1200],
                "output_chars": len(output_str),
                "duration_ms": durations[i],
                **({"calls_in_response": len(uses)} if len(uses) > 1 else {}),
                **extra,
            },
        )
        blocks[i] = block

    run_tool_calls(names, work, before=before, after=after)
    content: list[dict[str, Any]] = list(blocks)
    lines = notes.notes_for(clearable) if notes is not None else []
    if lines:
        # After every tool_result block, as the API requires; append-only, so
        # earlier turns are never edited and later thinking stays valid.
        content.append({"type": "text", "text": "\n".join(lines)})
    return {"role": "user", "content": content}


def _install_tool_execution(
    runner: Any,
    runnable: dict[str, Any],
    *,
    provider: str,
    model: str,
    notes: Optional[ClearingNotes] = None,
) -> bool:
    """Route the runner's tool execution through ``_execute_tool_uses``.

    ``BaseSyncToolRunner.generate_tool_call_response`` caches whatever
    ``_generate_tool_call_response`` returns, and the runner appends it to the
    conversation; that method is the one place the calls of a response are
    executed. Returns False — and the SDK's serial execution of the
    instrumented tools stays in charge — if a future SDK moves it.
    """
    original = getattr(runner, "_generate_tool_call_response", None)
    last_content = getattr(runner, "_get_last_assistant_message_content", None)
    if not callable(original) or not callable(last_content):
        return False

    def generate() -> Optional[dict[str, Any]]:
        content = last_content()
        if not content:
            return None
        uses = [block for block in content if getattr(block, "type", None) == "tool_use"]
        if not uses:
            return None
        return _execute_tool_uses(uses, runnable, provider=provider, model=model, notes=notes)

    runner._generate_tool_call_response = generate
    return True


# Parity with the OpenAI provider: when a caller sets a positive
# `max_iterations` (the defaults are unlimited) and the loop is cut off by it,
# the model has the tool results but never got a turn in which tools were
# unavailable, so the answer can come back empty.
CEILING_SYNTHESIS_NUDGE = (
    "You have reached this turn's tool-call limit, so no further tools are "
    "available. Using only the information already gathered above, write the "
    "complete final answer for the user now. Where something could not be "
    "verified with the tools you had, say so explicitly instead of omitting it."
)


def _runner_hit_ceiling(final: Any) -> bool:
    """True when `tool_runner` stopped while the model still wanted tools.

    The SDK loop appends the assistant turn AND its tool results before
    re-checking `_should_stop()` (`BaseSyncToolRunner.__run__`), so a final
    message still carrying ``stop_reason == "tool_use"`` means the iteration
    ceiling ended the loop rather than the model finishing its answer.
    """
    return getattr(final, "stop_reason", None) == "tool_use"


def _merge_usage(total: dict[str, Any], u: Any) -> None:
    """Accumulate one model turn's usage counters into a running total.

    ``cache_creation_1h_input_tokens`` is the part of the cache writes made at
    the 1-hour TTL, which bills at 2x the input rate rather than the 5-minute
    1.25x. Kept as its own key so the ledger can price it; recording it does
    not change what the ledger computes today.
    """
    if u is None:
        return
    for k in ("input_tokens", "output_tokens", "cache_creation_input_tokens", "cache_read_input_tokens"):
        v = getattr(u, k, None)
        if v:
            total[k] = total.get(k, 0) + v
    one_hour = getattr(getattr(u, "cache_creation", None), "ephemeral_1h_input_tokens", None)
    if one_hour:
        total["cache_creation_1h_input_tokens"] = total.get("cache_creation_1h_input_tokens", 0) + one_hour


def _with_last_request(usage: dict[str, Any], in_turn: Optional[InTurnContext]) -> dict[str, Any]:
    """Turn totals plus the last request's full input (all three input buckets).

    ``input_tokens`` and the cache buckets are summed over the tool loop, which
    is what billing needs; the context-fill reading needs the last request alone.
    """
    out = dict(usage)
    last = getattr(in_turn, "last_input_tokens", None)
    if last:
        out["last_request_input_tokens"] = int(last)
    return out


class _ServerClearingLog:
    """Logs a server-side clearing pass once, not on every request after it.

    The server reports the edits it applied on each request, and once it has
    cleared a chunk it keeps clearing that same chunk, so the report repeats.
    """

    def __init__(self, provider: str, model: str) -> None:
        self.provider = provider
        self.model = model
        self._seen: tuple[int, int] = (0, 0)

    def observe(self, message: Any) -> None:
        cm = getattr(message, "context_management", None)
        edits = getattr(cm, "applied_edits", None) or []
        uses = sum(int(getattr(e, "cleared_tool_uses", 0) or 0) for e in edits)
        tokens = sum(int(getattr(e, "cleared_input_tokens", 0) or 0) for e in edits)
        if (uses, tokens) == self._seen or not uses:
            return
        self._seen = (uses, tokens)
        log_workflow_event(
            "in_turn_context_cleared",
            {
                "provider": self.provider,
                "model": self.model,
                "server_side": True,
                "results_cleared": uses,
                "freed_tokens": tokens,
            },
        )


def _collect_tool_blocks(
    message: Any,
    tool_calls: list["ToolCallRecord"],
    hosted_calls: list["ToolCallRecord"],
    *,
    provider: str,
    model: str,
) -> None:
    """Harvest tool_use / hosted-tool blocks from one model turn's message."""
    for block in getattr(message, "content", []) or []:
        btype = getattr(block, "type", "")
        if btype == "tool_use":
            tool_calls.append(
                ToolCallRecord(
                    name=getattr(block, "name", ""),
                    arguments=getattr(block, "input", {}) or {},
                    raw={"id": getattr(block, "id", None)},
                )
            )
        elif btype in {"server_tool_use", "web_search_tool_result", "web_fetch_tool_result"}:
            record = ToolCallRecord(
                name=getattr(block, "name", btype),
                arguments=getattr(block, "input", {}) or {},
                is_hosted=True,
                raw={"type": btype, "id": getattr(block, "id", None)},
            )
            hosted_calls.append(record)
            log_workflow_event(
                "hosted_tool_call",
                {
                    "provider": provider,
                    "model": model,
                    "tool_name": record.name,
                    "tool_id": getattr(block, "id", None),
                    "raw_type": btype,
                },
            )


def _is_hosted_tool(tool: Any) -> bool:
    if isinstance(tool, BetaBuiltinFunctionTool):
        return True
    if isinstance(tool, dict):
        t = tool.get("type", "")
        return t.startswith(("web_search_", "web_fetch_", "code_execution_", "computer_"))
    return False


def _needs_web_betas(tools: Iterable[Tool]) -> bool:
    for t in tools:
        if isinstance(t, dict):
            ty = t.get("type", "")
            if ty.startswith("web_search_") or ty.startswith("web_fetch_"):
                return True
    return False


def _extract_final_text(message: Any) -> str:
    parts: list[str] = []
    content = getattr(message, "content", None) or []
    for block in content:
        text = getattr(block, "text", None)
        if text:
            parts.append(text)
    return "\n".join(parts).strip()


def _serialize_messages(messages: Iterable[Message]) -> list[dict[str, Any]]:
    out: list[dict[str, Any]] = []
    for m in messages:
        try:
            out.append(dict(m))
        except Exception:
            out.append({"role": getattr(m, "role", "?"), "content": str(m)})
    return out


def _has_evidence_log(text: str) -> bool:
    return any(marker in text for marker in ("Evidence Verification Log", "证据核验", "在线证据核验"))


def _has_claim_check(text: str) -> bool:
    return any(marker in text for marker in ("Claim-Level Sanity Check", "逐项核验", "逐项主张核验"))


def _has_sources(text: str) -> bool:
    return any(marker in text for marker in ("Sources", "资料来源", "来源与核验说明"))


def _hosted_query(block: Any) -> tuple[Optional[str], Optional[str]]:
    payload = getattr(block, "input", None) or {}
    if not isinstance(payload, dict):
        return None, None
    return payload.get("query"), payload.get("url")


class AnthropicProvider:
    name = "anthropic"
    supports_hosted_web_search = True
    supports_file_search = False
    supports_structured_outputs = True

    def __init__(self, settings: Settings, *, fast: bool = False) -> None:
        if not settings.anthropic_api_key:
            raise RuntimeError("ANTHROPIC_API_KEY is not set")
        self.settings = settings
        # An explicit long timeout is required so the SDK does not refuse the
        # non-streaming tool_runner path when max_tokens is large (it guards
        # against >10-minute non-streaming requests). Without this, raising
        # max_tokens to 32000 breaks `agent.run()` / sub-agent execute() with
        # "Streaming is required for operations that may take longer than 10
        # minutes." The web UI uses the streaming path and is unaffected.
        self.client = Anthropic(
            api_key=settings.anthropic_api_key,
            timeout=httpx.Timeout(1800.0, connect=10.0),
        )
        self.model = settings.anthropic_fast_model if fast else settings.anthropic_model

    # ----- request shape -----------------------------------------------------

    def _effort(self) -> str:
        # One dial for both providers; it lives under its historical name.
        return getattr(self.settings, "openai_reasoning_effort", "none")

    def _request_kwargs(
        self,
        system: str,
        messages_list: list[Message],
        tools_list: list[Any],
        *,
        max_iterations: Optional[int],
        stream: bool,
    ) -> tuple[dict[str, Any], list[str]]:
        """Parameters for one tool loop; identical on the run and stream paths.

        `tool_runner` used to send no thinking or effort at all, so Sonnet 5
        sub-agents ran at its default `high`, a level above the dial.
        """
        betas: list[str] = []
        if _needs_web_betas(tools_list):
            betas.extend(WEB_BETAS)
        if needs_anthropic_files_beta(messages_list):
            betas.append(FILES_API_BETA)
        tools = _tools_with_cache_breakpoint(
            _instrument_local_tools(tools_list, provider=self.name, model=self.model)
        )
        system_param = _cached_system(system)
        limit = iteration_limit(max_iterations, self.settings)
        kwargs: dict[str, Any] = dict(
            max_tokens=self.settings.max_tokens,
            messages=messages_list,
            model=self.model,
            tools=tools,
            system=system_param,
            max_iterations=limit,
        )
        if stream:
            kwargs["stream"] = True
        # A second request happens only when the model calls a local tool.
        loops = bool(_runnable_tools(tools_list)) and limit != 1
        kwargs.update(_automatic_cache_kwarg(tools, system_param, messages_list, loops=loops))
        kwargs.update(_thinking_kwargs(self._effort()))
        ctx_mgmt = anthropic_context_management(self.settings)
        if ctx_mgmt is not None:
            kwargs["context_management"] = ctx_mgmt
            betas.append(CONTEXT_MANAGEMENT_BETA)
        if betas:
            kwargs["betas"] = betas
        return kwargs, betas

    def _start_runner(
        self, kwargs: dict[str, Any], tools_list: list[Any], in_turn: Optional[InTurnContext]
    ) -> Any:
        runner = self.client.beta.messages.tool_runner(**kwargs)
        trigger = clearing_trigger(kwargs.get("context_management"))
        _install_tool_execution(
            runner,
            _runnable_tools(tools_list),
            provider=self.name,
            model=self.model,
            notes=ClearingNotes(in_turn, trigger) if trigger else None,
        )
        return runner

    # ----- core run paths ---------------------------------------------------

    def run(
        self,
        system: str,
        messages: list[Message],
        tools: Iterable[Tool] = (),
        *,
        max_iterations: Optional[int] = None,
        structured_output: Optional[type] = None,
        verbosity: Optional[str] = None,
    ) -> RunResult:
        return self.tool_runner(
            system,
            messages,
            tools,
            max_iterations=max_iterations or self.settings.max_iterations,
            structured_output=structured_output,
            verbosity=verbosity,
        )

    def _forced_synthesis_after_ceiling(
        self,
        runner: Any,
        *,
        system: str,
        betas: list[str],
        usage: dict[str, Any],
    ) -> str:
        """One more call carrying the tool results, with no tools offered.

        The runner has already folded the last assistant turn and its
        tool_result message into its params, so the conversation is complete —
        all that is missing is a turn the model can only answer in.
        """
        try:
            params = getattr(runner, "_params", None) or {}
            history = list(params.get("messages") or [])
            if not history:
                return ""
            kwargs: dict[str, Any] = dict(
                max_tokens=self.settings.max_tokens,
                model=self.model,
                system=_cached_system(system),
                messages=[
                    *history,
                    {"role": "user", "content": CEILING_SYNTHESIS_NUDGE},
                ],
            )
            # Same thinking and effort as the loop: a change to either is a
            # cache miss on everything already sent.
            kwargs.update(_thinking_kwargs(self._effort()))
            if params.get("context_management"):
                kwargs["context_management"] = params["context_management"]
            if betas:
                kwargs["betas"] = betas
            resp = self.client.beta.messages.create(**kwargs)
            _merge_usage(usage, getattr(resp, "usage", None))
            return _extract_final_text(resp)
        except Exception as exc:  # noqa: BLE001 — recovery must not raise
            log_workflow_event(
                "tool_loop_ceiling_synthesis_failed",
                {"provider": self.name, "model": self.model, "error": str(exc)[:500]},
            )
            return ""

    def tool_runner(
        self,
        system: str,
        messages: list[Message],
        tools: Iterable[Tool],
        *,
        max_iterations: int,
        structured_output: Optional[type] = None,
        verbosity: Optional[str] = None,
    ) -> RunResult:
        # ``verbosity`` has no Anthropic parameter; the caller's instruction
        # sentence in the user turn carries it on this provider.
        tools_list = list(tools)
        messages_list = list(messages)
        kwargs, betas = self._request_kwargs(
            system, messages_list, tools_list, max_iterations=max_iterations, stream=False
        )
        if structured_output is not None and structured_output != JSON_OBJECT:
            kwargs["output_format"] = structured_output

        log_workflow_event(
            "provider_turn_started",
            {
                "provider": self.name,
                "model": self.model,
                "tools_offered": [
                    getattr(t, "name", t.get("name") if isinstance(t, dict) else str(t)) for t in tools_list
                ],
                "betas": betas,
                "reasoning_effort": kwargs["output_config"]["effort"],
            },
        )
        tool_calls: list[ToolCallRecord] = []
        hosted_calls: list[ToolCallRecord] = []
        usage: dict[str, Any] = {}
        in_turn = InTurnContext.for_settings(self.settings, provider=self.name, model=self.model)
        clearing = _ServerClearingLog(self.name, self.model)
        with TurnTimer() as t:
            runner = self._start_runner(kwargs, tools_list, in_turn)
            # Iterate per model turn: `until_done()` alone exposes only the
            # final message, which undercounts usage on multi-tool turns and
            # drops every intermediate turn's tool_use blocks.
            final: Any = None
            for message in runner:
                final = message
                _merge_usage(usage, getattr(message, "usage", None))
                if in_turn is not None:
                    in_turn.observe_anthropic(getattr(message, "usage", None))
                clearing.observe(message)
                _collect_tool_blocks(
                    message, tool_calls, hosted_calls, provider=self.name, model=self.model
                )
            if final is None:
                final = runner.until_done()
                _merge_usage(usage, getattr(final, "usage", None))
                _collect_tool_blocks(
                    final, tool_calls, hosted_calls, provider=self.name, model=self.model
                )

            ceiling_text = ""
            if _runner_hit_ceiling(final):
                log_workflow_event(
                    "tool_loop_ceiling_hit",
                    {
                        "provider": self.name,
                        "model": self.model,
                        "max_iterations": max_iterations,
                        "had_text": bool(_extract_final_text(final).strip()),
                        "stream": False,
                    },
                )
                if not _extract_final_text(final).strip():
                    ceiling_text = self._forced_synthesis_after_ceiling(
                        runner, system=system, betas=betas, usage=usage
                    )

        record_usage(self.name, self.model, usage, state_dir=getattr(self.settings, "state_dir", None))

        structured_payload: Optional[dict[str, Any]] = None
        # The SDK's parsed message exposes `parsed_output`; this read `.parsed`,
        # which no longer exists, so every Anthropic structured call — the
        # planner's included — came back with no payload.
        parsed = getattr(final, "parsed_output", None) or getattr(final, "parsed", None)
        if parsed is not None:
            try:
                structured_payload = parsed.model_dump() if hasattr(parsed, "model_dump") else dict(parsed)
            except Exception:
                structured_payload = None

        text = _extract_final_text(final) or ceiling_text
        if structured_payload is None and structured_output is not None and text:
            # Same fallback as the OpenAI path: the schema-constrained text.
            try:
                loaded = json.loads(text)
                structured_payload = loaded if isinstance(loaded, dict) else None
            except ValueError:
                structured_payload = None
        message_id = getattr(final, "id", None)
        stop_reason = getattr(final, "stop_reason", None)

        log_turn(
            {
                "provider": self.name,
                "model": self.model,
                "system": system,
                "messages_in": _serialize_messages(messages),
                "tools_offered": [
                    getattr(t, "name", t.get("name") if isinstance(t, dict) else str(t)) for t in tools_list
                ],
                "response_content": text,
                "tool_calls": [c.__dict__ for c in tool_calls],
                "hosted_tool_calls": [c.__dict__ for c in hosted_calls],
                "structured_payload": structured_payload,
                "usage": usage,
                "latency_ms": t.latency_ms,
                "anthropic_message_id": message_id,
                "stop_reason": stop_reason,
                "provider_capabilities": {
                    "hosted_web_search": self.supports_hosted_web_search,
                    "file_search": self.supports_file_search,
                    "structured_outputs": self.supports_structured_outputs,
                },
            }
        )
        log_workflow_event(
            "provider_turn_finished",
            {
                "provider": self.name,
                "model": self.model,
                "response_chars": len(text),
                "local_tool_call_count": len(tool_calls),
                "hosted_tool_call_count": len(hosted_calls),
                "has_evidence_verification_log": _has_evidence_log(text),
                "has_claim_level_sanity_check": _has_claim_check(text),
                "has_sources": _has_sources(text),
                "latency_ms": t.latency_ms,
                "stop_reason": stop_reason,
            },
        )

        return RunResult(
            text=text,
            provider=self.name,
            model=self.model,
            usage=_with_last_request(usage, in_turn),
            tool_calls=tool_calls,
            hosted_tool_calls=hosted_calls,
            structured_payload=structured_payload,
            message_id=message_id,
            stop_reason=stop_reason,
        )

    # ----- streaming --------------------------------------------------------

    def stream(
        self,
        system: str,
        messages: list[Message],
        tools: Iterable[Tool] = (),
        *,
        max_iterations: Optional[int] = None,
        verbosity: Optional[str] = None,
    ) -> Iterator[StreamEvent]:
        # ``verbosity``: see tool_runner — accepted for parity, no API field.
        tools_list = list(tools)
        messages_list = list(messages)
        kwargs, betas = self._request_kwargs(
            system, messages_list, tools_list, max_iterations=max_iterations, stream=True
        )

        log_workflow_event(
            "provider_turn_started",
            {
                "provider": self.name,
                "model": self.model,
                "tools_offered": [
                    getattr(t, "name", t.get("name") if isinstance(t, dict) else str(t)) for t in tools_list
                ],
                "betas": betas,
                "reasoning_effort": kwargs["output_config"]["effort"],
                "stream": True,
            },
        )
        in_turn = InTurnContext.for_settings(self.settings, provider=self.name, model=self.model)
        runner = self._start_runner(kwargs, tools_list, in_turn)
        clearing = _ServerClearingLog(self.name, self.model)
        final_text_chunks: list[str] = []
        tool_calls: list[ToolCallRecord] = []
        hosted_calls: list[ToolCallRecord] = []
        usage: dict[str, Any] = {}
        with TurnTimer() as t:
            # Anthropic's streaming tool runner yields a BetaMessageStream for each
            # model turn. The stream itself yields content_block_* events.
            for stream in runner:
                progress = ToolProgressThrottle()
                for ev in stream:
                    ev_type = getattr(ev, "type", None) or (ev.get("type") if isinstance(ev, dict) else None)
                    delta = getattr(ev, "delta", None)
                    if delta is not None and getattr(delta, "type", "") == "text_delta":
                        chunk = getattr(delta, "text", "")
                        if chunk:
                            final_text_chunks.append(chunk)
                            yield StreamEvent("delta", {"text": chunk})
                        continue
                    if delta is not None and getattr(delta, "type", "") == "thinking_delta":
                        tchunk = getattr(delta, "thinking", "")
                        if tchunk:
                            yield StreamEvent("reasoning_delta", {"text": tchunk})
                        continue
                    if delta is not None and getattr(delta, "type", "") == "input_json_delta":
                        # A tool call's arguments still being written — for a
                        # long document that is most of the wait before it.
                        pev = progress.update(
                            getattr(ev, "index", None),
                            added=len(getattr(delta, "partial_json", "") or ""),
                        )
                        if pev is not None:
                            yield pev
                        continue
                    # signature_delta (thinking block signatures) carry no user text.

                    if ev_type == "content_block_start":
                        block = getattr(ev, "content_block", None)
                        if getattr(block, "type", "") == "tool_use":
                            progress.open(getattr(ev, "index", None), getattr(block, "name", ""), getattr(block, "id", None))
                        continue

                    # Emit tool_call/hosted_tool_call at content_block_STOP, not
                    # START: for tool_use blocks the `input` object is streamed
                    # incrementally via input_json_delta AFTER the block starts,
                    # so `block.input` is empty {} at start. The SDK assembles the
                    # full input onto the snapshot by content_block_stop, which
                    # carries the completed `content_block`. Reading it at start
                    # (as we did) shipped empty `arguments`, which silently
                    # stripped every source-chip query keyword on Anthropic (the
                    # OpenAI adapter emits assembled args, so this was a real
                    # provider-parity gap: no CoT chips on the Claude path).
                    if ev_type == "content_block_stop":
                        progress.close(getattr(ev, "index", None))
                        block = getattr(ev, "content_block", None)
                        btype = getattr(block, "type", "")
                        if btype == "tool_use":
                            record = ToolCallRecord(
                                name=getattr(block, "name", ""),
                                arguments=getattr(block, "input", {}) or {},
                                raw={"id": getattr(block, "id", None)},
                            )
                            tool_calls.append(record)
                            yield StreamEvent(
                                "tool_call",
                                {
                                    "name": record.name,
                                    "arguments": record.arguments,
                                    "agent": agent_name_var.get(),
                                },
                            )
                        elif btype in {"server_tool_use", "web_search_tool_result", "web_fetch_tool_result"}:
                            record = ToolCallRecord(
                                name=getattr(block, "name", btype),
                                arguments=getattr(block, "input", {}) or {},
                                is_hosted=True,
                                raw={"type": btype, "id": getattr(block, "id", None)},
                            )
                            hosted_calls.append(record)
                            event_data = {
                                "provider": self.name,
                                "model": self.model,
                                "tool_name": record.name,
                                "tool_id": getattr(block, "id", None),
                                "raw_type": btype,
                                "arguments": record.arguments,
                            }
                            log_workflow_event("hosted_tool_call", event_data)
                            # Progress first, so a client that ignores the
                            # raw hosted_tool_call still sees the search start
                            # (query known once the block's input is complete)
                            # and finish (its result block has landed).
                            if btype == "server_tool_use":
                                query, url = _hosted_query(block)
                                yield hosted_progress_event(
                                    provider=self.name,
                                    model=self.model,
                                    tool_name=record.name,
                                    tool_id=getattr(block, "id", None),
                                    status="searching",
                                    query=query,
                                    url=url,
                                )
                            else:
                                yield hosted_progress_event(
                                    provider=self.name,
                                    model=self.model,
                                    tool_name=btype.removesuffix("_tool_result"),
                                    tool_id=getattr(block, "tool_use_id", None),
                                    status="completed",
                                )
                            yield StreamEvent("hosted_tool_call", event_data)

                # Per-turn usage: harvest each turn's finished message so
                # multi-tool turns are not undercounted by the final-message-
                # only usage read below.
                get_final = getattr(stream, "get_final_message", None)
                if callable(get_final):
                    try:
                        turn_message = get_final()
                        turn_usage = getattr(turn_message, "usage", None)
                        _merge_usage(usage, turn_usage)
                        if in_turn is not None:
                            in_turn.observe_anthropic(turn_usage)
                        clearing.observe(turn_message)
                    except Exception:
                        pass

        final = runner.until_done()
        text = _extract_final_text(final) or "".join(final_text_chunks)
        if _runner_hit_ceiling(final):
            log_workflow_event(
                "tool_loop_ceiling_hit",
                {
                    "provider": self.name,
                    "model": self.model,
                    "max_iterations": iteration_limit(max_iterations, self.settings),
                    "had_text": bool(text.strip()),
                    "stream": True,
                },
            )
            if not text.strip():
                # The loop ran out of iterations before the model wrote prose.
                # Spend one more call with no tools rather than shipping an
                # empty assistant message.
                text = self._forced_synthesis_after_ceiling(
                    runner, system=system, betas=betas, usage=usage
                )
                if text:
                    final_text_chunks.append(text)
                    yield StreamEvent("delta", {"text": text})
        if text and not final_text_chunks:
            # Defensive fallback: if the SDK produces a final message without
            # exposing text deltas, still show the answer in the chat stream.
            final_text_chunks.append(text)
            yield StreamEvent("delta", {"text": text})
        stop_reason = getattr(final, "stop_reason", None)
        if stop_reason == "max_tokens":
            # The answer was cut off at the max_tokens ceiling. Make the
            # truncation visible instead of silently shipping a partial memo —
            # raise ``max_tokens`` in config.yaml for long legal analysis.
            notice = (
                "\n\n> ⚠️ **Output truncated** at the model's `max_tokens` limit "
                f"({self.settings.max_tokens} tokens). Increase `max_tokens` in "
                "config.yaml (Opus 4.x supports up to 128000 with streaming) to "
                "get the complete analysis."
            )
            text += notice
            yield StreamEvent("delta", {"text": notice})
        if not usage:
            # Fallback for SDK shapes without per-stream get_final_message.
            _merge_usage(usage, getattr(final, "usage", None))
        record_usage(self.name, self.model, usage, state_dir=getattr(self.settings, "state_dir", None))
        log_turn(
            {
                "provider": self.name,
                "model": self.model,
                "system": system,
                "messages_in": _serialize_messages(messages),
                "tools_offered": [
                    getattr(t, "name", t.get("name") if isinstance(t, dict) else str(t)) for t in tools_list
                ],
                "response_content": text,
                "tool_calls": [c.__dict__ for c in tool_calls],
                "hosted_tool_calls": [c.__dict__ for c in hosted_calls],
                "usage": usage,
                "latency_ms": t.latency_ms,
                "anthropic_message_id": getattr(final, "id", None),
                "stop_reason": stop_reason,
                "stream": True,
            }
        )
        log_workflow_event(
            "provider_turn_finished",
            {
                "provider": self.name,
                "model": self.model,
                "response_chars": len(text),
                "latency_ms": t.latency_ms,
                "stream": True,
                "has_evidence_verification_log": _has_evidence_log(text),
                "has_claim_level_sanity_check": _has_claim_check(text),
                "has_sources": _has_sources(text),
                "stop_reason": stop_reason,
            },
        )
        yield StreamEvent("done", {"text": text, "usage": _with_last_request(usage, in_turn), "stop_reason": stop_reason})


def aviation_hosted_search_tools(settings: Settings) -> list[dict[str, Any]]:
    """Hosted web tools used by sub-agents for FAA/EASA/ICAO lookups."""
    out: list[dict[str, Any]] = []
    if settings.enable_web_search:
        out.append(WEB_SEARCH_TOOL)
    if settings.enable_web_fetch:
        out.append(WEB_FETCH_TOOL)
    return out
