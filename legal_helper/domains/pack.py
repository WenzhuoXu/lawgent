"""DomainPack loader.

Each pack is a directory under ``legal_helper/domains/<name>/`` containing:

    pack.yaml          — metadata (name, jurisdictions, mcp_servers, skill_overlays)
    playbook.md        — domain-specific playbook (loaded after general_playbook.md)
    references/        — long-form domain prose pulled on demand
    overlays/<skill>.md — per-skill aviation deltas concatenated after SKILL.md

A pack is active when its name appears in ``settings.active_domain_packs`` (or
is passed via the ``--domain-pack`` flag / ``ChatSettings.domain_pack``).
"""

from __future__ import annotations

from dataclasses import dataclass, field
from pathlib import Path
from typing import Iterable

import yaml


_DOMAINS_ROOT = Path(__file__).resolve().parent


@dataclass(frozen=True)
class DomainPack:
    name: str
    display_name: str
    jurisdictions: tuple[str, ...]
    mcp_servers: tuple[str, ...]
    skill_overlays: dict[str, str]
    root: Path
    playbook_path: Path
    references_dir: Path
    overlay_dir: Path
    # Marker bundles used by domains.detect.detect_packs for auto-activation.
    # ``english`` matches are word-boundary aware; ``chinese`` is substring.
    markers_english: tuple[str, ...] = ()
    markers_chinese: tuple[str, ...] = ()
    default_language: str = "en"
    description: str = ""

    def overlay_for(self, skill_name: str) -> Path | None:
        rel = self.skill_overlays.get(skill_name)
        if not rel:
            return None
        path = self.root / rel
        return path if path.is_file() else None

    def list_references(self) -> list[Path]:
        if not self.references_dir.is_dir():
            return []
        return sorted(self.references_dir.glob("*.md"))


def _load_yaml(path: Path) -> dict:
    if not path.is_file():
        return {}
    return yaml.safe_load(path.read_text(encoding="utf-8")) or {}


def load_pack(name: str) -> DomainPack:
    """Load a single domain pack by directory name."""
    root = _DOMAINS_ROOT / name
    pack_yaml = root / "pack.yaml"
    if not pack_yaml.is_file():
        raise FileNotFoundError(f"domain pack '{name}' not found at {root}")
    meta = _load_yaml(pack_yaml)
    markers = meta.get("markers") or {}
    return DomainPack(
        name=meta.get("name", name),
        display_name=meta.get("display_name", name.title()),
        jurisdictions=tuple(meta.get("jurisdictions", ()) or ()),
        mcp_servers=tuple(meta.get("mcp_servers", ()) or ()),
        skill_overlays=dict(meta.get("skill_overlays", {}) or {}),
        root=root,
        playbook_path=root / meta.get("playbook_path", "playbook.md"),
        references_dir=root / meta.get("references_dir", "references"),
        overlay_dir=root / meta.get("overlay_dir", "overlays"),
        markers_english=tuple(m.lower() for m in (markers.get("english") or ())),
        markers_chinese=tuple(markers.get("chinese") or ()),
        default_language=meta.get("default_language", "en"),
        description=meta.get("description", ""),
    )


def available_packs() -> list[str]:
    """Return the names of every pack directory under domains/."""
    if not _DOMAINS_ROOT.is_dir():
        return []
    out: list[str] = []
    for child in sorted(_DOMAINS_ROOT.iterdir()):
        if child.is_dir() and (child / "pack.yaml").is_file():
            out.append(child.name)
    return out


def load_packs(names: Iterable[str]) -> list[DomainPack]:
    return [load_pack(n) for n in names if n]
