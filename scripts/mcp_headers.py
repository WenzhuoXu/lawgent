#!/usr/bin/env python3
"""Claude Code ``headersHelper`` for the HTTP MCP servers in ``.mcp.json``.

Claude Code runs this when it connects to one of those servers and sends the
JSON object printed on stdout as request headers. The token is read from the
same places the harness reads it — the process environment, then
``legal_helper/api_key``, then ``.env`` — so it lives in one file and never
in ``.mcp.json``. Claude Code runs a project's helper without the user's
credential variables and with whatever ``python3`` is on its PATH, so this
reads the files itself and uses the standard library only.
"""

from __future__ import annotations

import json
import os
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]

# Server-name prefix → (variable, header, value template).
_AUTH = {
    "pkulaw_": ("PKULAW_API_TOKEN", "Authorization", "Bearer {}"),
}


def _file_values(path: Path) -> dict[str, str]:
    """``KEY=value`` / ``export KEY=value`` lines of a dotenv-style file."""
    try:
        lines = path.read_text(encoding="utf-8").splitlines()
    except OSError:
        return {}
    out: dict[str, str] = {}
    for raw in lines:
        line = raw.strip()
        if not line or line.startswith("#"):
            continue
        if line.startswith("export "):
            line = line[len("export "):].lstrip()
        key, sep, value = line.partition("=")
        if not sep:
            continue
        value = value.strip()
        if len(value) >= 2 and value[0] == value[-1] and value[0] in "'\"":
            value = value[1:-1]
        out[key.strip()] = value
    return out


def _value(name: str) -> str:
    return (
        os.environ.get(name)
        or _file_values(ROOT / "legal_helper" / "api_key").get(name)
        or _file_values(ROOT / ".env").get(name)
        or ""
    )


def headers_for(server: str) -> dict[str, str]:
    for prefix, (variable, header, template) in _AUTH.items():
        if server.startswith(prefix):
            value = _value(variable)
            return {header: template.format(value)} if value else {}
    return {}


def main() -> None:
    server = os.environ.get("CLAUDE_CODE_MCP_SERVER_NAME") or (sys.argv[1] if len(sys.argv) > 1 else "")
    print(json.dumps(headers_for(server)))


if __name__ == "__main__":
    main()
