"""A complex turn end to end through the workflow executor with a mock provider.

The mock scripts every role by its system prompt:
  - router: a complex plan; decomposer: two parallel specialists
  - review-contract specialist: calls write_docx, returns its bundle
  - legal-risk-assessment specialist: returns its bundle
  - integrator: writes the final answer from the bundles

We then assert:
  - the artifact .docx exists and the final answer names it
  - JSONL log records exist for both the orchestrator and the specialist
  - the specialist's records carry the orchestrator's run_id as parent
"""

from __future__ import annotations

import json
import re
from pathlib import Path
from typing import Any, Iterator

from legal_helper.config import load_settings
from legal_helper.logging_setup import jsonl_path, log_turn, setup_logging
from legal_helper.providers import RunResult, StreamEvent

_PLAN = json.dumps(
    {
        "title": "Lease review",
        "execution_mode": "agent_workflow",
        "complexity": "complex",
        "routing_reason": "contract review plus risk ranking",
        "issues": [
            {"id": "I1", "issue": "Deviations from the leasing playbook"},
            {"id": "I2", "issue": "Exposure ranking"},
        ],
        "agent_tasks": [
            {
                "id": "task_1",
                "skill_name": "review-contract",
                "title": "Clause review",
                "issue_ids": ["I1"],
                "task": "Review the lease clause by clause and write the redline docx.\ncoverage: clauses",
                "depends_on": [],
            },
            {
                "id": "task_2",
                "skill_name": "legal-risk-assessment",
                "title": "Risk ranking",
                "issue_ids": ["I2"],
                "task": "Rank the exposure.\ncoverage: exposure",
                "depends_on": [],
            },
        ],
        "integration_instructions": "Lead with the two flags.",
    }
)


class MockProvider:
    name = "mock"
    model = "mock-model"
    supports_hosted_web_search = False
    supports_file_search = False
    supports_structured_outputs = False

    def __init__(self) -> None:
        self.artifact: str | None = None
        self.bundles_seen: str = ""

    def run(self, system, messages, tools=(), *, max_iterations=None, structured_output=None):
        return RunResult(text=_PLAN, provider=self.name, model=self.model)

    def tool_runner(self, *args, **kwargs):
        return self.run(*args, **kwargs)

    def stream(self, system, messages, tools=(), *, max_iterations=None, **_: Any) -> Iterator[StreamEvent]:
        tools_by_name = {getattr(t, "name", None): t for t in tools}
        content = json.dumps(messages[-1]["content"], ensure_ascii=False)
        if "name: review-contract" in system:
            self.artifact = tools_by_name["write_docx"].call(
                {
                    "filename": "mock_review.docx",
                    "title": "Mock Lease Review",
                    "sections": [{"heading": "Summary", "body_markdown": "Two deviations flagged."}],
                }
            )
            text = (
                "## Findings\n- Liability minimum below playbook "
                f"[Lease, cl. 12]({self.artifact}) (label: contract)\n\n"
                "## Out of scope\n- none\n\n## Sources\n- Lease — local — supports #1"
            )
            log_turn({"provider": self.name, "model": self.model, "phase": "sub_agent",
                      "tool_calls": [{"name": "write_docx", "result": self.artifact}]})
        elif "name: legal-risk-assessment" in system:
            text = "## Findings\n- Liability gap is ORANGE (label: risk)\n\n## Sources\n- Lease — local"
        else:
            self.bundles_seen = content
            text = f"## 结论\n\n两处偏离。Redline: {self.artifact}\n\n## 资料来源\n1. 租赁合同第12条 — 用户提供\n"
            log_turn({"provider": self.name, "model": self.model, "phase": "orchestrator",
                      "response_content": text})
        yield StreamEvent("delta", {"text": text})
        yield StreamEvent("done", {"text": text})


def test_complex_turn_runs_specialists_and_integrates(tmp_path, monkeypatch):
    settings = load_settings(refresh=True).model_copy(update={"outputs_dir": tmp_path / "out"})
    setup_logging(settings)
    mock = MockProvider()

    import legal_helper.workflow as workflow_mod
    from legal_helper.workflow import WorkflowExecutor

    monkeypatch.setattr(workflow_mod, "build_provider", lambda *a, **kw: mock)

    events = list(WorkflowExecutor(settings=settings, provider=mock).stream("请审阅这份租赁合同并评估风险。"))
    done = [ev for ev in events if ev.kind == "done"]
    assert len(done) == 1
    final = done[0].data["text"]

    # 1. the specialists ran and the integrator saw both bundles and the issues
    started = [ev.data.get("id") for ev in events if ev.kind == "agent_task_started"]
    assert sorted(started) == ["task_1", "task_2"]
    assert "bundle:task_1" in mock.bundles_seen and "bundle:task_2" in mock.bundles_seen
    assert "Issues to resolve" in mock.bundles_seen

    # 2. the .docx artifact exists and the answer names it
    assert mock.artifact and Path(mock.artifact).is_file()
    assert re.search(r"mock_review\.docx", final)

    # 3. JSONL log carries both phases, the specialist under the run as parent
    records = [json.loads(line) for line in jsonl_path(settings).read_text().splitlines() if line.strip()]
    orch = [r for r in records if r.get("agent") == "orchestrator"]
    sub = [r for r in records if r.get("agent") == "review-contract"]
    assert orch, "no orchestrator JSONL records"
    assert sub, "no specialist JSONL records"
    parent_run_id = orch[-1]["run_id"]
    assert all(r.get("parent_run_id") == parent_run_id for r in sub)
