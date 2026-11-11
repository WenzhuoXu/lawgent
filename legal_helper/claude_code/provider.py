"""One-shot side calls (chat titles, the rolling summary) on the Claude Code login.

A chat turn on provider ``claude_code`` never goes through a ``Provider``:
``ClaudeCodeExecutor`` runs it. What still reaches ``build_provider`` are the
calls with no tools — a title, a summary delta, a project compaction — and
this answers them with one tool-less, unsaved Claude Code query.
"""

from __future__ import annotations

import asyncio
import json
import threading
from typing import Any, Iterable, Iterator, Optional

from ..config import Settings
from ..providers.base import JSON_OBJECT, Message, RunResult, StreamEvent, Tool
from ..usage import record_usage
from .runtime import effort_for, options

PROVIDER = "claude_code"


def _flatten(messages: list[Message]) -> str:
    """The messages as one prompt; a side call carries one user message."""
    parts: list[str] = []
    for message in messages:
        content = message.get("content")
        if isinstance(content, str):
            text = content
        else:
            text = "\n".join(
                str(block.get("text", "")) for block in content or [] if isinstance(block, dict)
            )
        if message.get("role") == "assistant":
            text = f"[assistant]\n{text}"
        parts.append(text)
    return "\n\n".join(p for p in parts if p)


class ClaudeCodeProvider:
    name = PROVIDER
    supports_hosted_web_search = False
    supports_file_search = False
    supports_structured_outputs = True

    def __init__(self, settings: Settings, *, fast: bool = False) -> None:
        self.settings = settings
        self.model = settings.model_for_provider(fast=fast)
        self.fast = fast

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
        schema: Optional[dict[str, Any]] = None
        prompt = _flatten(messages)
        if structured_output is not None and structured_output != JSON_OBJECT:
            schema = {"type": "json_schema", "schema": structured_output.model_json_schema()}
        elif structured_output == JSON_OBJECT:
            prompt += "\n\nReply with one JSON object and nothing else."
        opts = options(
            self.settings,
            model=self.model,
            system_prompt=system,
            cwd=self.settings.state_dir / "claude_code" / "oneshot",
            effort=effort_for(self.settings, "summariser" if self.fast else "author"),
            tools=(),
            partial=False,
            persist=False,
            output_format=schema,
        )
        result_box: dict[str, Any] = {}

        async def main() -> None:
            from claude_agent_sdk import AssistantMessage, ResultMessage, TextBlock, query

            texts: list[str] = []
            async for msg in query(prompt=prompt, options=opts):
                if isinstance(msg, AssistantMessage):
                    texts.extend(b.text for b in msg.content if isinstance(b, TextBlock))
                elif isinstance(msg, ResultMessage):
                    result_box["result"] = msg
            result_box["text"] = "".join(texts)

        # A caller may already be inside an event loop; the query gets its own.
        worker = threading.Thread(target=lambda: asyncio.run(main()), daemon=True)
        worker.start()
        worker.join()
        result = result_box.get("result")
        if result is None:
            raise RuntimeError("Claude Code returned no result")
        if result.is_error:
            raise RuntimeError("; ".join(result.errors or []) or result.result or result.subtype)
        for name, mu in (result.model_usage or {}).items():
            record_usage(
                PROVIDER,
                str(mu.get("canonicalModel") or name),
                {
                    "input_tokens": mu.get("inputTokens"),
                    "output_tokens": mu.get("outputTokens"),
                    "cache_read_input_tokens": mu.get("cacheReadInputTokens"),
                    "cache_creation_input_tokens": mu.get("cacheCreationInputTokens"),
                },
                state_dir=self.settings.state_dir,
                context={"billing": "subscription", "api_equivalent_usd": mu.get("costUSD")},
            )
        text = result.result or result_box.get("text", "")
        payload = result.structured_output
        if payload is None and structured_output is not None:
            try:
                payload = json.loads(text)
            except ValueError:
                payload = None
        return RunResult(
            text=text,
            provider=PROVIDER,
            model=self.model,
            usage=dict(result.usage or {}),
            structured_payload=payload if isinstance(payload, dict) else None,
            iterations=result.num_turns,
            stop_reason=result.stop_reason,
        )

    def stream(self, system: str, messages: list[Message], tools: Iterable[Tool] = (), **_: Any) -> Iterator[StreamEvent]:
        raise NotImplementedError(
            "A claude_code turn runs through ClaudeCodeExecutor, not Provider.stream"
        )

    def tool_runner(self, system: str, messages: list[Message], tools: Iterable[Tool], **_: Any) -> RunResult:
        raise NotImplementedError(
            "A claude_code turn runs through ClaudeCodeExecutor, not Provider.tool_runner"
        )
