"""Lazy skill/playbook resource tools for specialist agents."""

from __future__ import annotations

import json

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


@beta_tool
def list_skill_references() -> str:
    """List the current skill's reference prose and resource files.

    ``references`` holds deeper method notes (Markdown); ``resources`` holds
    presets and templates (Markdown, YAML, JSON, CSV) that SKILL.md names.
    Read the specific file the task needs instead of loading all of them.
    """
    skill_name = _current_skill_name()
    root = skill_path(skill_name).parent
    payload: dict[str, object] = {"skill_name": skill_name}
    for directory, suffixes in _READABLE.items():
        base = root / directory
        payload[directory] = (
            [
                str(path.relative_to(root))
                for path in sorted(base.rglob("*"))
                if path.is_file() and path.suffix.lower() in suffixes
            ]
            if base.is_dir()
            else []
        )
    return json.dumps(payload, ensure_ascii=False)


@beta_tool
def read_skill_reference(path: str) -> str:
    """Read one file from the current skill's references/ or resources/.

    Args:
        path: Path returned by list_skill_references, e.g. references/methodology.md
            or resources/element_tables/contract_breach.yaml.
    """
    skill_name = _current_skill_name()
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
    return (
        f"ERROR: Path must stay under {skill_name}/references or {skill_name}/resources: {path}"
    )


