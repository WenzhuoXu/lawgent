from __future__ import annotations

import json
import sqlite3
from pathlib import Path

import pytest

from legal_helper.config import load_settings
from legal_helper.projects import (
    ProjectStore,
    active_project,
    active_project_context_block,
    slugify,
)


@pytest.fixture()
def store(tmp_path: Path) -> ProjectStore:
    db = tmp_path / "chat_sessions.sqlite3"
    # Simulate ChatStore's chats table so the project_id migration + assignment work.
    con = sqlite3.connect(db)
    con.execute(
        "CREATE TABLE chats (id TEXT PRIMARY KEY, title TEXT, updated_at TEXT, "
        "memory_summary TEXT DEFAULT '')"
    )
    con.execute(
        "INSERT INTO chats VALUES ('c1', 'Lease', '2026-06-16', "
        "'Agreed MC99 insurance floor for ET route.')"
    )
    con.commit()
    con.close()
    return ProjectStore(settings=load_settings(), db_path=db)


def test_slugify():
    assert slugify("ET Route Launch Review!") == "et-route-launch-review"
    assert slugify("   ") == "project"


def test_create_and_slug_uniqueness(store: ProjectStore):
    a = store.create_project("Route Launch", jurisdiction="ET", domain_packs=["aviation"])
    b = store.create_project("Route Launch")  # same name → unique slug
    assert a.slug == "route-launch"
    assert b.slug == "route-launch-2"
    assert a.jurisdiction == "ET" and a.domain_packs == ["aviation"]
    assert {p.slug for p in store.list_projects()} == {"route-launch", "route-launch-2"}


def test_resolve_by_slug_or_id(store: ProjectStore):
    p = store.create_project("Matter X")
    assert store.resolve("matter-x").id == p.id
    assert store.resolve(p.id).id == p.id
    assert store.resolve("nope") is None


def test_memory_crud_and_ranking(store: ProjectStore):
    p = store.create_project("M")
    store.add_memory(p.id, "fact", "low fact", salience=2, body="x")
    d = store.add_memory(p.id, "decision", "big decision", salience=5, body="y")
    listed = store.list_memory(p.id)
    assert listed[0].id == d.id  # highest salience first
    # update + supersede
    store.update_memory(d.id, salience=4)
    assert store.get_memory(d.id).salience == 4
    store.supersede_memory(d.id)
    assert store.get_memory(d.id).status == "superseded"


def test_search_memory(store: ProjectStore):
    p = store.create_project("M")
    store.add_memory(p.id, "fact", "ECAA established under Proclamation 273/2002",
                     body="Civil Aviation Proclamation 616/2008.", salience=4)
    store.add_memory(p.id, "decision", "Use MC99 liability floor", salience=5)
    hits = store.search_memory(p.id, "ECAA proclamation aviation")
    assert hits and "ECAA" in hits[0].title
    # superseded entries are excluded
    store.supersede_memory(hits[0].id)
    assert all("ECAA" not in h.title for h in store.search_memory(p.id, "ECAA"))


def test_chat_assignment(store: ProjectStore):
    p = store.create_project("M")
    store.assign_chat("c1", p.id)
    assert store.project_id_for_chat("c1") == p.id
    assert [c["id"] for c in store.list_project_chats(p.id)] == ["c1"]
    store.assign_chat("c1", None)
    assert store.project_id_for_chat("c1") is None


def test_build_context_block(store: ProjectStore):
    p = store.create_project(
        "ET Route", brief="Open an ET route; ECAA AOC validation.",
        jurisdiction="ET", domain_packs=["aviation"],
    )
    store.add_memory(p.id, "decision", "Adopt MC99 liability floor", salience=5)
    store.add_memory(p.id, "open_question", "Which BASA governs ADD?", salience=4)
    block = store.build_context_block(p.id)
    assert "Project context — ET Route" in block
    assert "ECAA AOC validation" in block          # brief
    assert "Adopt MC99 liability floor" in block    # decision
    assert "Which BASA governs ADD?" in block       # open question
    assert "jurisdiction **ET**" in block


def test_compact_local_fallback_and_prune(store: ProjectStore):
    p = store.create_project("M", brief="b")
    store.assign_chat("c1", p.id)
    store.add_memory(p.id, "decision", "key decision", salience=5)
    store.add_memory(p.id, "task", "trivial done task", salience=1, status="done")
    # Force the local fallback by stripping API keys from the passed settings.
    s = load_settings().model_copy(update={"anthropic_api_key": None, "openai_api_key": None})
    updated = store.compact(p.id, settings=s)
    assert "MC99" in updated.summary or "insurance floor" in updated.summary or "key decision" in updated.summary
    # low-salience done task pruned
    assert store.list_memory(p.id, kind="task", status="done") == []


def test_active_project_contextvar_injection(store: ProjectStore, monkeypatch):
    # Make the module-level ProjectStore() (used by active_project_context_block)
    # point at the temp DB.
    monkeypatch.setattr("legal_helper.projects.ProjectStore", lambda *a, **k: store)
    p = store.create_project("Ctx", brief="standing brief here")
    assert active_project_context_block() == ""  # no active project
    with active_project(p.id):
        block = active_project_context_block()
    assert "standing brief here" in block


def test_project_memory_tools(store: ProjectStore, monkeypatch):
    monkeypatch.setattr("legal_helper.tools.project_tools.ProjectStore", lambda *a, **k: store)
    from legal_helper.tools import project_tools

    p = store.create_project("Tools")
    # no active project → safe no-op
    res = json.loads(project_tools.project_memory_write.call(
        {"kind": "fact", "title": "x"}))
    assert res["ok"] is False
    with active_project(p.id):
        res = json.loads(project_tools.project_memory_write.call(
            {"kind": "decision", "title": "Adopt MC99", "salience": 5}))
        assert res["ok"] and res["kind"] == "decision"
        found = json.loads(project_tools.project_memory_search.call({"query": "MC99"}))
        assert found["count"] == 1
        brief = json.loads(project_tools.project_brief_read.call({}))
        assert "Adopt MC99" in brief["decisions"]
