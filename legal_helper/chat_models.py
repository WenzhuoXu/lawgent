"""Pydantic models for persistent web chat sessions and workflow plans."""

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


class ChatMemorySummary(BaseModel):
    chat_id: str
    summary: str
    updated_at: str
    source_message_count: int = 0


ProjectMemoryKind = Literal[
    "fact", "decision", "task", "open_question", "glossary", "artifact", "risk", "source"
]


class Project(BaseModel):
    """A durable container grouping chats + long-running work under shared context.

    The ``brief`` is the CLAUDE.md-equivalent — stable, hand-authored context
    and instructions re-injected into the orchestrator every turn (it survives
    chat compaction). ``summary`` is the rolling, model-distilled digest of work
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
    into the orchestrator's window and compaction can prune the rest.
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


class WorkflowPlan(BaseModel):
    title: str
    user_language: str = "English"
    execution_mode: Literal["agent_workflow", "direct_answer", "clarify"] = "agent_workflow"
    # Complexity tier sizes the workflow effort: "simple" → single fast pass,
    # "standard" → one specialist + integration, "complex" → parallel
    # multi-specialist decomposition (the country-launch / multi-section memo case).
    complexity: Literal["simple", "standard", "complex"] = "standard"
    direct_response: str = ""
    routing_reason: str = ""
    # Router's confidence that it has the facts needed to do the work, 0-1.
    # At or above `CLARIFY_CONFIDENCE_THRESHOLD` a `clarify` routing is treated
    # as a hedge and the turn proceeds. None means the router did not report a
    # number — the heuristic planner and any model that ignores the field — and
    # an unreported confidence must not be read as a confident one, so the gate
    # simply does not apply.
    intake_confidence: Optional[float] = None
    artifact_format: Optional[Literal["pdf", "docx"]] = None  # legacy; ignored
    issue_decomposition: list[str] = Field(default_factory=list)
    agent_tasks: list[AgentTask] = Field(default_factory=list)
    integration_instructions: str = ""
    citation_requirements: str = (
        "Cite document/instrument plus pinpoint where available: article, annex, "
        "standard/recommended practice, section, paragraph, page, clause, or "
        "state `pinpoint unavailable`."
    )


class CitationAuditResult(BaseModel):
    ok: bool
    warnings: list[str] = Field(default_factory=list)
    checked_at: str = Field(default_factory=utc_now)
