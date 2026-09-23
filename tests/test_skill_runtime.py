"""The runtime that lets the harness drive an external procedural skill.

Two of these tests are capability tests and the rest are containment tests.
The containment ones matter more: ``write_text_file`` and ``run_skill_script``
compose into arbitrary code execution if either boundary slips — one writes a
file, the other runs a ``.py`` — so the boundaries are asserted rather than
assumed. Specifically, the write tool must not reach the source tree, and the
script tool must not run anything outside a discovered skill's directory.
"""

from __future__ import annotations

import json
import os
from pathlib import Path

import pytest

from legal_helper.config import load_settings
from legal_helper.skills import (
    discover_skills,
    internal_skill_names,
    is_external_skill,
    skill_roots,
)
from legal_helper.tools.skill_runtime import (
    MAX_TEXT_BYTES,
    list_skill_dir,
    run_skill_script,
    write_text_file,
)


def _call(tool, **kwargs):
    """Invoke a @beta_tool-wrapped function directly."""
    return (getattr(tool, "func", None) or tool)(**kwargs)


@pytest.fixture()
def sandbox(tmp_path, monkeypatch):
    """Point outputs_dir at a temp directory for the duration of a test."""
    outputs = tmp_path / "outputs"
    outputs.mkdir()
    settings = load_settings(refresh=True)
    monkeypatch.setattr(
        "legal_helper.tools.skill_runtime.current_settings",
        lambda: settings.model_copy(update={"outputs_dir": outputs}),
    )
    return outputs


# --------------------------------------------------------------------------
# Discovery
# --------------------------------------------------------------------------


def test_internal_skills_are_not_external():
    for name in internal_skill_names():
        assert not is_external_skill(name), f"{name} ships in-tree"


def test_discovery_covers_the_in_tree_skills():
    found = discover_skills()
    for name in internal_skill_names():
        assert name in found


def test_skill_roots_are_configurable(monkeypatch, tmp_path):
    extra = tmp_path / "elsewhere"
    (extra / "my-proc").mkdir(parents=True)
    (extra / "my-proc" / "SKILL.md").write_text("---\nname: my-proc\n---\nbody\n")
    monkeypatch.setenv("LEGAL_HELPER_SKILL_ROOTS", str(extra))
    assert extra.resolve() in skill_roots()
    assert "my-proc" in discover_skills()


def test_configuring_roots_cannot_remove_the_in_tree_skills(monkeypatch, tmp_path):
    """A mistyped root must not delete the product's legal skills."""
    monkeypatch.setenv("LEGAL_HELPER_SKILL_ROOTS", str(tmp_path / "does-not-exist"))
    found = discover_skills()
    for name in internal_skill_names():
        assert name in found


def test_an_earlier_root_shadows_a_later_one(monkeypatch, tmp_path):
    """A third party must not silently replace a legal methodology."""
    first, second = tmp_path / "a", tmp_path / "b"
    for root, marker in ((first, "FIRST"), (second, "SECOND")):
        (root / "dup").mkdir(parents=True)
        (root / "dup" / "SKILL.md").write_text(f"---\nname: dup\n---\n{marker}\n")
    monkeypatch.setenv(
        "LEGAL_HELPER_SKILL_ROOTS", os.pathsep.join([str(first), str(second)])
    )
    assert discover_skills()["dup"] == first / "dup"


# --------------------------------------------------------------------------
# write_text_file — containment
# --------------------------------------------------------------------------


def test_write_text_file_writes_under_outputs(sandbox):
    out = _call(
        write_text_file,
        path="proj/svg_output/01_cover.svg",
        content='<svg xmlns="http://www.w3.org/2000/svg"/>',
    )
    assert not out.startswith("ERROR"), out
    written = Path(out)
    assert written.is_file()
    assert written.read_text(encoding="utf-8").startswith("<svg")
    assert sandbox in written.parents


@pytest.mark.parametrize(
    "path",
    [
        "../escaped.txt",
        "../../escaped.txt",
        "/etc/passwd",
        "proj/../../escaped.txt",
    ],
)
def test_write_text_file_refuses_to_escape_outputs(sandbox, path):
    result = _call(write_text_file, path=path, content="x")
    assert result.startswith("ERROR"), f"{path} was not refused"
    assert "outside the outputs directory" in result


def test_write_text_file_cannot_reach_the_source_tree(sandbox):
    """The sandbox is outputs_dir, not project_root, for exactly this reason."""
    result = _call(
        write_text_file, path="../legal_helper/agent.py", content="# pwned"
    )
    assert result.startswith("ERROR")
    assert Path("legal_helper/agent.py").read_text(encoding="utf-8") != "# pwned"


def test_write_text_file_modes(sandbox):
    first = _call(write_text_file, path="a.md", content="one\n")
    assert not first.startswith("ERROR")
    _call(write_text_file, path="a.md", content="two\n", mode="append")
    assert Path(first).read_text(encoding="utf-8") == "one\ntwo\n"
    refused = _call(write_text_file, path="a.md", content="x", mode="create_only")
    assert refused.startswith("ERROR") and "create_only" in refused
    _call(write_text_file, path="a.md", content="replaced\n")
    assert Path(first).read_text(encoding="utf-8") == "replaced\n"
    assert _call(write_text_file, path="a.md", content="x", mode="nonsense").startswith(
        "ERROR"
    )


def test_write_text_file_rejects_an_oversized_file(sandbox):
    result = _call(write_text_file, path="big.svg", content="x" * (MAX_TEXT_BYTES + 1))
    assert result.startswith("ERROR")
    assert "limit" in result


# --------------------------------------------------------------------------
# run_skill_script — containment and capability
# --------------------------------------------------------------------------


@pytest.fixture()
def proc_skill(monkeypatch, tmp_path):
    """A minimal external procedural skill with one script of its own."""
    root = tmp_path / "roots"
    skill = root / "probe-proc"
    (skill / "scripts").mkdir(parents=True)
    (skill / "SKILL.md").write_text("---\nname: probe-proc\n---\nprocedure\n")
    (skill / "scripts" / "gate.py").write_text(
        "import sys\n"
        "print('checked', *sys.argv[1:])\n"
        "sys.exit(3 if '--fail' in sys.argv else 0)\n"
    )
    outside = tmp_path / "outside.py"
    outside.write_text("print('should never run')\n")
    monkeypatch.setenv("LEGAL_HELPER_SKILL_ROOTS", str(root))
    return skill, outside


def test_run_skill_script_runs_the_skills_own_script(sandbox, proc_skill):
    payload = json.loads(
        _call(run_skill_script, skill="probe-proc", script="scripts/gate.py", args=["a"])
    )
    assert payload["exit_code"] == 0
    assert "checked a" in payload["stdout"]


def test_a_nonzero_exit_is_a_verdict_not_a_tool_error(sandbox, proc_skill):
    """A failing gate must reach the model as data it can act on."""
    payload = json.loads(
        _call(
            run_skill_script,
            skill="probe-proc",
            script="scripts/gate.py",
            args=["--fail"],
        )
    )
    assert payload["exit_code"] == 3
    assert "stdout" in payload and "stderr" in payload


@pytest.mark.parametrize(
    "script",
    [
        "../outside.py",
        "../../outside.py",
        "scripts/../../outside.py",
    ],
)
def test_run_skill_script_refuses_paths_outside_the_skill(sandbox, proc_skill, script):
    result = _call(run_skill_script, skill="probe-proc", script=script)
    assert result.startswith("ERROR"), f"{script} was not refused"
    assert "outside the skill directory" in result


def test_run_skill_script_refuses_non_python(sandbox, proc_skill):
    result = _call(run_skill_script, skill="probe-proc", script="SKILL.md")
    assert result.startswith("ERROR") and "only .py" in result


def test_run_skill_script_refuses_an_unknown_skill(sandbox, proc_skill):
    result = _call(run_skill_script, skill="not-a-skill", script="scripts/gate.py")
    assert result.startswith("ERROR") and "Unknown skill" in result


def test_run_skill_script_times_out_rather_than_hanging(sandbox, proc_skill):
    skill, _ = proc_skill
    (skill / "scripts" / "sleeper.py").write_text("import time\ntime.sleep(30)\n")
    payload = json.loads(
        _call(
            run_skill_script, skill="probe-proc", script="scripts/sleeper.py", timeout_s=1
        )
    )
    assert payload["timed_out"] is True
    assert payload["exit_code"] is None


def test_list_skill_dir_reports_the_resolved_skill_dir(sandbox, proc_skill):
    skill, _ = proc_skill
    payload = json.loads(_call(list_skill_dir, skill="probe-proc", subpath="scripts"))
    assert payload["skill_dir"] == str(skill)
    assert "gate.py" in payload["files"]


def test_list_skill_dir_refuses_to_escape(sandbox, proc_skill):
    result = _call(list_skill_dir, skill="probe-proc", subpath="../..")
    assert result.startswith("ERROR")


# --------------------------------------------------------------------------
# Wiring
# --------------------------------------------------------------------------


def test_the_authoring_runtime_is_registered_for_the_orchestrator():
    from legal_helper.tools import orchestrator_tools

    names = {getattr(t, "name", getattr(t, "__name__", "")) for t in orchestrator_tools()}
    assert {"write_text_file", "run_skill_script", "list_skill_dir"} <= names


def test_an_external_skill_gets_the_production_toolset(monkeypatch, proc_skill):
    from legal_helper.tools import skill_tools_for_task

    names = {
        getattr(t, "name", getattr(t, "__name__", ""))
        for t in skill_tools_for_task("probe-proc")
    }
    assert {"write_text_file", "run_skill_script", "list_skill_dir"} <= names
    # No legal research surface: a deck procedure has no use for statute search.
    assert "legal_source_search" not in names
    assert "retrieve_legal" not in names


def test_an_auditing_skill_never_gets_the_authoring_runtime():
    """cite-check reviews work; it must not be able to author or execute."""
    from legal_helper.tool_policy import MUTATING_TOOLS
    from legal_helper.tools import skill_tools_for_task

    assert {"write_text_file", "run_skill_script"} <= MUTATING_TOOLS
    names = {
        getattr(t, "name", getattr(t, "__name__", ""))
        for t in skill_tools_for_task("cite-check")
    }
    assert "write_text_file" not in names
    assert "run_skill_script" not in names


def test_no_skill_tool_loop_is_capped_by_default(proc_skill):
    """A capped loop stops a procedure short of its export step; none is capped."""
    from legal_helper.turn_compaction import iteration_limit

    from legal_helper.agent import SkillAgent

    settings = load_settings(refresh=True)

    class _P:
        name = "anthropic"

    assert SkillAgent("probe-proc", _P(), settings)._max_iterations() == (
        settings.external_skill_max_iterations
    )
    assert SkillAgent("brief", _P(), settings)._max_iterations() == (
        settings.sub_agent_max_iterations
    )
    for value in (settings.external_skill_max_iterations, settings.sub_agent_max_iterations):
        assert iteration_limit(value, settings) is None
