"""Skill loader and lazy skill-resource helpers.

A skill is a directory holding a ``SKILL.md``. Two kinds live side by side and
the difference is where the directory is, not what it contains:

- **in-tree** skills under this package are legal methodology. They are
  inlined into a specialist sub-agent along with the PRC playbook and the
  citation contract, because that *is* their content.
- **external** skills under an operator-configured root are procedures the
  harness does not maintain — ``ppt-master`` is the motivating case. They
  carry their own multi-step workflow, their own reference tree and their own
  scripts, and they are driven rather than inlined: the sub-agent is handed
  the resolved ``SKILL_DIR`` and reads its way in, exactly as the skill's own
  load order instructs.

Roots come from ``LEGAL_HELPER_SKILL_ROOTS`` (``os.pathsep``-separated) when
set, otherwise this package followed by ``~/.claude/skills``. Discovery runs at
import because :data:`SKILL_NAMES` is consumed at import by the tool registry
and by ``run_skill``'s enum; set the variable before importing the package.

Nothing here hardcodes a skill name. The previous two allowlists — a tuple in
this module and a separate ``Literal`` in ``tools/orchestrator.py`` — had
already drifted apart: ``docx-redline`` and ``flowchart`` existed on disk and
were absent from the dispatch enum, so the orchestrator could not reach its
own diagram skill.
"""

from __future__ import annotations

import os
import re
from pathlib import Path
from typing import Optional

import yaml


_HERE = Path(__file__).resolve().parent
_FRONTMATTER_RE = re.compile(r"^---\s*\n(.*?)\n---\s*\n(.*)$", re.DOTALL)

#: Directory names under a root that are never skills.
_NOT_SKILLS = frozenset({"__pycache__"})


#: Scanned when nothing extra is configured. Claude Code's user skill
#: directory is included because that is where such a skill is already
#: installed; a deployment that wants only in-tree skills sets
#: ``LEGAL_HELPER_SKILL_ROOTS`` to a directory holding none.
_DEFAULT_EXTERNAL_ROOTS = (Path.home() / ".claude" / "skills",)


def skill_roots() -> list[Path]:
    """Roots scanned for skills, in precedence order (first match wins).

    This package is **always** first and cannot be configured away: the
    in-tree legal skills are the product, and a mistyped environment variable
    must not be able to remove them. ``LEGAL_HELPER_SKILL_ROOTS``
    (``os.pathsep``-separated) *replaces the external roots only*.
    """
    roots: list[Path] = [_HERE]
    raw = os.environ.get("LEGAL_HELPER_SKILL_ROOTS", "").strip()
    if raw:
        configured = [
            Path(part.strip()).expanduser().resolve()
            for part in raw.split(os.pathsep)
            if part.strip()
        ]
    else:
        configured = [Path(r).expanduser() for r in _DEFAULT_EXTERNAL_ROOTS]
    for root in configured:
        if root not in roots:
            roots.append(root)
    return roots


def discover_skills() -> dict[str, Path]:
    """Map skill name -> its directory, scanning :func:`skill_roots`.

    An earlier root wins, so an in-tree skill can shadow an external one of
    the same name and a deployment cannot have a third party silently
    replace a legal methodology.
    """
    found: dict[str, Path] = {}
    for root in skill_roots():
        try:
            if not root.is_dir():
                continue
            entries = sorted(root.iterdir())
        except OSError:
            continue
        for entry in entries:
            if not entry.is_dir() or entry.name in _NOT_SKILLS:
                continue
            if entry.name in found:
                continue
            if (entry / "SKILL.md").is_file():
                found[entry.name] = entry
    return found


#: Discovered skill names. Computed at import; see the module docstring.
SKILL_NAMES: tuple[str, ...] = tuple(sorted(discover_skills()))


def internal_skill_names() -> tuple[str, ...]:
    """Skills shipped inside this package.

    The authoring contract — under 100 lines, frontmatter, an output block, a
    not-legal-advice line, a reference to the general playbook — is a contract
    about *legal methodology we maintain*. An external procedural skill is not
    ours to reshape and follows its own conventions, so contract checks
    iterate this set rather than every discovered skill.
    """
    return tuple(
        sorted(
            entry.name
            for entry in _HERE.iterdir()
            if entry.is_dir()
            and entry.name not in _NOT_SKILLS
            and (entry / "SKILL.md").is_file()
        )
    )


def skill_dir(name: str) -> Path:
    """Absolute directory of a skill. Raises if it is not discoverable."""
    found = discover_skills()
    if name not in found:
        raise FileNotFoundError(
            f"Unknown skill: {name}. Discovered: {', '.join(sorted(found)) or '(none)'}"
        )
    return found[name]


def is_external_skill(name: str) -> bool:
    """True when the skill lives outside this package (a driven procedure)."""
    try:
        directory = skill_dir(name)
    except FileNotFoundError:
        return False
    try:
        directory.relative_to(_HERE)
    except ValueError:
        return True
    return False


def skill_path(name: str) -> Path:
    try:
        return skill_dir(name) / "SKILL.md"
    except FileNotFoundError:
        # Preserve the historical shape: callers report the missing path.
        return _HERE / name / "SKILL.md"


def load_skill_text(name: str) -> str:
    """Return the raw SKILL.md content (used as the sub-agent system prompt)."""
    path = skill_path(name)
    if not path.is_file():
        raise FileNotFoundError(f"Missing skill: {name} at {path}")
    return path.read_text(encoding="utf-8")


def load_skill_frontmatter(name: str) -> dict:
    text = load_skill_text(name)
    m = _FRONTMATTER_RE.match(text)
    if not m:
        return {}
    return yaml.safe_load(m.group(1)) or {}


def skill_manifest(name: str) -> dict:
    """Small manifest safe to put in an agent's initial prompt."""
    fm = load_skill_frontmatter(name)
    return {
        "name": fm.get("name", name),
        "description": fm.get("description", ""),
        "argument_hint": fm.get("argument-hint"),
    }


def load_skill_body(name: str) -> str:
    text = load_skill_text(name)
    m = _FRONTMATTER_RE.match(text)
    return m.group(2) if m else text


def _section_map(markdown: str) -> dict[str, str]:
    matches = list(re.finditer(r"^(#{1,6})\s+(.+?)\s*$", markdown, flags=re.MULTILINE))
    if not matches:
        return {"full": markdown.strip()}
    sections: dict[str, str] = {}
    for idx, match in enumerate(matches):
        level = len(match.group(1))
        start = match.start()
        end = len(markdown)
        for later in matches[idx + 1 :]:
            if len(later.group(1)) <= level:
                end = later.start()
                break
        title = match.group(2).strip()
        sections[title] = markdown[start:end].strip()
    return sections


def list_skill_sections(name: str) -> list[str]:
    return list(_section_map(load_skill_body(name)).keys())


def list_playbook_sections(path: Optional[Path] = None) -> list[str]:
    return list(_section_map(load_playbook(path)).keys())


def _find_section(markdown: str, heading: str) -> str:
    sections = _section_map(markdown)
    wanted = " ".join(heading.lower().split())
    for title, body in sections.items():
        if " ".join(title.lower().split()) == wanted:
            return body
    for title, body in sections.items():
        normalized = " ".join(title.lower().split())
        if wanted in normalized or normalized in wanted:
            return body
    return ""


def load_skill_section(name: str, heading: str) -> str:
    return _find_section(load_skill_body(name), heading)


def load_playbook_section(heading: str, path: Optional[Path] = None) -> str:
    return _find_section(load_playbook(path), heading)


def list_skills() -> list[dict]:
    out: list[dict] = []
    for name in sorted(discover_skills()):
        try:
            fm = load_skill_frontmatter(name)
        except FileNotFoundError:
            continue
        out.append(
            {
                "name": name,
                "description": fm.get("description", ""),
                "argument_hint": fm.get("argument-hint"),
                "external": is_external_skill(name),
            }
        )
    return out


def load_playbook(path: Optional[Path] = None) -> str:
    """Load the general playbook from the configured path."""
    if path is None:
        from ..config import current_settings

        path = current_settings().playbook_path
    if not path.is_file():
        return ""
    return path.read_text(encoding="utf-8")


__all__ = [
    "SKILL_NAMES",
    "skill_roots",
    "discover_skills",
    "internal_skill_names",
    "skill_dir",
    "is_external_skill",
    "skill_path",
    "skill_manifest",
    "load_skill_text",
    "load_skill_frontmatter",
    "load_skill_body",
    "list_skill_sections",
    "list_playbook_sections",
    "load_skill_section",
    "load_playbook_section",
    "list_skills",
    "load_playbook",
]
