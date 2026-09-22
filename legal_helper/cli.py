"""CLI: `python -m legal_helper run | chat | serve`."""

from __future__ import annotations

import argparse
import sys
from pathlib import Path
from typing import Optional
from uuid import uuid4

from rich.console import Console
from rich.markdown import Markdown

from contextlib import contextmanager

from .chat_models import ChatMessage, utc_now
from .config import Settings, load_settings
from .domains import available_packs
from .logging_setup import setup_logging
from .memory import summarize_with_fast_model
from .projects import ProjectStore, active_project
from .workflow import WorkflowExecutor

# Mirrors the server chat path's recent-message lookback
# (server.py: `_RECENT_MESSAGE_LOOKBACK`). This is a pathological-load backstop,
# not the context bound — the digest is bounded by the token budget in
# `workflow._build_context_block`, which scales with the serving model's window.
_RECENT_WINDOW = 400


def _apply_overrides(settings: Settings, args: argparse.Namespace) -> Settings:
    """Apply --project / --domain-pack / --jurisdiction CLI overrides onto a Settings."""
    updates: dict = {}
    # A project pins jurisdiction + packs first; explicit flags override it.
    project = _resolve_project(args)
    if project is not None:
        if project.jurisdiction:
            updates["default_jurisdiction"] = project.jurisdiction
        if project.domain_packs:
            updates["active_domain_packs"] = list(project.domain_packs)
    packs = getattr(args, "domain_pack", None) or []
    if packs:
        # validate against on-disk packs
        valid = set(available_packs())
        unknown = [p for p in packs if p not in valid]
        if unknown:
            raise SystemExit(
                f"unknown domain pack(s): {unknown}; available: {sorted(valid) or '(none)'}"
            )
        updates["active_domain_packs"] = packs
    jurisdiction = getattr(args, "jurisdiction", None)
    if jurisdiction:
        updates["default_jurisdiction"] = jurisdiction
    if not updates:
        return settings
    return settings.model_copy(update=updates)


def _resolve_project(args: argparse.Namespace):
    ref = getattr(args, "project", None)
    if not ref:
        return None
    project = ProjectStore().resolve(ref)
    if project is None:
        raise SystemExit(f"unknown project: {ref!r} (use `project list` to see slugs)")
    return project


@contextmanager
def _project_scope(args: argparse.Namespace):
    """Activate the project (if any) for the duration of an orchestrator run."""
    project = _resolve_project(args)
    with active_project(project.id if project else None):
        yield project


def _stream_workflow_turn(
    console: Console,
    executor: WorkflowExecutor,
    message: str,
    *,
    attachments: Optional[list[Path]] = None,
    recent_messages: Optional[list[ChatMessage]] = None,
    memory_summary: str = "",
    echo_deltas: bool = True,
) -> tuple[Optional[str], bool]:
    """Drive one WorkflowExecutor turn and render its events to the terminal.

    Returns ``(final_text, ok)``. Both CLI commands go through here so they
    ride the same machinery as the web server: routing/complexity dial, plan
    normalization, sub-agent dispatch, and the citation audit.
    """
    final_text: Optional[str] = None
    ok = True
    for ev in executor.stream(
        message,
        attachments=attachments,
        recent_messages=recent_messages,
        memory_summary=memory_summary,
    ):
        if ev.kind == "delta":
            if echo_deltas:
                console.print(ev.data.get("text", ""), end="")
        elif ev.kind == "done":
            final_text = ev.data.get("text", final_text)
            if echo_deltas:
                console.print()
        elif ev.kind == "workflow_plan":
            mode = ev.data.get("execution_mode", "?")
            tasks = ev.data.get("agent_tasks") or []
            note = f"plan: {mode}"
            if tasks:
                note += f", {len(tasks)} specialist task(s)"
            console.print(f"[dim]{note}[/dim]")
        elif ev.kind == "active_domain_packs":
            packs = ", ".join(ev.data.get("packs", []))
            console.print(f"[dim]domain packs: {packs}[/dim]")
        elif ev.kind == "agent_task_started":
            console.print(
                f"[dim]→ task: {ev.data.get('title') or ev.data.get('id')} "
                f"({ev.data.get('skill_name', '?')})[/dim]"
            )
        elif ev.kind == "agent_task_finished":
            console.print(f"[dim]task done: {ev.data.get('title') or ev.data.get('task_id')}[/dim]")
        elif ev.kind in {"agent_task_failed", "agent_task_retried", "workflow_degraded"}:
            detail = ev.data.get("title") or ev.data.get("reason") or ""
            console.print(f"[yellow]{ev.kind}: {detail}[/yellow]")
        elif ev.kind in {"tool_call", "hosted_tool_call"}:
            console.print(f"\n[dim]→ tool: {ev.data.get('name')}[/dim]")
        elif ev.kind == "citation_audit":
            if ev.data.get("skipped"):
                note = ev.data.get("skip_reason") or "skipped"
            else:
                note = "ok" if ev.data.get("ok") else "flagged"
            console.print(f"[dim]citation audit: {note}[/dim]")
        elif ev.kind == "cancelled":
            ok = False
            console.print(f"[yellow]cancelled:[/yellow] {ev.data.get('message', '')}")
        elif ev.kind == "error":
            ok = False
            console.print(f"\n[red]error:[/red] {ev.data.get('message') or ev.data}")
    return final_text, ok


class _ChatSession:
    """In-process REPL chat state: transcript + rolling memory summary.

    Mirrors the server chat path so CLI follow-ups keep their context: the
    same WorkflowExecutor drives each turn, the last ``_RECENT_WINDOW``
    messages ride along as ``recent_messages``, and older turns are folded
    into the rolling summary before they drop out of that window.
    """

    def __init__(self, settings: Settings, executor: Optional[WorkflowExecutor] = None) -> None:
        self.settings = settings
        self.executor = executor or WorkflowExecutor(settings=settings)
        self.history: list[ChatMessage] = []
        self.memory_summary = ""
        self._chat_id = f"cli-{uuid4().hex[:8]}"

    def _record(self, role: str, content: str) -> None:
        self.history.append(
            ChatMessage(
                id=f"{self._chat_id}-{len(self.history)}",
                chat_id=self._chat_id,
                role=role,  # type: ignore[arg-type]
                content=content,
                created_at=utc_now(),
            )
        )

    def turn(self, console: Console, line: str) -> tuple[Optional[str], bool]:
        final_text, ok = _stream_workflow_turn(
            console,
            self.executor,
            line,
            recent_messages=self.history[-_RECENT_WINDOW:],
            memory_summary=self.memory_summary,
        )
        self._record("user", line)
        if final_text:
            self._record("assistant", final_text)
        # Until the transcript exceeds the recent window every message is
        # passed verbatim, so the summary would add nothing; from the first
        # full window on, fold turns in before they scroll out (lossless).
        if ok and len(self.history) >= _RECENT_WINDOW:
            self.memory_summary = summarize_with_fast_model(
                self.settings, self.history, self.memory_summary
            )
        return final_text, ok


def _outputs_snapshot(settings: Settings) -> set[Path]:
    if not settings.outputs_dir.exists():
        return set()
    return {p for p in settings.outputs_dir.rglob("*") if p.is_file()}


def _cmd_run(args: argparse.Namespace) -> int:
    settings = _apply_overrides(load_settings(), args)
    setup_logging(settings)
    console = Console()
    with _project_scope(args) as project:
        if project is not None:
            console.print(f"[dim]project: {project.slug} ({project.name})[/dim]")
        executor = WorkflowExecutor(settings=settings)
        attachments = [Path(p) for p in (args.input or [])]
        before = _outputs_snapshot(settings)
        # Deltas are suppressed while streaming so the final answer renders
        # once as Markdown, matching the previous `run` output shape.
        final_text, ok = _stream_workflow_turn(
            console,
            executor,
            args.task,
            attachments=attachments,
            echo_deltas=False,
        )
        new_files = sorted(_outputs_snapshot(settings) - before)
    console.print(Markdown(final_text or "(no response)"))
    if new_files:
        console.print("\n[bold]Artifacts[/bold]:")
        for p in new_files:
            console.print(f" • {p}")
    return 0 if ok else 1


def _cmd_chat(args: argparse.Namespace) -> int:
    settings = _apply_overrides(load_settings(), args)
    setup_logging(settings)
    session = _ChatSession(settings)
    console = Console()
    pack_note = f", packs={settings.active_domain_packs}" if settings.active_domain_packs else ""
    console.print(
        f"[bold cyan]Legal AI Helper[/bold cyan]  "
        f"(provider={settings.provider}, model={session.executor.provider.model}, "
        f"jurisdiction={settings.default_jurisdiction}{pack_note})"
    )
    console.print("[dim]Not legal advice. Type /exit to quit.[/dim]\n")
    with _project_scope(args) as project:
        if project is not None:
            console.print(f"[dim]project: {project.slug} ({project.name})[/dim]\n")
        while True:
            try:
                line = console.input("[bold green]you[/bold green] » ").strip()
            except (EOFError, KeyboardInterrupt):
                console.print()
                return 0
            if not line:
                continue
            if line in {"/exit", "/quit", "/q"}:
                return 0
            try:
                session.turn(console, line)
            except Exception as e:
                console.print(f"\n[red]error:[/red] {e}\n")
                continue
            console.print()


def _cmd_serve(args: argparse.Namespace) -> int:
    import uvicorn

    from .documents.graph_layout import unresolvable_render_binaries

    settings = _apply_overrides(load_settings(), args)
    setup_logging(settings)
    # A missing render binary is silent at author time and shows up much later
    # as a diagram that simply never appeared, so say so while someone is
    # watching the log.
    for problem in unresolvable_render_binaries():
        print(f"warning: {problem}", flush=True)
    uvicorn.run(
        "legal_helper.server:app",
        host=args.host,
        port=args.port,
        reload=args.reload,
    )
    return 0


def _cmd_project(args: argparse.Namespace) -> int:
    setup_logging(load_settings())
    store = ProjectStore()
    console = Console()
    action = args.project_action

    if action == "list":
        projects = store.list_projects(include_archived=args.all)
        if not projects:
            console.print("[dim]no projects yet — create one with `project new <name>`[/dim]")
            return 0
        for p in projects:
            packs = f" packs={p.domain_packs}" if p.domain_packs else ""
            juris = f" {p.jurisdiction}" if p.jurisdiction else ""
            console.print(f"[bold]{p.slug}[/bold]{juris}{packs}  — {p.name}  [dim]({p.status})[/dim]")
        return 0

    if action == "new":
        brief = ""
        if args.brief_file:
            brief = Path(args.brief_file).read_text(encoding="utf-8")
        elif args.brief:
            brief = args.brief
        p = store.create_project(
            args.name, slug=args.slug, brief=brief,
            jurisdiction=args.jurisdiction, domain_packs=args.domain_pack or None,
        )
        console.print(f"created project [bold]{p.slug}[/bold] ({p.name})")
        return 0

    # remaining actions operate on a named project
    project = store.resolve(args.project) if getattr(args, "project", None) else None
    if project is None:
        raise SystemExit("this action needs --project <slug>")

    if action == "show":
        console.print(f"[bold]{project.name}[/bold]  ({project.slug})")
        console.print(f"jurisdiction: {project.jurisdiction or '-'}  packs: {project.domain_packs}")
        if project.brief:
            console.print("\n[bold]Brief[/bold]:\n" + project.brief)
        if project.summary:
            console.print("\n[bold]Summary[/bold]:\n" + project.summary)
        mem = store.list_memory(project.id, status="open")
        if mem:
            console.print("\n[bold]Open memory[/bold]:")
            for m in mem:
                console.print(f"  [s{m.salience}] ({m.kind}) {m.title}")
        chats = store.list_project_chats(project.id)
        if chats:
            console.print(f"\n[bold]Chats[/bold]: {len(chats)}")
        return 0

    if action == "remember":
        mem = store.add_memory(
            project.id, args.kind, args.title, args.body or "", salience=args.salience,
        )
        console.print(f"remembered [{args.kind}] {mem.title} (s{mem.salience})")
        return 0

    if action == "brief":
        if args.set_file:
            store.update_project(project.id, brief=Path(args.set_file).read_text(encoding="utf-8"))
            console.print("brief updated")
        else:
            console.print(project.brief or "[dim](no brief)[/dim]")
        return 0

    if action == "compact":
        updated = store.compact(project.id)
        console.print("compacted. summary:\n" + (updated.summary or "[dim](empty)[/dim]"))
        return 0

    raise SystemExit(f"unknown project action: {action}")


def _add_common_overrides(p: argparse.ArgumentParser) -> None:
    p.add_argument(
        "--project",
        help="Run under a project (slug or id) — injects its brief + memory and "
        "pins its jurisdiction/packs",
    )
    p.add_argument(
        "--domain-pack",
        action="append",
        help="Activate a domain pack (repeatable); e.g. --domain-pack aviation",
    )
    p.add_argument(
        "--jurisdiction",
        choices=["CN", "US", "EU", "UK", "HK", "ET", "ICAO"],
        help="Override the default jurisdiction for this run",
    )


def _add_project_parser(sub) -> None:
    project = sub.add_parser("project", help="Manage durable projects (cross-chat context)")
    psub = project.add_subparsers(dest="project_action", required=True)

    psub.add_parser("list", help="List projects").add_argument(
        "--all", action="store_true", help="Include archived"
    )

    new = psub.add_parser("new", help="Create a project")
    new.add_argument("name")
    new.add_argument("--slug")
    new.add_argument("--brief")
    new.add_argument("--brief-file")
    new.add_argument("--jurisdiction", choices=["CN", "US", "EU", "UK", "HK", "ET", "ICAO"])
    new.add_argument("--domain-pack", action="append")

    show = psub.add_parser("show", help="Show a project")
    show.add_argument("--project", required=True)

    remember = psub.add_parser("remember", help="Add a durable memory entry")
    remember.add_argument("--project", required=True)
    remember.add_argument(
        "--kind", required=True,
        choices=["fact", "decision", "task", "open_question", "glossary", "artifact", "risk", "source"],
    )
    remember.add_argument("--title", required=True)
    remember.add_argument("--body", default="")
    remember.add_argument("--salience", type=int, default=3)

    brief = psub.add_parser("brief", help="Show or set the project brief")
    brief.add_argument("--project", required=True)
    brief.add_argument("--set-file", help="Replace the brief from a markdown file")

    compact = psub.add_parser("compact", help="Compact memory + chats into the rolling summary")
    compact.add_argument("--project", required=True)

    project.set_defaults(func=_cmd_project)


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        prog="legal-helper",
        description="Legal AI helper agent (Claude + GPT, sub-agent runtime, domain packs).",
    )
    sub = parser.add_subparsers(dest="cmd", required=True)

    run = sub.add_parser("run", help="Run one orchestrator turn and exit")
    run.add_argument("--task", required=True, help="The user instruction")
    run.add_argument("--input", action="append", help="Attachment path (repeatable)")
    _add_common_overrides(run)
    run.set_defaults(func=_cmd_run)

    chat = sub.add_parser("chat", help="Interactive REPL with streaming")
    _add_common_overrides(chat)
    chat.set_defaults(func=_cmd_chat)

    serve = sub.add_parser("serve", help="Run the FastAPI server + web UI")
    serve.add_argument("--host", default="0.0.0.0")
    serve.add_argument("--port", type=int, default=8010)
    serve.add_argument("--reload", action="store_true")
    _add_common_overrides(serve)
    serve.set_defaults(func=_cmd_serve)

    _add_project_parser(sub)

    return parser


def main(argv: list[str] | None = None) -> int:
    parser = build_parser()
    args = parser.parse_args(argv)
    return args.func(args)


if __name__ == "__main__":
    sys.exit(main())
