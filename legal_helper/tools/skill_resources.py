"""Skill resource tools: a specialist's own reference files, and the author's
method loader."""

from __future__ import annotations

import json
from pathlib import Path

from anthropic import beta_tool

from ..logging_setup import agent_name_var
from ..skills import SKILL_NAMES, skill_path


def _current_skill_name() -> str:
    skill_name = agent_name_var.get()
    if skill_name not in SKILL_NAMES:
        raise RuntimeError("Skill resource tools are only available inside a specialist agent")
    return skill_name


#: What a skill may hand its agent, by directory: prose in ``references/``,
#: and the presets and templates in ``resources/`` that SKILL.md files point
#: at (column grids, element tables, clause libraries, filing skeletons) —
#: 0 of 30,494 recorded events had ever read one, because this tool refused
#: everything but ``references/*.md``.
_READABLE: dict[str, frozenset[str]] = {
    "references": frozenset({".md"}),
    "resources": frozenset({".md", ".yaml", ".yml", ".json", ".csv"}),
}


def _skill_files(root: Path) -> dict[str, list[str]]:
    out: dict[str, list[str]] = {}
    for directory, suffixes in _READABLE.items():
        base = root / directory
        out[directory] = (
            [
                str(path.relative_to(root))
                for path in sorted(base.rglob("*"))
                if path.is_file() and path.suffix.lower() in suffixes
            ]
            if base.is_dir()
            else []
        )
    return out


def _read_skill_file(skill_name: str, path: str) -> str:
    root = skill_path(skill_name).parent.resolve()
    candidate = (root / path).resolve()
    for directory, suffixes in _READABLE.items():
        base = (root / directory).resolve()
        try:
            candidate.relative_to(base)
        except ValueError:
            continue
        if not candidate.is_file() or candidate.suffix.lower() not in suffixes:
            return f"ERROR: No matching {directory} file for {path}"
        return candidate.read_text(encoding="utf-8")
    return f"ERROR: Path must stay under {skill_name}/references or {skill_name}/resources: {path}"


@beta_tool
def list_skill_references() -> str:
    """List the current skill's reference prose and resource files.

    ``references`` holds deeper method notes (Markdown); ``resources`` holds
    presets and templates (Markdown, YAML, JSON, CSV) that SKILL.md names.
    Read the specific file the task needs instead of loading all of them.
    """
    skill_name = _current_skill_name()
    payload = {"skill_name": skill_name, **_skill_files(skill_path(skill_name).parent)}
    return json.dumps(payload, ensure_ascii=False)


@beta_tool
def read_skill_reference(path: str) -> str:
    """Read one file from the current skill's references/ or resources/.

    Args:
        path: Path returned by list_skill_references, e.g. references/methodology.md
            or resources/element_tables/contract_breach.yaml.
    """
    return _read_skill_file(_current_skill_name(), path)


# --- the author's skill tools ---------------------------------------------------
# The author picks its own method, so it names the skill explicitly;
# `agent_name_var` is "orchestrator" in its loop, which is why the two tools
# above cannot work there.


def _method_names() -> tuple[str, ...]:
    from ..skills import internal_skill_names

    return tuple(name for name in internal_skill_names() if name != "cite-check")


def render_skill(name: str) -> str:
    """A method as the author receives it: its steps without the specialist
    bundle contract, the active packs' overlay for it, and its file list."""
    from ..agent import _load_active_packs, _strip_inactive_pack_pointers
    from ..authoring import strip_bundle_sections
    from ..config import current_settings
    from ..skills import load_skill_body
    from ..turn_context import current_turn

    name = (name or "").strip().strip("`")
    methods = _method_names()
    if name not in methods:
        return "ERROR: Unknown method " + repr(name) + ". Methods: " + ", ".join(methods)
    turn = current_turn()
    settings = turn.settings if turn is not None else current_settings()
    packs = list(getattr(settings, "active_domain_packs", []) or [])
    body = _strip_inactive_pack_pointers(strip_bundle_sections(load_skill_body(name)), set(packs))
    blocks = [f'<skill_content name="{name}">', body.strip()]
    for pack in _load_active_packs(settings):
        overlay = pack.overlay_for(name)
        if overlay is None:
            continue
        try:
            text = overlay.read_text(encoding="utf-8").strip()
        except OSError:
            continue
        if text:
            blocks.append(f"## `{pack.name}` pack overlay for `{name}`\n\n{text}")
    files = _skill_files(skill_path(name).parent)
    listed = [*files.get("references", []), *files.get("resources", [])]
    if listed:
        blocks.append(
            "Files you can open with `read_skill_file`:\n" + "\n".join(f"- {f}" for f in listed)
        )
    blocks.append("</skill_content>")
    if turn is not None:
        turn.note_method(name)
    return "\n\n".join(blocks)


@beta_tool
def load_skill(name: str) -> str:
    """Load one of the listed legal methods before you work a request it fits.

    Returns the method's steps and artefacts, the active domain pack's overlay
    for it, and the reference and resource files you can open with
    `read_skill_file`. Load a method when the request matches its description
    in the system prompt's method list; never guess a name.

    Args:
        name: A method name from the method list, e.g. `review-contract`.
    """
    return render_skill(name)


@beta_tool
def read_skill_file(name: str, path: str) -> str:
    """Open a reference or resource file of a method you loaded.

    Args:
        name: The method, e.g. `review-contract`.
        path: A path `load_skill` listed, e.g. references/methodology.md.
    """
    name = (name or "").strip().strip("`")
    if name not in _method_names():
        return "ERROR: Unknown method " + repr(name) + ". Methods: " + ", ".join(_method_names())
    return _read_skill_file(name, path)
