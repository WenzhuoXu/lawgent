"""Specialist sub-agents.

The workflow executor (``workflow.py``) plans a turn and, for a multi-issue
request, runs several specialists in parallel; each is a ``SkillAgent`` with a
forked context. An in-tree skill gets its SKILL.md method, the general
playbook, the research conduct and the specialist bundle contract inlined; an
external procedural skill (ppt-master) gets the host runtime contract instead.
Only a specialist's bundle returns to the author — its internal turns stay in
the JSONL log under the same parent run_id.
"""

from __future__ import annotations

import re
from pathlib import Path
from typing import Any, Iterable, Iterator, Optional

from .answer_contract import specialist_contract
from .attachments import build_user_content
from .authoring import research_conduct
from .config import Settings, current_settings
from .domains import DomainPack, load_pack
from .logging_setup import (
    agent_name_var,
    log_workflow_event,
    new_run_id,
    parent_run_id_var,
    run_id_var,
    setup_logging,
)
from .providers import Provider, StreamEvent
from .skills import skill_manifest
from .tools import skill_tools_for_task
from .tools.search import hosted_search_tools_for_provider


def _load_active_packs(settings: Settings) -> list[DomainPack]:
    packs: list[DomainPack] = []
    for name in settings.active_domain_packs:
        try:
            packs.append(load_pack(name))
        except Exception:  # noqa: BLE001 — soft-fail per pack
            continue
    return packs


def _pack_playbook_rel_path(pack: DomainPack, settings: Settings) -> Path | None:
    """Project-root-relative pack playbook path, or None when the pack has no
    playbook file. Relative so `read_document` (which resolves against
    project_root) can open it directly."""
    if not pack.playbook_path.is_file():
        return None
    try:
        return pack.playbook_path.relative_to(settings.project_root)
    except ValueError:
        return pack.playbook_path


def _pack_playbook_section_index(pack: DomainPack) -> str:
    """Compact §-heading index for a pack playbook.

    Overlays cite positions as `playbook §N`; surfacing the numbered headings
    (not the body) lets a specialist resolve those references at runtime while
    the prompt stays compact. Falls back to all non-title headings for packs
    that do not number their sections.
    """
    if not pack.playbook_path.is_file():
        return ""
    try:
        from .skills import list_playbook_sections

        titles = list_playbook_sections(pack.playbook_path)
    except Exception:  # noqa: BLE001 — a malformed playbook never breaks a run
        return ""
    numbered = [t for t in titles if re.match(r"^\d+[a-z]?\.", t)]
    picked = numbered or titles[1:]
    return "; ".join(f"§{t}" for t in picked)


# Ceiling on inlined pack-playbook text. Above this a playbook is genuinely
# reference material rather than standing context, and the §-index + an
# explicit read stays the cheaper shape.
_MAX_INLINE_PLAYBOOK_CHARS = 40_000


_PACK_POINTER_RE = re.compile(r"/domains/([A-Za-z0-9_-]+)/")


def _strip_inactive_pack_pointers(body: str, active: set[str]) -> str:
    """Drop bullets pointing at domain packs that are not active this run.

    Ten SKILL.md files hard-code an aviation overlay pointer, against
    CLAUDE.md's "no aviation strings outside `domains/aviation/`" rule. That
    stayed invisible while the body was fetched section-by-section; inlining
    the whole body would otherwise put aviation guidance in front of every
    specialist on every non-aviation run.
    """
    blocks: list[list[str]] = []
    for line in body.split("\n"):
        # A bullet owns its wrapped continuation lines (indented, non-bullet),
        # which is where the pack path usually sits.
        starts_block = bool(re.match(r"^\s*[-*]\s", line)) or not re.match(r"^\s+\S", line)
        if starts_block or not blocks:
            blocks.append([line])
        else:
            blocks[-1].append(line)

    out: list[str] = []
    for block in blocks:
        text = "\n".join(block)
        packs = set(_PACK_POINTER_RE.findall(text))
        if packs and not (packs & active):
            continue
        out.extend(block)
    return "\n".join(out)


def _inline_skill_methodology(skill_name: str, active_packs: Iterable[str] = ()) -> str:
    """The specialist's SKILL.md body + the general playbook, inlined.

    Both are needed on essentially every invocation of a given skill, so they
    are standing context, not on-demand reference — fetching them cost a
    guaranteed two-iteration handshake per dispatch.
    """
    from .skills import load_playbook, load_skill_body

    blocks: list[str] = []
    try:
        body = load_skill_body(skill_name).strip()
    except Exception:  # noqa: BLE001 — a missing skill file never breaks a run
        body = ""
    if body:
        body = _strip_inactive_pack_pointers(body, set(active_packs))
    if body:
        blocks.append(f"# Your methodology (`{skill_name}` SKILL.md)\n\n{body}\n\n")
    try:
        playbook = load_playbook().strip()
    except Exception:  # noqa: BLE001
        playbook = ""
    if playbook:
        blocks.append(f"# General legal playbook\n\n{playbook}\n\n")
    return "".join(blocks)


def _inline_pack_playbooks(active_packs: list[DomainPack]) -> str:
    """Inline the active packs' playbook bodies into the prompt.

    Returns "" when no pack has a playbook, or when the combined text exceeds
    `_MAX_INLINE_PLAYBOOK_CHARS` — in which case the §-index already emitted
    by the caller plus `read_document` remains the fallback.
    """
    blocks: list[str] = []
    budget = _MAX_INLINE_PLAYBOOK_CHARS
    for pack in active_packs:
        if not pack.playbook_path.is_file():
            continue
        try:
            body = pack.playbook_path.read_text(encoding="utf-8").strip()
        except OSError:
            continue
        if not body or len(body) > budget:
            continue
        budget -= len(body)
        blocks.append(
            f"\n# `{pack.name}` pack playbook (full text — do not re-read it "
            f"from disk)\n\n{body}\n"
        )
    return "".join(blocks)


def _inline_pack_research_sources(active_packs: list[DomainPack]) -> str:
    """Each active pack's source routing — the domain's regulators, connectors
    and collections. It used to sit in the generic specialist contract, which
    put one domain's tool routing in front of every specialist on every run."""
    blocks: list[str] = []
    for pack in active_packs:
        path = pack.research_sources_path
        if path is None or not path.is_file():
            continue
        try:
            body = path.read_text(encoding="utf-8").strip()
        except OSError:
            continue
        if body:
            blocks.append(f"\n# `{pack.name}` pack — research sources\n\n{body}\n")
    return "".join(blocks)


def _inline_pack_overlays(active_packs: list[DomainPack], skill_name: Optional[str]) -> str:
    """The active packs' overlay for the chosen skill, inlined for the author."""
    if not skill_name:
        return ""
    blocks: list[str] = []
    for pack in active_packs:
        overlay = pack.overlay_for(skill_name)
        if overlay is None:
            continue
        try:
            body = overlay.read_text(encoding="utf-8").strip()
        except OSError:
            continue
        if body:
            blocks.append(f"\n# `{pack.name}` pack overlay for `{skill_name}`\n\n{body}\n")
    return "".join(blocks)


def _build_parent_addendum(settings: Settings, *, skill_name: Optional[str] = None) -> str:
    """Per-run jurisdiction + active-pack block appended to an author's prompt.

    The project's brief and memory are not here: they open the chat digest the
    author already receives as its cached request prefix. In the system prompt
    they invalidated the system cache on every project-memory update.
    """
    parts = [
        f"\n# Active jurisdiction\n\nDefault jurisdiction: **{settings.default_jurisdiction}**. "
        f"Secondary jurisdictions: {settings.secondary_jurisdictions}. "
        f"Citation style: **{settings.citation_style}**.\n"
    ]
    active_packs = _load_active_packs(settings)
    if active_packs:
        pack_list = ", ".join(f"`{p.name}`" for p in active_packs)
        parts.append(
            f"\n# Active domain pack(s)\n\nActive: {pack_list}. The pack's playbook "
            "and source routing follow; its connectors are on your tool list.\n"
        )
        # Inlined rather than fetched: telling the model to read the pack
        # playbook produced 35 whole-file reads of one static 18KB file in a
        # month, each a full tool iteration re-billed on every later one.
        parts.append(_inline_pack_playbooks(active_packs))
        parts.append(_inline_pack_research_sources(active_packs))
        parts.append(_inline_pack_overlays(active_packs, skill_name))
    try:
        from .projects import active_project_id

        project_active = bool(active_project_id())
    except Exception:  # noqa: BLE001 — never let context injection break a run
        project_active = False
    if project_active:
        parts.append(
            "\n# Project\n\nThis chat belongs to a project; its brief and memory open "
            "the conversation context. Stay consistent with them, and record durable "
            "new facts, decisions and open questions with `project_memory_write`.\n"
        )
    return "".join(parts)


def _has_sources(text: str) -> bool:
    return any(marker in text for marker in ("Sources", "资料来源"))


def _has_findings(text: str) -> bool:
    return "## Findings" in text or "\nFindings\n" in text


def _has_out_of_scope(text: str) -> bool:
    return "## Out of scope" in text


def _has_legacy_scaffold(text: str) -> bool:
    """True when the sub-agent leaked the deprecated polished-output sections."""
    return any(marker in text for marker in (
        "Evidence Verification Log",
        "Claim-Level Sanity Check",
        "Reporting / Escalation Map",
        "在线证据核验记录",
        "逐项主张核验",
    ))


def _section_preview(text: str, markers: tuple[str, ...], *, limit: int = 1600) -> str:
    starts = [idx for marker in markers if (idx := text.find(marker)) >= 0]
    if not starts:
        return ""
    start = min(starts)
    return text[start : start + limit].strip()


def _build_user_message(task: str, attachments: Iterable[Path]) -> str:
    """Plain-text rendering of the user turn used by the cheap planner.

    Filenames are appended as a hint so the planner can route based on
    what is attached. The provider only sees actual file bytes via
    `build_user_content` at the sub-agent / integration-step boundary.
    """
    parts = [task.strip()]
    paths = [str(Path(p)) for p in (attachments or [])]
    if paths:
        parts.append("\nAttachments (the model will see file contents at execution time):")
        parts.extend(f"- {p}" for p in paths)
    return "\n".join(parts)


def _external_skill_prompt(skill_name: str, outputs_dir: Path) -> str:
    """Host contract for an external procedural skill.

    An in-tree legal skill is handed methodology because methodology is its
    content. An external skill is a *procedure*: it already carries its own
    workflow, reference tree, artifact contracts and gate scripts, and what it
    needs from a host is the three things a host alone knows — where the skill
    lives on disk, which calls stand in for reading/writing/executing, and
    which of its interactive assumptions do not hold here.

    Getting this wrong is the whole failure mode. Such a skill typically
    opens with a mandatory load order keyed to an absolute directory it
    expects the host to supply, and instructs the agent never to guess it. So
    it is stated once, exactly, along with the prefix-rewriting rule its own
    documentation depends on.
    """
    from .skills import load_skill_body, skill_dir
    from .tool_budget import budget_for
    from .tools.skill_runtime import MAX_TIMEOUT_S

    root = skill_dir(skill_name)
    try:
        body = load_skill_body(skill_name).strip()
    except Exception:  # noqa: BLE001 — a missing body never breaks a run
        body = ""
    # Stated from the limits that enforce them, so the contract cannot drift.
    read_budget = budget_for("read_document")
    read_cap_k = round(read_budget.max_tokens / 1000) if read_budget else None

    read_paging = (
        f"`read_document` returns at most about {read_cap_k}K tokens per call. A\n"
        "longer file comes back with a notice naming a saved copy: keep reading it\n"
        "with `read_document(path=<that copy>, offset=<line>, limit=<lines>)` until\n"
        "you have read everything the procedure tells you to read.\n"
        if read_cap_k
        else ""
    )
    contract = f"""# Host Runtime Contract

You are driving the external skill **{skill_name}**, whose own instructions
follow. They are authoritative for *what* to do; this section is
authoritative for *how* to act in this harness.

## Paths

- `SKILL_DIR` is exactly: `{root}`
- Expand every `${{SKILL_DIR}}` to that path, and rewrite any documented
  `skills/{skill_name}/...` prefix to `{root}/...`.
- Never `cd`, never rely on a working directory, never search for the skill
  directory, and never guess a path. Use `list_skill_dir` when you need to
  see what a subdirectory actually contains.
- `SKILL_DIR` is read-only for this run. Nothing you create goes inside it:
  where one of its commands would write inside the skill's own installation by
  default, pass that command's own location option (`--dir`, `-o`, …) with a
  path under the work directory below.

## Calls that stand in for the usual ones

| The skill says | Use |
|---|---|
| read a file (any absolute path) | `read_document(path=...)` |
| list a directory inside the skill | `list_skill_dir(skill="{skill_name}", subpath="...")` |
| write/author a text file (.svg, .md, .json, .css) | `write_text_file(path=..., content=...)` |
| run one of its scripts (`python3 ${{SKILL_DIR}}/scripts/x.py a b`) | `run_skill_script(skill="{skill_name}", script="scripts/x.py", args=["a", "b"])` |
| look at a rendered page | `render_pptx_slides` / `render_pdf_pages` / `view_image` |

`run_skill_script` returns JSON with `exit_code`, `stdout` and `stderr`. A
non-zero `exit_code` is the script's verdict, not a tool error: read it and
act on it. That is how the skill's own gates reach you, so run them where its
procedure says to and treat a failure as blocking exactly as it instructs.
It cannot set environment variables: run a documented `VAR=value python3 …`
command without the prefix. It waits `timeout_s` seconds (default 300, at
most {MAX_TIMEOUT_S}); give a long export or conversion a larger one.

{read_paging}
## Where work goes

The work directory is `{outputs_dir}`; `write_text_file` writes only under it
and scripts run with it as their current directory. Put the project under a
named subdirectory of it — `<project-name>/` — and pass absolute paths under it
to the skill's scripts.

## Interactive assumptions that do not hold here

- **Gates are delegated.** There is no interactive channel in this run, so
  you cannot wait for a user confirmation. Where the procedure defines a
  blocking user gate, act under its own explicit-delegation provision: make
  the decision yourself, record it in the artifact the gate governs, proceed,
  and state in your final answer what you decided and why. Never fabricate a
  user's submitted values or invent a confirmation receipt.
- **No background processes.** `run_skill_script` captures output until the
  process exits, so anything long-running (a preview or editor server) would
  simply hang until its timeout. Do not start one. Where the procedure allows
  recording that run instructions forbade starting it, do that and continue.
- **Nothing is skipped for lack of a gate.** Delegation replaces who decides,
  not whether the work is done. Run every deterministic check the procedure
  requires and satisfy it before you report a deliverable as finished.

## Reporting

Finish with the absolute path of every artifact you produced and the verdict
of the last gate you ran, then a section headed exactly
`## Decisions under delegation` with one bullet per decision you made in the
user's place (route or profile, template or free design, canvas, page roster,
style) and why. If you could not finish, say exactly which step stopped you
and what its output was — do not present a partial deliverable as complete.
"""
    if body:
        contract += f"\n---\n\n# {skill_name} — SKILL.md\n\n{body}\n"
    return contract


class SkillAgent:
    """Sub-agent: forked context, compact manifest + lazy skill resources."""

    def __init__(
        self,
        skill_name: str,
        provider: Provider,
        settings: Optional[Settings] = None,
    ) -> None:
        # Validate against live discovery, not the import-time snapshot in
        # SKILL_NAMES: every other path here resolves a skill by scanning the
        # roots, so a snapshot check rejects a skill the rest of the harness
        # can already find (a root configured after import, or one installed
        # while the process is up).
        from .skills import discover_skills

        if skill_name not in discover_skills():
            raise ValueError(f"Unknown skill: {skill_name}")
        setup_logging()
        self.skill_name = skill_name
        self.provider = provider
        self.settings = settings or current_settings()

    def _active_packs(self) -> list[DomainPack]:
        return _load_active_packs(self.settings)

    def _system_prompt(self, tools: Optional[list[Any]] = None) -> str:
        # An external skill is a procedure, not a methodology: hand it the
        # host contract and its own instructions, and none of the PRC
        # playbook / citation-audit framing, which would be noise at best and
        # a conflicting instruction at worst.
        from .skills import is_external_skill

        if is_external_skill(self.skill_name):
            return _external_skill_prompt(self.skill_name, self.settings.outputs_dir)

        manifest = skill_manifest(self.skill_name)
        active_packs = self._active_packs()
        pack_names = ", ".join(p.name for p in active_packs) or "(none)"
        overlay_hints: list[str] = []
        for pack in active_packs:
            overlay = pack.overlay_for(self.skill_name)
            if overlay is not None:
                # Express the hint path relative to the project root so
                # `read_document` (which resolves against project_root) finds it.
                # `pack.root` is `<root>/legal_helper/domains/<pack>`, so the
                # path must keep the `legal_helper/` package segment.
                try:
                    rel = overlay.relative_to(self.settings.project_root)
                except ValueError:
                    rel = overlay
                overlay_hints.append(
                    f"- Pack `{pack.name}` overlay for this skill: "
                    f"`{rel}` — read it with `read_document(\"{rel}\")` when domain "
                    f"context is needed (it is a file path, not a SKILL.md heading)."
                )
            # Pack playbook: the pack's own domain defaults. Overlays cite it
            # as `playbook §N` — surface the path + §-heading index so those
            # references resolve at runtime instead of being guessed at.
            rel_pb = _pack_playbook_rel_path(pack, self.settings)
            if rel_pb is not None:
                hint = (
                    f"- Pack `{pack.name}` playbook: `{rel_pb}` — the pack's "
                    "domain defaults (specified positions, thresholds, minima). "
                    "Overlay references like `playbook §N` resolve to its "
                    "numbered sections"
                )
                index = _pack_playbook_section_index(pack)
                if index:
                    hint += f": {index}"
                hint += "."
                overlay_hints.append(hint)

        prompt = (
            "---\n"
            f"name: {manifest['name']}\n"
            f"description: {manifest.get('description') or ''}\n"
            f"argument_hint: {manifest.get('argument_hint') or ''}\n"
            "---\n\n"
            "# Specialist Runtime Contract\n\n"
            f"Jurisdiction default: **{self.settings.default_jurisdiction}** "
            f"(secondary: {self.settings.secondary_jurisdictions}). "
            f"Citation style: **{self.settings.citation_style}**. "
            f"Active domain pack(s): **{pack_names}**.\n\n"
            "You are a specialist researching one part of a request for the "
            "author of the final answer. Your methodology and the general "
            "playbook are inlined below — go straight to the assigned task. The "
            "conversation so far and the chat's files open your request.\n\n"
        )
        # Inlined rather than fetched: withholding this content made a
        # section-fetch handshake open every dispatch, and 51.9% of specialist
        # tool calls were instruction fetches. The section readers are gone;
        # `read_skill_reference` remains for the long `references/` tail.
        prompt += _inline_skill_methodology(
            self.skill_name, self.settings.active_domain_packs
        )
        if overlay_hints:
            prompt += (
                "Active domain-pack resources for this run:\n"
                + "\n".join(overlay_hints)
                + "\n\n"
            )
        # A pack is only active when its defaults are needed, so its playbook
        # and source routing are standing context.
        prompt += _inline_pack_playbooks(active_packs)
        prompt += _inline_pack_research_sources(active_packs)
        prompt += (
            "\nUse only the tools made available for this task. If a tool is not "
            "available, continue with hosted search or explain the gap instead "
            "of inventing tool outputs.\n\n"
        )
        prompt += research_conduct() + "\n"
        if _has_pkulaw(tools):
            prompt += _PKULAW_ROUTING
        prompt += "\n---\n\n" + specialist_contract()
        return prompt

    def _max_iterations(self) -> int:
        """Tool-loop ceiling: a procedure needs a different budget than research.

        Both the run path and the stream path read this, so they stay in step.
        """
        from .skills import is_external_skill

        if is_external_skill(self.skill_name):
            return self.settings.external_skill_max_iterations
        return self.settings.sub_agent_max_iterations

    def _tools(self, task: str = "") -> list:
        tools = skill_tools_for_task(
            self.skill_name,
            task,
            jurisdictions=[self.settings.default_jurisdiction, *self.settings.secondary_jurisdictions],
            active_packs=self.settings.active_domain_packs,
        )
        # Hosted web_search / web_fetch is the primary research path for
        # jurisdictions without a structured connector and the fallback
        # everywhere else; the deployment's enable flags decide, not keywords
        # in the task text.
        return tools + hosted_search_tools_for_provider(self.settings)

    def _user_content(
        self, task: str, attachments: list[Path], cache_prefix: str = ""
    ) -> Any:
        return build_user_content(
            task, attachments, self.provider.name, cache_prefix=cache_prefix or None
        )

    def execute(
        self,
        task: str,
        attachments: Optional[Iterable[Path]] = None,
        *,
        cache_prefix: str = "",
    ) -> str:
        parent_run_id = run_id_var.get()
        sub_run_id = new_run_id()
        token_run = run_id_var.set(sub_run_id)
        token_parent = parent_run_id_var.set(parent_run_id)
        token_agent = agent_name_var.set(self.skill_name)
        try:
            attachment_paths = list(attachments or [])
            log_workflow_event(
                "skill_started",
                {
                    "skill_name": self.skill_name,
                    "task_language": "free per material",
                    "verification_required": True,
                    "attachment_count": len(attachment_paths),
                },
            )
            tools = self._tools(task)
            result = self.provider.tool_runner(
                system=self._system_prompt(tools),
                messages=[
                    {"role": "user", "content": self._user_content(task, attachment_paths, cache_prefix)}
                ],
                tools=tools,
                max_iterations=self._max_iterations(),
            )
            text = result.text or ""
            log_workflow_event(
                "skill_finished",
                {
                    "skill_name": self.skill_name,
                    "response_chars": len(text),
                    "local_tool_calls": [c.name for c in result.tool_calls],
                    "hosted_tool_calls": [c.name for c in result.hosted_tool_calls],
                    "hosted_tool_call_count": len(result.hosted_tool_calls),
                    "has_findings": _has_findings(text),
                    "has_out_of_scope": _has_out_of_scope(text),
                    "has_sources": _has_sources(text),
                    "has_legacy_scaffold_leak": _has_legacy_scaffold(text),
                    "findings_preview": _section_preview(text, ("## Findings",)),
                    "sources_preview": _section_preview(text, ("## Sources",)),
                },
            )
        finally:
            run_id_var.reset(token_run)
            parent_run_id_var.reset(token_parent)
            agent_name_var.reset(token_agent)
        return result.text or ""

    def stream(
        self,
        task: str,
        attachments: Optional[Iterable[Path]] = None,
        *,
        cache_prefix: str = "",
    ) -> Iterator[StreamEvent]:
        """Stream a specialist turn while preserving parent/sub-run logging context.

        ``cache_prefix`` is the chat digest (project, files, transcript,
        summary). Specialists used to get only their task text, so files
        uploaded in an earlier turn were unreachable and the facts the user
        had already given had to be re-derived from a 700-character task.
        """
        parent_run_id = run_id_var.get()
        sub_run_id = new_run_id()
        token_run = run_id_var.set(sub_run_id)
        token_parent = parent_run_id_var.set(parent_run_id)
        token_agent = agent_name_var.set(self.skill_name)
        text_chunks: list[str] = []
        try:
            attachment_paths = list(attachments or [])
            log_workflow_event(
                "skill_started",
                {
                    "skill_name": self.skill_name,
                    "task_language": "free per material",
                    "verification_required": True,
                    "stream": True,
                    "attachment_count": len(attachment_paths),
                },
            )
            tools = self._tools(task)
            for ev in self.provider.stream(
                system=self._system_prompt(tools),
                messages=[
                    {"role": "user", "content": self._user_content(task, attachment_paths, cache_prefix)}
                ],
                tools=tools,
                max_iterations=self._max_iterations(),
            ):
                if ev.kind == "delta":
                    text_chunks.append(ev.data.get("text", ""))
                elif ev.kind == "done" and not text_chunks and ev.data.get("text"):
                    text_chunks.append(ev.data.get("text", ""))
                yield ev
            text = "".join(text_chunks)
            log_workflow_event(
                "skill_finished",
                {
                    "skill_name": self.skill_name,
                    "response_chars": len(text),
                    "has_findings": _has_findings(text),
                    "has_out_of_scope": _has_out_of_scope(text),
                    "has_sources": _has_sources(text),
                    "has_legacy_scaffold_leak": _has_legacy_scaffold(text),
                    "findings_preview": _section_preview(text, ("## Findings",)),
                    "sources_preview": _section_preview(text, ("## Sources",)),
                    "stream": True,
                },
            )
        finally:
            run_id_var.reset(token_run)
            parent_run_id_var.reset(token_parent)
            agent_name_var.reset(token_agent)


_PKULAW_ROUTING = """
# PKULaw (PRC)
PKULaw tools are namespaced `<server>__<tool>`; route by need:
`pkulaw_law_search__search_article` / `pkulaw_fatiao__get_law_item_content`
for statutes and 第X条 text; `pkulaw_case_search__search_case` /
`pkulaw_case_list__get_case_list` for 司法案例; `pkulaw_anhao__anhao_recognition`
to normalise 案号; `pkulaw_law_recognition__law_recognition` to resolve statute
references in a draft; `pkulaw_citation_validator__adjust_provisions` before
emitting a PRC citation chain; `pkulaw_nl_search__ai_pkulaw_search` for broad
questions across statutes, cases and commentary. `flk_npc_search` is a
status/metadata fallback only — it does not return statute bodies.
"""


def _has_pkulaw(tools: Optional[Iterable[Any]]) -> bool:
    for tool in tools or ():
        name = tool.get("name") if isinstance(tool, dict) else getattr(tool, "name", "")
        if str(name or "").startswith("pkulaw_"):
            return True
    return False
