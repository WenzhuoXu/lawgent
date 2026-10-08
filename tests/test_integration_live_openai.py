"""Live OpenAI Responses integration test (opt-in).

Run with::

    LEGAL_HELPER_LIVE_OPENAI=1 OPENAI_API_KEY=... pytest tests/test_integration_live_openai.py -m live
"""

from __future__ import annotations

import json
import os
from pathlib import Path

import pytest


live = pytest.mark.live


@live
def test_review_contract_live_openai():
    if not os.getenv("LEGAL_HELPER_LIVE_OPENAI"):
        pytest.skip("LEGAL_HELPER_LIVE_OPENAI not set")
    if not os.getenv("OPENAI_API_KEY"):
        pytest.skip("OPENAI_API_KEY not set")

    os.environ["MODEL_PROVIDER"] = "openai"

    from legal_helper.config import load_settings
    from legal_helper.logging_setup import jsonl_path
    from legal_helper.workflow import WorkflowExecutor

    settings = load_settings(refresh=True)
    sample = Path("examples/aviation/sample_aircraft_lease.md").resolve()
    events = list(
        WorkflowExecutor(settings=settings).stream(
            "Review this sample aircraft lease against the aviation playbook. "
            "Focus on IDERA, insurance, AD/SB allocation, and return conditions.",
            attachments=[sample],
        )
    )
    done = [ev for ev in events if ev.kind == "done"]
    assert done and done[-1].data.get("text"), "empty openai response"
    text = done[-1].data["text"].lower()
    assert any(
        kw in text
        for kw in ("idera", "return condition", "airworthiness", "ad/sb", "avn52", "avn67")
    ), f"output should mention an aviation-specific clause; got: {text[:400]}"
    assert any(ev.kind in {"tool_call", "hosted_tool_call"} for ev in events), "expected at least one tool call"

    # JSONL log must include a record with the openai_response_id.
    log_path = jsonl_path(settings)
    records = [json.loads(line) for line in log_path.read_text().splitlines() if line.strip()]
    assert any(r.get("openai_response_id") for r in records), "no openai_response_id in turn log"
