"""Check the Claude Agent SDK and its bundled CLI without a model call.

Starts the CLI with the options a real chat turn uses (system prompt, the
in-process `legal` MCP server, the plugin's skills, the sub-agents) and asks it
only control requests: server info, MCP status, context usage, and the plan's
usage. Nothing reaches the model, so the check costs no tokens and no plan
quota. Run it before and after an SDK upgrade and compare the two reports.

    python scripts/agent_sdk_check.py --out before.json
    PYTHONPATH=<staged dir holding claude_agent_sdk> python scripts/agent_sdk_check.py --out after.json

Exit status 1 when a check fails. The report holds no account e-mail or token.
"""

from __future__ import annotations

import argparse
import asyncio
import json
import subprocess
import sys
import tempfile
import time
from pathlib import Path
from typing import Any

sys.path.insert(1, str(Path(__file__).resolve().parents[1]))


def _login(cli: Path, env: dict[str, str]) -> dict[str, Any]:
    """`claude auth status`, reduced to how the CLI is signed in and until when."""
    try:
        out = subprocess.run([str(cli), "auth", "status"], env=env, capture_output=True, text=True, timeout=30)
        data = json.loads(out.stdout)
    except Exception as exc:  # noqa: BLE001 — reported, not fatal
        return {"error": f"{type(exc).__name__}: {exc}"[:200]}
    keep = ("loggedIn", "authMethod", "apiProvider", "subscriptionType", "loginExpiresAt")
    return {k: data.get(k) for k in keep if k in data}


async def _probe(opts: Any) -> dict[str, Any]:
    from claude_agent_sdk import ClaudeSDKClient

    from legal_helper.claude_code.runtime import request_plan_usage

    out: dict[str, Any] = {}
    async with ClaudeSDKClient(opts) as client:
        # The in-process `legal` server connects a moment after the CLI starts.
        t0 = time.monotonic()
        while time.monotonic() - t0 < 15:
            status = await client.get_mcp_status()
            if any(s.get("name") == "legal" and s.get("status") == "connected" for s in status.get("mcpServers") or []):
                break
            await asyncio.sleep(0.5)
        out.setdefault("_seconds", {})["legal_connected"] = round(time.monotonic() - t0, 2)
        for key, call in (
            ("server_info", client.get_server_info),
            ("mcp_status", client.get_mcp_status),
            ("context_usage", client.get_context_usage),
            ("plan_usage_raw", lambda: request_plan_usage(client, timeout=20)),
        ):
            t0 = time.monotonic()
            try:
                out[key] = await call()
            except Exception as exc:  # noqa: BLE001 — every request is reported on its own
                out[key] = {"error": f"{type(exc).__name__}: {exc}"[:300]}
            out.setdefault("_seconds", {})[key] = round(time.monotonic() - t0, 2)
    return out


def _as_dict(value: Any) -> Any:
    if hasattr(value, "__dataclass_fields__"):
        from dataclasses import asdict

        return asdict(value)
    return value


def main() -> int:
    ap = argparse.ArgumentParser(description=__doc__.split("\n\n")[0])
    ap.add_argument("--out", help="write the full report as JSON here")
    args = ap.parse_args()

    import claude_agent_sdk
    from claude_agent_sdk._cli_version import __cli_version__

    from legal_helper.claude_code import prompt as cc_prompt
    from legal_helper.claude_code.runtime import normalize_plan_usage, options, subprocess_env
    from legal_helper.claude_code.tools import legal_tool_server, legal_tools
    from legal_helper.config import load_settings
    from legal_helper.turn_context import TurnContext, open_turn

    settings = load_settings()
    model = settings.model_for_provider()
    cli = Path(claude_agent_sdk.__file__).parent / "_bundled" / "claude"
    expected_tools = sorted(t.name for t in legal_tools(settings))
    with tempfile.TemporaryDirectory(prefix="agent-sdk-check-") as tmp, open_turn(TurnContext(settings=settings)):
        cwd = Path(tmp)
        opts = options(
            settings,
            model=model,
            system_prompt=cc_prompt.author_system_prompt(settings, uploads_dir=cwd),
            cwd=cwd,
            mcp_server=legal_tool_server(settings),
            agents=cc_prompt.agents(settings),
            persist=False,
        )
        t0 = time.monotonic()
        raw = asyncio.run(_probe(opts))
        seconds = round(time.monotonic() - t0, 1)

    info = raw.get("server_info") or {}
    if isinstance(info, dict):
        info.pop("account", None)  # e-mail and organisation: never written out
    mcp = _as_dict(raw.get("mcp_status")) or {}
    ctx = _as_dict(raw.get("context_usage")) or {}
    servers = mcp.get("mcpServers") or mcp.get("servers") or []
    legal = next((s for s in servers if s.get("name") == "legal"), {})
    got_tools = sorted(
        str(t.get("name") if isinstance(t, dict) else t).removeprefix("mcp__legal__")
        for t in (legal.get("tools") or [])
    )
    plan_raw = raw.get("plan_usage_raw")
    plan = normalize_plan_usage(plan_raw) if isinstance(plan_raw, dict) and "error" not in plan_raw else None

    checks = {
        "cli_started": "error" not in info,
        "legal_server_connected": legal.get("status") == "connected",
        "legal_tools_complete": bool(got_tools) and set(expected_tools) <= set(got_tools) if got_tools else None,
        "context_usage_read": "error" not in ctx and bool(ctx),
        "plan_usage_read": plan is not None and bool(plan.get("limits")),
        "signed_in_with_subscription": None,
    }
    login = _login(cli, subprocess_env())
    if "error" not in login:
        checks["signed_in_with_subscription"] = bool(login.get("loggedIn")) and login.get("authMethod") not in (
            "api_key",
            "apiKey",
        )
    report = {
        "sdk_version": claude_agent_sdk.__version__,
        "cli_version": __cli_version__,
        "model": model,
        "seconds": seconds,
        "request_seconds": raw.get("_seconds"),
        "login": login,
        "legal_tools": {"expected": len(expected_tools), "reported": len(got_tools),
                        "missing": sorted(set(expected_tools) - set(got_tools)),
                        "unexpected": sorted(set(got_tools) - set(expected_tools))},
        "server_info_keys": sorted(info) if isinstance(info, dict) else str(info)[:200],
        "commands": len(info.get("commands") or []) if isinstance(info, dict) else None,
        "agents": [a.get("name") for a in (info.get("agents") or []) if isinstance(a, dict)]
        if isinstance(info, dict)
        else None,
        "mcp_servers": [{"name": s.get("name"), "status": s.get("status")} for s in servers],
        "context_usage": {
            **{k: ctx.get(k) for k in ("totalTokens", "maxTokens", "autoCompactThreshold", "isAutoCompactEnabled") if k in ctx},
            "categories": {c.get("id"): c.get("tokens") for c in ctx.get("categories") or []},
            "skills_included": (ctx.get("skills") or {}).get("includedSkills"),
            "mcp_tools": len(ctx.get("mcpTools") or []),
        },
        "plan_usage": plan,
        "checks": checks,
    }
    print(json.dumps({k: v for k, v in report.items() if k != "plan_usage"}, ensure_ascii=False, indent=1))
    if args.out:
        full = dict(report, raw={k: _as_dict(v) for k, v in raw.items()})
        Path(args.out).write_text(json.dumps(full, ensure_ascii=False, indent=1, default=str), encoding="utf-8")
    failed = [k for k, v in checks.items() if v is False]
    if failed:
        print("FAILED:", ", ".join(failed), file=sys.stderr)
    return 1 if failed else 0


if __name__ == "__main__":
    raise SystemExit(main())
