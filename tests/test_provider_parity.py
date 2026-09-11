"""Verify both providers' tool_runners return the same normalized RunResult shape."""

from __future__ import annotations

import json
from types import SimpleNamespace
from typing import Any, Iterable, Iterator, Optional
from unittest.mock import patch

from legal_helper.config import load_settings
from legal_helper.providers import RunResult
from legal_helper.providers.base import Message, StreamEvent, Tool, ToolCallRecord


class MockAnthropicProvider:
    name = "anthropic"
    supports_hosted_web_search = True
    supports_file_search = False
    supports_structured_outputs = True
    model = "claude-opus-4-7"

    def run(self, *a, **k):
        return self.tool_runner(*a, **k)

    def stream(self, *a, **k) -> Iterator[StreamEvent]:
        yield StreamEvent("done", {"text": "ok"})

    def tool_runner(
        self,
        system: str,
        messages: list[Message],
        tools: Iterable[Tool],
        *,
        max_iterations: int,
        structured_output: Optional[type] = None,
    ) -> RunResult:
        return RunResult(
            text="Aviation review complete.\nIDERA: PASS\nLiability: FLAG\nAD: FLAG",
            provider="anthropic",
            model=self.model,
            usage={"input_tokens": 12, "output_tokens": 4},
            tool_calls=[ToolCallRecord(name="run_skill", arguments={"skill_name": "review-contract"})],
            message_id="msg_anth_1",
            stop_reason="end_turn",
        )


class MockOpenAIProvider(MockAnthropicProvider):
    name = "openai"
    supports_hosted_web_search = True
    supports_file_search = True
    supports_structured_outputs = True
    model = "gpt-5.5"

    def tool_runner(self, *a, **k) -> RunResult:
        r = super().tool_runner(*a, **k)
        return RunResult(
            text=r.text,
            provider="openai",
            model=self.model,
            usage={"input_tokens": 10, "output_tokens": 3, "total_tokens": 13},
            tool_calls=r.tool_calls,
            response_id="resp_oai_1",
            iterations=2,
        )


def test_both_providers_share_runresult_fields():
    a = MockAnthropicProvider()
    o = MockOpenAIProvider()
    ra = a.tool_runner("sys", [{"role": "user", "content": "go"}], [], max_iterations=2)
    ro = o.tool_runner("sys", [{"role": "user", "content": "go"}], [], max_iterations=2)

    for r in (ra, ro):
        assert r.text
        assert r.provider in {"anthropic", "openai"}
        assert r.model
        assert isinstance(r.usage, dict)
        assert isinstance(r.tool_calls, list)
    assert ra.message_id and not ra.response_id
    assert ro.response_id and not ro.message_id
    assert "IDERA" in ra.text and "IDERA" in ro.text


def test_orchestrator_dispatches_through_both_providers(monkeypatch):
    """The same orchestrator flow runs end-to-end through Anthropic and OpenAI mocks."""
    settings = load_settings(refresh=True)

    from legal_helper.agent import OrchestratorAgent

    for provider in (MockAnthropicProvider(), MockOpenAIProvider()):
        agent = OrchestratorAgent(provider=provider, settings=settings)
        result = agent.run("Review the lease.")
        assert "Aviation review complete" in result.text
        assert result.provider in {"anthropic", "openai"}


def test_all_nine_skills_loadable_under_either_provider():
    """Skills are provider-agnostic and start from compact manifests."""
    from legal_helper.agent import SkillAgent
    from legal_helper.skills import SKILL_NAMES

    settings = load_settings(refresh=True)
    for provider in (MockAnthropicProvider(), MockOpenAIProvider()):
        for skill in SKILL_NAMES:
            agent = SkillAgent(skill, provider, settings)
            sp = agent._system_prompt()
            assert f"name: {skill}" in sp
            # Methodology is inlined at build time, not fetched at runtime.
            assert "# Your methodology" in sp
            assert "# General legal playbook" in sp
            assert "list_skill_sections" not in sp
            assert "Aviation Playbook" not in sp
            # Guardrail on the sub-agent SYSTEM PROMPT (input side). This is
            # unrelated to output length: the answer ceiling is
            # settings.max_tokens. Headroom 7000 → 8000 (per-jurisdiction
            # source guidance) → 10000 (2026-07: runtime playbook path +
            # section-index injection and the flexible-language contract
            # rewrite) → 30000 (2026-08).
            #
            # The 2026-08 raise is a deliberate trade, not drift. The prompt
            # now inlines the SKILL.md body and the general playbook instead of
            # ordering a `list_skill_sections` → `read_skill_section` handshake
            # to fetch them. That handshake opened 13 of 13 specialist
            # dispatches, is structurally unparallelisable, and cost two of
            # eight iterations before any legal work began; 51.9% of all
            # specialist tool calls were instruction fetches. Inlining costs
            # MORE first-send tokens and buys back iterations — and the prefix
            # is stable, so it caches, where tool results re-bill per
            # iteration. ~23KB is ~12% of a 200K window and ~2% of a 1M one.
            assert len(sp) < 30000


def test_runtime_model_lists_stay_consistent_with_defaults():
    """Both providers' selectable model lists include the configured defaults.

    gpt-5.6 only ships as named variants (terra/sol/luna); the bare "gpt-5.6"
    alias routes to Sol and must never appear in a selectable list.
    """
    from legal_helper.config import (
        ANTHROPIC_HIGH_EFFORT_MODELS,
        OPENAI_HIGH_EFFORT_MODELS,
        Settings,
    )

    s = Settings()
    assert s.anthropic_model in ANTHROPIC_HIGH_EFFORT_MODELS
    assert s.openai_model in OPENAI_HIGH_EFFORT_MODELS
    assert s.openai_model == "gpt-5.6-terra"
    assert s.openai_fast_model == "gpt-5.6-luna"
    assert "gpt-5.6" not in OPENAI_HIGH_EFFORT_MODELS
    assert s.high_effort_models_for_provider("openai") == list(OPENAI_HIGH_EFFORT_MODELS)
    assert s.high_effort_models_for_provider("anthropic") == list(ANTHROPIC_HIGH_EFFORT_MODELS)
