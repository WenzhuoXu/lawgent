"""What every Claude Code entry point shares: environment, plugin, options."""

from __future__ import annotations

import json
import os
import sys
import threading
from pathlib import Path
from typing import Any, Optional

from ..config import Settings

#: The in-process MCP server and the plugin both carry this name, so the legal
#: tools are ``mcp__legal__<tool>`` and the methods ``legal:<skill>``.
SERVER_NAME = "legal"
PLUGIN_NAME = "legal"
TOOL_PREFIX = f"mcp__{SERVER_NAME}__"

#: Claude Code's own tools a turn may use. Web, file and shell work go to these
#: before any legal tool that overlaps them; ToolSearch lets Claude Code keep
#: the legal tools deferred until one is needed.
BUILTIN_TOOLS = (
    "WebSearch",
    "WebFetch",
    "Read",
    "Write",
    "Edit",
    "Glob",
    "Grep",
    "Bash",
    "TodoWrite",
    "Agent",
    "Skill",
    "ToolSearch",
)

#: The tool that starts a sub-agent (older CLIs call it Task).
AGENT_TOOLS = frozenset({"Agent", "Task"})

_EFFORTS = ("low", "medium", "high", "xhigh", "max")

#: Environment names that hold credentials. The CLI runs Bash for the model, so
#: none of the harness's keys may reach its environment; the legal tools run in
#: this process and keep them.
_SECRET_MARKERS = ("KEY", "TOKEN", "SECRET", "PASSWORD", "CREDENTIAL")


def effort_for(settings: Settings, role: str = "author") -> str:
    """The SDK effort for a role: the shared dial, with "none" at the lowest."""
    role_settings, _fast = settings.for_role(role)
    effort = (role_settings.openai_reasoning_effort or "medium").strip().lower()
    return effort if effort in _EFFORTS else "low"


def subprocess_env() -> dict[str, str]:
    """Overrides for the CLI's environment.

    The subscription, never an API key: a set ``ANTHROPIC_API_KEY`` would take
    precedence over the claude.ai login and bill the API. The CLI's own
    ``CLAUDE_CODE_*`` variables are left alone. Bash gets this interpreter's
    directory first on PATH, so a skill's ``python`` has the harness's packages.
    """
    env = {
        name: ""
        for name in os.environ
        if not name.startswith("CLAUDE_CODE_")
        and any(marker in name.upper() for marker in _SECRET_MARKERS)
    }
    env["ANTHROPIC_API_KEY"] = ""
    env["ANTHROPIC_AUTH_TOKEN"] = ""
    # A turn ends when its result arrives, and the client then stops the CLI:
    # a sub-agent or a tool call sent to the background would be killed
    # unfinished, after the answer said it was running. Everything runs in the
    # foreground — parallel sub-agents are parallel Agent calls.
    env["CLAUDE_CODE_DISABLE_BACKGROUND_TASKS"] = "1"
    env["CLAUDE_CODE_DISABLE_MCP_TASK_BACKGROUND"] = "1"
    env["PATH"] = str(Path(sys.executable).parent) + os.pathsep + os.environ.get("PATH", "")
    return env


_PLUGIN_LOCK = threading.Lock()


def plugin_dir(settings: Settings) -> Path:
    """A plugin whose skills are every discovered skill, linked in place.

    In-tree methods and external procedures alike (`skills.discover_skills`,
    in-tree first); links are refreshed on every call, so a skill installed or
    removed while the server runs is picked up by the next turn.
    """
    from ..skills import discover_skills

    root = settings.state_dir / "claude_code" / "plugin"
    skills_root = root / "skills"
    wanted = discover_skills()
    with _PLUGIN_LOCK:
        skills_root.mkdir(parents=True, exist_ok=True)
        manifest = root / ".claude-plugin" / "plugin.json"
        body = json.dumps(
            {
                "name": PLUGIN_NAME,
                "version": "1.0.0",
                "description": "legal_helper's legal methods and document procedures.",
            },
            indent=2,
        )
        manifest.parent.mkdir(parents=True, exist_ok=True)
        if not manifest.is_file() or manifest.read_text(encoding="utf-8") != body:
            manifest.write_text(body, encoding="utf-8")
        for entry in skills_root.iterdir():
            target = wanted.get(entry.name)
            if entry.is_symlink() and (target is None or entry.resolve() != target):
                entry.unlink()
        for name, directory in wanted.items():
            link = skills_root / name
            if not link.is_symlink() and not link.exists():
                link.symlink_to(directory, target_is_directory=True)
    return root


def skill_names() -> list[str]:
    """The plugin's skills as Claude Code names them."""
    from ..skills import discover_skills

    return [f"{PLUGIN_NAME}:{name}" for name in sorted(discover_skills())]


def _rate_limit_path(settings: Settings) -> Path:
    return settings.state_dir / "claude_code" / "rate_limit.json"


def remember_rate_limit(settings: Settings, info: dict[str, Any]) -> None:
    """Keep the subscription's latest usage window for the usage panel."""
    from datetime import datetime, timezone

    path = _rate_limit_path(settings)
    try:
        path.parent.mkdir(parents=True, exist_ok=True)
        stamped = {**info, "observed_at": datetime.now(timezone.utc).isoformat(timespec="seconds")}
        path.write_text(json.dumps(stamped), encoding="utf-8")
    except OSError:
        pass


def last_rate_limit(settings: Settings) -> Optional[dict[str, Any]]:
    try:
        return json.loads(_rate_limit_path(settings).read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return None


#: How long a fetched plan usage is served before the CLI is asked again.
PLAN_USAGE_MAX_AGE_S = 60.0


def normalize_plan_usage(raw: dict[str, Any]) -> dict[str, Any]:
    """The plan part of the CLI's `get_usage` answer (what `/usage` shows).

    Kept: the plan type, every limit window (kind, percent used, reset time,
    severity) and the extra-usage credits. Dropped: the CLI's own session
    totals and its behaviour recommendations, which describe Claude Code use
    on this machine, not the plan.
    """
    from datetime import datetime, timezone

    limits_raw = (raw.get("rate_limits") or {}) if isinstance(raw, dict) else {}
    limits = [
        {
            "kind": str(item.get("kind") or item.get("group") or "limit"),
            "group": item.get("group"),
            "percent": item.get("percent"),
            "resets_at": item.get("resets_at"),
            "severity": item.get("severity") or "normal",
            "is_active": item.get("is_active", True),
            "scope": item.get("scope"),
        }
        for item in limits_raw.get("limits") or []
        if isinstance(item, dict)
    ]
    extra = limits_raw.get("extra_usage") or {}
    return {
        "source": "claude_code",
        "subscription_type": raw.get("subscription_type"),
        "available": bool(raw.get("rate_limits_available", bool(limits))),
        "limits": limits,
        "extra_usage": {
            "enabled": bool(extra.get("is_enabled")),
            "utilization": extra.get("utilization"),
            "used_credits": extra.get("used_credits"),
            "monthly_limit": extra.get("monthly_limit"),
            "currency": extra.get("currency"),
        },
        "fetched_at": datetime.now(timezone.utc).isoformat(timespec="seconds"),
    }


def _plan_usage_path(settings: Settings) -> Path:
    return settings.state_dir / "claude_code" / "plan_usage.json"


def remember_plan_usage(settings: Settings, usage: dict[str, Any]) -> None:
    path = _plan_usage_path(settings)
    try:
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(json.dumps(usage, ensure_ascii=False), encoding="utf-8")
    except OSError:
        pass


async def request_plan_usage(client: Any, timeout: float = 15.0) -> dict[str, Any]:
    """Ask a connected CLI for the plan's usage (`get_usage`, no model call).

    The SDK (0.2.176) has no public method for this control request; it goes
    through the same channel as its own `get_context_usage`. The login token
    stays inside the CLI.
    """
    return await client._query._send_control_request({"subtype": "get_usage"}, timeout=timeout)


def fetch_plan_usage(settings: Settings) -> Optional[dict[str, Any]]:
    """Start a CLI with no tools and no prompt, and read the plan's usage."""
    import asyncio

    from claude_agent_sdk import ClaudeAgentOptions, ClaudeSDKClient

    cwd = settings.state_dir / "claude_code" / "oneshot"
    cwd.mkdir(parents=True, exist_ok=True)
    opts = ClaudeAgentOptions(
        tools=[],
        setting_sources=[],
        strict_mcp_config=True,
        env=subprocess_env(),
        cwd=str(cwd),
        extra_args={"no-session-persistence": None},
    )
    box: dict[str, Any] = {}

    async def main() -> None:
        async with ClaudeSDKClient(opts) as client:
            box["raw"] = await request_plan_usage(client)

    worker = threading.Thread(target=lambda: asyncio.run(main()), daemon=True)
    worker.start()
    worker.join(timeout=45)
    raw = box.get("raw")
    if not isinstance(raw, dict):
        return None
    usage = normalize_plan_usage(raw)
    remember_plan_usage(settings, usage)
    return usage


def plan_usage(settings: Settings, *, max_age_s: float = PLAN_USAGE_MAX_AGE_S) -> Optional[dict[str, Any]]:
    """The plan's usage: the last reading if it is recent enough, else a fresh one.

    A failed fetch falls back to the last reading, however old (its
    ``fetched_at`` says when it was taken).
    """
    from datetime import datetime, timezone

    cached: Optional[dict[str, Any]] = None
    try:
        cached = json.loads(_plan_usage_path(settings).read_text(encoding="utf-8"))
        age = (datetime.now(timezone.utc) - datetime.fromisoformat(cached["fetched_at"])).total_seconds()
        if age < max_age_s:
            return cached
    except (OSError, ValueError, KeyError, TypeError):
        cached = None
    try:
        fresh = fetch_plan_usage(settings)
    except Exception:  # noqa: BLE001 — the panel shows the last reading instead
        fresh = None
    return fresh or cached


def display_tool_name(name: str) -> str:
    """A tool's name as the UI knows it: the legal tools without their prefix."""
    return name[len(TOOL_PREFIX):] if name.startswith(TOOL_PREFIX) else name


def options(
    settings: Settings,
    *,
    model: str,
    system_prompt: str,
    cwd: Path,
    effort: Optional[str] = None,
    tools: tuple[str, ...] = BUILTIN_TOOLS,
    mcp_server: Any = None,
    resume: Optional[str] = None,
    add_dirs: tuple[Path, ...] = (),
    agents: Optional[dict[str, Any]] = None,
    partial: bool = True,
    persist: bool = True,
    output_format: Optional[dict[str, Any]] = None,
) -> Any:
    """ClaudeAgentOptions for a legal_helper session.

    Nothing from the filesystem loads: no settings, CLAUDE.md, user skills or
    user MCP servers — this repository's developer instructions and the
    user's own Claude Code setup must not reach a legal answer. Every tool on
    the list runs without a prompt (``dontAsk`` denies anything else), since a
    web chat has nobody to answer one.
    """
    from claude_agent_sdk import ClaudeAgentOptions

    servers = {SERVER_NAME: mcp_server} if mcp_server is not None else {}
    # The SDK adds a `Skill(<name>)` rule per listed skill; a bare "Skill" rule
    # would admit every skill the CLI bundles as well.
    allowed = [t for t in tools if t != "Skill"] + ([f"mcp__{SERVER_NAME}"] if servers else [])
    plugin = plugin_dir(settings)
    extra: dict[str, Optional[str]] = {}
    if not persist:
        extra["no-session-persistence"] = None
    cwd.mkdir(parents=True, exist_ok=True)
    return ClaudeAgentOptions(
        model=model,
        system_prompt={"type": "custom", "prompt": system_prompt, "snapshot": False},
        tools=list(tools),
        allowed_tools=allowed,
        permission_mode="dontAsk",
        mcp_servers=servers,
        strict_mcp_config=True,
        setting_sources=[],
        plugins=[{"type": "local", "path": str(plugin)}] if tools else [],
        skills=skill_names() if "Skill" in tools else None,
        agents=agents,
        cwd=str(cwd),
        add_dirs=[str(d) for d in add_dirs if d],
        include_partial_messages=partial,
        effort=effort or effort_for(settings),
        thinking={"type": "adaptive", "display": "summarized"},
        resume=resume,
        env=subprocess_env(),
        extra_args=extra,
        output_format=output_format,
        # A rendered page comes back as a base64 image in one stdout line.
        max_buffer_size=64 * 1024 * 1024,
    )
