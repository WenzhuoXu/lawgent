"""Anthropic provider — Claude via `client.beta.messages.tool_runner`."""

from __future__ import annotations

import copy
import os
from typing import Any, Iterable, Iterator, Optional

import httpx
from anthropic import Anthropic
from anthropic.lib.tools._beta_functions import (
    BetaBuiltinFunctionTool,
    BetaFunctionTool,
)

from ..attachments import needs_anthropic_files_beta
from ..config import Settings
from ..logging_setup import TurnTimer, agent_name_var, log_turn, log_workflow_event
from ..tool_budget import apply_result_budget, budget_log_fields, turn_result_budget
from ..tools.multimodal import split_inline_images, to_anthropic_tool_content
from ..usage import record_usage
from .base import Message, RunResult, StreamEvent, Tool, ToolCallRecord


# Extended-thinking token budgets keyed by the shared reasoning-effort dial.
# "none" disables thinking entirely.
THINKING_BUDGETS: dict[str, int] = {
    "none": 0,
    "low": 2048,
    "medium": 8192,
    "high": 16384,
    "xhigh": 32768,
}


def _thinking_kwarg(effort: str, max_tokens: int) -> Optional[dict[str, Any]]:
    """Build the Anthropic ``thinking`` param from the reasoning-effort dial.

    Returns None when disabled. Enforces ``budget < max_tokens`` (the API
    requires headroom for the answer after thinking).
    """
    budget = THINKING_BUDGETS.get((effort or "none").lower(), 0)
    if budget <= 0:
        return None
    budget = min(budget, max(1024, max_tokens - 1024))
    return {"type": "enabled", "budget_tokens": budget}


WEB_SEARCH_TOOL = {"type": "web_search_20250305", "name": "web_search"}
WEB_FETCH_TOOL = {"type": "web_fetch_20250910", "name": "web_fetch"}
WEB_BETAS = ["web-search-2025-03-05", "web-fetch-2025-09-10"]
FILES_API_BETA = "files-api-2025-04-14"
CACHE_EPHEMERAL: dict[str, str] = {"type": "ephemeral"}

# Server-side context management. Clearing old tool results is measured at a
# ~50% peak-token reduction on long research runs, but it INVALIDATES the
# cached prompt prefix every time it fires — and this provider relies on
# cache_control breakpoints for both the system block and the tool list. On a
# cache-heavy legal workload that trade can go either way, so it stays opt-in
# until the eval harness can measure it. Memory tool results are excluded
# because they are meant to persist and be re-read deliberately.
CONTEXT_MANAGEMENT_BETA = "context-management-2025-06-27"
_CLEAR_TOOL_USES_TYPE = "clear_tool_uses_20250919"
_CLEAR_TRIGGER_TOKENS = 120_000
_CLEAR_KEEP_TOOL_USES = 6


def _context_management_kwarg() -> Optional[dict[str, Any]]:
    """``context_management`` param when LEGAL_HELPER_ANTHROPIC_CLEAR_TOOLS is on.

    Set the env var to a token count to override the trigger threshold, or to
    ``1``/``true`` for the default. Unset (the default) → no context management,
    and the prompt cache stays intact.
    """
    raw = (os.getenv("LEGAL_HELPER_ANTHROPIC_CLEAR_TOOLS") or "").strip().lower()
    if not raw or raw in {"0", "false", "no", "off"}:
        return None
    trigger = _CLEAR_TRIGGER_TOKENS
    if raw.isdigit():
        trigger = max(int(raw), 1_000)
    return {
        "edits": [
            {
                "type": _CLEAR_TOOL_USES_TYPE,
                "trigger": {"type": "input_tokens", "value": trigger},
                "keep": {"type": "tool_uses", "value": _CLEAR_KEEP_TOOL_USES},
                "exclude_tools": ["memory"],
            }
        ]
    }


def _cached_system(system: str) -> Any:
    """System prompt as a text block carrying a cache_control breakpoint.

    Prompt caching is a prefix match and the prefix renders tools -> system ->
    messages, so this single breakpoint caches the stable tool schemas + system
    prompt together; volatile per-turn content stays after it in ``messages``.
    Cached reads bill ~0.1x the input rate, so every tool-runner iteration and
    follow-up chat turn stops re-billing the large legal system prompt in full.
    """
    if not system:
        return system
    return [{"type": "text", "text": system, "cache_control": dict(CACHE_EPHEMERAL)}]


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
            out[-1] = {**last, "cache_control": dict(CACHE_EPHEMERAL)}
    elif isinstance(last, BetaFunctionTool) and getattr(last, "_cache_control", None) is None:
        annotated = copy.copy(last)
        annotated._cache_control = dict(CACHE_EPHEMERAL)
        out[-1] = annotated
    return out


def _instrument_local_tools(tools: list[Any], *, provider: str, model: str) -> list[Any]:
    """Wrap local ``BetaFunctionTool`` objects so each execution emits
    ``local_tool_call_started`` / ``local_tool_call_finished`` log events with
    the SAME shape OpenAI already emits (openai_provider.py:485-512).

    The Anthropic SDK ``tool_runner`` auto-executes local tools inside its
    stream loop, so we cannot observe results at the provider layer without
    intercepting ``call``. We ``copy.copy`` each tool (preserving type for any
    ``isinstance`` checks in the runner) and override the bound ``call`` to
    wrap execution. Hosted/dict tool defs pass through untouched.
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


# Parity with the OpenAI provider: when the tool loop is cut off by
# `max_iterations` the model has the tool results but never got a turn in
# which tools were unavailable, so the answer can come back empty.
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
    """Accumulate one model turn's usage counters into a running total."""
    if u is None:
        return
    for k in ("input_tokens", "output_tokens", "cache_creation_input_tokens", "cache_read_input_tokens"):
        v = getattr(u, k, None)
        if v:
            total[k] = total.get(k, 0) + v


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

    # ----- core run paths ---------------------------------------------------

    def run(
        self,
        system: str,
        messages: list[Message],
        tools: Iterable[Tool] = (),
        *,
        max_iterations: Optional[int] = None,
        structured_output: Optional[type] = None,
    ) -> RunResult:
        return self.tool_runner(
            system,
            messages,
            tools,
            max_iterations=max_iterations or self.settings.max_iterations,
            structured_output=structured_output,
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
    ) -> RunResult:
        tools_list = list(tools)
        messages_list = list(messages)
        betas: list[str] = []
        if _needs_web_betas(tools_list):
            betas.extend(WEB_BETAS)
        if needs_anthropic_files_beta(messages_list):
            betas.append(FILES_API_BETA)

        kwargs: dict[str, Any] = dict(
            max_tokens=self.settings.max_tokens,
            messages=messages_list,
            model=self.model,
            tools=_tools_with_cache_breakpoint(
                _instrument_local_tools(tools_list, provider=self.name, model=self.model)
            ),
            system=_cached_system(system),
            max_iterations=max_iterations,
        )
        ctx_mgmt = _context_management_kwarg()
        if ctx_mgmt is not None:
            kwargs["context_management"] = ctx_mgmt
            if CONTEXT_MANAGEMENT_BETA not in betas:
                betas.append(CONTEXT_MANAGEMENT_BETA)
        if betas:
            kwargs["betas"] = betas
        if structured_output is not None:
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
            },
        )
        tool_calls: list[ToolCallRecord] = []
        hosted_calls: list[ToolCallRecord] = []
        usage: dict[str, Any] = {}
        with TurnTimer() as t, turn_result_budget(self.settings):
            runner = self.client.beta.messages.tool_runner(**kwargs)
            # Iterate per model turn: `until_done()` alone exposes only the
            # final message, which undercounts usage on multi-tool turns and
            # drops every intermediate turn's tool_use blocks.
            final: Any = None
            for message in runner:
                final = message
                _merge_usage(usage, getattr(message, "usage", None))
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
        parsed = getattr(final, "parsed", None)
        if parsed is not None:
            try:
                structured_payload = parsed.model_dump() if hasattr(parsed, "model_dump") else dict(parsed)
            except Exception:
                structured_payload = None

        text = _extract_final_text(final) or ceiling_text
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
            },
        )

        return RunResult(
            text=text,
            provider=self.name,
            model=self.model,
            usage=usage,
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
    ) -> Iterator[StreamEvent]:
        tools_list = list(tools)
        messages_list = list(messages)
        betas: list[str] = []
        if _needs_web_betas(tools_list):
            betas.extend(WEB_BETAS)
        if needs_anthropic_files_beta(messages_list):
            betas.append(FILES_API_BETA)

        kwargs: dict[str, Any] = dict(
            max_tokens=self.settings.max_tokens,
            messages=messages_list,
            model=self.model,
            tools=_tools_with_cache_breakpoint(
                _instrument_local_tools(tools_list, provider=self.name, model=self.model)
            ),
            system=_cached_system(system),
            max_iterations=max_iterations or self.settings.max_iterations,
            stream=True,
        )
        ctx_mgmt = _context_management_kwarg()
        if ctx_mgmt is not None:
            kwargs["context_management"] = ctx_mgmt
            if CONTEXT_MANAGEMENT_BETA not in betas:
                betas.append(CONTEXT_MANAGEMENT_BETA)
        if betas:
            kwargs["betas"] = betas
        thinking = _thinking_kwarg(
            getattr(self.settings, "openai_reasoning_effort", "none"), self.settings.max_tokens
        )
        if thinking is not None:
            kwargs["thinking"] = thinking

        log_workflow_event(
            "provider_turn_started",
            {
                "provider": self.name,
                "model": self.model,
                "tools_offered": [
                    getattr(t, "name", t.get("name") if isinstance(t, dict) else str(t)) for t in tools_list
                ],
                "betas": betas,
                "stream": True,
            },
        )
        runner = self.client.beta.messages.tool_runner(**kwargs)
        final_text_chunks: list[str] = []
        tool_calls: list[ToolCallRecord] = []
        hosted_calls: list[ToolCallRecord] = []
        usage: dict[str, Any] = {}
        with TurnTimer() as t, turn_result_budget(self.settings):
            # Anthropic's streaming tool runner yields a BetaMessageStream for each
            # model turn. The stream itself yields content_block_* events.
            for stream in runner:
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
                    # signature_delta (thinking block signatures) carry no user text.

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
                            yield StreamEvent("hosted_tool_call", event_data)

                # Per-turn usage: harvest each turn's finished message so
                # multi-tool turns are not undercounted by the final-message-
                # only usage read below.
                get_final = getattr(stream, "get_final_message", None)
                if callable(get_final):
                    try:
                        _merge_usage(usage, getattr(get_final(), "usage", None))
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
                    "max_iterations": max_iterations or self.settings.max_iterations,
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
            },
        )
        yield StreamEvent("done", {"text": text, "usage": usage, "stop_reason": stop_reason})


def aviation_hosted_search_tools(settings: Settings) -> list[dict[str, Any]]:
    """Hosted web tools used by sub-agents for FAA/EASA/ICAO lookups."""
    out: list[dict[str, Any]] = []
    if settings.enable_web_search:
        out.append(WEB_SEARCH_TOOL)
    if settings.enable_web_fetch:
        out.append(WEB_FETCH_TOOL)
    return out
