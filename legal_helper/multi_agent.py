"""The multi-agent workflow, as a tool the author calls.

The author — the chat's own model — answers every turn. When a request has
several distinct issues that each need sustained research, or the user asks for
a workflow, it calls `run_workflow` with the specialists it wants: each is a
sub-agent on the specialist role, holding one legal method and the full
research tool set. They run in parallel; their bundles come back as the tool
result, and the author integrates them in the same loop. Depth is one:
specialists never get this tool.

The conventions follow Claude Code (sub-agent events name the parent tool
call), Codex (depth 1, at most three children, "do not delegate the step your
next move depends on") and ZCode (an explicit request for a workflow makes the
tool mandatory).

Events, all carrying ``task_id`` / ``parent_call_id`` (see
``logging_setup.task_id_var``): ``workflow_started``, ``agent_task_started``,
``agent_delta`` (transient preview), ``agent_task_finished``,
``agent_task_failed`` / ``_retried`` / ``_rate_limited``,
``workflow_agent_concurrency_reduced``, ``workflow_degraded``, ``workflow_finished``.
"""

from __future__ import annotations

import contextvars
import queue
import re
import threading
import time
from dataclasses import dataclass, field
from typing import Any, Optional

from anthropic import beta_tool

from . import answer_contract
from .chat_models import AgentTask
from .errors import OUT_OF_MONEY_MESSAGE, is_out_of_money_error, is_rate_limit_error
from .logging_setup import (
    agent_name_var,
    emit_transient,
    log_workflow_event,
    parent_call_id_var,
    task_id_var,
    tool_call_id_var,
)
from .turn_context import TurnContext, current_turn

GENERAL_TASK_SKILL = "general-answer"

#: Codex's default for parallel children.
DEFAULT_MAX_WORKFLOW_TASKS = 3

_GENERAL_SPECIALIST_PROMPT = """You are a specialist sub-agent working one part of a larger request. The author who assigned it writes the answer the user reads, from what you return.

Do the assignment with your tools: hosted web search for current facts, prices,
schedules and source-backed claims; the document tools for files. Return your
findings plainly, with the sources and links you relied on. Do not add legal
disclaimers unless the assignment raises a legal issue. Do not emit
`<tool_call>` or any pseudo function-call envelope as text.

""" + answer_contract.deliverable_rules() + "\n"

# One transient-failure retry per specialist, after a short pause. Rate limits
# are handled by lowering concurrency for the whole batch instead.
_RETRY_BACKOFF_SECONDS = 2.0
_PREVIEW_INTERVAL_S = 0.25

_CONTEXT_OVERFLOW_RE = re.compile(
    r"context[_ ]length|context window|maximum context|too many tokens|prompt is too long|input is too long",
    re.IGNORECASE,
)
_OUT_OF_SCOPE_RE = re.compile(
    r"^##\s*Out of scope\s*$(.*?)(?=^##\s|\Z)", re.MULTILINE | re.DOTALL | re.IGNORECASE
)


def workflow_methods() -> tuple[str, ...]:
    """Methods a specialist may hold: the in-tree legal methods, plus general."""
    from .skills import internal_skill_names

    return (*[n for n in internal_skill_names() if n != "cite-check"], GENERAL_TASK_SKILL)


def max_workflow_tasks(settings: Any) -> int:
    value = getattr(settings, "max_workflow_tasks", DEFAULT_MAX_WORKFLOW_TASKS)
    try:
        return max(1, int(value))
    except (TypeError, ValueError):
        return DEFAULT_MAX_WORKFLOW_TASKS


class WorkflowRejected(ValueError):
    """The task list cannot run as given; the message tells the model why."""


def validate_tasks(raw: Any, *, max_tasks: int) -> list[AgentTask]:
    """The model's task list as `AgentTask`s, or a `WorkflowRejected` naming the fix."""
    if not isinstance(raw, list) or not raw:
        raise WorkflowRejected("`tasks` must be a non-empty list of specialist assignments.")
    if len(raw) > max_tasks:
        raise WorkflowRejected(
            f"At most {max_tasks} specialists run in one workflow; you gave {len(raw)}. "
            "Merge the closest assignments, or answer part of the request yourself."
        )
    methods = workflow_methods()
    tasks: list[AgentTask] = []
    seen: set[str] = set()
    for index, item in enumerate(raw, 1):
        if not isinstance(item, dict):
            raise WorkflowRejected(f"Task {index} is not an object.")
        title = str(item.get("title") or "").strip()
        method = str(item.get("method") or "").strip().strip("`")
        body = str(item.get("task") or "").strip()
        if not body:
            raise WorkflowRejected(f"Task {index} has no `task` text.")
        if method not in methods:
            raise WorkflowRejected(
                f"Task {index}: unknown method {method!r}. Methods: {', '.join(methods)}."
            )
        task_id = re.sub(r"[^A-Za-z0-9_-]", "_", str(item.get("id") or f"task_{index}").strip())[:40]
        if not task_id or task_id in seen:
            task_id = f"task_{index}"
        if task_id in seen:
            raise WorkflowRejected(f"Task id {task_id!r} is used twice.")
        seen.add(task_id)
        deps = item.get("depends_on") or []
        if not isinstance(deps, list):
            deps = [deps]
        tasks.append(
            AgentTask(
                id=task_id,
                skill_name=method,
                title=title or body[:60],
                task=body,
                depends_on=[str(d).strip() for d in deps if str(d).strip()],
            )
        )
    ids = {task.id for task in tasks}
    for task in tasks:
        unknown = [d for d in task.depends_on if d not in ids or d == task.id]
        if unknown:
            raise WorkflowRejected(
                f"Task {task.id} depends on {', '.join(unknown)}, which is not another task here."
            )
    # Kahn's algorithm: a cycle leaves tasks that never become ready.
    remaining = {task.id: set(task.depends_on) for task in tasks}
    while remaining:
        ready = [tid for tid, deps in remaining.items() if not deps]
        if not ready:
            raise WorkflowRejected(
                "The dependencies form a cycle: " + ", ".join(sorted(remaining)) + "."
            )
        for tid in ready:
            remaining.pop(tid)
        for deps in remaining.values():
            deps.difference_update(ready)
    return tasks


@dataclass
class TaskResult:
    task: AgentTask
    text: str = ""
    #: completed | truncated | empty | refused | failed | reused | cancelled
    outcome: str = "completed"
    error: str = ""
    tool_count: int = 0
    total_tokens: int = 0
    duration_ms: int = 0


@dataclass
class WorkflowOutcome:
    objective: str
    results: list[TaskResult] = field(default_factory=list)
    duration_ms: int = 0
    cancelled: bool = False

    @property
    def completed(self) -> list[TaskResult]:
        return [r for r in self.results if r.outcome in {"completed", "truncated", "reused"} and r.text.strip()]


def _is_context_overflow(exc: BaseException) -> bool:
    return bool(_CONTEXT_OVERFLOW_RE.search(str(exc)))


def _usage_tokens(usage: Any) -> int:
    if not isinstance(usage, dict):
        return 0
    total = usage.get("total_tokens")
    if isinstance(total, (int, float)) and total:
        return int(total)
    return int(usage.get("input_tokens") or 0) + int(usage.get("output_tokens") or 0)


def _specialist_prompt(task: AgentTask, objective: str, turn: TurnContext, outputs: dict[str, str], tasks: list[AgentTask]) -> str:
    parts: list[str] = []
    if turn.context_tail:
        parts.append(turn.context_tail)
    parts.append(f"# The request this workflow answers\n{objective}")
    parts.append(f"# Your task\n{task.task}")
    upstream: list[str] = []
    for dep_id in task.depends_on:
        dep = next((t for t in tasks if t.id == dep_id), None)
        body = (outputs.get(dep_id) or "").strip()
        if not body:
            continue
        label = f"{dep.title} ({dep.skill_name}, {dep_id})" if dep else dep_id
        upstream.append(f"## Upstream output — {label}\n{body}")
    if upstream:
        parts.append(
            "# Upstream specialist outputs (use as inputs to your own analysis — "
            "do not merely restate or summarize them)\n\n" + "\n\n".join(upstream)
        )
    return "\n\n".join(parts)


def _attachments_for(task: AgentTask, tasks: list[AgentTask], attachments: list) -> list:
    """A task gets the files it names, or every file when no task names any."""
    if not attachments:
        return []

    def names(candidate: AgentTask) -> list:
        text = f"{candidate.title}\n{candidate.task}".lower()
        return [
            path
            for path in attachments
            if path.name.lower() in text or (len(path.stem) >= 4 and path.stem.lower() in text)
        ]

    if not any(names(candidate) for candidate in tasks):
        return list(attachments)
    return names(task)


def _short_query_arg(arguments: dict[str, Any]) -> str:
    """Pull the most descriptive arg (query / keyword / text) and trim it."""
    if not isinstance(arguments, dict):
        return ""
    for key in ("query", "keyword", "law_name", "ad_number", "text", "url"):
        val = arguments.get(key)
        if isinstance(val, str) and val.strip():
            snippet = val.strip().replace("\n", " ")
            return snippet[:60] + ("…" if len(snippet) > 60 else "")
    return ""


# Source-retrieval tools whose calls contribute a "retrieved source" chip to a
# specialist's row. Mirrors the frontend SOURCE_TOOL_NAMES set in
# ``src/lib/toolLabels.ts`` — keep the two in sync.
_SOURCE_RETRIEVAL_TOOLS = {
    "web_search", "web_fetch", "web_search_call", "file_search_call",
    "web_search_tool_result", "web_fetch_tool_result", "retrieve_legal",
    "legal_source_search", "aviation_source_search", "drs_search", "drs_fetch",
    "easa_ad_search", "easa_ad_fetch", "easa_ear_index", "faa_title14_search",
    "ecfr_search", "federal_register_search", "govinfo_search",
    "courtlistener_search", "eurlex_search", "flk_npc_search", "flk_npc_fetch",
    "ccar_search", "ccar_fetch", "caac_local_search", "caac_local_fetch",
}


def _source_chip(data: dict[str, Any]) -> Optional[dict[str, str]]:
    """A retrieved-source chip (``{label, kind}``) for a specialist's row.

    Faithful to what the worker can observe: the provider runs tools inside its
    own loop, so tool *results* never surface here — the query keyword plus the
    source system is the honest signal. Returns ``None`` for non-source tool
    calls or calls without a descriptive query arg.
    """
    # Local tool_call events use "name"; hosted (web_search) events use
    # "tool_name" — accept either so both providers' source calls yield chips.
    name = str(data.get("name") or data.get("tool_name") or "").strip()
    if not name:
        return None
    bare = name.split("__", 1)[0]
    if not (name in _SOURCE_RETRIEVAL_TOOLS or bare in _SOURCE_RETRIEVAL_TOOLS or bare.startswith("pkulaw_")):
        return None
    label = _short_query_arg(data.get("arguments") or {})
    if not label:
        return None
    return {"label": label, "kind": bare}


def run_specialists(
    tasks: list[AgentTask],
    *,
    objective: str,
    turn: TurnContext,
    parent_call_id: Optional[str] = None,
) -> WorkflowOutcome:
    """Run the specialists, honouring dependencies, the concurrency cap and
    rate limits. Blocks until every task has an outcome or the turn is
    cancelled; never raises for a failed specialist (it degrades to a gap)."""
    from .config import override_current_settings
    from .providers import build_provider

    settings = turn.settings
    started = time.monotonic()
    outcome = WorkflowOutcome(objective=objective)
    results: dict[str, TaskResult] = {task.id: TaskResult(task=task) for task in tasks}
    outputs: dict[str, str] = {}
    pending = {task.id: task for task in tasks}
    completed: set[str] = set()

    # A resumed run keeps the research a previous attempt finished: the same
    # method under the same id or, since the author re-plans and may rename a
    # task, the same title.
    unclaimed = dict(turn.resume_tasks)

    def _prior_for(task: AgentTask) -> Optional[dict[str, str]]:
        def usable(prior: Optional[dict[str, str]]) -> bool:
            return bool(prior) and prior.get("skill_name") == task.skill_name and bool((prior.get("text") or "").strip())

        if usable(unclaimed.get(task.id)):
            return unclaimed.pop(task.id)
        title = task.title.strip().lower()
        for prior_id, prior in list(unclaimed.items()):
            if usable(prior) and (prior.get("title") or "").strip().lower() == title:
                return unclaimed.pop(prior_id)
        return None

    for task in tasks:
        prior = _prior_for(task)
        if prior:
            outputs[task.id] = prior["text"]
            results[task.id].text = prior["text"]
            results[task.id].outcome = "reused"
            completed.add(task.id)
            pending.pop(task.id, None)
    reused = sorted(tid for tid, r in results.items() if r.outcome == "reused")
    if reused:
        log_workflow_event("workflow_tasks_reused", {"task_ids": reused, "parent_call_id": parent_call_id})
    for task_id in reused:
        # A reused bundle is finished work: say so, or a client left waiting
        # for its lifecycle shows it as pending forever.
        log_workflow_event(
            "agent_task_finished",
            {
                "task_id": task_id,
                "skill_name": results[task_id].task.skill_name,
                "outcome": "reused",
                "response_chars": len(results[task_id].text),
                "parent_call_id": parent_call_id,
            },
        )

    max_concurrent = max(1, int(settings.effective_max_concurrent_agents()))
    rate_limit_retried: set[str] = set()

    while pending and not turn.cancelled():
        ready_all = [t for t in pending.values() if all(dep in completed for dep in t.depends_on)]
        if not ready_all:  # validate_tasks rules this out; never spin
            for task in pending.values():
                results[task.id].outcome = "failed"
                results[task.id].error = "unresolved dependencies"
            break
        ready = ready_all[:max_concurrent]
        if len(ready_all) > len(ready):
            log_workflow_event(
                "workflow_agent_concurrency_limited",
                {
                    "ready_agent_count": len(ready_all),
                    "started_agent_count": len(ready),
                    "max_concurrent_agents": max_concurrent,
                    "parent_call_id": parent_call_id,
                },
            )
        q: "queue.Queue[tuple[str, str, str]]" = queue.Queue()

        def worker(task: AgentTask) -> None:
            task_token = task_id_var.set(task.id)
            parent_token = parent_call_id_var.set(parent_call_id)
            try:
                with override_current_settings(settings):
                    _run_one(task)
            finally:
                task_id_var.reset(task_token)
                parent_call_id_var.reset(parent_token)

        def _run_one(task: AgentTask) -> None:
            result = results[task.id]
            role_settings, role_fast = settings.for_role("specialist")
            last_error = ""
            for attempt in range(2):
                if turn.cancelled():
                    q.put((task.id, "cancelled", ""))
                    return
                try:
                    provider = build_provider(role_settings, fast=True) if role_fast else build_provider(role_settings)
                    text, status = _attempt(task, provider)
                    q.put((task.id, status, text))
                    return
                except Exception as exc:  # noqa: BLE001 — degrade, never abort the turn
                    if is_out_of_money_error(exc):
                        q.put((task.id, "out_of_money", OUT_OF_MONEY_MESSAGE))
                        return
                    if is_rate_limit_error(exc):
                        log_workflow_event(
                            "agent_task_rate_limited",
                            {"task_id": task.id, "skill_name": task.skill_name, "message": str(exc)[:500]},
                        )
                        q.put((task.id, "rate_limit", ""))
                        return
                    last_error = str(exc)
                    if _is_context_overflow(exc):
                        break  # the same request overflows again
                    if attempt == 0:
                        log_workflow_event(
                            "agent_task_retried",
                            {"task_id": task.id, "skill_name": task.skill_name, "message": last_error[:500]},
                        )
                        if turn.cancel_event is not None:
                            if turn.cancel_event.wait(_RETRY_BACKOFF_SECONDS):
                                q.put((task.id, "cancelled", ""))
                                return
                        else:
                            time.sleep(_RETRY_BACKOFF_SECONDS)
            result.error = last_error[:500] or "specialist failed"
            q.put((task.id, "failed", ""))

        def _attempt(task: AgentTask, provider: Any) -> tuple[str, str]:
            from .agent import SkillAgent
            from .attachments import build_user_content
            from .workflow import author_tool_surface

            result = results[task.id]
            task_start = time.monotonic()
            prompt = _specialist_prompt(task, objective, turn, outputs, tasks)
            files = _attachments_for(task, tasks, list(turn.attachments))
            log_workflow_event(
                "agent_task_started",
                {
                    "task_id": task.id,
                    "skill_name": task.skill_name,
                    "title": task.title,
                    "task": task.task,
                    "depends_on": task.depends_on,
                    "model": getattr(provider, "model", None),
                },
            )
            if turn.run_store is not None:
                turn.run_store.task_started(task.id)
            if task.skill_name == GENERAL_TASK_SKILL:
                agent_token = agent_name_var.set(GENERAL_TASK_SKILL)
                try:
                    events = provider.stream(
                        system=_GENERAL_SPECIALIST_PROMPT,
                        messages=[
                            {
                                "role": "user",
                                "content": build_user_content(
                                    prompt, files, provider.name, cache_prefix=turn.context_block
                                ),
                            }
                        ],
                        tools=author_tool_surface(settings, legal=False, workflow=False),
                        max_iterations=settings.sub_agent_max_iterations,
                    )
                    text, status = _consume(task, events, task_start)
                finally:
                    agent_name_var.reset(agent_token)
            else:
                agent = SkillAgent(task.skill_name, provider, settings)
                text, status = _consume(
                    task, agent.stream(prompt, attachments=files, cache_prefix=turn.context_block), task_start
                )
            if turn.cancelled():
                return text, "cancelled"
            result.duration_ms = int((time.monotonic() - task_start) * 1000)
            return text, status

        def _consume(task: AgentTask, events: Any, task_start: float) -> tuple[str, str]:
            result = results[task.id]
            chunks: list[str] = []
            reasoning: list[str] = []
            sources: list[dict[str, str]] = []
            seen: set[str] = set()
            last_preview = 0.0
            stop = None

            def preview(phase: str, text: str, force: bool = False) -> None:
                nonlocal last_preview
                now = time.monotonic()
                if not force and now - last_preview < _PREVIEW_INTERVAL_S:
                    return
                last_preview = now
                emit_transient(
                    "agent_delta",
                    {
                        "task_id": task.id,
                        "skill_name": task.skill_name,
                        "tail": text[-300:],
                        "total_chars": len(text),
                        "phase": phase,
                        "tool_count": result.tool_count,
                        "elapsed_ms": int((now - task_start) * 1000),
                    },
                )

            for ev in events:
                if turn.cancelled():
                    return "".join(chunks), "cancelled"
                kind = ev.kind
                if kind == "delta":
                    chunks.append(ev.data.get("text", ""))
                    preview("answer", "".join(chunks))
                elif kind == "reasoning_delta":
                    reasoning.append(ev.data.get("text", ""))
                    preview("reasoning", "".join(reasoning))
                elif kind == "done":
                    if not chunks and ev.data.get("text"):
                        chunks.append(ev.data.get("text", ""))
                    result.total_tokens += _usage_tokens(ev.data.get("usage"))
                    stop = ev.data.get("stop_reason")
                elif kind in {"tool_call", "hosted_tool_call"}:
                    data = dict(ev.data)
                    if kind == "hosted_tool_call" and str(data.get("raw_type") or "").endswith("_tool_result"):
                        continue  # Anthropic's result echo is not a second call
                    result.tool_count += 1
                    chip = _source_chip(data)
                    if chip and chip["label"] not in seen:
                        seen.add(chip["label"])
                        sources.append(chip)
                    emit_transient(
                        "agent_delta",
                        {
                            "task_id": task.id,
                            "skill_name": task.skill_name,
                            "tool_count": result.tool_count,
                            "sources": list(sources),
                            "phase": "research",
                            "elapsed_ms": int((time.monotonic() - task_start) * 1000),
                        },
                    )
                elif kind in {"tool_progress", "hosted_tool_progress"}:
                    emit_transient(kind, dict(ev.data))
            text = "".join(chunks)
            if chunks:
                preview("answer", text, force=True)
            if stop == "refusal":
                result.outcome = "refused"
            elif not text.strip():
                result.outcome = "empty"
            else:
                result.outcome = "truncated" if stop == "max_tokens" else "completed"
            result.duration_ms = int((time.monotonic() - task_start) * 1000)
            log_workflow_event(
                "agent_task_finished",
                {
                    "task_id": task.id,
                    "skill_name": task.skill_name,
                    "outcome": result.outcome,
                    "response_chars": len(text),
                    "tool_count": result.tool_count,
                    "total_tokens": result.total_tokens,
                    "duration_ms": result.duration_ms,
                    "sources": sources,
                },
            )
            return text, "done"

        threads = [
            threading.Thread(target=contextvars.copy_context().run, args=(worker, task), daemon=True)
            for task in ready
        ]
        for thread in threads:
            thread.start()

        finished: set[str] = set()
        rate_limited: set[str] = set()
        out_of_money = False
        while len(finished) < len(ready):
            try:
                task_id, status, text = q.get(timeout=0.2)
            except queue.Empty:
                if turn.cancelled():
                    break
                continue
            finished.add(task_id)
            result = results[task_id]
            task = pending[task_id]
            if status == "done":
                outputs[task_id] = text
                result.text = text
                completed.add(task_id)
                if turn.run_store is not None:
                    turn.run_store.task_finished(task_id, text, skill_name=task.skill_name, title=task.title)
            elif status == "failed":
                result.outcome = "failed"
                completed.add(task_id)  # dependents run without it
                log_workflow_event(
                    "agent_task_failed",
                    {
                        "task_id": task_id,
                        "skill_name": task.skill_name,
                        "title": task.title,
                        "message": result.error,
                        "attempts": 2,
                        "parent_call_id": parent_call_id,
                    },
                )
                if turn.run_store is not None:
                    turn.run_store.task_finished(
                        task_id, "", skill_name=task.skill_name, title=task.title, error=result.error
                    )
            elif status == "rate_limit":
                rate_limited.add(task_id)
            elif status == "out_of_money":
                out_of_money = True
            elif status == "cancelled":
                result.outcome = "cancelled"
        for thread in threads:
            thread.join(timeout=1.0 if turn.cancelled() else None)
        if out_of_money:
            raise RuntimeError(OUT_OF_MONEY_MESSAGE)
        if turn.cancelled():
            break
        if rate_limited:
            retryable = rate_limited - rate_limit_retried
            if retryable and max_concurrent > 1:
                rate_limit_retried.update(retryable)
                log_workflow_event(
                    "workflow_agent_concurrency_reduced",
                    {
                        "old_max_concurrent_agents": max_concurrent,
                        "new_max_concurrent_agents": 1,
                        "retry_task_ids": sorted(retryable),
                        "parent_call_id": parent_call_id,
                    },
                )
                max_concurrent = 1
                for task in ready:
                    if task.id not in rate_limited:
                        pending.pop(task.id, None)
                continue
            for task_id in sorted(rate_limited):
                result = results[task_id]
                result.outcome = "failed"
                result.error = "The provider kept rate-limiting after concurrency was reduced."
                completed.add(task_id)
                log_workflow_event(
                    "agent_task_failed",
                    {
                        "task_id": task_id,
                        "skill_name": result.task.skill_name,
                        "title": result.task.title,
                        "message": result.error,
                        "rate_limited": True,
                        "parent_call_id": parent_call_id,
                    },
                )
            log_workflow_event(
                "workflow_agent_rate_limit_degraded",
                {"failed_task_ids": sorted(rate_limited), "parent_call_id": parent_call_id},
            )
        for task in ready:
            pending.pop(task.id, None)

    outcome.cancelled = turn.cancelled()
    outcome.results = [results[task.id] for task in tasks]
    outcome.duration_ms = int((time.monotonic() - started) * 1000)
    failed = [r.task.id for r in outcome.results if r.outcome == "failed"]
    if failed and outcome.completed:
        log_workflow_event(
            "workflow_degraded",
            {
                "failed_task_ids": failed,
                "message": "Some specialist research failed; the answer covers only the completed bundles.",
                "parent_call_id": parent_call_id,
            },
        )
    return outcome


def out_of_scope_items(bundle: str) -> list[str]:
    """The bullets of a bundle's `## Out of scope` section."""
    match = _OUT_OF_SCOPE_RE.search(bundle or "")
    if not match:
        return []
    items = []
    for line in match.group(1).splitlines():
        text = line.strip().lstrip("-*•").strip()
        if text and text.lower() not in {"none", "n/a", "无", "（无）", "(none)"}:
            items.append(text[:300])
    return items[:12]


def _duration(ms: int) -> str:
    seconds = max(0, int(round(ms / 1000)))
    return f"{seconds // 60}m {seconds % 60}s" if seconds >= 60 else f"{seconds}s"


def render_outcome(outcome: WorkflowOutcome) -> str:
    """The tool result the author integrates: outcomes, bundles, open gaps."""
    total = len(outcome.results)
    done = len(outcome.completed)
    lines = [
        f"Workflow finished: {done} of {total} specialists returned research "
        f"({_duration(outcome.duration_ms)})."
        + (" The turn was cancelled before it finished." if outcome.cancelled else "")
    ]
    for r in outcome.results:
        meta = f"{r.outcome}, {r.tool_count} tool calls, {_duration(r.duration_ms)}"
        lines.append(f"- {r.task.id} · {r.task.title} ({r.task.skill_name}): {meta}")
    bundles: list[str] = []
    gaps: list[str] = []
    for r in outcome.results:
        label = f"{r.task.title} ({r.task.skill_name})"
        body = (r.text or "").strip()
        if r.outcome == "failed":
            gaps.append(f"- {label}: the specialist FAILED — {r.error[:300]}")
            continue
        if r.outcome == "refused":
            gaps.append(f"- {label}: the specialist's model declined the task")
            continue
        if r.outcome == "cancelled":
            gaps.append(f"- {label}: cancelled before it finished")
            continue
        if not body:
            gaps.append(f"- {label}: the specialist returned nothing")
            continue
        note = ""
        if r.outcome == "truncated":
            note = " [cut off at the output limit — its last findings may be incomplete]"
            gaps.append(f"- {label}: its bundle was cut off; check the tail before relying on it")
        bundles.append(f"## bundle {r.task.id} — {label}{note}\n\n{body}")
        for item in out_of_scope_items(body):
            gaps.append(f"- {label} left out of scope: {item}")
    lines.append("")
    lines.append(
        "The bundles below are raw research for you to integrate (see \"Integrating "
        "a workflow's bundles\" in your instructions) — never paste them, never name "
        "the specialists to the reader."
    )
    lines.append("")
    lines.append("\n\n".join(bundles) if bundles else "(no bundle completed)")
    if gaps:
        lines.append("")
        lines.append(
            "## Open gaps\nClose each with your own tools if you can; otherwise say "
            "plainly which angles the answer does not cover. Never invent findings for "
            "them.\n" + "\n".join(gaps)
        )
    return "\n".join(lines)


# --- the tool --------------------------------------------------------------------

_TOOL_DESCRIPTION = """Run a multi-agent workflow: up to {max_tasks} specialist sub-agents research separate parts of the request in parallel, each with the research tools and the method you assign; their bundles come back as this tool's result for you to integrate.

Mandatory when the user asks for a workflow or several agents, in any language or phrasing ("use a workflow", "multi-agent", "用工作流", "多智能体") — size it to the task; never judge it too small or do the work inline instead. A workflow that is the request's subject (审批工作流程) or an agent in agency law is not such a request.

Otherwise use it only when the request has several distinct issues that each need sustained research in a different body of law, jurisdiction or evidence base. It costs several times a direct answer: depth alone is not a reason, and never delegate the step your next move depends on.

Tasks: self-contained (a specialist sees the chat digest and files, not your reasoning), non-overlapping, each naming its method and what to return; no synthesis, translation or citation-check task."""


def _input_schema(max_tasks: int) -> dict[str, Any]:
    return {
        "type": "object",
        "properties": {
            "objective": {
                "type": "string",
                "description": (
                    "The question the workflow answers, with the facts and assumptions every specialist needs."
                ),
            },
            "tasks": {
                "type": "array",
                "minItems": 1,
                "maxItems": max_tasks,
                "description": "One entry per specialist.",
                "items": {
                    "type": "object",
                    "properties": {
                        "id": {"type": "string", "description": "Short id; defaults to task_N."},
                        "title": {
                            "type": "string",
                            "description": "3-8 words naming the part this specialist owns.",
                        },
                        "method": {
                            "type": "string",
                            "enum": list(workflow_methods()),
                            "description": "Method from the method list; general-answer for non-legal work.",
                        },
                        "task": {
                            "type": "string",
                            "description": "What to research or produce, with the jurisdictions and facts it needs, and what to return.",
                        },
                        "depends_on": {
                            "type": "array",
                            "items": {"type": "string"},
                            "description": "Ids of tasks whose findings this one needs; usually empty.",
                        },
                    },
                    "required": ["title", "method", "task"],
                },
            },
        },
        "required": ["objective", "tasks"],
    }


def _run_workflow(objective: str, tasks: list[dict[str, Any]]) -> str:
    turn = current_turn()
    if turn is None:
        return "ERROR: run_workflow is only available while answering a chat turn."
    parent_call_id = tool_call_id_var.get()
    limit = max_workflow_tasks(turn.settings)
    try:
        agent_tasks = validate_tasks(tasks, max_tasks=limit)
    except WorkflowRejected as exc:
        log_workflow_event("workflow_task_rejected", {"reason": str(exc), "parent_call_id": parent_call_id})
        return f"ERROR: {exc}"
    objective = (objective or "").strip() or turn.user_message
    for task in agent_tasks:
        turn.note_method(task.skill_name)
    log_workflow_event(
        "workflow_started",
        {
            "parent_call_id": parent_call_id,
            "objective": objective,
            "tasks": [
                {"id": t.id, "title": t.title, "skill_name": t.skill_name, "depends_on": t.depends_on}
                for t in agent_tasks
            ],
            "max_concurrent_agents": turn.settings.effective_max_concurrent_agents(),
        },
    )
    if turn.run_store is not None:
        turn.run_store.record_plan(
            {
                "title": objective[:120],
                "objective": objective,
                "agent_tasks": [t.model_dump() for t in agent_tasks],
            }
        )
    outcome = run_specialists(agent_tasks, objective=objective, turn=turn, parent_call_id=parent_call_id)
    log_workflow_event(
        "workflow_finished",
        {
            "parent_call_id": parent_call_id,
            "completed": len(outcome.completed),
            "failed": sum(1 for r in outcome.results if r.outcome == "failed"),
            "duration_ms": outcome.duration_ms,
            "cancelled": outcome.cancelled,
        },
    )
    return render_outcome(outcome)


def run_workflow_tool(settings: Any) -> Any:
    """The `run_workflow` tool, with its task cap and method enum filled in."""
    limit = max_workflow_tasks(settings)
    return beta_tool(
        _run_workflow,
        name="run_workflow",
        description=_TOOL_DESCRIPTION.format(max_tasks=limit),
        input_schema=_input_schema(limit),
    )


__all__ = [
    "DEFAULT_MAX_WORKFLOW_TASKS",
    "GENERAL_TASK_SKILL",
    "TaskResult",
    "WorkflowOutcome",
    "WorkflowRejected",
    "max_workflow_tasks",
    "out_of_scope_items",
    "render_outcome",
    "run_specialists",
    "run_workflow_tool",
    "validate_tasks",
    "workflow_methods",
]
