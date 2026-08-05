"""Project-memory tools — the agent's read/write surface over durable project
context (the legal analog of Claude Code's ``/memory``).

These operate on the **active project** (set by the server/CLI for the run via
``projects.active_project``). When no project is active they return a clear
no-op message rather than erroring, so they are always safe to expose.
"""

from __future__ import annotations

import json

from anthropic import beta_tool

from ..projects import ProjectStore, active_project_id

_VALID_KINDS = (
    "fact", "decision", "task", "open_question", "glossary", "artifact", "risk", "source",
)


@beta_tool
def project_memory_write(
    kind: str,
    title: str,
    body: str = "",
    salience: int = 3,
    status: str = "open",
) -> str:
    """Append a durable entry to the active project's memory so later chats
    inherit it. Use this to checkpoint a load-bearing fact, a settled decision,
    an open question, a glossary term, a produced artifact, a risk, or a source
    the matter should remember across sessions.

    Only write things that should persist beyond this chat — not transient
    working notes. Prefer one crisp entry per call.

    Args:
        kind: One of ``fact``, ``decision``, ``task``, ``open_question``,
            ``glossary``, ``artifact``, ``risk``, ``source``.
        title: One-line summary (the entry's headline).
        body: Optional supporting detail, pinpoint, or rationale.
        salience: Importance 1-5 (5 = always surface). Decisions/risks that
            shape the whole matter are 5; minor facts are 2-3.
        status: ``open`` (default), ``done`` (for completed tasks), or
            ``superseded``.
    """
    pid = active_project_id()
    if not pid:
        return json.dumps(
            {"ok": False, "note": "No active project; nothing written. Start a chat under a "
                                   "project (CLI --project / web project picker) to persist memory."},
            ensure_ascii=False,
        )
    if kind not in _VALID_KINDS:
        return json.dumps(
            {"ok": False, "error": f"invalid kind {kind!r}; use one of {list(_VALID_KINDS)}"},
            ensure_ascii=False,
        )
    store = ProjectStore()
    mem = store.add_memory(pid, kind, title.strip(), body.strip(), salience=salience, status=status)
    return json.dumps(
        {"ok": True, "id": mem.id, "kind": mem.kind, "title": mem.title, "salience": mem.salience},
        ensure_ascii=False,
    )


@beta_tool
def project_memory_search(query: str, kind: str = "", limit: int = 8) -> str:
    """Search the active project's durable memory for relevant prior context
    (facts, decisions, open questions, glossary, sources) before answering, so
    your work stays consistent with earlier chats in the same matter.

    Args:
        query: Free-text query (legal issue, party, statute, topic).
        kind: Optional filter to one memory kind (e.g. ``decision``).
        limit: Maximum entries to return (default 8).
    """
    pid = active_project_id()
    if not pid:
        return json.dumps({"results": [], "note": "No active project."}, ensure_ascii=False)
    store = ProjectStore()
    hits = store.search_memory(pid, query, kind=kind or None, limit=max(1, min(int(limit), 20)))
    return json.dumps(
        {
            "count": len(hits),
            "results": [
                {"kind": m.kind, "title": m.title, "body": m.body,
                 "status": m.status, "salience": m.salience}
                for m in hits
            ],
        },
        ensure_ascii=False,
    )


@beta_tool
def project_brief_read() -> str:
    """Read the active project's standing brief, rolling summary, and open
    questions — the durable instructions and state that span every chat in the
    matter. Call this when you need the full project picture beyond what is
    already in your context.
    """
    pid = active_project_id()
    if not pid:
        return json.dumps({"note": "No active project."}, ensure_ascii=False)
    store = ProjectStore()
    project = store.get_project(pid)
    open_q = store.list_memory(pid, kind="open_question", status="open", limit=12)
    decisions = store.list_memory(pid, kind="decision", status="open", limit=12)
    return json.dumps(
        {
            "name": project.name,
            "jurisdiction": project.jurisdiction,
            "domain_packs": project.domain_packs,
            "brief": project.brief,
            "summary": project.summary,
            "open_questions": [m.title for m in open_q],
            "decisions": [m.title for m in decisions],
        },
        ensure_ascii=False,
    )


__all__ = ["project_memory_write", "project_memory_search", "project_brief_read"]
