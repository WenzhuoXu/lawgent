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

    from legal_helper.agent import OrchestratorAgent
    from legal_helper.config import load_settings

    settings = load_settings(refresh=True)
    agent = OrchestratorAgent(settings=settings)
    sample = Path("examples/aviation/sample_aircraft_lease.md").resolve()
    result = agent.run(
        "Review this sample aircraft lease against the aviation playbook. "
        "Focus on IDERA, insurance, AD/SB allocation, and return conditions.",
        attachments=[sample],
    )

    assert result.text, "empty Anthropic response"
    text = result.text.lower()
    assert any(
        kw in text
        for kw in ("idera", "return condition", "airworthiness", "ad/sb", "av52", "avn52", "av67", "avn67")
    ), f"output should mention an aviation-specific clause; got: {result.text[:400]}"
    assert result.tool_calls or result.hosted_tool_calls, "expected at least one tool call"
