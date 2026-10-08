"""Skill resources reach the agent; GB/T 9704 output passes an external verifier.

* 0 of 30,494 recorded events read a skill resource, though tabular-review and
  litigation-analysis point their agents at ``resources/`` — the reader
  refused anything but ``references/*.md`` (D-14).
* ``layout="gbt9704"`` set Letter paper, put 一级标题 in 楷体 and printed no
  页码 (D-10). Its output is now checked by the verifier that ships with
  mizzlelover/gongwen-gbt9704-skill (MIT), driven through ``run_skill_script``
  exactly as the harness drives it — which also exercises the Node path. The
  skill is installed under the shared staging root; the test skips when it is
  absent, and never touches ``~/.claude/skills``.
"""

from __future__ import annotations

import json
import os
from pathlib import Path

import pytest

from legal_helper.logging_setup import agent_name_var
from legal_helper.tools.skill_resources import list_skill_references, read_skill_reference


def _call(tool, **kwargs):
    return (getattr(tool, "func", None) or tool)(**kwargs)


@pytest.fixture()
def as_skill():
    def enter(name):
        return agent_name_var.set(name)

    tokens = []
    yield lambda name: tokens.append(enter(name))
    for token in reversed(tokens):
        agent_name_var.reset(token)


def test_a_skill_can_list_and_read_its_resources(as_skill):
    as_skill("litigation-analysis")
    listing = json.loads(_call(list_skill_references))
    assert "resources/element_tables/contract_breach.yaml" in listing["resources"]
    assert any(path.startswith("references/") for path in listing["references"])
    text = _call(read_skill_reference, path="resources/element_tables/contract_breach.yaml")
    assert not text.startswith("ERROR") and text.strip()


def test_tabular_review_reads_its_grid_presets(as_skill):
    as_skill("tabular-review")
    text = _call(read_skill_reference, path="resources/grid_templates/nda_screen.yaml")
    assert not text.startswith("ERROR")


@pytest.mark.parametrize(
    "path",
    [
        "SKILL.md",
        "../brief/SKILL.md",
        "resources/../../brief/SKILL.md",
        "references/../SKILL.md",
        "/etc/passwd",
    ],
)
def test_reading_stays_inside_references_and_resources(as_skill, path):
    as_skill("litigation-analysis")
    assert _call(read_skill_reference, path=path).startswith("ERROR")


def test_only_text_resource_types_are_readable(as_skill, tmp_path, monkeypatch):
    skill = tmp_path / "draft-agreement"
    (skill / "resources").mkdir(parents=True)
    (skill / "SKILL.md").write_text("---\nname: draft-agreement\n---\n")
    (skill / "resources" / "preset.yaml").write_text("columns: [a]\n")
    (skill / "resources" / "grid.csv").write_text("a,b\n")
    (skill / "resources" / "helper.py").write_text("print('no')\n")
    monkeypatch.setattr("legal_helper.tools.skill_resources.skill_path", lambda name: skill / "SKILL.md")
    as_skill("draft-agreement")
    listing = json.loads(_call(list_skill_references))
    assert listing["resources"] == ["resources/grid.csv", "resources/preset.yaml"]
    assert _call(read_skill_reference, path="resources/preset.yaml") == "columns: [a]\n"
    assert _call(read_skill_reference, path="resources/helper.py").startswith("ERROR")
    assert _call(read_skill_reference, path="resources/missing.yaml").startswith("ERROR")


# ---------------------------------------------------------------------------
# gongwen-gbt9704-skill's verifier against our GB/T 9704 output
# ---------------------------------------------------------------------------


def _staged_gongwen() -> Path | None:
    """A skill root holding gongwen: an explicit staging root, a configured
    root where it is installed, or a staging checkout beside the tree."""
    from legal_helper.skills import skill_roots

    candidates = [os.environ.get("LEGAL_HELPER_SKILL_STAGING", "")]
    candidates += [str(root) for root in skill_roots()]
    candidates += [
        str(parent / ".claude" / "skill-staging" / "root")
        for parent in Path(__file__).resolve().parents
    ]
    for root in candidates:
        if root and (Path(root) / "gongwen" / "scripts" / "verify_gongwen_docx.mjs").is_file():
            return Path(root)
    return None


#: Checks that describe a document this layout does not produce: the
#: "ordinary" profile's own convention of a centred page number (GB/T 9704
#: 7.5 puts it outside), and the red-head 版头 of a formal 发文.
_OUT_OF_SCOPE = {
    "ordinary": {"Centered layout footer"},
    "formal": {
        "Title has two explicit blank grid lines",
        "Preprinted letterhead reserve",
        "Title spacing below physical red rule",
    },
}

GB_BODY = """现将有关情况报告如下。

一、基本情况

（一）项目背景

1.项目概况

（1）具体事项

正文自然段，正文自然段，正文自然段。

二、存在问题

1. 列表项一
2. 列表项二
"""


@pytest.mark.parametrize("profile", ["ordinary", "formal"])
def test_gbt9704_output_passes_the_gongwen_verifier(tmp_path, monkeypatch, profile):
    from legal_helper.config import load_settings
    from legal_helper.documents.writers import write_docx_document
    from legal_helper.documents.writers._node import find_node
    from legal_helper.tools.skill_runtime import run_skill_script

    staging = _staged_gongwen()
    if staging is None or find_node() is None:
        pytest.skip("gongwen-gbt9704-skill is not installed or staged, or node is missing")
    monkeypatch.setenv("LEGAL_HELPER_SKILL_ROOTS", str(staging))
    settings = load_settings(refresh=True)
    monkeypatch.setattr(
        "legal_helper.tools.skill_runtime.current_settings",
        lambda: settings.model_copy(update={"outputs_dir": tmp_path}),
    )
    out = tmp_path / "gb.docx"
    write_docx_document(output_path=out, title="关于某事项的请示", sections=[("", GB_BODY)], layout="gbt9704")

    payload = json.loads(_call(
        run_skill_script, skill="gongwen", script="scripts/verify_gongwen_docx.mjs",
        args=["--input", str(out), "--profile", profile],
    ))
    lines = [line for line in payload["stdout"].splitlines() if line.startswith(("PASS", "FAIL"))]
    assert lines, payload
    failed = {line.split(":", 1)[0][4:].strip() for line in lines if line.startswith("FAIL")}
    passed = {line.split(":", 1)[0][4:].strip() for line in lines if line.startswith("PASS")}
    assert failed <= _OUT_OF_SCOPE[profile], failed
    for check in ("A4 page size", "Page margins", "Document grid", "Default body font",
                  "Heading1 applied when present", "Heading2 applied when present",
                  "Heading3 applied when present", "Heading4 applied when present"):
        assert check in passed, check
    if profile == "formal":
        assert {"Odd/even page fields", "Odd/even page positions", "Formal page-number distance"} <= passed
