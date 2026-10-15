"""A workflow turn end to end through the executor with a mock provider.

The mock scripts every role by its system prompt:
  - author: calls `run_workflow` with two specialists, then writes the answer
    from the bundles the tool returns
  - review-contract specialist: calls write_docx, returns its bundle
  - legal-risk-assessment specialist: returns its bundle

We then assert:
  - both specialists ran and the author saw both bundles
  - the artifact .docx exists and the final answer names it
  - JSONL log records exist for both the author and the specialist
  - the specialist's records carry the author's run_id as parent
"""

from __future__ import annotations

import json
import re
from pathlib import Path
from typing import Any, Iterator

import legal_helper.providers as providers_mod
from legal_helper.config import load_settings
from legal_helper.logging_setup import jsonl_path, log_turn, setup_logging, workflow_event_sink_var
from legal_helper.providers import StreamEvent

_TASKS = [
    {
        "id": "task_1",
        "method": "review-contract",
        "title": "Clause review",
        "task": "Review the lease clause by clause and write the redline docx.",
    },
    {
        "id": "task_2",
        "method": "legal-risk-assessment",
        "title": "Risk ranking",
        "task": "Rank the exposure.",
    },
]


class MockProvider:
    name = "mock"
    model = "mock-model"

    def __init__(self) -> None:
        self.artifact: str | None = None
        self.bundles_seen: str = ""

    def stream(self, system, messages, tools=(), *, max_iterations=None, **_: Any) -> Iterator[StreamEvent]:
        tools_by_name = {getattr(t, "name", None): t for t in tools}
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
            # The author: the provider would run this call inside its tool loop.
            self.bundles_seen = tools_by_name["run_workflow"].call(
                {"objective": "Review the lease and rank its risks.", "tasks": _TASKS}
            )
            text = f"## 结论\n\n两处偏离。Redline: {self.artifact}\n\n## 资料来源\n1. 租赁合同第12条 — 用户提供\n"
            log_turn({"provider": self.name, "model": self.model, "phase": "author",
                      "response_content": text})
        yield StreamEvent("delta", {"text": text})
        yield StreamEvent("done", {"text": text})


def test_workflow_turn_runs_specialists_and_the_author_integrates(tmp_path, monkeypatch):
    settings = load_settings(refresh=True).model_copy(
        update={"outputs_dir": tmp_path / "out", "enable_cite_check": False}
    )
    setup_logging(settings)
    mock = MockProvider()

    from legal_helper.workflow import WorkflowExecutor

    monkeypatch.setattr(providers_mod, "build_provider", lambda *a, **kw: mock)

    logged: list[dict] = []
    token = workflow_event_sink_var.set(logged.append)
    try:
        events = list(WorkflowExecutor(settings=settings, provider=mock).stream("请审阅这份租赁合同并评估风险。"))
    finally:
        workflow_event_sink_var.reset(token)
    done = [ev for ev in events if ev.kind == "done"]
    assert len(done) == 1
    final = done[0].data["text"]

    # 1. the specialists ran and the author saw both bundles
    started = [r["data"]["task_id"] for r in logged if r["event_type"] == "agent_task_started"]
    assert sorted(started) == ["task_1", "task_2"]
    assert "## bundle task_1" in mock.bundles_seen and "## bundle task_2" in mock.bundles_seen

    # 2. the .docx artifact exists and the answer names it
    assert mock.artifact and Path(mock.artifact).is_file()
    assert re.search(r"mock_review\.docx", final)

    # 3. JSONL log carries both, the specialist under the author's run as parent
    records = [json.loads(line) for line in jsonl_path(settings).read_text().splitlines() if line.strip()]
    author = [r for r in records if r.get("agent") == "orchestrator"]
    sub = [r for r in records if r.get("agent") == "review-contract"]
    assert author, "no author JSONL records"
    assert sub, "no specialist JSONL records"
    parent_run_id = author[-1]["run_id"]
    assert all(r.get("parent_run_id") == parent_run_id for r in sub)
