"""``run_procedure``: run an installed external procedure other than the deck skill.

``create_deck`` drives ppt-master. Any other external skill — a GB/T 9704 公文
generator such as gongwen-gbt9704-skill, with its red-head 版头, 发文字号 and
版记 — is driven here the same way: a sub-agent follows the procedure under
the host runtime contract (``agent._external_skill_prompt``) in a fresh work
directory under ``<outputs_dir>/_procedures/``, out of the chat's file list,
and only the files it names as deliverables are copied out and reported.

It is on the author's surface only while at least one such procedure is
installed: an always-present tool that can only answer "nothing installed" is
schema budget spent on every turn for nothing.
"""

from __future__ import annotations

import copy
import json
import re
import shutil
import time
import uuid
from datetime import datetime
from pathlib import Path
from typing import Any, Optional

from anthropic import beta_tool
from anthropic.lib.tools._beta_functions import BetaFunctionTool

from ..config import Settings, current_settings, override_current_settings
from ..logging_setup import log_workflow_event, sub_agent_scope
from .decks import DECK_SKILL, decisions_under_delegation

#: Under ``outputs_dir`` but out of the user's file list: the server's artifact
#: scan skips any path component that starts with an underscore.
WORKSPACE_DIRNAME = "_procedures"

#: What counts as a deliverable when the report names none.
_DELIVERABLE_SUFFIXES = frozenset({".docx", ".pdf", ".xlsx", ".pptx", ".odt", ".ofd"})
_DELIVERABLES_RE = re.compile(
    r"^#{1,6}\s*Deliverables\s*$(.*?)(?=^#{1,6}\s|\Z)", re.IGNORECASE | re.MULTILINE | re.DOTALL
)
_PATH_RE = re.compile(r"(/[^\s`'\"<>|*]+\.[A-Za-z0-9]{2,5})(?![\w.])")


def installed_procedures() -> dict[str, Path]:
    """External skills this tool can run: every one discovered except the deck skill."""
    from ..skills import discover_skills, is_external_skill

    return {
        name: path
        for name, path in sorted(discover_skills().items())
        if name != DECK_SKILL and is_external_skill(name)
    }


def _describe(name: str) -> str:
    from ..skills import skill_manifest

    try:
        description = str(skill_manifest(name).get("description") or "").strip()
    except Exception:  # noqa: BLE001 — an unreadable manifest still lists the name
        description = ""
    return f"{name}: {description}" if description else name


def _new_workspace(settings: Settings, skill: str, title: str) -> Path:
    from .documents import _safe_filename

    slug = _safe_filename(title, default=skill).replace(" ", "_")[:48] or skill
    stamp = datetime.now().strftime("%Y%m%d-%H%M%S")
    workspace = settings.outputs_dir / WORKSPACE_DIRNAME / f"{slug}-{stamp}-{uuid.uuid4().hex[:6]}"
    workspace.mkdir(parents=True, exist_ok=True)
    return workspace.resolve()


def compose_procedure_task(
    *, skill: str, title: str, brief: str, brief_file: Path, sources: list[Path], workspace: Path
) -> str:
    """The sub-agent sees this task and nothing of the chat."""
    lines = [
        "# Request",
        "",
        f"Use {skill} to produce: {title}",
        "",
        "## Where to work",
        "",
        f"- Work directory, the only place you may write: `{workspace}`. Give every command "
        "absolute paths under it, including its output path.",
        "",
        "## Content",
        "",
        f"The complete content is below and also saved at `{brief_file}`. It is a closed "
        "corpus: every fact, date, name, number and citation in the document comes from it or "
        "from the source files listed here — add nothing from elsewhere.",
    ]
    if sources:
        lines += ["", "Source files:"]
        lines += [f"- `{p}`" for p in sources]
    lines += ["", "<brief>", brief.strip(), "</brief>", ""]
    lines += [
        "## Delivery",
        "",
        "- Run the procedure's own checks where it says to, and fix what they report.",
        "- Finish with a `## Deliverables` section listing the absolute path of each file the "
        "user should receive, one per line, then the `## Decisions under delegation` section.",
    ]
    return "\n".join(lines)


def run_procedure_agent(skill: str, task: str, settings: Settings) -> str:
    """Run the procedure to completion in a forked sub-agent on the chat's model.

    No round cap, for the reason ``decks.run_deck_agent`` gives.
    """
    from ..agent import SkillAgent
    from ..providers import build_provider

    with override_current_settings(settings):
        provider = build_provider(settings)
        return SkillAgent(skill, provider, settings).execute(task)


def find_deliverables(report: str, workspace: Path, exclude: set[Path], since: float) -> list[Path]:
    """The documents to hand over: those the report lists, else those made during the run.

    Only documents inside the work directory count. The sub-agent cannot write
    anywhere else, so a path outside it is a quoted input; and a procedure's
    Markdown source, decision log and preview images are its working files —
    the first live 公文 run listed all of them and they landed in the chat's
    file list beside the one .docx the user asked for.
    """
    workspace = workspace.resolve()

    def usable(path: Path) -> bool:
        try:
            resolved = path.resolve()
        except OSError:
            return False
        return (
            resolved.is_file()
            and resolved.suffix.lower() in _DELIVERABLE_SUFFIXES
            and resolved not in exclude
            and workspace in resolved.parents
        )

    named: list[Path] = []
    section = _DELIVERABLES_RE.search(report or "")
    for raw in _PATH_RE.findall(section.group(1) if section else ""):
        path = Path(raw)
        if usable(path) and path.resolve() not in named:
            named.append(path.resolve())
    if named:
        return named
    made = [p.resolve() for p in workspace.rglob("*") if usable(p) and p.stat().st_mtime >= since]
    return sorted(made, key=lambda p: p.stat().st_mtime, reverse=True)


def _deliver(path: Path, settings: Settings) -> Path:
    """Copy ``path`` to ``outputs_dir`` under its own name, never over an existing file."""
    settings.outputs_dir.mkdir(parents=True, exist_ok=True)
    target = settings.outputs_dir / path.name
    counter = 2
    while target.exists():
        target = settings.outputs_dir / f"{path.stem}_{counter}{path.suffix}"
        counter += 1
    shutil.copy2(path, target)
    return target.resolve()


def _lint(path: Path) -> dict[str, Any]:
    from ..documents.quality import lint_artifact

    try:
        payload = lint_artifact(path).to_payload()
    except Exception as exc:  # noqa: BLE001 — a linter never fails a delivery
        return {"error": f"{type(exc).__name__}: {exc}"}
    payload.pop("metrics", None)
    return payload


def _excerpt(text: str, head: int = 300, tail: int = 900) -> str:
    text = (text or "").strip()
    return text if len(text) <= head + tail else f"{text[:head]}\n…\n{text[-tail:]}"


def _result(payload: dict[str, Any]) -> str:
    return json.dumps(payload, ensure_ascii=False, indent=2)


@beta_tool
def run_procedure(skill: str, brief: str, title: str, source_paths: list[str] | None = None) -> str:
    """Make a document with an installed external procedure (e.g. a red-head 公文). Returns JSON.

    For a document the built-in writers cannot make — a GB/T 9704 正式发文 with
    版头, 发文字号 and 版记, for instance. Decks are made with create_deck. The
    procedure runs in a sub-agent that follows its own workflow and checks and
    does not see this conversation, so ``brief`` must carry the full text and
    every element the document needs (发文机关, 文号, 签发人, 主送, 正文, 附件,
    成文日期, 抄送, 印发机关 …).

    Args:
        skill: Name of the installed procedure.
        brief: The complete content and every required element, in the document's language.
        title: Title of the document.
        source_paths: Absolute paths of uploaded files the document draws on.
    """
    started = time.monotonic()
    settings = current_settings()
    installed = installed_procedures()
    skill = (skill or "").strip()
    if skill == DECK_SKILL:
        return _result({"status": "failed", "files": [], "next_step": "Decks are made with create_deck."})
    if skill not in installed:
        return _result({
            "status": "unavailable",
            "files": [],
            "installed": [_describe(name) for name in installed],
            "next_step": (
                f"No procedure named {skill!r} is installed. Use one of `installed`, or make the "
                "document with the built-in writers and tell the user what they lack."
            ),
        })
    brief = (brief or "").strip()
    if not brief:
        return _result({
            "status": "failed",
            "files": [],
            "next_step": "The brief is empty; the sub-agent cannot see this conversation.",
        })
    sources: list[Path] = []
    missing: list[str] = []
    for raw in source_paths or []:
        path = Path(raw).expanduser()
        path = (path if path.is_absolute() else settings.project_root / path).resolve()
        if path.is_file():
            sources.append(path)
        else:
            missing.append(raw)
    if missing:
        return _result({
            "status": "failed",
            "files": [],
            "next_step": "These input files do not exist: " + ", ".join(map(str, missing)),
        })

    title = (title or "").strip() or skill
    workspace = _new_workspace(settings, skill, title)
    brief_file = workspace / "brief.md"
    brief_file.write_text(f"# {title}\n\n{brief}\n", encoding="utf-8")
    task = compose_procedure_task(
        skill=skill, title=title, brief=brief, brief_file=brief_file, sources=sources, workspace=workspace
    )
    since = time.time()
    report, error = "", ""
    with sub_agent_scope(skill):
        log_workflow_event("procedure_started", {"skill": skill, "title": title, "workspace": str(workspace)})
        try:
            report = run_procedure_agent(skill, task, settings.model_copy(update={"outputs_dir": workspace})) or ""
        except Exception as exc:  # noqa: BLE001 — a failed run is reported, not raised
            error = f"{type(exc).__name__}: {exc}"
    elapsed = round(time.monotonic() - started, 1)
    exclude = {p.resolve() for p in sources} | {brief_file.resolve()}
    made = find_deliverables(report, workspace, exclude, since)
    decisions = decisions_under_delegation(report)
    if not made:
        log_workflow_event("procedure_finished", {"skill": skill, "status": "failed", "elapsed_s": elapsed})
        return _result({
            "status": "failed",
            "files": [],
            "elapsed_s": elapsed,
            "work_dir": str(workspace),
            "decisions_under_delegation": decisions,
            **({"error": error} if error else {}),
            "sub_agent_report": _excerpt(report),
            "next_step": "The procedure produced no document. Tell the user what stopped it.",
        })

    files = []
    for path in made:
        delivered = _deliver(path, settings)
        try:
            from ..documents.lineage import record_derivation

            record_derivation(delivered, sources, "run_procedure")
        except Exception:  # noqa: BLE001 — lineage is advisory
            pass
        files.append({"path": str(delivered), "lint_summary": _lint(delivered)})
    log_workflow_event(
        "procedure_finished",
        {"skill": skill, "status": "ok", "elapsed_s": elapsed, "files": [f["path"] for f in files]},
    )
    return _result({
        "status": "ok",
        "files": files,
        "elapsed_s": elapsed,
        "decisions_under_delegation": decisions,
        "sub_agent_report": _excerpt(report),
        "next_step": (
            "Give the user each path. render_docx_pages / render_pdf_pages show the pages if "
            "you want to look first; report the decisions made under delegation."
        ),
    })


def procedure_tool() -> Optional[BetaFunctionTool]:
    """``run_procedure`` with the installed procedures in its schema, or None.

    The names go into an enum and the description, so the model never has to
    discover them: with only a free-text ``skill``, luna called the tool with
    ``"unknown"`` mid-way through an unrelated EU 261 answer to see what it did.
    """
    installed = installed_procedures()
    if not installed:
        return None
    schema = copy.deepcopy(run_procedure.input_schema)
    schema["properties"]["skill"]["enum"] = list(installed)
    listing = "\n".join(f"- {_describe(name)}" for name in installed)
    return BetaFunctionTool(
        run_procedure.func,
        name=run_procedure.name,
        description=f"{run_procedure.description}\n\nInstalled procedures:\n{listing}",
        input_schema=schema,
    )


__all__ = [
    "compose_procedure_task",
    "find_deliverables",
    "installed_procedures",
    "procedure_tool",
    "run_procedure",
    "run_procedure_agent",
]
