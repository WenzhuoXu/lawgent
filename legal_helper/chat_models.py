"""Pydantic models for persistent web chat sessions, projects and workflow tasks."""

from __future__ import annotations

from datetime import datetime, timezone
from typing import Any, Literal, Optional

from pydantic import BaseModel, Field


def utc_now() -> str:
    return datetime.now(timezone.utc).isoformat()


class ChatSettings(BaseModel):
    provider: Literal["anthropic", "openai"]
    model: str
    reasoning_effort: str = "medium"
    # "auto" lets the author size each answer; a chat can pin one.
    answer_length: Literal["auto", "brief", "focused", "full"] = "auto"
    skill_hint: Optional[str] = None
    enable_web_search: bool = True
    enable_web_fetch: bool = True


class ChatSession(BaseModel):
    id: str
    title: str
    created_at: str
    updated_at: str
    archived: bool = False
    settings: ChatSettings
    memory_summary: str = ""
    last_response_id: Optional[str] = None
    project_id: Optional[str] = None


class ChatMessage(BaseModel):
    id: str
    chat_id: str
    role: Literal["user", "assistant", "system"]
    content: str
    created_at: str
    metadata: dict[str, Any] = Field(default_factory=dict)


class WorkflowEvent(BaseModel):
    id: str
    chat_id: str
    created_at: str
    event_type: str
    agent_name: str = "orchestrator"
    run_id: Optional[str] = None
    parent_run_id: Optional[str] = None
    data: dict[str, Any] = Field(default_factory=dict)


class ChatRun(BaseModel):
    id: str
    chat_id: str
    user_message_id: str
    assistant_message_id: str
    status: Literal["running", "complete", "error", "cancelled"] = "running"
    created_at: str
    updated_at: str
    finished_at: Optional[str] = None
    error: Optional[str] = None


class ArtifactRecord(BaseModel):
    id: str
    chat_id: str
    message_id: Optional[str] = None
    filename: str
    path: str
    url: str
    origin: str = "generated"
    created_at: str


ProjectMemoryKind = Literal[
    "fact", "decision", "task", "open_question", "glossary", "artifact", "risk", "source"
]


class Project(BaseModel):
    """A durable container grouping chats + long-running work under shared context.

    The ``brief`` is the CLAUDE.md-equivalent — stable, hand-authored context
    and instructions re-injected into the author's context every turn (it
    survives chat compaction). ``summary`` is the rolling, model-distilled digest of work
    done across the project's chats. ``jurisdiction`` / ``domain_packs`` let a
    project pin a working jurisdiction + pack set (e.g. ET + aviation for
    Ethiopian route work).
    """

    id: str
    name: str
    slug: str
    brief: str = ""
    summary: str = ""
    jurisdiction: Optional[str] = None
    domain_packs: list[str] = Field(default_factory=list)
    status: Literal["active", "archived"] = "active"
    created_at: str
    updated_at: str
    metadata: dict[str, Any] = Field(default_factory=dict)


class ProjectMemory(BaseModel):
    """One durable, typed memory entry attached to a project.

    Mirrors the structured-memory model used by long-running agents: each entry
    is a single load-bearing fact / decision / task / glossary item, scored by
    ``salience`` (1-5) so the context builder can rank the most important ones
    into the author's context and compaction can prune the rest.
    """

    id: str
    project_id: str
    kind: ProjectMemoryKind
    title: str
    body: str
    status: Literal["open", "done", "superseded"] = "open"
    salience: int = 3
    source_chat_id: Optional[str] = None
    tags: list[str] = Field(default_factory=list)
    created_at: str
    updated_at: str
    superseded_by: Optional[str] = None


class AgentTask(BaseModel):
    id: str
    skill_name: str
    title: str
    task: str
    depends_on: list[str] = Field(default_factory=list)


class CitationAuditResult(BaseModel):
    ok: bool
    warnings: list[str] = Field(default_factory=list)
    checked_at: str = Field(default_factory=utc_now)
