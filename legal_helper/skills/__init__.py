"""Skill loader and lazy skill-resource helpers."""

from __future__ import annotations

import re
from pathlib import Path
from typing import Optional

import yaml


SKILL_NAMES = (
    "brief",
    "cite-check",
    "compliance-check",
    "docx-redline",
    "draft-agreement",
    "flowchart",
    "legal-response",
    "legal-risk-assessment",
    "litigation-analysis",
    "meeting-briefing",
    "review-contract",
    "signature-request",
    "tabular-review",
    "triage-nda",
    "vendor-check",
)


_HERE = Path(__file__).resolve().parent
_FRONTMATTER_RE = re.compile(r"^---\s*\n(.*?)\n---\s*\n(.*)$", re.DOTALL)


def skill_path(name: str) -> Path:
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
    for name in SKILL_NAMES:
        fm = load_skill_frontmatter(name)
        out.append(
            {
                "name": name,
                "description": fm.get("description", ""),
                "argument_hint": fm.get("argument-hint"),
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
