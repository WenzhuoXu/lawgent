"""OpenAI provider — Responses API runtime."""

from __future__ import annotations

import json
from typing import Any, Iterable, Iterator, Optional

from openai import OpenAI

from ..config import Settings
from ..logging_setup import TurnTimer, agent_name_var, log_turn, log_workflow_event
from ..tool_budget import apply_result_budget, budget_log_fields
from ..turn_compaction import InTurnContext, iteration_limit
from ..tools.multimodal import split_inline_images, to_openai_tool_content
from ..usage import record_usage
from .base import Message, RunResult, StreamEvent, Tool, ToolCallRecord


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


def _accumulate_usage(usage_total: dict[str, Any], u: Any) -> None:
    """Fold one Responses-API usage object into a running total.

    Captures the cached-input subset (``input_tokens_details.cached_tokens``)
    alongside the headline counts so the cost center can price it at the
    discounted cached rate.
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
    return {
        "type": "function",
        "name": name,
        "description": desc or "",
        "parameters": schema or {"type": "object", "properties": {}},
    }


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


def _has_evidence_log(text: str) -> bool:
    return any(marker in text for marker in ("Evidence Verification Log", "证据核验", "在线证据核验"))


def _has_claim_check(text: str) -> bool:
    return any(marker in text for marker in ("Claim-Level Sanity Check", "逐项核验", "逐项主张核验"))


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
        openai_tools = [_tool_to_openai(t) for t in tools_list]
        tools_by_name: dict[str, Any] = {}
        for t in tools_list:
            n = getattr(t, "name", None)
            if n:
                tools_by_name[n] = t

        input_items: list[dict[str, Any]] = _stringify_input_messages(messages)
        previous_response_id: Optional[str] = None

        all_tool_calls: list[ToolCallRecord] = []
        all_hosted_calls: list[ToolCallRecord] = []
        final_text = ""
        usage_total: dict[str, Any] = {}
        last_response_id: Optional[str] = None

        kwargs_base: dict[str, Any] = {
            "model": self.model,
            "instructions": system,
            "tools": openai_tools or None,
            # Honor the configured output ceiling (parity with the Anthropic
            # max_tokens param); without it the truncation notice below would
            # blame a knob that does nothing.
            "max_output_tokens": self.settings.max_tokens,
        }
        if self.reasoning_effort and self.reasoning_effort != "none":
            kwargs_base["reasoning"] = {"effort": self.reasoning_effort}
        if structured_output is not None and hasattr(structured_output, "model_json_schema"):
            kwargs_base["text"] = {
                "format": {
                    "type": "json_schema",
                    "name": structured_output.__name__,
                    "schema": structured_output.model_json_schema(),
                    "strict": True,
                }
            }

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
                "reasoning_effort": self.reasoning_effort,
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
                last_response_id = getattr(resp, "id", None)
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
                            ToolCallRecord(
                                name=name,
                                arguments=(json.loads(args) if args else {}) if args.strip().startswith("{") else {},
                                raw={"call_id": call_id},
                            )
                        )
                    elif itype in {"web_search_call", "file_search_call", "code_interpreter_call"}:
                        record = ToolCallRecord(
                            name=itype,
                            arguments={},
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
                            },
                        )

                _accumulate_usage(usage_total, getattr(resp, "usage", None))

                if not pending_function_calls:
                    final_text = _extract_text_from_response(resp)
                    break

                # Append function_call_output items and continue
                input_items = []
                for call_id, name, args in pending_function_calls:
                    log_workflow_event(
                        "local_tool_call_started",
                        {
                            "provider": self.name,
                            "model": self.model,
                            "tool_name": name,
                            "arguments": (json.loads(args) if args and args.strip().startswith("{") else {}),
                            "iteration": iterations,
                        },
                    )
                    raw_output = _call_local_tool(tools_by_name, name, args)
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
                            "output_preview": output_str[:1200],
                            "output_chars": len(output_str),
                            "iteration": iterations,
                            **({"inline_images": len(images)} if images else {}),
                            **budget_log_fields(budgeted),
                        },
                    )
                    input_items.append(
                        {
                            "type": "function_call_output",
                            "call_id": call_id,
                            "output": (
                                to_openai_tool_content(raw_output)
                                if images
                                else (budgeted.text if budgeted is not None else output_str)
                            ),
                        }
                    )
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
                        system=system,
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
                "reasoning_effort": self.reasoning_effort,
                "iterations": iterations,
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
                "has_evidence_verification_log": _has_evidence_log(final_text),
                "has_claim_level_sanity_check": _has_claim_check(final_text),
                "has_sources": _has_sources(final_text),
                "latency_ms": t.latency_ms,
                "iterations": iterations,
            },
        )

        return RunResult(
            text=final_text,
            provider=self.name,
            model=self.model,
            usage=usage_total,
            tool_calls=all_tool_calls,
            hosted_tool_calls=all_hosted_calls,
            structured_payload=structured_payload,
            response_id=last_response_id,
            iterations=iterations,
        )

    # ----- tool-loop ceiling recovery ---------------------------------------

    def _forced_synthesis_kwargs(
        self,
        *,
        system: str,
        input_items: list[dict[str, Any]],
        previous_response_id: Optional[str],
    ) -> dict[str, Any]:
        """One more call with the tool results but WITHOUT tools."""
        items = list(input_items or [])
        items.append(
            {
                "role": "user",
                "content": [{"type": "input_text", "text": CEILING_SYNTHESIS_NUDGE}],
            }
        )
        kwargs: dict[str, Any] = {
            "model": self.model,
            "instructions": system,
            "input": items,
            "max_output_tokens": self.settings.max_tokens,
        }
        if previous_response_id is not None:
            kwargs["previous_response_id"] = previous_response_id
        if self.reasoning_effort and self.reasoning_effort != "none":
            kwargs["reasoning"] = {"effort": self.reasoning_effort}
        return kwargs

    def _forced_synthesis_text(
        self,
        *,
        system: str,
        input_items: list[dict[str, Any]],
        previous_response_id: Optional[str],
        usage_total: dict[str, Any],
    ) -> str:
        try:
            resp = self.client.responses.create(
                **self._forced_synthesis_kwargs(
                    system=system,
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
        system: str,
        input_items: list[dict[str, Any]],
        previous_response_id: Optional[str],
        text_buffer: list[str],
        usage_total: dict[str, Any],
    ) -> Iterator[StreamEvent]:
        kwargs = self._forced_synthesis_kwargs(
            system=system,
            input_items=input_items,
            previous_response_id=previous_response_id,
        )
        if self.reasoning_effort and self.reasoning_effort != "none":
            kwargs["reasoning"] = {"effort": self.reasoning_effort, "summary": "auto"}
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
    ) -> Iterator[StreamEvent]:
        """Stream text deltas; tool calls are still handled in a loop.

        Note: when the model invokes function tools we cannot stream through
        the tool-resolution step transparently — we emit ``tool_call`` events
        and then yield further deltas from the next request.
        """
        tools_list = list(tools)
        openai_tools = [_tool_to_openai(t) for t in tools_list]
        tools_by_name: dict[str, Any] = {n: t for t in tools_list if (n := getattr(t, "name", None))}

        input_items: list[dict[str, Any]] = _stringify_input_messages(messages)
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

        log_workflow_event(
            "provider_turn_started",
            {
                "provider": self.name,
                "model": self.model,
                "tools_offered": [tdef.get("name", tdef.get("type")) for tdef in openai_tools],
                "reasoning_effort": self.reasoning_effort,
                "stream": True,
            },
        )
        with TurnTimer() as t:
            while limit is None or iterations < limit:
                iterations += 1
                kwargs: dict[str, Any] = {
                    "model": self.model,
                    "instructions": system,
                    "tools": openai_tools or None,
                    "input": input_items,
                    # Same output ceiling as the non-streaming path.
                    "max_output_tokens": self.settings.max_tokens,
                }
                if self.reasoning_effort and self.reasoning_effort != "none":
                    # Request a reasoning summary so we can stream a "thinking"
                    # preview; degrades gracefully if the org lacks summary access.
                    kwargs["reasoning"] = {"effort": self.reasoning_effort, "summary": "auto"}
                if previous_response_id is not None:
                    if in_turn is not None and in_turn.before_openai_request(history, call_names):
                        kwargs["input"] = history
                    else:
                        kwargs["previous_response_id"] = previous_response_id
                kwargs = {k: v for k, v in kwargs.items() if v is not None}

                pending: list[tuple[str, str, str]] = []
                last_response_id: Optional[str] = None
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
                                elif getattr(item, "type", None) in {
                                    "web_search_call",
                                    "file_search_call",
                                    "code_interpreter_call",
                                }:
                                    tool_name = getattr(item, "type", "")
                                    event_data = {
                                        "provider": self.name,
                                        "model": self.model,
                                        "tool_name": tool_name,
                                        "tool_id": getattr(item, "id", None),
                                        "iteration": iterations,
                                    }
                                    log_workflow_event("hosted_tool_call", event_data)
                                    yield StreamEvent("hosted_tool_call", event_data)

                if not pending:
                    break

                input_items = []
                for call_id, name, args in pending:
                    parsed_args = json.loads(args) if args and args.strip().startswith("{") else {}
                    yield StreamEvent(
                        "tool_call",
                        {
                            "name": name,
                            "arguments": parsed_args,
                            "agent": agent_name_var.get(),
                        },
                    )
                    log_workflow_event(
                        "local_tool_call_started",
                        {
                            "provider": self.name,
                            "model": self.model,
                            "tool_name": name,
                            "arguments": parsed_args,
                            "iteration": iterations,
                        },
                    )
                    raw_output = _call_local_tool(tools_by_name, name, args)
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
                            "output_preview": output_str[:1200],
                            "output_chars": len(output_str),
                            "iteration": iterations,
                            **({"inline_images": len(images)} if images else {}),
                            **budget_log_fields(budgeted),
                        },
                    )
                    input_items.append(
                        {
                            "type": "function_call_output",
                            "call_id": call_id,
                            "output": (
                                to_openai_tool_content(raw_output)
                                if images
                                else (budgeted.text if budgeted is not None else output_str)
                            ),
                        }
                    )
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
                        system=system,
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
                "has_evidence_verification_log": _has_evidence_log(final_text),
                "has_claim_level_sanity_check": _has_claim_check(final_text),
                "has_sources": _has_sources(final_text),
            },
        )
        yield StreamEvent(
            "done",
            {
                "text": final_text,
                "usage": usage_total,
                "stop_reason": "max_tokens" if truncated else "stop",
            },
        )


def aviation_openai_hosted_tools(settings: Settings) -> list[dict[str, Any]]:
    """Hosted Responses-API tools used by sub-agents."""
    out: list[dict[str, Any]] = []
    if settings.enable_web_search:
        out.append({"type": "web_search"})
    if settings.openai_enable_file_search and settings.openai_file_search_vector_store_ids:
        out.append(
            {
                "type": "file_search",
                "vector_store_ids": settings.openai_file_search_vector_store_ids,
            }
        )
    return out
