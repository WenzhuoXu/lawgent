"""Shared provider protocol + lightweight data shapes.

The orchestrator, skill agents, CLI and server never special-case Claude vs.
GPT: they go through this protocol.  Each concrete provider returns the same
`RunResult` shape and yields the same `StreamEvent` records, so we can swap
providers per invocation via the `MODEL_PROVIDER` env var.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from typing import Any, Iterable, Iterator, Optional, Protocol, runtime_checkable

from ..config import Settings


Message = dict[str, Any]
Tool = Any  # BetaFunctionTool / dict / hosted-tool param


@dataclass
class ToolCallRecord:
    name: str
    arguments: dict[str, Any]
    result: Optional[str] = None
    is_hosted: bool = False
    raw: Optional[dict[str, Any]] = None


@dataclass
class RunResult:
    text: str
    provider: str
    model: str
    usage: dict[str, Any] = field(default_factory=dict)
    tool_calls: list[ToolCallRecord] = field(default_factory=list)
    hosted_tool_calls: list[ToolCallRecord] = field(default_factory=list)
    structured_payload: Optional[dict[str, Any]] = None
    artifact_paths: list[str] = field(default_factory=list)
    message_id: Optional[str] = None       # Anthropic
    response_id: Optional[str] = None      # OpenAI
    iterations: int = 0
    stop_reason: Optional[str] = None
    raw: Optional[dict[str, Any]] = None


@dataclass
class StreamEvent:
    """Normalized streaming event emitted by both providers."""

    kind: str          # "delta" | "tool_call" | "hosted_tool_call" | "artifact" |
                       # "skill_started" | "skill_finished" | "done" | "error"
    data: dict[str, Any] = field(default_factory=dict)


@runtime_checkable
class Provider(Protocol):
    name: str
    model: str
    supports_hosted_web_search: bool
    supports_file_search: bool
    supports_structured_outputs: bool

    def run(
        self,
        system: str,
        messages: list[Message],
        tools: Iterable[Tool] = (),
        *,
        max_iterations: Optional[int] = None,
        structured_output: Optional[type] = None,
    ) -> RunResult: ...

    def stream(
        self,
        system: str,
        messages: list[Message],
        tools: Iterable[Tool] = (),
        *,
        max_iterations: Optional[int] = None,
    ) -> Iterator[StreamEvent]: ...

    def tool_runner(
        self,
        system: str,
        messages: list[Message],
        tools: Iterable[Tool],
        *,
        max_iterations: int,
        structured_output: Optional[type] = None,
    ) -> RunResult: ...


def build_provider(settings: Settings, *, fast: bool = False) -> Provider:
    """Construct the configured provider (lazy import to keep cold start small)."""
    if settings.provider == "openai":
        from .openai_provider import OpenAIProvider

        return OpenAIProvider(settings, fast=fast)
    from .anthropic_provider import AnthropicProvider

    return AnthropicProvider(settings, fast=fast)
