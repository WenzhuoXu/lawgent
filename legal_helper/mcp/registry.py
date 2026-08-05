"""Load and filter ``.mcp.json`` entries by jurisdiction + active packs."""

from __future__ import annotations

import json
import os
import re
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, Iterable, Literal, Optional


_ENV_VAR_RE = re.compile(r"\$\{([A-Za-z_][A-Za-z0-9_]*)\}")


@dataclass(frozen=True)
class McpServerSpec:
    name: str
    transport: Literal["stdio", "http"] = "stdio"
    command: Optional[str] = None
    args: tuple[str, ...] = ()
    env: dict[str, str] = field(default_factory=dict)
    url: Optional[str] = None
    headers: dict[str, str] = field(default_factory=dict)
    jurisdictions: tuple[str, ...] = ()
    domain_packs: tuple[str, ...] = ()


def _expand_env(value: str) -> str:
    """Replace every ``${VAR}`` substring with its env value (or empty)."""
    return _ENV_VAR_RE.sub(lambda m: os.getenv(m.group(1), ""), value)


def _expand_env_dict(d: dict[str, str] | None) -> dict[str, str]:
    return {k: _expand_env(v) for k, v in (d or {}).items()}


def _spec_from_obj(name: str, obj: dict[str, Any]) -> McpServerSpec:
    transport = "http" if "url" in obj else "stdio"
    return McpServerSpec(
        name=name,
        transport=transport,
        command=obj.get("command"),
        args=tuple(obj.get("args") or ()),
        env=_expand_env_dict(obj.get("env")),
        url=obj.get("url"),
        headers=_expand_env_dict(obj.get("headers")),
        jurisdictions=tuple(obj.get("jurisdictions") or ()),
        domain_packs=tuple(obj.get("domain_packs") or ()),
    )


def load_registry(path: Path | None = None) -> list[McpServerSpec]:
    """Load every server declared in ``.mcp.json``, plus the built-in
    first-party MCP specs (CourtListener, GovInfo) for any name the file
    does not declare itself — ``.mcp.json`` always wins on conflicts."""
    if path is None:
        # default: ./.mcp.json at repo root
        root = Path(__file__).resolve().parents[2]
        path = root / ".mcp.json"
    out: list[McpServerSpec] = []
    if path.is_file():
        payload = json.loads(path.read_text(encoding="utf-8"))
        for name, obj in (payload.get("mcpServers") or {}).items():
            if not isinstance(obj, dict):
                continue
            out.append(_spec_from_obj(name, obj))
    from .first_party import first_party_specs  # local import — avoids cycle

    declared = {s.name for s in out}
    out.extend(s for s in first_party_specs() if s.name not in declared)
    return out


def filter_mcps(
    specs: Iterable[McpServerSpec],
    jurisdictions: Iterable[str],
    active_packs: Iterable[str],
) -> list[McpServerSpec]:
    """Return the specs visible under the given jurisdictions + active packs."""
    juris = set(jurisdictions)
    packs = set(active_packs)
    visible: list[McpServerSpec] = []
    for s in specs:
        if s.domain_packs and not (set(s.domain_packs) & packs):
            continue
        if s.jurisdictions and juris and not (set(s.jurisdictions) & juris):
            continue
        visible.append(s)
    return visible
