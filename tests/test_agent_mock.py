"""Full orchestrator + sub-agent run using a mock provider.

The mock scripts:
  Turn 1 (orchestrator): call run_skill("review-contract", ...)
  Turn 2 (review-contract sub-agent): call write_docx(...)
  Turn 3 (sub-agent): return final review text

We then assert:
  - the artifact .docx exists
  - JSONL log records exist for both the orchestrator and the sub-agent
  - the parent + sub-agent log records share the right parent_run_id / run_id
"""

from __future__ import annotations

import json
from pathlib import Path
from typing import Any, Iterable, Iterator, Optional

from legal_helper.agent import OrchestratorAgent
from legal_helper.config import load_settings
from legal_helper.logging_setup import jsonl_path, log_turn, setup_logging
from legal_helper.providers import RunResult, StreamEvent
from legal_helper.providers.base import Message, Tool, ToolCallRecord


class MockProvider:
    """Mock provider whose responses are scripted by an agent role.

    The orchestrator's system prompt mentions "orchestrator"; the sub-agent's
    system prompt is the SKILL.md (begins with "---\\nname: ...").  We branch
    on that.
    """

    name = "mock"
    supports_hosted_web_search = False
    supports_file_search = False
    supports_structured_outputs = False
    model = "mock-model"

    def __init__(self) -> None:
        self.calls: list[dict[str, Any]] = []

    def run(self, system, messages, tools=(), *, max_iterations=None, structured_output=None):
        return self.tool_runner(system, messages, tools, max_iterations=max_iterations or 1)

    def stream(self, *args, **kwargs) -> Iterator[StreamEvent]:
        result = self.run(*args, **kwargs)
        yield StreamEvent("delta", {"text": result.text})
        yield StreamEvent("done", {"text": result.text})

    def tool_runner(
        self,
        system: str,
        messages: list[Message],
        tools: Iterable[Tool],
        *,
        max_iterations: int,
        structured_output: Optional[type] = None,
    ) -> RunResult:
        tools_by_name = {getattr(t, "name", None): t for t in tools}
        self.calls.append({"system_head": system[:80], "tool_names": list(tools_by_name.keys())})

        # Orchestrator side: dispatch to the review-contract sub-agent.
        if "legal AI helper orchestrator" in system or "aviation legal-helper orchestrator" in system:
            run_skill_tool = tools_by_name["run_skill"]
            sub_text = run_skill_tool.call(
                {
                    "skill_name": "review-contract",
                    "task": (
                        "Review the sample aircraft lease against relevant skill/playbook "
                        "sections and produce a docx redline deliverable."
                    ),
                }
            )
            log_turn(
                {
                    "provider": self.name,
                    "model": self.model,
                    "tool_calls": [{"name": "run_skill", "result_preview": sub_text[:80]}],
                    "response_content": sub_text,
                    "phase": "orchestrator",
                }
            )
            return RunResult(
                text=sub_text,
                provider=self.name,
                model=self.model,
                tool_calls=[
                    ToolCallRecord(name="run_skill", arguments={"skill_name": "review-contract"})
                ],
            )

        # Sub-agent side (a compact skill manifest with task-scoped tools).
        write_docx_tool = tools_by_name["write_docx"]
        artifact = write_docx_tool.call(
            {
                "filename": "mock_review.docx",
                "title": "Mock Aviation Lease Review",
                "sections": [
                    {
                        "heading": "Summary",
                        "body_markdown": (
                            "Reviewed sample aircraft lease.  "
                            "Top finding: IDERA package present (GREEN). "
                            "AD threshold at US$2M (YELLOW).  "
                            "Liability minimum US$750M (YELLOW)."
                        ),
                    }
                ],
            }
        )
        final = (
            "## Review Summary\n\nAircraft: A321-200 MSN 7842.\n\n"
            "- IDERA: PASS\n- AD threshold: FLAG (US$2M, below US$5M playbook)\n"
            "- Liability minimum: FLAG (US$750M, playbook US$1B)\n\n"
            f"Redline package written to: {artifact}"
        )
        log_turn(
            {
                "provider": self.name,
                "model": self.model,
                "tool_calls": [{"name": "write_docx", "result": artifact}],
                "response_content": final,
                "phase": "sub_agent",
            }
        )
        return RunResult(
            text=final,
            provider=self.name,
            model=self.model,
            tool_calls=[ToolCallRecord(name="write_docx", arguments={"filename": "mock_review.docx"})],
            artifact_paths=[artifact],
        )


def test_full_orchestrator_subagent_run(tmp_path, monkeypatch):
    # Patch the orchestrator tool's lazy provider build to return our mock.
    settings = load_settings(refresh=True)
    setup_logging(settings)
    mock = MockProvider()

    # The orchestrator tool does a deferred import; patch the source module
    # so the deferred ``from ..providers import build_provider`` resolves to
    # our mock factory.
    import legal_helper.providers as providers_mod

    monkeypatch.setattr(providers_mod, "build_provider", lambda settings, **kw: mock)

    agent = OrchestratorAgent(provider=mock, settings=settings)
    result = agent.run("Please review the sample aircraft lease.")

    # 1. orchestrator returned text from the sub-agent
    assert "Review Summary" in result.text
    assert "IDERA" in result.text

    # 2. the .docx artifact actually exists
    p = Path("/")  # find the artifact via the result text
    found_path = None
    for token in result.text.split():
        token = token.rstrip(".,)")
        if token.endswith(".docx") and Path(token).is_file():
            found_path = Path(token)
            break
    assert found_path is not None and found_path.is_file(), "redline artifact not written"
    assert "outputs" in str(found_path)

    # 3. JSONL log contains both phases with shared run_id structure
    log_file = jsonl_path(settings)
    records = [json.loads(line) for line in log_file.read_text().splitlines() if line.strip()]
    orch_records = [r for r in records if r.get("agent") == "orchestrator"]
    sub_records = [r for r in records if r.get("agent") == "review-contract"]
    assert orch_records, "no orchestrator JSONL records"
    assert sub_records, "no sub-agent JSONL records"
    parent_run_id = orch_records[-1]["run_id"]
    assert all(r.get("parent_run_id") == parent_run_id for r in sub_records), \
        "sub-agent records should reference the orchestrator run_id as parent"
