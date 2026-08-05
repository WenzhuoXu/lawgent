"""Lazy skill/playbook resource tools for specialist agents."""

from __future__ import annotations

import json

from anthropic import beta_tool

from ..logging_setup import agent_name_var
from ..skills import (
    SKILL_NAMES,
    skill_path,
    list_playbook_sections,
    list_skill_sections as _list_skill_sections,
    load_playbook_section,
    load_skill_section,
)


def _current_skill_name() -> str:
    skill_name = agent_name_var.get()
    if skill_name not in SKILL_NAMES:
        raise RuntimeError("Skill resource tools are only available inside a specialist agent")
    return skill_name


@beta_tool
def list_skill_sections(include_playbook: bool = True) -> str:
    """List available headings in the current skill file and optional playbook.

    Use this before reading detailed methodology. Pick only the sections that
    match the assigned task instead of loading every instruction into context.

    Args:
        include_playbook: Include the general legal playbook headings as well
            as the current skill's headings.
    """
    skill_name = _current_skill_name()
    payload = {
        "skill_name": skill_name,
        "skill_sections": _list_skill_sections(skill_name),
    }
    if include_playbook:
        payload["playbook_sections"] = list_playbook_sections()
    return json.dumps(payload, ensure_ascii=False)


@beta_tool
def list_skill_references() -> str:
    """List markdown reference files under the current skill's references/.

    Use this only after the compact SKILL.md points you to a deeper reference.
    Read the specific file needed for the task instead of loading all of them.
    """
    skill_name = _current_skill_name()
    root = skill_path(skill_name).parent
    ref_dir = root / "references"
    refs: list[str] = []
    if ref_dir.is_dir():
        refs = [
            str(path.relative_to(root))
            for path in sorted(ref_dir.rglob("*.md"))
            if path.is_file()
        ]
    return json.dumps({"skill_name": skill_name, "references": refs}, ensure_ascii=False)


@beta_tool
def read_skill_reference(path: str) -> str:
    """Read one markdown reference file from the current skill's references/.

    Args:
        path: Path returned by list_skill_references, e.g. references/pptx.md.
    """
    skill_name = _current_skill_name()
    root = skill_path(skill_name).parent
    ref_dir = (root / "references").resolve()
    candidate = (root / path).resolve()
    try:
        candidate.relative_to(ref_dir)
    except ValueError:
        return f"ERROR: Reference path must stay under {skill_name}/references: {path}"
    if not candidate.is_file() or candidate.suffix.lower() != ".md":
        return f"ERROR: No matching reference file for {path}"
    return candidate.read_text(encoding="utf-8")


@beta_tool
def read_skill_section(heading: str) -> str:
    """Read one relevant section from the current skill's SKILL.md.

    Args:
        heading: Heading returned by list_skill_sections, or a close phrase.
    """
    skill_name = _current_skill_name()
    section = load_skill_section(skill_name, heading)
    if not section:
        return f"ERROR: No matching section in {skill_name}/SKILL.md for heading: {heading}"
    return section


@beta_tool
def read_playbook_section(heading: str) -> str:
    """Read one relevant section from the general legal playbook.

    Args:
        heading: Heading returned by list_skill_sections(include_playbook=true),
            or a close phrase.
    """
    section = load_playbook_section(heading)
    if not section:
        return f"ERROR: No matching general playbook section for heading: {heading}"
    return section
