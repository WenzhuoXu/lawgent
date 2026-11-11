"""One chat turn run by Claude Code, translated into the web UI's event stream.

``ClaudeCodeExecutor.stream`` has ``WorkflowExecutor.stream``'s signature and
yields the same events, so the server runs either one. The SDK is async and
the server's turn is a thread draining a generator: the SDK runs on its own
event loop in a worker thread, every message it produces goes through one
queue, and the translator turns each into events here, in the server's
thread — one channel, so events keep the order they happened in.

What the UI receives:
- text deltas, segmented exactly as on the other path: the text before a tool
  call leaves as ``commentary``, the text after the last call is the answer
  (through the answer lint);
- thinking as ``reasoning_delta`` previews and one ``reasoning`` event per block;
- every tool call, Claude Code's own and the legal ones (prefix stripped), as
  ``local_tool_call_started`` / ``_finished`` keyed by ``call_id``;
- a sub-agent (the Agent tool) as an ``agent_task_*`` row under its call, its
  own tool calls nested under it by ``task_id`` / ``parent_call_id``;
- Claude Code's own states: ``claude_code_session`` (model, login, tools),
  ``todo_list`` (TodoWrite), ``claude_code_status`` (compacting, retrying),
  ``context_compaction`` (an auto-compaction boundary), ``rate_limit`` (the
  window Claude Code reports while it runs) and ``plan_usage`` (the plan's
  limits after the turn, as `/usage` shows them).
"""

from __future__ import annotations

import asyncio
import contextvars
import json
import queue
import threading
import time
from pathlib import Path
from typing import Any, Iterable, Iterator, Optional

from .. import answer_contract
from ..answer_lint import AnswerLinter
from ..chat_models import ChatMessage
from ..config import Settings, current_settings, load_settings, override_current_settings
from ..logging_setup import (
    agent_name_var,
    log_workflow_event,
    new_run_id,
    parent_run_id_var,
    run_id_var,
    setup_logging,
)
from ..providers.base import StreamEvent
from ..providers.progress import ToolProgressThrottle
from ..runs import RunStore
from ..turn_context import TurnContext, open_turn
from ..usage import record_usage, usage_context_var
from . import prompt as cc_prompt
from .runtime import (
    AGENT_TOOLS,
    display_tool_name,
    normalize_plan_usage,
    options,
    remember_plan_usage,
    remember_rate_limit,
    request_plan_usage,
)
from .tools import legal_tool_server

PROVIDER = "claude_code"
_POLL_S = 0.2
#: How long a cancelled turn may take to wind down before the CLI is dropped.
_CANCEL_GRACE_S = 10.0


class _Cancelled(Exception):
    pass


def previous_session(recent_messages: Optional[Iterable[ChatMessage]]) -> Optional[str]:
    """The Claude Code session the chat's previous answer came from, if any.

    Only the turn right before this one counts: when another provider answered
    in between, that session lacks the turn, and a fresh session gets the
    digest instead.
    """
    for message in reversed(list(recent_messages or [])):
        if message.role != "assistant":
            continue
        meta = message.metadata or {}
        if meta.get("provider") == PROVIDER and meta.get("cc_session_id"):
            return str(meta["cc_session_id"])
        return None
    return None


def _turn_prompt(
    *,
    user_message: str,
    language: str,
    tier: str,
    skill_hint: Optional[str],
    attachments: list[Path],
    files_block: str,
    digest: Optional[tuple[str, str]],
) -> str:
    """The user message Claude Code receives for this turn.

    A fresh session opens with the chat digest (transcript, files, summary); a
    resumed one already holds the conversation and gets only what changed.
    """
    parts: list[str] = []
    if digest is not None:
        parts.extend(p for p in digest if p)
    elif files_block:
        parts.append(files_block)
    parts.append(answer_contract.author_tail(user_language=language, tier=tier))
    if skill_hint and skill_hint != "cite-check":
        parts.append(
            f"The user picked a method for this turn: load `legal:{skill_hint}` with the "
            "Skill tool and follow it."
        )
    if attachments:
        parts.append(
            "Files attached to this message — open the ones you need (Read for text, PDF "
            "and images; `read_document` for Word, Excel, PowerPoint, OFD and zip):\n"
            + "\n".join(f"- {p}" for p in attachments)
        )
    parts.append(f"Current user request:\n{user_message}")
    return "\n\n".join(parts)


def _result_text(content: Any) -> tuple[str, int]:
    """A tool result's text, and how many images rode with it."""
    if content is None:
        return "", 0
    if isinstance(content, str):
        return content, 0
    texts: list[str] = []
    images = 0
    for item in content if isinstance(content, list) else [content]:
        if isinstance(item, dict):
            if item.get("type") == "text":
                texts.append(str(item.get("text", "")))
            elif item.get("type") == "image":
                images += 1
            elif item.get("type") == "tool_reference":
                texts.append(str(item.get("tool_name", "")))
        else:
            texts.append(str(item))
    return "\n".join(t for t in texts if t), images


class _Translator:
    """Claude Code messages → the events the web UI reads (one turn)."""

    def __init__(self, settings: Settings, model: str, language: str) -> None:
        self.settings = settings
        self.model = model
        self.lint_language = answer_contract.language_code(language)
        self.linter = AnswerLinter(language=self.lint_language)
        self.chunks: list[str] = []
        self.commentary: list[str] = []
        self.thought: list[str] = []
        self.blocks: dict[int, str] = {}
        self.streamed_text = False
        self.throttle = ToolProgressThrottle()
        #: call id → {name, raw, parent, started}
        self.calls: dict[str, dict[str, Any]] = {}
        #: Agent call id → {tools, tokens, started}
        self.agents: dict[str, dict[str, Any]] = {}
        self.todo_calls: set[str] = set()
        self.session_id: Optional[str] = None
        self.saw_init = False
        self.status: Optional[str] = None
        self.last_request_input = 0
        self.result: Any = None
        self.exception: Optional[BaseException] = None
        self.assistant_error: Optional[str] = None
        self.rate_limit: Optional[dict[str, Any]] = None

    # --- text ---------------------------------------------------------------

    def _emit_text(self, clean: str) -> Iterator[StreamEvent]:
        if clean:
            self.chunks.append(clean)
            yield StreamEvent("delta", {"text": clean})

    def _text(self, text: str) -> Iterator[StreamEvent]:
        self.streamed_text = True
        yield from self._emit_text(self.linter.feed(text))

    def _close_segment(self) -> Iterator[StreamEvent]:
        # A tool call follows the text written so far, so that text was
        # commentary on the way to the call, not the answer.
        yield from self._emit_text(self.linter.flush())
        text = "".join(self.chunks).strip()
        self.chunks.clear()
        self.linter = AnswerLinter(language=self.lint_language)
        if text:
            self.commentary.append(text)
            yield StreamEvent("commentary", {"text": text})

    def _close_thought(self) -> None:
        text = "".join(self.thought).strip()
        self.thought.clear()
        if text:
            log_workflow_event("reasoning", {"text": text})

    def finish(self) -> Iterator[StreamEvent]:
        """Close the last segment; the answer is the text after the last call."""
        self._close_thought()
        yield from self._emit_text(self.linter.flush())
        if self.linter.counts.any():
            log_workflow_event("answer_lint", {"stage": "answer", **self.linter.counts.as_dict()})
        answer = "".join(self.chunks)
        if not answer.strip() and self.commentary:
            # The answer came before a last tool call and nothing followed it.
            answer = self.commentary[-1]
            log_workflow_event("answer_from_commentary", {"stage": "answer"})
            yield StreamEvent("delta", {"text": answer})
        return answer

    # --- dispatch -----------------------------------------------------------

    def on_message(self, msg: Any) -> Iterator[StreamEvent]:
        from claude_agent_sdk import (
            AssistantMessage,
            RateLimitEvent,
            ResultMessage,
            StreamEvent as SdkStreamEvent,
            SystemMessage,
            TaskNotificationMessage,
            TaskProgressMessage,
            TaskStartedMessage,
            UserMessage,
        )

        if isinstance(msg, SdkStreamEvent):
            if msg.parent_tool_use_id is None:
                yield from self._on_stream_event(msg.event)
        elif isinstance(msg, AssistantMessage):
            yield from self._on_assistant(msg)
        elif isinstance(msg, UserMessage):
            self._on_user(msg)
        elif isinstance(msg, TaskStartedMessage):
            self._on_task_started(msg)
        elif isinstance(msg, TaskProgressMessage):
            yield from self._on_task_progress(msg)
        elif isinstance(msg, TaskNotificationMessage):
            self._on_task_notification(msg)
        elif isinstance(msg, SystemMessage):
            yield from self._on_system(msg)
        elif isinstance(msg, RateLimitEvent):
            self._on_rate_limit(msg)
        elif isinstance(msg, ResultMessage):
            self.result = msg
            self.session_id = msg.session_id or self.session_id

    # --- the raw API stream (main session) ----------------------------------

    def _on_stream_event(self, ev: dict[str, Any]) -> Iterator[StreamEvent]:
        et = ev.get("type")
        if et == "message_start":
            self.streamed_text = False
            usage = (ev.get("message") or {}).get("usage") or {}
            total = sum(
                int(usage.get(k) or 0)
                for k in ("input_tokens", "cache_read_input_tokens", "cache_creation_input_tokens")
            )
            if total:
                self.last_request_input = total
        elif et == "content_block_start":
            index = int(ev.get("index", 0))
            block = ev.get("content_block") or {}
            kind = str(block.get("type") or "")
            self.blocks[index] = kind
            if kind in {"tool_use", "server_tool_use"}:
                self._close_thought()
                yield from self._close_segment()
                self.throttle.open(index, display_tool_name(str(block.get("name") or "")), block.get("id"))
        elif et == "content_block_delta":
            index = int(ev.get("index", 0))
            delta = ev.get("delta") or {}
            dt = delta.get("type")
            if dt == "text_delta":
                yield from self._text(str(delta.get("text") or ""))
            elif dt == "thinking_delta":
                text = str(delta.get("thinking") or "")
                if text:
                    self.thought.append(text)
                    yield StreamEvent("reasoning_delta", {"text": text})
            elif dt == "input_json_delta":
                progress = self.throttle.update(index, added=len(delta.get("partial_json") or ""))
                if progress is not None:
                    yield progress
        elif et == "content_block_stop":
            index = int(ev.get("index", 0))
            self.throttle.close(index)
            if self.blocks.pop(index, "") in {"thinking", "redacted_thinking"}:
                self._close_thought()

    # --- complete messages --------------------------------------------------

    def _on_assistant(self, msg: Any) -> Iterator[StreamEvent]:
        from claude_agent_sdk import ServerToolUseBlock, TextBlock, ToolUseBlock

        parent = msg.parent_tool_use_id
        if parent is None and msg.error:
            self.assistant_error = str(msg.error)
        for block in msg.content:
            if isinstance(block, ToolUseBlock):
                yield from self._tool_started(block.id, block.name, block.input or {}, parent)
            elif isinstance(block, ServerToolUseBlock):
                if parent is None:
                    log_workflow_event(
                        "hosted_tool_call",
                        {
                            "provider": PROVIDER,
                            "model": self.model,
                            "tool_name": str(block.name),
                            "tool_id": block.id,
                            "raw_type": "server_tool_use",
                            "arguments": block.input or {},
                        },
                    )
            elif isinstance(block, TextBlock) and parent is None and not self.streamed_text:
                # A message whose text never streamed (a retried request):
                # without this its words would be lost.
                yield from self._text(block.text)

    def _tool_started(
        self, call_id: str, raw: str, args: dict[str, Any], parent: Optional[str]
    ) -> Iterator[StreamEvent]:
        name = display_tool_name(raw)
        self.calls[call_id] = {"name": name, "raw": raw, "parent": parent, "started": time.monotonic()}
        if parent is not None:
            agent = self.agents.get(parent)
            if agent is not None:
                agent["tools"] += 1
            log_workflow_event(
                "local_tool_call_started",
                {
                    "provider": PROVIDER,
                    "model": self.model,
                    "tool_name": name,
                    "arguments": args,
                    "call_id": call_id,
                    "task_id": parent,
                    "parent_call_id": parent,
                },
            )
            return
        if raw == "TodoWrite":
            # The plan is shown as a list, not as one more tool row.
            self.todo_calls.add(call_id)
            log_workflow_event("todo_list", {"todos": args.get("todos") or []})
            return
        log_workflow_event(
            "local_tool_call_started",
            {"provider": PROVIDER, "model": self.model, "tool_name": name, "arguments": args, "call_id": call_id},
        )
        if raw in AGENT_TOOLS:
            self.agents[call_id] = {"tools": 0, "tokens": None, "started": time.monotonic()}
            log_workflow_event(
                "agent_task_started",
                {
                    "task_id": call_id,
                    "parent_call_id": call_id,
                    "title": str(args.get("description") or "")[:200] or None,
                    "skill_name": str(args.get("subagent_type") or "general-purpose"),
                    "task": str(args.get("prompt") or "")[:400],
                },
            )
        elif raw == "WebSearch":
            yield StreamEvent(
                "hosted_tool_progress",
                {"provider": PROVIDER, "tool_name": raw, "tool_id": call_id, "status": "searching", "query": args.get("query")},
            )
        elif raw == "WebFetch":
            yield StreamEvent(
                "hosted_tool_progress",
                {"provider": PROVIDER, "tool_name": raw, "tool_id": call_id, "status": "searching", "url": args.get("url")},
            )

    def _on_user(self, msg: Any) -> None:
        from claude_agent_sdk import ToolResultBlock

        content = msg.content if isinstance(msg.content, list) else []
        for block in content:
            if isinstance(block, ToolResultBlock):
                self._tool_finished(block.tool_use_id, block.content, bool(block.is_error))

    def _tool_finished(self, call_id: str, content: Any, is_error: bool) -> None:
        call = self.calls.pop(call_id, None)
        if call is None or call_id in self.todo_calls:
            return
        text, images = _result_text(content)
        preview = json.dumps({"error": text[:1000]}, ensure_ascii=False) if is_error else text[:1200]
        data: dict[str, Any] = {
            "provider": PROVIDER,
            "model": self.model,
            "tool_name": call["name"],
            "call_id": call_id,
            "output_preview": preview,
            "output_chars": len(text),
        }
        if images:
            data["inline_images"] = images
        if is_error:
            data["error"] = text[:500]
        if call["parent"] is not None:
            data.update(task_id=call["parent"], parent_call_id=call["parent"])
        log_workflow_event("local_tool_call_finished", data)
        agent = self.agents.pop(call_id, None) if call["parent"] is None else None
        if agent is not None:
            log_workflow_event(
                "agent_task_finished",
                {
                    "task_id": call_id,
                    "parent_call_id": call_id,
                    "outcome": "failed" if is_error else ("completed" if text.strip() else "empty"),
                    "tool_count": agent["tools"],
                    "total_tokens": agent["tokens"],
                    "duration_ms": int((time.monotonic() - agent["started"]) * 1000),
                },
            )

    # --- sub-agent lifecycle ------------------------------------------------

    def _on_task_started(self, msg: Any) -> None:
        if msg.tool_use_id and msg.tool_use_id in self.agents and msg.description:
            log_workflow_event(
                "agent_task_started",
                {"task_id": msg.tool_use_id, "parent_call_id": msg.tool_use_id, "title": msg.description[:200]},
            )

    def _on_task_progress(self, msg: Any) -> Iterator[StreamEvent]:
        agent = self.agents.get(msg.tool_use_id or "")
        if agent is None:
            return
        usage = msg.usage or {}
        agent["tokens"] = usage.get("total_tokens") or agent["tokens"]
        yield StreamEvent(
            "agent_delta",
            {
                "task_id": msg.tool_use_id,
                "parent_call_id": msg.tool_use_id,
                "tool_count": usage.get("tool_uses") or agent["tools"],
                "elapsed_ms": usage.get("duration_ms"),
                "tail": (msg.description or "")[-240:] or None,
            },
        )

    def _on_task_notification(self, msg: Any) -> None:
        agent = self.agents.get(msg.tool_use_id or "")
        if agent is not None and msg.usage:
            agent["tokens"] = msg.usage.get("total_tokens") or agent["tokens"]

    # --- session state ------------------------------------------------------

    def _on_system(self, msg: Any) -> Iterator[StreamEvent]:
        data = msg.data or {}
        if msg.subtype == "init":
            self.saw_init = True
            self.session_id = data.get("session_id") or self.session_id
            self.model = data.get("model") or self.model
            log_workflow_event(
                "claude_code_session",
                {
                    "session_id": self.session_id,
                    "model": self.model,
                    # "none" = the claude.ai login. (Not named *key*: the
                    # event log redacts such fields.)
                    "auth": "subscription"
                    if data.get("apiKeySource") in (None, "none")
                    else str(data.get("apiKeySource")),
                    "version": data.get("claude_code_version"),
                    "permission_mode": data.get("permissionMode"),
                    "tool_count": len(data.get("tools") or []),
                    "skill_count": len(data.get("skills") or []),
                    "mcp_servers": [
                        {"name": s.get("name"), "status": s.get("status")}
                        for s in data.get("mcp_servers") or []
                    ],
                },
            )
        elif msg.subtype == "status":
            status = data.get("status") or "requesting"
            if status != self.status:
                self.status = status
                yield StreamEvent("claude_code_status", {"status": status})
        elif msg.subtype == "api_retry":
            yield StreamEvent(
                "claude_code_status",
                {
                    "status": "retrying",
                    "attempt": data.get("attempt"),
                    "max_retries": data.get("max_retries"),
                    "error_status": data.get("error_status"),
                },
            )
        elif msg.subtype == "compact_boundary":
            meta = data.get("compact_metadata") or {}
            log_workflow_event(
                "context_compaction",
                {
                    "reason": "claude_code_auto_compact",
                    "trigger": meta.get("trigger"),
                    "pre_tokens": meta.get("pre_tokens"),
                },
            )

    def _on_rate_limit(self, msg: Any) -> None:
        info = msg.rate_limit_info
        data = {
            "status": info.status,
            "rate_limit_type": info.rate_limit_type,
            "utilization": info.utilization,
            "resets_at": info.resets_at,
            "overage_status": info.overage_status,
            "overage_resets_at": info.overage_resets_at,
        }
        # Claude Code reports the window on every request; the chat keeps one
        # row per change, the usage panel the latest.
        changed = self.rate_limit is None or any(
            self.rate_limit.get(k) != data[k] for k in ("status", "rate_limit_type", "utilization")
        )
        self.rate_limit = data
        remember_rate_limit(self.settings, data)
        if changed:
            log_workflow_event("rate_limit", data)

    def on_plan_usage(self, raw: Any) -> None:
        """The subscription's limits after the turn: kept for the usage panel,
        and logged so the chat can say when a limit is close."""
        if not isinstance(raw, dict):
            return
        usage = normalize_plan_usage(raw)
        remember_plan_usage(self.settings, usage)
        log_workflow_event(
            "plan_usage",
            {
                "subscription_type": usage["subscription_type"],
                "limits": usage["limits"],
                "extra_usage": usage["extra_usage"],
            },
        )

    # --- the turn's outcome -------------------------------------------------

    def usage_payload(self) -> dict[str, Any]:
        usage = dict((self.result.usage if self.result is not None else None) or {})
        return {
            "input_tokens": int(usage.get("input_tokens") or 0),
            "output_tokens": int(usage.get("output_tokens") or 0),
            "cache_read_input_tokens": int(usage.get("cache_read_input_tokens") or 0),
            "cache_creation_input_tokens": int(usage.get("cache_creation_input_tokens") or 0),
            "last_request_input_tokens": self.last_request_input,
        }

    def error_message(self) -> Optional[str]:
        """Why the turn failed, or None when it did not."""
        result = self.result
        if self.rate_limit and self.rate_limit.get("status") == "rejected":
            resets = self.rate_limit.get("resets_at")
            when = time.strftime("%H:%M", time.localtime(resets)) if resets else "later"
            return (
                f"Claude 订阅额度已用完，{when} 重置。 / The Claude subscription's usage "
                f"limit is reached; it resets at {when}."
            )
        if result is None:
            if self.exception is not None:
                return f"Claude Code stopped: {self.exception}"
            return "Claude Code ended the turn without a result."
        if not result.is_error:
            return None
        if result.errors:
            return "; ".join(str(e) for e in result.errors)[:2000]
        if result.api_error_status:
            return f"Claude API error {result.api_error_status}" + (
                f" ({self.assistant_error})" if self.assistant_error else ""
            )
        return result.result or self.assistant_error or f"Claude Code ended with {result.subtype}."


def _drive(opts: Any, prompt: str, q: "queue.Queue[tuple[str, Any]]", cancel: threading.Event, stop: threading.Event) -> None:
    """Run one Claude Code query on a private event loop; feed its messages to ``q``."""

    async def main() -> None:
        from claude_agent_sdk import ClaudeSDKClient

        async with ClaudeSDKClient(opts) as client:
            await client.query(prompt)

            async def receive() -> None:
                async for msg in client.receive_response():
                    q.put(("msg", msg))

            task = asyncio.create_task(receive())
            interrupted = False
            while not task.done():
                await asyncio.wait({task}, timeout=_POLL_S)
                if stop.is_set():
                    task.cancel()
                    break
                if cancel.is_set() and not interrupted:
                    interrupted = True
                    try:
                        await client.interrupt()
                    except Exception:  # noqa: BLE001 — a dead CLI is dropped below
                        task.cancel()
                        break
            try:
                await task
            except asyncio.CancelledError:
                return
            if not cancel.is_set() and not stop.is_set():
                # The plan's usage after this turn, read on the same CLI (no
                # model call). Best-effort: the answer never waits on it long.
                try:
                    q.put(("plan_usage", await request_plan_usage(client, timeout=10)))
                except Exception:  # noqa: BLE001
                    pass

    try:
        asyncio.run(main())
    except BaseException as exc:  # noqa: BLE001 — reported to the consumer, which decides
        q.put(("exc", exc))
    finally:
        q.put(("end", None))


class ClaudeCodeExecutor:
    """Answers a chat turn inside Claude Code. Same interface as WorkflowExecutor."""

    def __init__(self, settings: Optional[Settings] = None) -> None:
        setup_logging()
        self.settings = settings or current_settings()
        self.model = self.settings.model_for_provider()

    def _uploads_dir(self) -> Optional[Path]:
        chat_id = (usage_context_var.get() or {}).get("chat_id")
        if not chat_id:
            return None
        return load_settings().outputs_dir / "chat_uploads" / "".join(
            ch for ch in str(chat_id) if ch.isalnum() or ch in {"_", "-"}
        )

    def _run(
        self,
        opts: Any,
        prompt: str,
        translator: _Translator,
        cancel_event: threading.Event,
    ) -> Iterator[StreamEvent]:
        q: "queue.Queue[tuple[str, Any]]" = queue.Queue()
        stop = threading.Event()
        # The worker carries this thread's context: the event sink, the turn,
        # the settings override and the usage attribution reach the tools.
        ctx = contextvars.copy_context()
        worker = threading.Thread(
            target=ctx.run,
            args=(_drive, opts, prompt, q, cancel_event, stop),
            daemon=True,
            name="claude-code-turn",
        )
        worker.start()
        cancelled_at: Optional[float] = None
        try:
            while True:
                if cancel_event.is_set() and cancelled_at is None:
                    cancelled_at = time.monotonic()
                if cancelled_at is not None and time.monotonic() - cancelled_at > _CANCEL_GRACE_S:
                    break
                try:
                    kind, item = q.get(timeout=_POLL_S)
                except queue.Empty:
                    continue
                if kind == "end":
                    break
                if kind == "exc":
                    translator.exception = item
                elif kind == "plan_usage":
                    translator.on_plan_usage(item)
                elif cancelled_at is None:
                    yield from translator.on_message(item)
        finally:
            # Also reached when the client goes away mid-turn and the
            # generator is closed: the CLI is stopped rather than left running.
            stop.set()
            worker.join(timeout=_CANCEL_GRACE_S)

    def stream(
        self,
        user_message: str,
        *,
        attachments: Optional[Iterable[Path]] = None,
        available_files: Optional[list[dict]] = None,
        generated_files: Optional[list[dict]] = None,
        recent_messages: Optional[list[ChatMessage]] = None,
        memory_summary: str = "",
        skill_hint: Optional[str] = None,
        cancel_event: Optional[threading.Event] = None,
        resume_run_id: Optional[str] = None,
    ) -> Iterator[StreamEvent]:
        from ..workflow import _build_context_parts, _render_available_files, _turn_language

        run_id = new_run_id()
        token_run = run_id_var.set(run_id)
        token_parent = parent_run_id_var.set(None)
        token_agent = agent_name_var.set("orchestrator")
        chat_id = (usage_context_var.get() or {}).get("chat_id")
        run_store = RunStore(self.settings, run_id, chat_id=chat_id)
        cancel_event = cancel_event or threading.Event()
        started = time.monotonic()
        answered = False
        try:
            cwd = Path(self.settings.outputs_dir)
            uploads = self._uploads_dir()
            attachment_paths = [Path(p) for p in (attachments or [])]
            language = _turn_language(user_message, recent_messages, self.settings.default_language)
            tier = answer_contract.length_tier(self.settings.answer_length)
            stable, volatile = _build_context_parts(
                memory_summary,
                recent_messages or [],
                available_files=available_files,
                generated_files=generated_files,
            )
            files_block = _render_available_files(available_files, generated_files)
            session = previous_session(recent_messages)
            turn = TurnContext(
                settings=self.settings,
                user_message=user_message,
                user_language=language,
                context_block=stable,
                context_tail=volatile,
                attachments=attachment_paths,
                cancel_event=cancel_event,
                run_store=run_store,
            )
            with open_turn(turn), override_current_settings(self.settings):
                system = cc_prompt.author_system_prompt(self.settings, uploads_dir=uploads or cwd)
                server = legal_tool_server(self.settings)
                agents = cc_prompt.agents(self.settings)
                add_dirs = tuple(
                    d for d in (uploads, self.settings.state_dir / "tool_results") if d is not None
                )
                log_workflow_event(
                    "author_started",
                    {
                        "provider": PROVIDER,
                        "model": self.model,
                        "length_tier": tier,
                        "skill_hint": skill_hint,
                        "attachment_count": len(attachment_paths),
                        "user_language": language,
                        "resumed_session": session,
                    },
                )
                run_store.set_phase("answer")
                translator = _Translator(self.settings, self.model, language)
                for resume in ([session, None] if session else [None]):
                    prompt = _turn_prompt(
                        user_message=user_message,
                        language=language,
                        tier=tier,
                        skill_hint=skill_hint,
                        attachments=attachment_paths,
                        files_block=files_block,
                        digest=None if resume else (stable, volatile),
                    )
                    opts = options(
                        self.settings,
                        model=self.model,
                        system_prompt=system,
                        cwd=cwd,
                        mcp_server=server,
                        resume=resume,
                        add_dirs=add_dirs,
                        agents=agents,
                    )
                    yield from self._run(opts, prompt, translator, cancel_event)
                    if cancel_event.is_set():
                        raise _Cancelled()
                    if resume and not translator.saw_init and translator.error_message():
                        # The session is gone (another working directory, a
                        # cleaned ~/.claude): start over with the digest.
                        log_workflow_event(
                            "claude_code_resume_failed",
                            {"session_id": resume, "reason": translator.error_message()},
                        )
                        translator = _Translator(self.settings, self.model, language)
                        continue
                    break
                answer = yield from translator.finish()
            error = translator.error_message()
            if error:
                run_store.finish("failed", pause_reason="error", error=error[:2000])
                yield StreamEvent(
                    "error",
                    {"message": error, "run_id": run_id, "cc_session_id": translator.session_id},
                )
                return
            result = translator.result
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
            answered = True
            yield StreamEvent(
                "done",
                {
                    "text": answer,
                    "citation_audit": None,
                    "provider": PROVIDER,
                    "model": translator.model,
                    "usage": translator.usage_payload(),
                    "duration_ms": int((time.monotonic() - started) * 1000),
                    "cc_session_id": translator.session_id,
                    "api_equivalent_usd": result.total_cost_usd,
                    "num_turns": result.num_turns,
                },
            )
        except _Cancelled:
            run_store.finish("paused", pause_reason="cancelled")
            yield StreamEvent("cancelled", {"message": "Cancelled by user.", "run_id": run_id, "resumable": False})
        except Exception as exc:  # noqa: BLE001 — reported as the turn's error
            run_store.finish("failed", pause_reason="error", error=str(exc)[:2000])
            yield StreamEvent("error", {"message": f"Claude Code: {exc}", "run_id": run_id})
        finally:
            if run_store.snapshot.status == "running":
                run_store.finish("complete" if answered else "paused", pause_reason=None if answered else "abandoned")
            run_id_var.reset(token_run)
            parent_run_id_var.reset(token_parent)
            agent_name_var.reset(token_agent)
