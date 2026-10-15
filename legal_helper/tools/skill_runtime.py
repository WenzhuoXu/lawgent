"""The runtime an external procedural skill needs to actually run.

An in-tree legal skill produces prose and calls the document writers. An
external skill such as ``ppt-master`` works differently: it *authors text
files* (a design spec, a lock, one SVG per page, a notes document) and it
*runs its own scripts* (an integrity guard, a structure validator, a quality
gate, an exporter). ``write_docx``/``write_pdf`` take structured parameters
and emit binaries; neither writes an ``.svg`` or runs the checker that is the
skill's own gate, so without these tools the skill cannot be driven at all —
not for want of permission but for want of a call.

Both tools are deliberately narrow:

- :func:`write_text_file` writes only under ``outputs_dir``. Not
  ``project_root`` — that contains the source tree, and an agent that can
  overwrite ``legal_helper/*.py`` is a different kind of tool than this.
- :func:`run_skill_script` runs only a script that resolves *inside* a
  discovered skill's own directory, with ``argv`` passed as a list so there is
  no shell to inject into: ``.py`` under this Python, ``.mjs``/``.js``/``.cjs``
  under the Node that ships with it (``writers._node.find_node``). Node was
  needed for gongwen-gbt9704-skill, whose generator and 13-check verifier are
  both ``.mjs``; the containment is identical for every interpreter.

The trust boundary is the skill root, not the call: a script under a
configured root runs with the harness's privileges, which is what "drive this
skill" means. Roots are operator-configured (``LEGAL_HELPER_SKILL_ROOTS``);
never point one at a directory the model can write to, or these two tools
compose into arbitrary code execution.
"""

from __future__ import annotations

import json
import os
import subprocess
import sys
from pathlib import Path
from typing import Any

from anthropic import beta_tool

from ..config import current_settings
from ..skills import skill_dir

#: Refuse a single authored file larger than this. An SVG page runs ~10-40 KB;
#: a megabyte means something has gone wrong upstream, and writing it would
#: only move the failure later.
MAX_TEXT_BYTES = 2_000_000

#: Per-stream cap on captured script output. The head and the tail both matter
#: for a checker: the head carries the per-page verdicts, the tail carries the
#: summary and the exit reason. Keep both and say what was dropped.
_MAX_STREAM_CHARS = 20_000
_HEAD_SHARE = 0.6

#: Hard ceiling on a script's wall clock, whatever the caller asks for.
MAX_TIMEOUT_S = 900


def _outputs_root() -> Path:
    root = current_settings().outputs_dir
    root.mkdir(parents=True, exist_ok=True)
    return root.resolve()


def _resolve_under_outputs(path: str) -> tuple[Path | None, str]:
    """Resolve ``path`` inside ``outputs_dir``, or explain the refusal.

    Resolution happens before the containment test so that ``..`` segments and
    symlinks are collapsed first; testing the unresolved string would pass a
    path that escapes once the OS walks it.
    """
    root = _outputs_root()
    candidate = Path(path).expanduser()
    if not candidate.is_absolute():
        candidate = root / candidate
    try:
        resolved = candidate.resolve()
    except OSError as exc:
        return None, f"ERROR: cannot resolve path {path!r}: {exc}"
    if resolved != root and root not in resolved.parents:
        return None, (
            f"ERROR: refusing to write outside the outputs directory. "
            f"{resolved} is not under {root}. Pass a path relative to "
            f"outputs/, e.g. 'my-deck/svg_output/01_cover.svg'."
        )
    return resolved, ""


@beta_tool
def write_text_file(path: str, content: str, mode: str = "overwrite") -> str:
    """Write a UTF-8 text file (SVG, Markdown, JSON, CSS, …) under outputs/.

    Use this to author the text artifacts a procedural skill asks for — a
    design spec, a lock file, one SVG per page, a speaker-notes document.
    Parent directories are created. For .docx/.pptx/.xlsx/.pdf deliverables
    use the dedicated writers instead; this tool writes text, not documents.

    Args:
        path: Destination, relative to outputs/ (or an absolute path that
            already resolves under it). Parent directories are created.
        content: The exact file content. Written verbatim as UTF-8.
        mode: ``overwrite`` (default), ``append``, or ``create_only`` which
            refuses if the file already exists.
    """
    if mode not in {"overwrite", "append", "create_only"}:
        return f"ERROR: mode must be overwrite, append or create_only (got {mode!r})"
    encoded = content.encode("utf-8")
    if len(encoded) > MAX_TEXT_BYTES:
        return (
            f"ERROR: content is {len(encoded)} bytes, over the "
            f"{MAX_TEXT_BYTES}-byte limit for a single text file."
        )
    resolved, err = _resolve_under_outputs(path)
    if resolved is None:
        return err
    if mode == "create_only" and resolved.exists():
        return f"ERROR: {resolved} already exists and mode is create_only."
    try:
        resolved.parent.mkdir(parents=True, exist_ok=True)
        with open(resolved, "a" if mode == "append" else "w", encoding="utf-8") as fh:
            fh.write(content)
    except OSError as exc:
        return f"ERROR writing {resolved}: {type(exc).__name__}: {exc}"
    return str(resolved)


@beta_tool
def edit_text_file(path: str, old_string: str, new_string: str, replace_all: bool = False) -> str:
    """Replace exact text in a text file under outputs/ — change a file without rewriting it.

    Args:
        path: The file, relative to outputs/ (or an absolute path under it).
        old_string: Text to replace, copied exactly; it must occur once unless replace_all.
        new_string: The replacement.
        replace_all: Replace every occurrence.
    """
    resolved, err = _resolve_under_outputs(path)
    if resolved is None:
        return err
    if not resolved.is_file():
        return f"ERROR: {resolved} does not exist; write it with write_text_file."
    if not old_string:
        return "ERROR: old_string is empty."
    try:
        text = resolved.read_text(encoding="utf-8")
    except (OSError, UnicodeDecodeError) as exc:
        return f"ERROR reading {resolved}: {type(exc).__name__}: {exc}"
    count = text.count(old_string)
    if count == 0:
        return f"ERROR: old_string not found in {resolved}; read the file and copy the text exactly."
    if count > 1 and not replace_all:
        return f"ERROR: old_string occurs {count} times in {resolved}; add context to make it unique, or set replace_all."
    updated = text.replace(old_string, new_string) if replace_all else text.replace(old_string, new_string, 1)
    if len(updated.encode("utf-8")) > MAX_TEXT_BYTES:
        return f"ERROR: the edited file would exceed the {MAX_TEXT_BYTES}-byte limit."
    try:
        resolved.write_text(updated, encoding="utf-8")
    except OSError as exc:
        return f"ERROR writing {resolved}: {type(exc).__name__}: {exc}"
    return f"{resolved} ({count if replace_all else 1} replaced)"


@beta_tool
def list_skill_dir(skill: str, subpath: str = "") -> str:
    """List files and directories inside a skill's own directory.

    A procedural skill's instructions name their references by relative path
    (``workflows/routing.md``). This resolves those against the real skill
    root so paths are read rather than guessed, and reports the absolute
    ``skill_dir`` to build further paths from.

    Args:
        skill: A discovered skill name.
        subpath: Optional directory inside the skill, e.g. ``workflows``.
    """
    try:
        root = _real_skill_dir(skill)
    except FileNotFoundError as exc:
        return f"ERROR: {exc}"
    target = (root / subpath).resolve() if subpath else root
    if target != root and root not in target.parents:
        return f"ERROR: {subpath!r} escapes the skill directory."
    if not target.is_dir():
        return f"ERROR: not a directory: {target}"
    dirs: list[str] = []
    files: list[str] = []
    try:
        for entry in sorted(target.iterdir()):
            if entry.name == "__pycache__":
                continue
            (dirs if entry.is_dir() else files).append(entry.name)
    except OSError as exc:
        return f"ERROR listing {target}: {exc}"
    return json.dumps(
        {
            "skill": skill,
            "skill_dir": str(root),
            "listing_of": str(target),
            "directories": dirs,
            "files": files,
        },
        ensure_ascii=False,
        indent=2,
    )


def _real_skill_dir(skill: str) -> Path:
    """The skill's directory with symlinks resolved.

    A skill root is operator-configured and trusted, and a staged skill is
    often a symlink into a checkout (``root/ppt-master -> …/skills/ppt-master``).
    Comparing a resolved script path against the unresolved link refused every
    script of such a skill as "outside the skill directory". Resolving the
    skill's own directory follows the operator's link once; a script that
    escapes it by ``..`` or by a link of its own is still refused.
    """
    return skill_dir(skill).resolve()


#: Interpreter per script suffix. Python runs under this interpreter so a
#: skill's script sees the harness's own packages; Node is looked up the way
#: every Node renderer here is (``LEGAL_HELPER_NODE`` → beside Python → PATH).
_PYTHON_SUFFIXES = frozenset({".py"})
_NODE_SUFFIXES = frozenset({".mjs", ".js", ".cjs"})


def _interpreter_for(script: Path) -> tuple[list[str] | None, dict[str, str], str]:
    """``(argv prefix, extra env, error)`` for running ``script``."""
    suffix = script.suffix.lower()
    if suffix in _PYTHON_SUFFIXES:
        return [sys.executable], {}, ""
    if suffix in _NODE_SUFFIXES:
        from ..documents.writers._node import find_node, node_diagnostic

        node = find_node()
        if not node:
            return None, {}, f"ERROR: cannot run {script.name}: {node_diagnostic()}"
        # A script that spawns its own children (npx, a second node) must find
        # them beside the interpreter, not on whatever PATH launched the server.
        node_bin = str(Path(node).resolve().parent)
        path = os.environ.get("PATH", "")
        return [node], {"PATH": f"{node_bin}{os.pathsep}{path}" if path else node_bin}, ""
    return None, {}, f"ERROR: only .py, .mjs, .js or .cjs scripts may be run (got {script.name!r})."


def _clip(stream: str) -> tuple[str, bool]:
    if len(stream) <= _MAX_STREAM_CHARS:
        return stream, False
    head = int(_MAX_STREAM_CHARS * _HEAD_SHARE)
    tail = _MAX_STREAM_CHARS - head
    dropped = len(stream) - _MAX_STREAM_CHARS
    return (
        f"{stream[:head]}\n... [{dropped} characters dropped from the middle] ...\n"
        f"{stream[-tail:]}",
        True,
    )


@beta_tool
def run_skill_script(
    skill: str,
    script: str,
    args: list[str] | None = None,
    timeout_s: int = 300,
) -> str:
    """Run one of a skill's own scripts (Python or Node) and return its output.

    This is how a procedural skill's gates and exporters are executed — its
    integrity guard, structure validator, quality checker and export step.
    The script must live inside that skill's directory. Arguments are passed
    as an argv list, not through a shell. Relative paths in ``args`` resolve
    against outputs/, the script's working directory.

    Returns JSON with ``exit_code``, ``stdout``, ``stderr`` and the resolved
    command. A non-zero ``exit_code`` is the script's verdict, not a tool
    failure: read it and act on it.

    Args:
        skill: A discovered skill name that owns the script.
        script: Path to the script relative to the skill directory, e.g.
            ``scripts/svg_quality_checker.py`` or
            ``scripts/verify_gongwen_docx.mjs``. Must end in .py, .mjs, .js or .cjs.
        args: Arguments passed to the script, each as its own list item.
        timeout_s: Wall-clock limit in seconds (default 300, max 900).
    """
    try:
        root = _real_skill_dir(skill)
    except FileNotFoundError as exc:
        return f"ERROR: {exc}"
    candidate = (root / script).expanduser()
    try:
        resolved = candidate.resolve()
    except OSError as exc:
        return f"ERROR: cannot resolve script {script!r}: {exc}"
    prefix, extra_env, error = _interpreter_for(resolved)
    if prefix is None:
        return error
    if root not in resolved.parents:
        return (
            f"ERROR: {resolved} is outside the skill directory {root}. "
            "A skill may only run its own scripts."
        )
    if not resolved.is_file():
        return f"ERROR: script not found: {resolved}"

    argv = [*prefix, str(resolved), *[str(a) for a in (args or [])]]
    limit = max(1, min(int(timeout_s), MAX_TIMEOUT_S))
    env = {**os.environ, "PYTHONIOENCODING": "utf-8", **extra_env}
    try:
        proc = subprocess.run(  # noqa: S603 — argv list, no shell; path is skill-confined
            argv,
            cwd=str(_outputs_root()),
            capture_output=True,
            text=True,
            encoding="utf-8",
            errors="replace",
            timeout=limit,
            env=env,
            check=False,
        )
    except subprocess.TimeoutExpired:
        return json.dumps(
            {
                "command": argv,
                "exit_code": None,
                "timed_out": True,
                "timeout_s": limit,
                "hint": (
                    "The script exceeded its time limit. Do not start a "
                    "long-running server through this tool; it captures output "
                    "until the process exits."
                ),
            },
            ensure_ascii=False,
            indent=2,
        )
    except OSError as exc:
        return f"ERROR running {resolved}: {type(exc).__name__}: {exc}"

    stdout, out_clipped = _clip(proc.stdout or "")
    stderr, err_clipped = _clip(proc.stderr or "")
    payload: dict[str, Any] = {
        "command": argv,
        "cwd": str(_outputs_root()),
        "exit_code": proc.returncode,
        "stdout": stdout,
        "stderr": stderr,
    }
    if out_clipped or err_clipped:
        payload["output_clipped"] = True
    return json.dumps(payload, ensure_ascii=False, indent=2)


__all__ = [
    "edit_text_file",
    "write_text_file",
    "run_skill_script",
    "list_skill_dir",
    "MAX_TEXT_BYTES",
    "MAX_TIMEOUT_S",
]
