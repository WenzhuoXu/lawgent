"""OpenAI provider — Responses API runtime."""

from __future__ import annotations

import json
import time
from typing import Any, Iterable, Iterator, Optional

from openai import OpenAI

from ..config import Settings
from ..context import estimate_tokens
from ..logging_setup import TurnTimer, agent_name_var, log_turn, log_workflow_event, tool_call_id_var
from ..tool_exposure import openai_tools as openai_tool_surface
from ..tool_budget import apply_result_budget, budget_log_fields
from ..turn_compaction import InTurnContext, iteration_limit
from ..tools.multimodal import split_inline_images, to_openai_tool_content
from ..usage import OPENAI_LONG_CONTEXT_THRESHOLD, record_usage
from .base import JSON_OBJECT, VERBOSITY_LEVELS, Message, RunResult, StreamEvent, Tool, ToolCallRecord
from .progress import ToolProgressThrottle, hosted_progress_event
from .tool_exec import run_tool_calls


# Sent when the tool loop is cut off by a positive `max_iterations` (the
# defaults are unlimited; a caller may still set one). The tool results are
# already in the conversation, so the model has everything it needs; what it
# lacks is a turn in which tools are not an option. Without this the turn
# shipped an empty assistant message — 9 of them in a single month, every one
# at iterations == parent_max_iterations.
CEILING_SYNTHESIS_NUDGE = (
    "You have reached this turn's tool-call limit, so no further tools are "
    "available. Using only the information already gathered above, write the "
    "complete final answer for the user now. Where something could not be "
    "verified with the tools you had, say so explicitly instead of omitting it."
)

# --- per-model request capabilities ------------------------------------------
# Measured with live calls on 2026-10-07; a 400 names the values a model takes.
# A model missing from a table gets the conservative behaviour.

# Reasoning effort each model accepts. gpt-6.1-sol rejects "none" and
# "minimal"; gpt-6-luna takes "none" but not "minimal". Omitting the parameter
# runs either model at "medium", so "none" is sent, never omitted, where the
# model takes it — omitting it made every "none" luna call reason at medium.
_EFFORT_ORDER = ("none", "minimal", "low", "medium", "high", "xhigh", "max")
OPENAI_REASONING_EFFORTS: dict[str, tuple[str, ...]] = {
    "gpt-6.1-sol": ("low", "medium", "high", "xhigh", "max"),
    "gpt-6-luna": ("none", "low", "medium", "high", "xhigh", "max"),
}

# Models that accept `text.verbosity` (low | medium | high), alone and next to a
# strict json_schema `text.format`.
OPENAI_VERBOSITY_MODELS = frozenset({"gpt-6.1-sol", "gpt-6-luna"})

# Models that accept an explicit `prompt_cache_breakpoint` on a content block.
# The breakpoint is added in the default (implicit) mode, where it takes one of
# the four cache-write slots and the automatic end-of-input breakpoint still
# applies — `prompt_cache_options.mode: "explicit"` would switch that one off
# and with it the in-turn caching the tool loop relies on.
OPENAI_EXPLICIT_CACHE_MODELS = frozenset({"gpt-6.1-sol", "gpt-6-luna"})
# Shortest prefix the API will cache. Below it a breakpoint writes nothing, so
# the system text stays in `instructions`.
OPENAI_MIN_CACHEABLE_TOKENS = 1_024
_CACHE_BREAKPOINT = {"mode": "explicit"}


def reasoning_effort_for(model: str, effort: Optional[str]) -> Optional[str]:
    """The `reasoning.effort` to send for ``effort`` on ``model``; None to omit.

    A level the model rejects becomes the nearest level it accepts (ties go
    to the cheaper side), so a caller asking for "none" on gpt-6.1-sol gets
    "low" rather than a 400 or the model's "medium" default.
    """
    level = (effort or "").strip().lower()
    if not level:
        return None
    accepted = OPENAI_REASONING_EFFORTS.get(model)
    if accepted is None:
        return None if level in {"none", "minimal"} else level
    if level in accepted:
        return level
    if level not in _EFFORT_ORDER:
        return None
    rank = _EFFORT_ORDER.index(level)
    return min(accepted, key=lambda v: (abs(_EFFORT_ORDER.index(v) - rank), _EFFORT_ORDER.index(v)))


def verbosity_for(model: str, verbosity: Optional[str]) -> Optional[str]:
    level = (verbosity or "").strip().lower()
    if level in VERBOSITY_LEVELS and model in OPENAI_VERBOSITY_MODELS:
        return level
    return None


def _stable_prefix_tokens(system: str, openai_tools: list[dict[str, Any]]) -> int:
    """Estimated size of what precedes the conversation: tool schemas + system."""
    tools_text = json.dumps(openai_tools, ensure_ascii=False, sort_keys=True) if openai_tools else ""
    return estimate_tokens(system) + estimate_tokens(tools_text)


def _developer_prefix(system: str) -> dict[str, Any]:
    """The system text as a developer message carrying a cache breakpoint.

    `instructions` cannot carry an explicit breakpoint, and a one-shot call
    (summary, title) never reaches the automatic one: a one-shot luna call read
    0.7% of its input from cache in September. A breakpoint after the stable
    text makes it a read on the next call with the same prefix.
    """
    return {
        "role": "developer",
        "content": [{"type": "input_text", "text": system, "prompt_cache_breakpoint": dict(_CACHE_BREAKPOINT)}],
    }


def _accumulate_usage(usage_total: dict[str, Any], u: Any) -> None:
    """Fold one Responses-API usage object into a running total.

    Captures the cached-input subset (``input_tokens_details.cached_tokens``)
    alongside the headline counts so the cost center can price it at the
    discounted cached rate, and the cache-write count the API reports
    (``cache_write_tokens``, also a subset of input) as
    ``cache_write_input_tokens``, which the cost center prices at the
    model's cache-write rate.
    """
    if u is None:
        return
    for k in ("input_tokens", "output_tokens", "total_tokens"):
        v = getattr(u, k, None)
        if v is not None:
            usage_total[k] = usage_total.get(k, 0) + v
    details = getattr(u, "input_tokens_details", None)
    cached = getattr(details, "cached_tokens", None) if details is not None else None
    if cached:
        usage_total["cached_input_tokens"] = usage_total.get("cached_input_tokens", 0) + cached
    written = getattr(details, "cache_write_tokens", None) if details is not None else None
    if written:
        usage_total["cache_write_input_tokens"] = usage_total.get("cache_write_input_tokens", 0) + written
    # The long-context surcharge is decided per request, while the ledger
    # records the turn's sum; keep the over-threshold requests' share apart.
    usage_total["requests"] = usage_total.get("requests", 0) + 1
    request_input = getattr(u, "input_tokens", None) or 0
    if request_input > OPENAI_LONG_CONTEXT_THRESHOLD:
        lc = usage_total.setdefault("long_context", {})
        for k, v in (
            ("requests", 1),
            ("input_tokens", request_input),
            ("output_tokens", getattr(u, "output_tokens", None) or 0),
            ("cached_input_tokens", cached or 0),
            ("cache_write_input_tokens", written or 0),
        ):
            lc[k] = lc.get(k, 0) + v


def _with_last_request(usage_total: dict[str, Any], last_input: Optional[int]) -> dict[str, Any]:
    """Usage for callers: the turn totals plus the last request's input size.

    ``input_tokens`` is summed over every request of the tool loop — right for
    billing, wrong for "how full is the context". A 30-round turn summed to
    3.64M on a 1.05M-window model, and the compaction decision read that sum.
    """
    out = dict(usage_total)
    if last_input:
        out["last_request_input_tokens"] = int(last_input)
    return out


def _output_refs(resp: Any) -> list[dict[str, Any]]:
    """A response's output items as references into the stored conversation.

    Used only when an in-turn clearing pass re-sends the history instead of
    chaining on ``previous_response_id``: referencing by id keeps reasoning
    items intact without re-serialising them.
    """
    refs: list[dict[str, Any]] = []
    for item in getattr(resp, "output", None) or []:
        item_id = getattr(item, "id", None)
        if item_id:
            refs.append({"type": "item_reference", "id": item_id})
        elif hasattr(item, "model_dump"):
            refs.append(item.model_dump(exclude_none=True))
    return refs


def _tool_to_openai(tool: Any) -> dict[str, Any]:
    """Normalize a tool to the OpenAI Responses tool schema."""
    if isinstance(tool, dict):
        # Pass through hosted tools or already-shaped tool dicts unchanged.
        return tool
    # Anthropic BetaFunctionTool → OpenAI function tool
    name = getattr(tool, "name", None)
    desc = getattr(tool, "description", "")
    schema = getattr(tool, "input_schema", None)
    if schema is None and hasattr(tool, "to_dict"):
        td = tool.to_dict()
        name = name or td.get("name")
        desc = desc or td.get("description", "")
        schema = td.get("input_schema")
    out = {
        "type": "function",
        "name": name,
        "description": desc or "",
        "parameters": schema or {"type": "object", "properties": {}},
    }
    if getattr(tool, "_defer_loading", None):
        out["defer_loading"] = True
    return out


def _parse_args(args: str) -> dict[str, Any]:
    """Function-call arguments as a dict; {} for empty, non-object or cut-off JSON.

    A response stopped at the output ceiling can end mid-argument, and
    `json.loads` on that used to raise out of the whole tool loop.
    """
    text = (args or "").strip()
    if not text.startswith("{"):
        return {}
    try:
        parsed = json.loads(text)
    except json.JSONDecodeError:
        return {}
    return parsed if isinstance(parsed, dict) else {}


def _call_local_tool(tools_by_name: dict[str, Any], name: str, args_json: str) -> str:
    tool = tools_by_name.get(name)
    if tool is None:
        return json.dumps({"error": f"Unknown tool: {name}"})
    try:
        args = json.loads(args_json) if args_json else {}
    except json.JSONDecodeError:
        args = {}
    try:
        if hasattr(tool, "call"):
            result = tool.call(args)
        else:
            result = tool(**args)
    except Exception as e:  # tool surfaced an error: feed it back to the model
        return json.dumps({"error": str(e)})
    if isinstance(result, str):
        return result
    try:
        return json.dumps(result, default=str)
    except Exception:
        return str(result)


def _stringify_input_messages(messages: Iterable[Message]) -> list[dict[str, Any]]:
    """Convert internal messages to OpenAI Responses `input` items.

    Plain string content is passed through unchanged. Structured content
    (list of blocks) is passed through too — the attachment ingestion
    layer already shapes blocks as Responses `input_text` / `input_file`
    / `input_image` parts.
    """
    out: list[dict[str, Any]] = []
    for m in messages:
        role = m.get("role", "user")
        content = m.get("content", "")
        if isinstance(content, list):
            out.append({"role": role, "content": list(content)})
        else:
            out.append({"role": role, "content": content})
    return out


def _extract_text_from_response(resp: Any) -> str:
    text = getattr(resp, "output_text", None)
    if text:
        return text
    pieces: list[str] = []
    for item in getattr(resp, "output", []) or []:
        if getattr(item, "type", None) == "message":
            for c in getattr(item, "content", []) or []:
                t = getattr(c, "text", None)
                if t:
                    pieces.append(t)
    return "\n".join(pieces).strip()


def _has_sources(text: str) -> bool:
    return any(marker in text for marker in ("Sources", "资料来源", "来源与核验说明"))


def _is_max_output_truncation(resp: Any) -> bool:
    """True when a Responses-API response stopped at the output-token ceiling.

    Mirrors the Anthropic ``stop_reason == "max_tokens"`` check so both providers
    surface the same truncation signal for long legal analyses.
    """
    if resp is None:
        return False
    if getattr(resp, "status", None) != "incomplete":
        return False
    details = getattr(resp, "incomplete_details", None)
    reason = getattr(details, "reason", None) if details is not None else None
    return reason == "max_output_tokens"


def _stop_reason(resp: Any, *, truncated: bool) -> str:
    """Normalized stop reason, in the Anthropic vocabulary where one exists.

    ``truncated`` is whether any request of the loop hit the output ceiling —
    the condition the stream path already reports, so both paths agree.
    """
    if truncated or _is_max_output_truncation(resp):
        return "max_tokens"
    if resp is not None and getattr(resp, "status", None) == "incomplete":
        details = getattr(resp, "incomplete_details", None)
        return str(getattr(details, "reason", None) or "incomplete")
    return "stop"


_HOSTED_CALL_TYPES = {"web_search_call", "file_search_call", "code_interpreter_call", "tool_search_call"}
_HOSTED_PROGRESS_NAMES = {"web_search_call": "web_search", "file_search_call": "file_search"}


def _hosted_arguments(item: Any) -> dict[str, Any]:
    """What a hosted call searched for, so a source chip can name it.

    The Anthropic path already carries the query on `hosted_tool_call`; the
    OpenAI path shipped `{}` and its chips had nothing to show.
    """
    arguments = getattr(item, "arguments", None)  # tool_search_call
    if isinstance(arguments, dict) and arguments.get("query"):
        return {"query": str(arguments["query"])}
    action = getattr(item, "action", None)
    if action is not None:
        query = getattr(action, "query", None)
        queries = getattr(action, "queries", None)
        if not query and queries:
            query = queries[0]
        url = getattr(action, "url", None)
        out = {k: v for k, v in (("query", query), ("url", url)) if v}
        if out:
            return out
    queries = getattr(item, "queries", None)  # file_search_call
    if queries:
        return {"query": queries[0]}
    return {}


class OpenAIProvider:
    name = "openai"
    supports_hosted_web_search = True
    supports_file_search = True
    supports_structured_outputs = True

    def __init__(self, settings: Settings, *, fast: bool = False) -> None:
        if not settings.openai_api_key:
            raise RuntimeError("OPENAI_API_KEY is not set")
        self.settings = settings
        # Generous timeout (parity with the Anthropic provider) so a long
        # non-streaming generation at the raised max_tokens isn't cut off by
        # the SDK's default ~10-minute timeout.
        self.client = OpenAI(api_key=settings.openai_api_key, timeout=1800.0)
        self.model = settings.openai_fast_model if fast else settings.openai_model
        self.reasoning_effort = settings.openai_reasoning_effort

    # ----- request shape -----------------------------------------------------

    def _base_request(
        self,
        system: str,
        openai_tools: list[dict[str, Any]],
        *,
        verbosity: Optional[str],
        structured_output: Optional[type] = None,
        reasoning_summary: bool = False,
    ) -> tuple[dict[str, Any], list[dict[str, Any]]]:
        """Per-loop request parameters, plus input items that open the conversation.

        A system prompt large enough to cache goes in as a developer message
        with a cache breakpoint, sent once: `previous_response_id` carries it
        to every chained request, which `instructions` would not.
        """
        kwargs: dict[str, Any] = {
            "model": self.model,
            "tools": openai_tools or None,
            # Honor the configured output ceiling (parity with the Anthropic
            # max_tokens param); without it the truncation notice would blame
            # a knob that does nothing.
            "max_output_tokens": self.settings.max_tokens,
        }
        prefix: list[dict[str, Any]] = []
        if system:
            if (
                self.model in OPENAI_EXPLICIT_CACHE_MODELS
                and _stable_prefix_tokens(system, openai_tools) >= OPENAI_MIN_CACHEABLE_TOKENS
            ):
                prefix.append(_developer_prefix(system))
            else:
                kwargs["instructions"] = system
        effort = reasoning_effort_for(self.model, self.reasoning_effort)
        if effort:
            kwargs["reasoning"] = {"effort": effort}
            if reasoning_summary:
                # Request a reasoning summary so we can stream a "thinking"
                # preview; degrades gracefully if the org lacks summary access.
                kwargs["reasoning"]["summary"] = "auto"
        text: dict[str, Any] = {}
        if structured_output == JSON_OBJECT:
            text["format"] = {"type": "json_object"}
        elif structured_output is not None and hasattr(structured_output, "model_json_schema"):
            text["format"] = {
                "type": "json_schema",
                "name": structured_output.__name__,
                "schema": structured_output.model_json_schema(),
                "strict": True,
            }
        level = verbosity_for(self.model, verbosity)
        if level:
            text["verbosity"] = level
        if text:
            kwargs["text"] = text
        return kwargs, prefix

    def _run_function_calls(
        self,
        pending: list[tuple[str, str, str]],
        tools_by_name: dict[str, Any],
        iteration: int,
    ) -> list[dict[str, Any]]:
        """Execute one response's function calls; return their outputs in order.

        Independent read-only calls run side by side (`tool_exec`); the
        started/finished events still go out in call order, so each result
        lands on its own tool card.
        """
        names = [name for _, name, _ in pending]
        durations: list[int] = [0] * len(pending)
        outputs: list[dict[str, Any]] = [{} for _ in pending]

        def before(i: int) -> None:
            call_id, name, args = pending[i]
            log_workflow_event(
                "local_tool_call_started",
                {
                    "provider": self.name,
                    "model": self.model,
                    "tool_name": name,
                    "call_id": call_id,
                    "arguments": _parse_args(args),
                    "iteration": iteration,
                },
            )

        def work(i: int) -> str:
            call_id, name, args = pending[i]
            started = time.monotonic()
            token = tool_call_id_var.set(call_id or None)
            try:
                return _call_local_tool(tools_by_name, name, args)
            finally:
                tool_call_id_var.reset(token)
                durations[i] = int((time.monotonic() - started) * 1000)

        def after(i: int, raw_output: str) -> None:
            call_id, name, _ = pending[i]
            output_str, images = split_inline_images(raw_output)
            # Image-carrying results keep their protocol intact; text-only
            # results are cut to the tool's declared model-facing budget.
            budgeted = apply_result_budget(name, output_str) if not images else None
            log_workflow_event(
                "local_tool_call_finished",
                {
                    "provider": self.name,
                    "model": self.model,
                    "tool_name": name,
                    "call_id": call_id,
                    "output_preview": output_str[:1200],
                    "output_chars": len(output_str),
                    "iteration": iteration,
                    "duration_ms": durations[i],
                    **({"calls_in_response": len(pending)} if len(pending) > 1 else {}),
                    **({"inline_images": len(images)} if images else {}),
                    **budget_log_fields(budgeted),
                },
            )
            outputs[i] = {
                "type": "function_call_output",
                "call_id": call_id,
                "output": (
                    to_openai_tool_content(raw_output)
                    if images
                    else (budgeted.text if budgeted is not None else output_str)
                ),
            }

        run_tool_calls(names, work, before=before, after=after)
        return outputs

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
        tools_list = list(tools)
        # Deferred capability tools go into namespaces behind the hosted tool search.
        openai_tools = openai_tool_surface([_tool_to_openai(t) for t in tools_list])
        tools_by_name: dict[str, Any] = {}
        for t in tools_list:
            n = getattr(t, "name", None)
            if n:
                tools_by_name[n] = t

        kwargs_base, prefix_items = self._base_request(
            system, openai_tools, verbosity=verbosity, structured_output=structured_output
        )
        input_items: list[dict[str, Any]] = [*prefix_items, *_stringify_input_messages(messages)]
        previous_response_id: Optional[str] = None

        all_tool_calls: list[ToolCallRecord] = []
        all_hosted_calls: list[ToolCallRecord] = []
        final_text = ""
        usage_total: dict[str, Any] = {}
        last_response_id: Optional[str] = None
        last_resp: Any = None
        truncated = False

        iterations = 0
        ceiling_hit = False
        limit = iteration_limit(max_iterations, self.settings)
        in_turn = InTurnContext.for_settings(self.settings, provider=self.name, model=self.model)
        # Local mirror of the server-side conversation, so a clearing pass can
        # re-send it with old tool outputs stubbed out.
        history: list[dict[str, Any]] = list(input_items)
        call_names: dict[str, str] = {}
        log_workflow_event(
            "provider_turn_started",
            {
                "provider": self.name,
                "model": self.model,
                "tools_offered": [tdef.get("name", tdef.get("type")) for tdef in openai_tools],
                "reasoning_effort": (kwargs_base.get("reasoning") or {}).get("effort"),
                **({"verbosity": kwargs_base["text"]["verbosity"]} if "verbosity" in kwargs_base.get("text", {}) else {}),
                "cache_prefix": bool(prefix_items),
            },
        )
        with TurnTimer() as t:
            while limit is None or iterations < limit:
                iterations += 1
                kwargs = {k: v for k, v in kwargs_base.items() if v is not None}
                kwargs["input"] = input_items
                if previous_response_id is not None:
                    if in_turn is not None and in_turn.before_openai_request(history, call_names):
                        kwargs["input"] = history
                    else:
                        kwargs["previous_response_id"] = previous_response_id

                resp = self.client.responses.create(**kwargs)
                last_resp = resp
                last_response_id = getattr(resp, "id", None)
                truncated = truncated or _is_max_output_truncation(resp)
                history.extend(_output_refs(resp))
                if in_turn is not None:
                    in_turn.observe_openai(getattr(resp, "usage", None))

                # Collect tool calls + hosted calls from output items
                pending_function_calls: list[tuple[str, str, str]] = []  # (call_id, name, args)
                for item in getattr(resp, "output", []) or []:
                    itype = getattr(item, "type", None)
                    if itype == "function_call":
                        call_id = getattr(item, "call_id", None) or getattr(item, "id", "")
                        name = getattr(item, "name", "")
                        args = getattr(item, "arguments", "") or ""
                        pending_function_calls.append((call_id, name, args))
                        call_names[str(call_id)] = name
                        all_tool_calls.append(
                            ToolCallRecord(name=name, arguments=_parse_args(args), raw={"call_id": call_id})
                        )
                    elif itype in _HOSTED_CALL_TYPES:
                        arguments = _hosted_arguments(item)
                        record = ToolCallRecord(
                            name=itype,
                            arguments=arguments,
                            is_hosted=True,
                            raw={"id": getattr(item, "id", None)},
                        )
                        all_hosted_calls.append(record)
                        log_workflow_event(
                            "hosted_tool_call",
                            {
                                "provider": self.name,
                                "model": self.model,
                                "tool_name": itype,
                                "tool_id": getattr(item, "id", None),
                                "iteration": iterations,
                                "arguments": arguments,
                            },
                        )

                _accumulate_usage(usage_total, getattr(resp, "usage", None))

                if not pending_function_calls:
                    final_text = _extract_text_from_response(resp)
                    break

                # Append function_call_output items and continue
                input_items = self._run_function_calls(pending_function_calls, tools_by_name, iterations)
                history.extend(input_items)
                previous_response_id = last_response_id
                if limit is not None and iterations >= limit:
                    ceiling_hit = True

            if ceiling_hit:
                log_workflow_event(
                    "tool_loop_ceiling_hit",
                    {
                        "provider": self.name,
                        "model": self.model,
                        "iterations": iterations,
                        "max_iterations": limit,
                        "had_text": bool(final_text.strip()),
                        "stream": False,
                    },
                )
                if not final_text.strip():
                    final_text = self._forced_synthesis_text(
                        kwargs_base=kwargs_base,
                        input_items=input_items,
                        previous_response_id=previous_response_id,
                        usage_total=usage_total,
                    )

        record_usage(self.name, self.model, usage_total, state_dir=getattr(self.settings, "state_dir", None))

        structured_payload: Optional[dict[str, Any]] = None
        if structured_output is not None and final_text:
            try:
                structured_payload = json.loads(final_text)
            except Exception:
                structured_payload = None

        stop_reason = _stop_reason(last_resp, truncated=truncated)
        log_turn(
            {
                "provider": self.name,
                "model": self.model,
                "system": system,
                "messages_in": list(messages),
                "tools_offered": [tdef.get("name", tdef.get("type")) for tdef in openai_tools],
                "response_content": final_text,
                "tool_calls": [c.__dict__ for c in all_tool_calls],
                "hosted_tool_calls": [c.__dict__ for c in all_hosted_calls],
                "structured_payload": structured_payload,
                "usage": usage_total,
                "latency_ms": t.latency_ms,
                "openai_response_id": last_response_id,
                "reasoning_effort": (kwargs_base.get("reasoning") or {}).get("effort"),
                "iterations": iterations,
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
                "response_chars": len(final_text),
                "local_tool_call_count": len(all_tool_calls),
                "hosted_tool_call_count": len(all_hosted_calls),
                "has_sources": _has_sources(final_text),
                "latency_ms": t.latency_ms,
                "iterations": iterations,
                "stop_reason": stop_reason,
            },
        )

        return RunResult(
            text=final_text,
            provider=self.name,
            model=self.model,
            usage=_with_last_request(usage_total, getattr(in_turn, "last_input_tokens", None)),
            tool_calls=all_tool_calls,
            hosted_tool_calls=all_hosted_calls,
            structured_payload=structured_payload,
            response_id=last_response_id,
            iterations=iterations,
            stop_reason=stop_reason,
        )

    # ----- tool-loop ceiling recovery ---------------------------------------

    def _forced_synthesis_kwargs(
        self,
        *,
        kwargs_base: dict[str, Any],
        input_items: list[dict[str, Any]],
        previous_response_id: Optional[str],
    ) -> dict[str, Any]:
        """One more call with the tool results but WITHOUT tools.

        Everything else matches the loop's own requests: the system text (in
        `instructions`, or carried by `previous_response_id` as the developer
        prefix), the reasoning effort and any structured-output format.
        """
        items = list(input_items or [])
        items.append(
            {
                "role": "user",
                "content": [{"type": "input_text", "text": CEILING_SYNTHESIS_NUDGE}],
            }
        )
        kwargs = {k: v for k, v in kwargs_base.items() if k != "tools" and v is not None}
        kwargs["input"] = items
        if previous_response_id is not None:
            kwargs["previous_response_id"] = previous_response_id
        return kwargs

    def _forced_synthesis_text(
        self,
        *,
        kwargs_base: dict[str, Any],
        input_items: list[dict[str, Any]],
        previous_response_id: Optional[str],
        usage_total: dict[str, Any],
    ) -> str:
        try:
            resp = self.client.responses.create(
                **self._forced_synthesis_kwargs(
                    kwargs_base=kwargs_base,
                    input_items=input_items,
                    previous_response_id=previous_response_id,
                )
            )
            _accumulate_usage(usage_total, getattr(resp, "usage", None))
            return _extract_text_from_response(resp)
        except Exception as exc:  # noqa: BLE001 — recovery must not raise
            log_workflow_event(
                "tool_loop_ceiling_synthesis_failed",
                {"provider": self.name, "model": self.model, "error": str(exc)[:500]},
            )
            return ""

    def _forced_synthesis_stream(
        self,
        *,
        kwargs_base: dict[str, Any],
        input_items: list[dict[str, Any]],
        previous_response_id: Optional[str],
        text_buffer: list[str],
        usage_total: dict[str, Any],
    ) -> Iterator[StreamEvent]:
        kwargs = self._forced_synthesis_kwargs(
            kwargs_base=kwargs_base,
            input_items=input_items,
            previous_response_id=previous_response_id,
        )
        try:
            with self.client.responses.stream(**kwargs) as stream:
                for event in stream:
                    et = getattr(event, "type", "")
                    if et == "response.output_text.delta":
                        chunk = getattr(event, "delta", "")
                        if chunk:
                            text_buffer.append(chunk)
                            yield StreamEvent("delta", {"text": chunk})
                    elif et in {"response.completed", "response.incomplete"}:
                        _accumulate_usage(
                            usage_total,
                            getattr(getattr(event, "response", None), "usage", None),
                        )
        except Exception as exc:  # noqa: BLE001 — recovery must not raise
            log_workflow_event(
                "tool_loop_ceiling_synthesis_failed",
                {"provider": self.name, "model": self.model, "error": str(exc)[:500]},
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
        """Stream text deltas; tool calls are still handled in a loop.

        Note: when the model invokes function tools we cannot stream through
        the tool-resolution step transparently — we emit ``tool_call`` events
        and then yield further deltas from the next request.
        """
        tools_list = list(tools)
        # Deferred capability tools go into namespaces behind the hosted tool search.
        openai_tools = openai_tool_surface([_tool_to_openai(t) for t in tools_list])
        tools_by_name: dict[str, Any] = {n: t for t in tools_list if (n := getattr(t, "name", None))}

        kwargs_base, prefix_items = self._base_request(
            system, openai_tools, verbosity=verbosity, reasoning_summary=True
        )
        input_items: list[dict[str, Any]] = [*prefix_items, *_stringify_input_messages(messages)]
        previous_response_id: Optional[str] = None
        text_buffer: list[str] = []
        truncated = False
        iterations = 0
        ceiling_hit = False
        usage_total: dict[str, Any] = {}
        limit = iteration_limit(max_iterations, self.settings)
        in_turn = InTurnContext.for_settings(self.settings, provider=self.name, model=self.model)
        history: list[dict[str, Any]] = list(input_items)
        call_names: dict[str, str] = {}
        final: Any = None

        log_workflow_event(
            "provider_turn_started",
            {
                "provider": self.name,
                "model": self.model,
                "tools_offered": [tdef.get("name", tdef.get("type")) for tdef in openai_tools],
                "reasoning_effort": (kwargs_base.get("reasoning") or {}).get("effort"),
                **({"verbosity": kwargs_base["text"]["verbosity"]} if "verbosity" in kwargs_base.get("text", {}) else {}),
                "cache_prefix": bool(prefix_items),
                "stream": True,
            },
        )
        with TurnTimer() as t:
            while limit is None or iterations < limit:
                iterations += 1
                kwargs: dict[str, Any] = {k: v for k, v in kwargs_base.items() if v is not None}
                kwargs["input"] = input_items
                if previous_response_id is not None:
                    if in_turn is not None and in_turn.before_openai_request(history, call_names):
                        kwargs["input"] = history
                    else:
                        kwargs["previous_response_id"] = previous_response_id

                pending: list[tuple[str, str, str]] = []
                last_response_id: Optional[str] = None
                progress = ToolProgressThrottle()
                searching: set[str] = set()
                with self.client.responses.stream(**kwargs) as stream:
                    for event in stream:
                        et = getattr(event, "type", "")
                        if et == "response.output_text.delta":
                            chunk = getattr(event, "delta", "")
                            if chunk:
                                text_buffer.append(chunk)
                                yield StreamEvent("delta", {"text": chunk})
                        elif et == "response.reasoning_summary_text.delta":
                            rchunk = getattr(event, "delta", "")
                            if rchunk:
                                yield StreamEvent("reasoning_delta", {"text": rchunk})
                        elif et == "response.reasoning_summary_part.added":
                            # separate summary parts with a blank line
                            yield StreamEvent("reasoning_delta", {"text": "\n\n"})
                        elif et == "response.function_call_arguments.delta":
                            snapshot = getattr(event, "snapshot", None)
                            ev = progress.update(
                                getattr(event, "item_id", None),
                                chars_so_far=len(snapshot) if isinstance(snapshot, str) else None,
                                added=len(getattr(event, "delta", "") or ""),
                            )
                            if ev is not None:
                                yield ev
                        elif et == "response.output_item.added":
                            item = getattr(event, "item", None)
                            if getattr(item, "type", None) == "function_call":
                                progress.open(getattr(item, "id", None), getattr(item, "name", ""), getattr(item, "call_id", None))
                        elif et in {"response.web_search_call.searching", "response.file_search_call.searching"}:
                            item_id = getattr(event, "item_id", None)
                            if item_id and item_id not in searching:
                                searching.add(item_id)
                                kind = et.split(".")[1]
                                yield hosted_progress_event(
                                    provider=self.name,
                                    model=self.model,
                                    tool_name=_HOSTED_PROGRESS_NAMES.get(kind, kind),
                                    tool_id=item_id,
                                    status="searching",
                                )
                        elif et == "response.output_item.done":
                            item = getattr(event, "item", None)
                            itype = getattr(item, "type", None)
                            if itype == "function_call":
                                progress.close(getattr(item, "id", None))
                            elif itype in _HOSTED_PROGRESS_NAMES:
                                arguments = _hosted_arguments(item)
                                yield hosted_progress_event(
                                    provider=self.name,
                                    model=self.model,
                                    tool_name=_HOSTED_PROGRESS_NAMES[itype],
                                    tool_id=getattr(item, "id", None),
                                    status="completed",
                                    query=arguments.get("query"),
                                    url=arguments.get("url"),
                                )
                        elif et in {"response.completed", "response.incomplete"}:
                            final = getattr(event, "response", None)
                            if _is_max_output_truncation(final):
                                truncated = True
                            last_response_id = getattr(final, "id", None)
                            _accumulate_usage(usage_total, getattr(final, "usage", None))
                            history.extend(_output_refs(final))
                            if in_turn is not None:
                                in_turn.observe_openai(getattr(final, "usage", None))
                            for item in getattr(final, "output", []) or []:
                                if getattr(item, "type", None) == "function_call":
                                    pending.append(
                                        (
                                            getattr(item, "call_id", "") or getattr(item, "id", ""),
                                            getattr(item, "name", ""),
                                            getattr(item, "arguments", "") or "",
                                        )
                                    )
                                    call_names[str(pending[-1][0])] = pending[-1][1]
                                elif getattr(item, "type", None) in _HOSTED_CALL_TYPES:
                                    tool_name = getattr(item, "type", "")
                                    event_data = {
                                        "provider": self.name,
                                        "model": self.model,
                                        "tool_name": tool_name,
                                        "tool_id": getattr(item, "id", None),
                                        "iteration": iterations,
                                        "arguments": _hosted_arguments(item),
                                    }
                                    log_workflow_event("hosted_tool_call", event_data)
                                    yield StreamEvent("hosted_tool_call", event_data)

                if not pending:
                    break

                # The model asked for these together; announce them all, then
                # run them (side by side where tool_policy allows it).
                for call_id, name, args in pending:
                    yield StreamEvent(
                        "tool_call",
                        {
                            "name": name,
                            "call_id": call_id,
                            "arguments": _parse_args(args),
                            "agent": agent_name_var.get(),
                        },
                    )
                input_items = self._run_function_calls(pending, tools_by_name, iterations)
                history.extend(input_items)
                previous_response_id = last_response_id
                # Reaching here means the model asked for more tools. If a
                # caller set a round limit and this was the last round, the loop
                # exits below with tool results in hand and possibly no prose.
                if limit is not None and iterations >= limit:
                    ceiling_hit = True

            if ceiling_hit:
                log_workflow_event(
                    "tool_loop_ceiling_hit",
                    {
                        "provider": self.name,
                        "model": self.model,
                        "iterations": iterations,
                        "max_iterations": limit,
                        "had_text": bool("".join(text_buffer).strip()),
                        "stream": True,
                    },
                )
                if not "".join(text_buffer).strip():
                    yield from self._forced_synthesis_stream(
                        kwargs_base=kwargs_base,
                        input_items=input_items,
                        previous_response_id=previous_response_id,
                        text_buffer=text_buffer,
                        usage_total=usage_total,
                    )

        final_text = "".join(text_buffer)
        if truncated:
            notice = (
                "\n\n> ⚠️ **Output truncated** at the model's `max_tokens` limit "
                f"({self.settings.max_tokens} tokens). Increase `max_tokens` in "
                "config.yaml to get the complete analysis."
            )
            final_text += notice
            yield StreamEvent("delta", {"text": notice})
        stop_reason = _stop_reason(final, truncated=truncated)
        record_usage(self.name, self.model, usage_total, state_dir=getattr(self.settings, "state_dir", None))
        log_turn(
            {
                "provider": self.name,
                "model": self.model,
                "system": system,
                "messages_in": list(messages),
                "response_content": final_text,
                "usage": usage_total,
                "latency_ms": t.latency_ms,
                "openai_response_id": previous_response_id,
                "stream": True,
                "iterations": iterations,
                "stop_reason": stop_reason,
            }
        )
        log_workflow_event(
            "provider_turn_finished",
            {
                "provider": self.name,
                "model": self.model,
                "response_chars": len(final_text),
                "latency_ms": t.latency_ms,
                "iterations": iterations,
                "stream": True,
                "has_sources": _has_sources(final_text),
                "stop_reason": stop_reason,
            },
        )
        yield StreamEvent(
            "done",
            {
                "text": final_text,
                "usage": _with_last_request(usage_total, getattr(in_turn, "last_input_tokens", None)),
                "stop_reason": stop_reason,
            },
        )

