"""Live Anthropic integration test (opt-in).

Run with::

    LEGAL_HELPER_LIVE=1 ANTHROPIC_API_KEY=... pytest tests/test_integration_live.py -m live
"""

from __future__ import annotations

import os
from pathlib import Path

import pytest


live = pytest.mark.live


@live
def test_review_contract_live_anthropic():
    if not os.getenv("LEGAL_HELPER_LIVE"):
        pytest.skip("LEGAL_HELPER_LIVE not set")
    if not os.getenv("ANTHROPIC_API_KEY"):
        pytest.skip("ANTHROPIC_API_KEY not set")

    os.environ["MODEL_PROVIDER"] = "anthropic"

    from legal_helper.config import load_settings
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
    assert done and done[-1].data.get("text"), "empty anthropic response"
    text = done[-1].data["text"].lower()
    assert any(
        kw in text
        for kw in ("idera", "return condition", "airworthiness", "ad/sb", "av52", "avn52", "av67", "avn67")
    ), f"output should mention an aviation-specific clause; got: {text[:400]}"
    assert any(ev.kind in {"tool_call", "hosted_tool_call"} for ev in events), "expected at least one tool call"
