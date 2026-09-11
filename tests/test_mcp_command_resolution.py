"""stdio MCP launcher resolution: bare commands must reach the harness env."""

from __future__ import annotations

import sys

from legal_helper.mcp.registry import (
    McpServerSpec,
    resolve_command,
    unresolvable_stdio_specs,
)


def test_python_resolves_to_the_running_interpreter():
    # The in-tree ccar_aviation server declares a bare "python"; outside the
    # conda env that is the system interpreter, which has no `mcp` package and
    # exits immediately (the CONNECTION_CLOSED failure).
    assert resolve_command("python") == sys.executable
    assert resolve_command("python3") == sys.executable


def test_missing_command_resolves_to_none_rather_than_spawning():
    assert resolve_command("definitely-not-a-real-binary-xyz") is None
    assert resolve_command(None) is None
    assert resolve_command("") is None


def test_absolute_command_must_be_executable():
    assert resolve_command(sys.executable) == sys.executable
    assert resolve_command("/nonexistent/bin/thing") is None


def test_unresolvable_stdio_specs_names_the_offender():
    specs = [
        McpServerSpec(name="good", command="python"),
        McpServerSpec(name="bad", command="definitely-not-a-real-binary-xyz"),
        McpServerSpec(name="remote", transport="http", url="https://example.invalid"),
    ]
    assert unresolvable_stdio_specs(specs) == [
        ("bad", "definitely-not-a-real-binary-xyz")
    ]
