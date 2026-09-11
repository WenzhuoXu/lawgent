"""Load and filter ``.mcp.json`` entries by jurisdiction + active packs."""

from __future__ import annotations

import json
import os
import re
import shutil
import sys
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, Iterable, Literal, Optional


_ENV_VAR_RE = re.compile(r"\$\{([A-Za-z_][A-Za-z0-9_]*)\}")

# CLAUDE.md pins every toolchain to the ``llm`` conda env. A stdio MCP spec
# naming a bare ``python`` / ``npx`` resolves against the *launching* process's
# PATH, which outside that env is the system interpreter (no ``mcp`` package →
# the child exits and the transport reports CONNECTION_CLOSED) or nothing at
# all (npx → ENOENT). Resolve commands against the env we actually run in.
_PREFERRED_ENV = os.getenv("LEGAL_HELPER_MCP_ENV", "llm")


def _candidate_bin_dirs() -> list[Path]:
    """conda bin directories to search, most specific first."""
    dirs: list[Path] = []
    prefix = os.getenv("CONDA_PREFIX")
    if prefix:
        dirs.append(Path(prefix) / "bin")
        # …/envs/<name> → …/envs/<preferred>
        envs = Path(prefix).parent
        if envs.name == "envs":
            dirs.append(envs / _PREFERRED_ENV / "bin")
    # The interpreter running us is the most reliable anchor of all.
    dirs.append(Path(sys.executable).parent)
    seen: set[Path] = set()
    out: list[Path] = []
    for d in dirs:
        if d not in seen:
            seen.add(d)
            out.append(d)
    return out


def resolve_command(command: Optional[str]) -> Optional[str]:
    """Resolve a stdio MCP ``command`` to an absolute executable.

    ``python``/``python3`` map to ``sys.executable`` so an in-tree server always
    runs under the same interpreter (and therefore the same installed deps) as
    the harness. Everything else is looked up on PATH first, then in the conda
    bin directories. Returns ``None`` when nothing resolves, so callers can
    report a named error instead of spawning a doomed child process.
    """
    if not command:
        return None
    if command in {"python", "python3", sys.executable}:
        return sys.executable
    candidate = Path(command)
    if candidate.is_absolute():
        return command if os.access(command, os.X_OK) else None
    found = shutil.which(command)
    if found:
        return found
    for bin_dir in _candidate_bin_dirs():
        cand = bin_dir / command
        if cand.is_file() and os.access(cand, os.X_OK):
            return str(cand)
    return None


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

    def launch_command(self) -> Optional[str]:
        """Absolute executable for a stdio spec, or ``None`` if unresolvable."""
        return resolve_command(self.command)


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


def unresolvable_stdio_specs(specs: Iterable[McpServerSpec]) -> list[tuple[str, str]]:
    """``(server_name, command)`` for every stdio spec whose command is missing.

    Used by the health probe so a misconfigured launcher surfaces as a named
    diagnostic rather than as an opaque closed pipe.
    """
    bad: list[tuple[str, str]] = []
    for s in specs:
        if s.transport != "stdio" or not s.command:
            continue
        if s.launch_command() is None:
            bad.append((s.name, s.command))
    return bad


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
