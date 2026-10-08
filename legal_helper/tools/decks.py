"""create_deck: the one way any agent in this harness makes a slide deck.

Decks are made by ppt-master, an external procedural skill (MIT, maintained
upstream), never by an in-tree writer. The two in-tree paths this replaced — a
ten-layout pptxgenjs template and an HTML→PPTX converter — were each a
narrower reimplementation of what ppt-master already does with its own
planning, its own native-shape authoring and its own blocking quality gate;
the HTML one was the documented default and was never called once.

A generic skill dispatcher could already drive the skill, but nothing a deck
needs was in it: where the project goes, what to pass in, how to tell a
delivered deck from a sub-agent that stopped half way, and what the user sees
during a run that takes many minutes. This tool owns exactly that:

- **A workspace the user's file list never shows.** A ppt-master project is
  dozens of files (one SVG per page twice over, validation reports, a backup,
  notes). The server registers every new file under ``outputs_dir`` as a chat
  artifact except under a path component starting with ``_`` or ``.``, so the
  project lives under ``<outputs_dir>/_decks/`` and only the exported
  ``.pptx`` is copied out beside the chat's other deliverables.
- **A self-contained task.** The sub-agent does not see the conversation, so
  the brief is written to the workspace and named as the deck's closed corpus.
- **Verification by this tool, not by the sub-agent's word.** The export is
  found on disk, opened with python-pptx, counted, linted, and matched to
  ppt-master's own postflight report before anything is called done.
- **Progress while it runs.** One ``deck_progress`` event per sub-agent tool
  call (page authored, gate run, export), through the same event sink that
  carries every other workflow event to the stream.

There is deliberately no fallback writer: when ppt-master is not installed the
tool says so and says how to install it.
"""

from __future__ import annotations

import json
import re
import shutil
import threading
import time
import uuid
from datetime import datetime
from pathlib import Path
from typing import Any, Callable, Optional

from anthropic import beta_tool

from ..config import Settings, current_settings, override_current_settings
from ..logging_setup import log_workflow_event, workflow_event_sink_var

#: The external skill that makes every deck.
DECK_SKILL = "ppt-master"

INSTALL_HINT = (
    "ppt-master is not installed under any configured skill root, so no deck can be made. "
    "Tell the user the deck could not be made, and do not substitute another format. "
    "The operator installs the official distribution with its .git kept for updates: "
    "`git clone https://github.com/hugohe3/ppt-master <src>/ppt-master` and "
    "`ln -s <src>/ppt-master/skills/ppt-master ~/.claude/skills/ppt-master` "
    "(or a directory named in LEGAL_HELPER_SKILL_ROOTS), then installs its Python "
    "requirements into the harness env (CLAUDE.md, Environment)."
)

#: Directories inside a ppt-master project or round-trip workspace that hold
#: inputs or intermediates rather than a deliverable: a copied source deck in
#: ``sources/`` is a .pptx too, and must never be mistaken for the export.
_NOT_DELIVERABLE_DIRS = frozenset(
    {"sources", "templates", "analysis", "native-payloads", "backup", "imports", "validation"}
)

_POSTFLIGHT_PASSED = frozenset({"passed", "passed-with-warnings"})

_HAN_RE = re.compile(r"[\u3400-\u4dbf\u4e00-\u9fff\uf900-\ufaff]")
_PPTX_PATH_RE = re.compile(r"(/[^\s`'\"<>|*]+?\.pptx)(?![\w.])")
_DECISIONS_RE = re.compile(
    r"^#{1,6}\s*Decisions under delegation\s*$(.*?)(?=^#{1,6}\s|\Z)",
    re.IGNORECASE | re.MULTILINE | re.DOTALL,
)


def _deck_skill_dir() -> Optional[Path]:
    """The resolved ppt-master directory, or None when it is not discoverable."""
    from ..skills import discover_skills, is_external_skill

    found = discover_skills().get(DECK_SKILL)
    if found is None or not is_external_skill(DECK_SKILL):
        return None
    return found


def deck_language(text: str) -> str:
    """BCP-47 tag for the deck when the caller gave none: the brief's language.

    A share rather than "any Han character": an English brief quoting one
    Chinese statute title is still an English deck.
    """
    han = len(_HAN_RE.findall(text or ""))
    latin_words = len(re.findall(r"[A-Za-z]+", text or ""))
    return "zh-CN" if han and han >= latin_words else "en"


#: Under ``outputs_dir`` but out of the user's file list: the server's artifact
#: scan skips any path component that starts with an underscore.
WORKSPACE_DIRNAME = "_decks"


def _slug(text: str, default: str = "deck") -> str:
    from .documents import _safe_filename

    return _safe_filename(text, default=default).replace(" ", "_")[:48] or default


def _new_workspace(settings: Settings, title: str) -> Path:
    """A fresh directory for one deck project, under ``<outputs_dir>/_decks/``.

    Unique per call, so a second deck the same day never trips ppt-master's
    "project directory already exists" refusal or picks up the previous run's
    export as this one's.
    """
    stamp = datetime.now().strftime("%Y%m%d-%H%M%S")
    workspace = settings.outputs_dir / WORKSPACE_DIRNAME / f"{_slug(title)}-{stamp}-{uuid.uuid4().hex[:6]}"
    workspace.mkdir(parents=True, exist_ok=True)
    return workspace.resolve()


def _resolve_input(raw: str, settings: Settings) -> Path:
    path = Path(raw).expanduser()
    if not path.is_absolute():
        path = settings.project_root / path
    return path.resolve()


def compose_deck_task(
    *,
    title: str,
    language: str,
    audience: str,
    purpose: str,
    page_count: int,
    quick: bool,
    workspace: Path,
    brief_file: Path,
    brief: str,
    sources: list[Path],
    template: Optional[Path],
) -> str:
    """The sub-agent's whole world: it sees this task and nothing of the chat.

    Everything here is information only the host has — the content, the user's
    stated constraints, and where in this filesystem work may be written. How to
    make the deck is ppt-master's procedure, which the sub-agent is already
    following; nothing in this text second-guesses it.
    """
    lines = [
        "# Deck request",
        "",
        "Make one presentation with ppt-master and deliver it as a single .pptx file.",
        "",
        f"- Title: {title}",
        f"- Deck language: `{language}` — every word on the slides is in this language.",
        f"- Audience: {audience.strip() or 'not stated; infer it from the content below'}",
        f"- Purpose: {purpose.strip() or 'not stated; infer it from the content below'}",
        (
            f"- Length: about {page_count} slides."
            if page_count > 0
            else "- Length: not fixed by the user; decide it from the content."
        ),
    ]
    if quick:
        lines.append(
            "- Pace: the user wants this fast — quick generation, skipping the strategy "
            "and confirmation stages."
        )
    if template is not None:
        lines.append(
            f"- Template: `{template}` — the user's own deck/template. The new deck keeps its "
            "design and carries the content below."
        )
    lines += [
        "",
        "## Where to work",
        "",
        f"- Work directory, the only place you may write: `{workspace}`",
        f"- Create every project or workspace inside it. `project_manager.py init` defaults to a "
        f"`projects/` folder inside the skill's own installation, which is read-only here, so "
        f"always pass `--dir {workspace}`; give every other command absolute paths under the "
        "work directory.",
        "",
        "## Content",
        "",
        f"The complete content is below and is also saved at `{brief_file}`; import that file "
        "as the project's source. It is a closed corpus: every fact, figure, date and citation "
        "on a slide comes from it or from the source files listed here — add nothing from "
        "elsewhere, and keep citations with their pinpoints exactly as written.",
    ]
    if sources:
        lines += ["", "Source files the deck also draws on:"]
        lines += [f"- `{p}`" for p in sources]
    lines += ["", "<brief>", brief.strip(), "</brief>", ""]
    lines += [
        "## Delivery",
        "",
        "- Export exactly one .pptx with ppt-master's own exporter, after its final quality "
        "gate passes.",
        "- Finish with the absolute path of the exported .pptx, the verdict of the last "
        "quality gate, and the `## Decisions under delegation` section.",
    ]
    return "\n".join(lines)


# ---------------------------------------------------------------------------
# Progress
# ---------------------------------------------------------------------------

#: ppt-master script → the step it is, as the reader would name it.
_SCRIPT_STEPS = {
    "attribution_guard.py": "integrity_check",
    "source_to_md.py": "source_conversion",
    "text_measure.py": "calibration",
    "svg_quality_checker.py": "quality_gate",
    "finalize_svg.py": "finalize",
    "svg_to_pptx.py": "export",
    "total_md_split.py": "notes",
    "pptx_to_svg.py": "template_import",
    "analyze_images.py": "images",
    "image_search.py": "images",
    "image_gen.py": "images",
    "icon_sync.py": "icons",
}
_READ_TOOLS = frozenset(
    {
        "read_document",
        "list_skill_dir",
        "list_skill_references",
        "read_skill_reference",
    }
)
_LOOK_TOOLS = frozenset({"render_pptx_slides", "render_pdf_pages", "render_docx_pages", "view_image", "inspect_pptx"})
_EXIT_CODE_RE = re.compile(r'"exit_code":\s*(-?\d+|null)')


def classify_deck_call(tool: str, args: dict[str, Any]) -> tuple[str, str]:
    """``(step, detail)`` for one sub-agent tool call."""
    if tool == "write_text_file":
        path = str(args.get("path") or "").replace("\\", "/")
        name = Path(path).name
        if path.endswith(".svg") and "/svg_output/" in f"/{path}":
            return "page_authored", name
        return "plan_written", name
    if tool == "run_skill_script":
        script = Path(str(args.get("script") or "")).name
        argv = [str(a) for a in (args.get("args") or [])]
        step = _SCRIPT_STEPS.get(script, "script")
        if script == "project_manager.py" and argv:
            step = "project_" + argv[0].replace("-", "_")
        if script == "svg_quality_checker.py" and "--stage" in argv:
            index = argv.index("--stage")
            if index + 1 < len(argv):
                return step, f"{script} ({argv[index + 1]})"
        return step, script
    if tool in _READ_TOOLS:
        return "reading", Path(str(args.get("path") or args.get("subpath") or args.get("name") or "")).name
    if tool in _LOOK_TOOLS:
        return "looking", Path(str(args.get("path") or "")).name
    return "other", tool


class DeckProgress:
    """Event sink that turns the deck sub-agent's tool calls into progress.

    Installed over whatever sink is active (the server's SSE emitter, or none
    on the CLI) and forwarding everything to it, so the sub-agent's own tool
    events still reach the stream unchanged. Started/finished events are paired
    per tool name in order, which holds whether the provider runs a round's
    calls one after another or side by side.
    """

    def __init__(
        self,
        forward: Optional[Callable[[dict[str, Any]], None]],
        *,
        agent: str = DECK_SKILL,
        target_pages: int = 0,
    ) -> None:
        self._forward = forward
        self._agent = agent
        self._target = target_pages if target_pages > 0 else None
        self._lock = threading.Lock()
        self._pending: dict[str, list[dict[str, Any]]] = {}
        self._started = time.monotonic()
        self.calls = 0
        self.pages: list[str] = []

    def __call__(self, record: dict[str, Any]) -> None:
        if self._forward is not None:
            self._forward(record)
        if record.get("agent") != self._agent:
            return
        event = record.get("event_type")
        data = record.get("data") or {}
        tool = str(data.get("tool_name") or "")
        if not tool:
            return
        if event == "local_tool_call_started":
            with self._lock:
                self._pending.setdefault(tool, []).append(dict(data.get("arguments") or {}))
            return
        if event != "local_tool_call_finished":
            return
        with self._lock:
            queue = self._pending.get(tool) or []
            args = queue.pop(0) if queue else {}
            self.calls += 1
            step, detail = classify_deck_call(tool, args)
            if step == "page_authored" and detail not in self.pages:
                self.pages.append(detail)
            calls, pages = self.calls, len(self.pages)
        preview = str(data.get("output_preview") or "")
        exit_code: Optional[int] = None
        if tool == "run_skill_script":
            match = _EXIT_CODE_RE.search(preview)
            if match and match.group(1) != "null":
                exit_code = int(match.group(1))
            ok = exit_code == 0
        else:
            ok = not preview.startswith("ERROR")
        message = f"{step.replace('_', ' ')}: {detail}" if detail else step.replace("_", " ")
        if exit_code is not None:
            message += f" (exit {exit_code})"
        log_workflow_event(
            "deck_progress",
            {
                "stage": step,
                "message": message,
                "tool_name": tool,
                "ok": ok,
                "exit_code": exit_code,
                "current": pages,
                "total": self._target,
                "calls": calls,
                "elapsed_s": round(time.monotonic() - self._started, 1),
            },
        )


# ---------------------------------------------------------------------------
# Verification
# ---------------------------------------------------------------------------


def find_export(workspace: Path, report: str, exclude: set[Path]) -> Optional[Path]:
    """The deck the sub-agent exported, found on disk rather than taken on trust.

    A path the sub-agent names counts only if it exists inside the workspace.
    ppt-master writes its deliverables to an ``exports/`` folder, so one found
    there wins; the newest wins among several, because a revision round keeps
    the earlier exports beside the new one.
    """

    def deliverable(path: Path) -> bool:
        try:
            parts = path.resolve().relative_to(workspace).parts[:-1]
        except ValueError:
            return False
        return path.is_file() and path.resolve() not in exclude and not (set(parts) & _NOT_DELIVERABLE_DIRS)

    named = [Path(m.group(1)) for m in _PPTX_PATH_RE.finditer(report or "")]
    named = [p.resolve() for p in named if deliverable(p)]
    found = [p.resolve() for p in workspace.rglob("*.pptx") if deliverable(p)]
    pool = (
        [p for p in named if p.parent.name == "exports"]
        or [p for p in found if p.parent.name == "exports"]
        or named
        or found
    )
    if not pool:
        return None
    return max(pool, key=lambda p: p.stat().st_mtime)


def gate_verdict(export: Path) -> dict[str, Any]:
    """ppt-master's own verdict on the export, read from its postflight report.

    The exporter refuses to run without a passing final quality report and
    writes ``validation/<export stem>.report.json`` beside it; that report, not
    the sub-agent's summary, is what this returns.
    """
    project = export.parent.parent if export.parent.name == "exports" else export.parent
    validation = project / "validation"
    verdict: dict[str, Any] = {}
    report = validation / f"{export.stem}.report.json"
    if not report.is_file():
        reports = sorted(validation.glob("*.report.json"), key=lambda p: p.stat().st_mtime) if validation.is_dir() else []
        report = reports[-1] if reports else report
    if report.is_file():
        try:
            data = json.loads(report.read_text(encoding="utf-8"))
        except (OSError, ValueError):
            data = {}
        checks = data.get("checks") or {}
        verdict["export"] = data.get("status") or "unknown"
        if checks.get("quality_gate"):
            verdict["quality_gate"] = checks["quality_gate"]
        failed = sorted(k for k, v in checks.items() if str(v).lower() in {"failed", "error", "blocked"})
        if failed:
            verdict["failed_checks"] = failed
        verdict["report"] = str(report)
    quality = validation / "svg_quality_report.json"
    if quality.is_file():
        try:
            summary = json.loads(quality.read_text(encoding="utf-8")).get("summary") or {}
        except (OSError, ValueError):
            summary = {}
        if summary:
            verdict["pages_checked"] = {
                k: summary[k] for k in ("total", "passed", "warnings", "errors") if k in summary
            }
    if "export" not in verdict:
        verdict["export"] = "unverified"
        verdict["detail"] = "no ppt-master postflight report beside the export"
    return verdict


def decisions_under_delegation(report: str, *, limit: int = 10) -> list[str]:
    """The choices the sub-agent says it made on the user's behalf."""
    match = _DECISIONS_RE.search(report or "")
    if not match:
        return []
    items: list[str] = []
    for line in match.group(1).splitlines():
        text = line.strip()
        if not text:
            continue
        text = re.sub(r"^(?:[-*•]|\d+[.)])\s*", "", text)
        if text:
            items.append(text[:300])
        if len(items) >= limit:
            break
    return items


def _deliver(export: Path, settings: Settings, title: str) -> Path:
    """Copy the export — and nothing else — to ``<outputs_dir>/<safe title>.pptx``.

    Never over an existing file: the chat's artifact row for an earlier deck
    points at its path, and overwriting it would silently change a delivered
    document, so a repeated title gets a numeric suffix instead.
    """
    from .documents import _safe_filename

    settings.outputs_dir.mkdir(parents=True, exist_ok=True)
    stem = _safe_filename(title, default="deck")
    target = settings.outputs_dir / f"{stem}.pptx"
    counter = 2
    while target.exists():
        target = settings.outputs_dir / f"{stem}_{counter}.pptx"
        counter += 1
    shutil.copy2(export, target)
    return target.resolve()


def _slide_count(path: Path) -> int:
    from pptx import Presentation

    return len(Presentation(str(path)).slides)


def _lint_summary(path: Path) -> dict[str, Any]:
    from ..documents.quality import lint_artifact

    payload = lint_artifact(path).to_payload()
    payload.pop("metrics", None)
    return payload


def _excerpt(text: str, head: int = 300, tail: int = 900) -> str:
    text = (text or "").strip()
    if len(text) <= head + tail:
        return text
    return f"{text[:head]}\n…\n{text[-tail:]}"


def run_deck_agent(task: str, settings: Settings) -> str:
    """Run ppt-master to completion in a forked sub-agent on the chat's model.

    No round cap: a procedure spends rounds reading its workflow, authoring a
    page per call, running its gates and repairing, and a capped loop fails
    after paying for most of that work.
    """
    from ..agent import SkillAgent
    from ..providers import build_provider

    # The `deck` row of role_profiles: by default the chat's own model and effort.
    settings, fast = settings.for_role("deck")
    with override_current_settings(settings):
        provider = build_provider(settings, fast=fast)
        return SkillAgent(DECK_SKILL, provider, settings).execute(task)


def _result(payload: dict[str, Any]) -> str:
    return json.dumps(payload, ensure_ascii=False, indent=2)


@beta_tool
def create_deck(
    brief: str,
    title: str,
    language: str = "",
    audience: str = "",
    purpose: str = "",
    page_count: int = 0,
    source_paths: list[str] | None = None,
    template_path: str = "",
    quick: bool = False,
) -> str:
    """Make a slide deck — PPT / PPTX / presentation / slides / 幻灯片 / 演示文稿 / 汇报材料 / 课件. Returns JSON.

    This is THE way to produce a presentation here, whatever its size: a full
    deck, a single slide, or one diagram on a slide (流程图 / 结构图 / 思维导图 /
    timeline). It drives ppt-master in a sub-agent that plans the deck, draws
    every page as editable native PowerPoint shapes, runs ppt-master's own
    quality gate and exports one .pptx; this tool then opens, counts and lints
    the export before reporting it. A deck takes several minutes.

    The sub-agent does not see this conversation. ``brief`` must carry
    everything the slides say — the finished analysis or outline, the facts,
    figures, dates, parties, and every citation with its pinpoint — because
    nothing absent from the brief or the source files will appear on a slide.

    For a deck the user already has: edit_pptx_text / reshape_pptx change it,
    render_pptx_slides shows it. To restyle or refill the user's own template,
    pass it as ``template_path``.

    Args:
        brief: The complete content of the deck, in the deck's language:
            analysis, outline, figures and sources with pinpoints.
        title: Deck title.
        language: BCP-47 tag for the slide text, e.g. "zh-CN" or "en".
            Default: the language of the brief.
        audience: Who it is for, e.g. 管理层, the client's board, new hires.
        purpose: What it is for, e.g. 汇报 / 宣贯培训 / pitch / seminar talk.
        page_count: Target number of slides; 0 lets the procedure decide.
        source_paths: Absolute paths of uploaded files the deck draws on.
        template_path: Absolute path of an uploaded .pptx/.potx whose design
            the deck must follow or fill.
        quick: True when the user asked for speed over the full planning pass.
    """
    started = time.monotonic()
    settings = current_settings()
    title = (title or "").strip() or "Presentation"
    skill_dir = _deck_skill_dir()
    if skill_dir is None:
        log_workflow_event("deck_finished", {"status": "unavailable"})
        return _result({"status": "unavailable", "pptx_path": None, "next_step": INSTALL_HINT})

    brief = (brief or "").strip()
    if not brief:
        return _result({
            "status": "failed",
            "pptx_path": None,
            "next_step": (
                "The brief is empty. The deck sub-agent cannot see this conversation; "
                "call create_deck again with the full content the slides must carry."
            ),
        })

    sources: list[Path] = []
    missing: list[str] = []
    for raw in source_paths or []:
        path = _resolve_input(raw, settings)
        if path.is_file():
            sources.append(path)
        else:
            missing.append(raw)
    template: Optional[Path] = None
    if (template_path or "").strip():
        candidate = _resolve_input(template_path, settings)
        if not candidate.is_file() or candidate.suffix.lower() not in {".pptx", ".potx"}:
            missing.append(template_path)
        else:
            template = candidate
    if missing:
        return _result({
            "status": "failed",
            "pptx_path": None,
            "next_step": (
                "These input files do not exist (or the template is not a .pptx/.potx): "
                + ", ".join(str(m) for m in missing)
                + ". Pass the absolute paths of the uploaded files."
            ),
        })

    lang = (language or "").strip() or deck_language(f"{title}\n{brief}")
    workspace = _new_workspace(settings, title)
    brief_file = workspace / "brief.md"
    brief_file.write_text(f"# {title}\n\n{brief}\n", encoding="utf-8")
    task = compose_deck_task(
        title=title,
        language=lang,
        audience=audience,
        purpose=purpose,
        page_count=max(int(page_count or 0), 0),
        quick=bool(quick),
        workspace=workspace,
        brief_file=brief_file,
        brief=brief,
        sources=sources,
        template=template,
    )
    # The sub-agent writes, runs and renders inside the workspace: its
    # write_text_file, run_skill_script cwd and render outputs all follow
    # outputs_dir, so pointing that at the workspace keeps a project of dozens
    # of files out of the chat's artifact list.
    deck_settings = settings.model_copy(update={"outputs_dir": workspace})
    progress = DeckProgress(workflow_event_sink_var.get(), target_pages=max(int(page_count or 0), 0))
    log_workflow_event(
        "deck_started",
        {"title": title, "language": lang, "quick": bool(quick), "workspace": str(workspace),
         "sources": len(sources), "template": bool(template)},
    )
    sink_token = workflow_event_sink_var.set(progress)
    report, error = "", ""
    try:
        report = run_deck_agent(task, deck_settings) or ""
    except Exception as exc:  # noqa: BLE001 — a failed run is reported, not raised
        error = f"{type(exc).__name__}: {exc}"
    finally:
        workflow_event_sink_var.reset(sink_token)

    elapsed = round(time.monotonic() - started, 1)
    exclude = {p.resolve() for p in sources} | ({template.resolve()} if template else set())
    export = find_export(workspace, report, exclude)
    base: dict[str, Any] = {
        "elapsed_s": elapsed,
        "tool_calls": progress.calls,
        "pages_authored": len(progress.pages),
        "project_dir": str(workspace),
        "decisions_under_delegation": decisions_under_delegation(report),
    }
    if export is None:
        log_workflow_event("deck_finished", {"status": "failed", "elapsed_s": elapsed})
        return _result({
            "status": "failed",
            "pptx_path": None,
            **base,
            **({"error": error} if error else {}),
            "sub_agent_report": _excerpt(report),
            "next_step": (
                "ppt-master did not export a deck. Tell the user what stopped it (see "
                "sub_agent_report / error); do not present any other file as the deck. "
                "If the run stopped on a fixable input problem, call create_deck again with it fixed."
            ),
        })

    verdict = gate_verdict(export)
    delivered = _deliver(export, settings, title)
    try:
        slides = _slide_count(delivered)
    except Exception as exc:  # noqa: BLE001 — an unopenable export is a failed deck
        log_workflow_event("deck_finished", {"status": "failed", "elapsed_s": elapsed})
        return _result({
            "status": "failed",
            "pptx_path": str(delivered),
            **base,
            "gate_verdict": verdict,
            "error": f"the exported .pptx does not open: {type(exc).__name__}: {exc}",
            "next_step": "Do not deliver this file; tell the user the export is corrupt.",
        })
    lint = _lint_summary(delivered)
    try:
        from ..documents.lineage import record_derivation

        # The user's own inputs, not the internal brief file: lineage answers
        # "which delivered file was made from which of mine".
        record_derivation(delivered, [*sources, *([template] if template else [])], "create_deck")
    except Exception:  # noqa: BLE001 — lineage is advisory
        pass

    gate_failed = verdict.get("export") not in _POSTFLIGHT_PASSED | {"unverified"}
    status = "failed" if gate_failed or slides < 1 else "ok"
    critical = (lint.get("counts") or {}).get("critical", 0)
    if status == "failed":
        next_step = (
            "ppt-master's own postflight did not pass (see gate_verdict); do not present this "
            "deck as finished — tell the user what failed."
        )
    elif critical:
        next_step = (
            f"Deck exported, but the conformance lint found {critical} critical problem(s) "
            "(see lint_summary). Look with render_pptx_slides, fix with edit_pptx_text / "
            "reshape_pptx or call create_deck again with the problem stated in the brief, "
            "then give the user the path."
        )
    else:
        next_step = (
            "Deck delivered. Give the user pptx_path; render_pptx_slides shows the pages if you "
            "want to look before you describe them."
        )
    log_workflow_event(
        "deck_finished",
        {"status": status, "slides": slides, "elapsed_s": elapsed, "pptx_path": str(delivered)},
    )
    return _result({
        "status": status,
        "pptx_path": str(delivered),
        "slides": slides,
        "gate_verdict": verdict,
        **base,
        "lint_summary": lint,
        **({"error": error} if error else {}),
        "next_step": next_step,
    })


__all__ = [
    "DECK_SKILL",
    "DeckProgress",
    "INSTALL_HINT",
    "classify_deck_call",
    "compose_deck_task",
    "create_deck",
    "deck_language",
    "decisions_under_delegation",
    "find_export",
    "gate_verdict",
    "run_deck_agent",
]
