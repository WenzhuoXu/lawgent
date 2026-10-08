"""An external skill installed the recommended way: a symlink into a clone.

ppt-master keeps its skill in its repository's ``skills/ppt-master/`` folder,
so the install that keeps ``.git`` for updates is a symlink from a skill root
into that folder. These tests pin that the harness can drive such an install.
"""

from __future__ import annotations

import json
import os

from legal_helper.config import load_settings


def _call(tool, **kwargs):
    return (getattr(tool, "func", None) or tool)(**kwargs)


def test_a_symlinked_skill_install_runs_its_own_scripts(tmp_path, monkeypatch):
    """ppt-master lives in its repo's skills/ folder and is installed by symlink.

    Discovery used to return the unresolved link, and every containment check
    downstream compares resolved paths — so run_skill_script refused each of
    the skill's own scripts as "outside the skill directory".
    """
    from legal_helper.skills import discover_skills
    from legal_helper.tools.skill_runtime import list_skill_dir, run_skill_script

    real = tmp_path / "clone" / "skills" / "probe-deck"
    (real / "scripts").mkdir(parents=True)
    (real / "SKILL.md").write_text("---\nname: probe-deck\n---\nprocedure\n")
    (real / "scripts" / "gate.py").write_text("print('gate ran')\n")
    root = tmp_path / "root"
    root.mkdir()
    os.symlink(real, root / "probe-deck")
    monkeypatch.setenv("LEGAL_HELPER_SKILL_ROOTS", str(root))
    outputs = tmp_path / "out"
    outputs.mkdir()
    settings = load_settings(refresh=True).model_copy(update={"outputs_dir": outputs})
    monkeypatch.setattr("legal_helper.tools.skill_runtime.current_settings", lambda: settings)

    assert discover_skills()["probe-deck"] == real.resolve()
    ran = json.loads(_call(run_skill_script, skill="probe-deck", script="scripts/gate.py"))
    assert ran["exit_code"] == 0 and "gate ran" in ran["stdout"]
    listing = json.loads(_call(list_skill_dir, skill="probe-deck", subpath="scripts"))
    assert "gate.py" in listing["files"]
    # Containment still holds through the link.
    escaped = _call(run_skill_script, skill="probe-deck", script="../../../outside.py")
    assert escaped.startswith("ERROR")
