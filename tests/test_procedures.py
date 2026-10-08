"""run_procedure: an installed external procedure other than the deck skill.

The sub-agent is replaced by a fake that writes what a procedure writes, so
these tests check what this tool owns: when it is offered, what the sub-agent
is told, which files reach the chat, and what the caller is shown.
"""

from __future__ import annotations

import json
from pathlib import Path

import pytest
from docx import Document

from legal_helper.config import load_settings, override_current_settings
from legal_helper.tools import procedures


def _call(**kwargs):
    return json.loads((getattr(procedures.run_procedure, "func", None) or procedures.run_procedure)(**kwargs))


def _names(tools) -> set[str]:
    return {getattr(t, "name", getattr(t, "__name__", "")) for t in tools}


@pytest.fixture()
def env(tmp_path, monkeypatch):
    root = tmp_path / "skills"
    for name, description in (("gongwen", "按 GB/T 9704 生成公文 DOCX"), ("ppt-master", "decks")):
        (root / name).mkdir(parents=True)
        (root / name / "SKILL.md").write_text(f"---\nname: {name}\ndescription: {description}\n---\nbody\n")
    monkeypatch.setenv("LEGAL_HELPER_SKILL_ROOTS", str(root))
    settings = load_settings(refresh=True).model_copy(
        update={"outputs_dir": tmp_path / "out", "state_dir": tmp_path / "state", "logs_dir": tmp_path / "logs"}
    )
    settings.outputs_dir.mkdir(parents=True)
    with override_current_settings(settings):
        yield settings


def _docx(path: Path, text: str) -> Path:
    doc = Document()
    doc.add_paragraph(text)
    path.parent.mkdir(parents=True, exist_ok=True)
    doc.save(str(path))
    return path


def test_only_non_deck_external_skills_are_procedures(env):
    assert list(procedures.installed_procedures()) == ["gongwen"]


def test_an_unknown_procedure_lists_what_is_installed(env):
    result = _call(skill="nope", brief="x", title="t")
    assert result["status"] == "unavailable"
    assert result["installed"] == ["gongwen: 按 GB/T 9704 生成公文 DOCX"]
    assert _call(skill="ppt-master", brief="x", title="t")["next_step"].endswith("create_deck.")


def test_the_named_deliverable_reaches_the_chat_and_nothing_else(env, monkeypatch):
    seen = {}

    def fake(skill, task, settings):
        seen.update(skill=skill, task=task, outputs=settings.outputs_dir)
        work = settings.outputs_dir
        _docx(work / "draft" / "scratch.docx", "草稿")
        final = _docx(work / "draft" / "关于开展检查的通知.docx", "正文")
        source = work / "draft" / "source.md"
        source.write_text("# 通知\n")
        preview = work / "draft" / "preview-1.jpg"
        preview.write_bytes(b"\xff\xd8")
        return (
            f"Done.\n\n## Deliverables\n- {final}\n- {source}\n- {preview}\n\n"
            "## Decisions under delegation\n- 采用电子红头\n"
        )

    monkeypatch.setattr(procedures, "run_procedure_agent", fake)
    result = _call(skill="gongwen", brief="发文机关：某局\n正文：……", title="关于开展检查的通知")

    assert result["status"] == "ok", result
    # Working files the report also listed (source, previews) stay in the workspace.
    assert [Path(f["path"]).name for f in result["files"]] == ["关于开展检查的通知.docx"]
    assert sorted(p.name for p in env.outputs_dir.iterdir() if p.is_file()) == ["关于开展检查的通知.docx"]
    delivered = Path(result["files"][0]["path"])
    assert delivered.parent == env.outputs_dir and delivered.is_file()
    assert result["decisions_under_delegation"] == ["采用电子红头"]
    # The sub-agent works out of the chat's file list and is told so.
    assert "_procedures" in str(seen["outputs"]) and str(seen["outputs"]) in seen["task"]
    assert "发文机关：某局" in seen["task"] and "## Deliverables" in seen["task"]


def test_without_a_named_deliverable_the_documents_made_during_the_run_are_used(env, monkeypatch):
    monkeypatch.setattr(
        procedures, "run_procedure_agent",
        lambda skill, task, settings: (_docx(settings.outputs_dir / "out.docx", "x"), "finished")[1],
    )
    result = _call(skill="gongwen", brief="正文", title="通知")
    assert [Path(f["path"]).name for f in result["files"]] == ["out.docx"]


def test_a_run_that_makes_nothing_is_a_failure_with_the_reason(env, monkeypatch):
    def crash(skill, task, settings):
        raise RuntimeError("node not found")

    monkeypatch.setattr(procedures, "run_procedure_agent", crash)
    result = _call(skill="gongwen", brief="正文", title="通知")
    assert result["status"] == "failed" and "node not found" in result["error"]
    assert not list(env.outputs_dir.glob("*.docx"))


def test_a_repeated_title_never_overwrites_a_delivered_file(env, monkeypatch):
    monkeypatch.setattr(
        procedures, "run_procedure_agent",
        lambda skill, task, settings: (_docx(settings.outputs_dir / "通知.docx", "x"), "")[1],
    )
    first = _call(skill="gongwen", brief="正文", title="通知")["files"][0]["path"]
    second = _call(skill="gongwen", brief="正文", title="通知")["files"][0]["path"]
    assert first != second and Path(first).is_file() and Path(second).is_file()


def test_offered_to_the_author_only_while_a_procedure_is_installed(env, monkeypatch, tmp_path):
    from legal_helper.tool_policy import is_mutating
    from legal_helper.tools import _external_skill_tools, skill_tools_for_task
    from legal_helper.workflow import _document_io_tools

    assert "run_procedure" in _names(_document_io_tools())
    assert is_mutating("run_procedure")
    # A procedure's own sub-agent must not recurse, and an auditor must not author.
    assert "run_procedure" not in _names(_external_skill_tools())
    assert "run_procedure" not in _names(skill_tools_for_task("cite-check"))

    empty = tmp_path / "empty"
    empty.mkdir()
    monkeypatch.setenv("LEGAL_HELPER_SKILL_ROOTS", str(empty))
    assert "run_procedure" not in _names(_document_io_tools())


def test_the_offered_tool_names_the_installed_procedures(env):
    """With a free-text name the model probed the tool mid-answer to see what
    it did; the names are in the schema instead."""
    tool = procedures.procedure_tool()
    spec = tool.to_dict()
    assert spec["name"] == "run_procedure"
    assert spec["input_schema"]["properties"]["skill"]["enum"] == ["gongwen"]
    assert "gongwen: 按 GB/T 9704 生成公文 DOCX" in spec["description"]
    # The module-level tool keeps its static schema.
    assert "enum" not in procedures.run_procedure.input_schema["properties"]["skill"]
