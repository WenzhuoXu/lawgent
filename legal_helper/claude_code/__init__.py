"""Claude Code as the engine of a chat turn (provider ``claude_code``).

The Agent SDK runs the `claude` CLI, signed in with the user's claude.ai
subscription, and Claude Code's own loop answers the turn: its built-in tools
do the web (WebSearch, WebFetch), files (Read, Write, Edit, Glob, Grep),
scripts (Bash), planning (TodoWrite) and delegation (Agent). legal_helper
plugs in beside them as

- ``tools.legal_tool_server`` — the legal tools Claude Code has no equivalent
  for (connectors, the local corpus, Office documents, diagrams, deadlines,
  packs, project memory) as an in-process MCP server named ``legal``;
- ``runtime.plugin_dir`` — the legal methods and the installed procedures
  (ppt-master, …) as the skills of a plugin named ``legal``;
- ``prompt`` — the playbook and answer contract as the system prompt, and the
  two sub-agents (``legal-specialist``, ``deck-builder``);
- ``executor.ClaudeCodeExecutor`` — one turn, translated into the event stream
  the web UI already reads; each chat resumes its own Claude Code session.

``provider.ClaudeCodeProvider`` serves the one-shot side calls (titles, the
rolling summary) on the same login.
"""

from .executor import ClaudeCodeExecutor
from .provider import ClaudeCodeProvider

__all__ = ["ClaudeCodeExecutor", "ClaudeCodeProvider"]
