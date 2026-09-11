"""Mock the OpenAI Responses API to test function-tool loop + structured output."""

from __future__ import annotations

import json
from types import SimpleNamespace
from unittest.mock import patch

import pytest

from legal_helper.config import load_settings
from legal_helper.providers.openai_provider import OpenAIProvider


def _fc_item(call_id: str, name: str, args: dict) -> SimpleNamespace:
    return SimpleNamespace(
        type="function_call",
        call_id=call_id,
        id=call_id,
        name=name,
        arguments=json.dumps(args),
    )


def _msg_item(text: str) -> SimpleNamespace:
    return SimpleNamespace(
        type="message",
        content=[SimpleNamespace(text=text)],
    )


def _resp(items, response_id="resp_x", usage=None) -> SimpleNamespace:
    return SimpleNamespace(
        id=response_id,
        output=items,
        output_text="\n".join(getattr(c, "text", "") for it in items for c in getattr(it, "content", [])),
        usage=usage,
    )


def test_function_tool_loop_routes_local_call(monkeypatch):
    settings = load_settings(refresh=True)
    provider = OpenAIProvider(settings)

    from legal_helper.tools.documents import read_document

    calls: list = []

    def fake_create(**kwargs):
        calls.append(kwargs)
        if len(calls) == 1:
            return _resp(
                [_fc_item("call_1", "read_document", {"path": "examples/aviation/sample_aircraft_lease.md"})],
                response_id="resp_1",
                usage=SimpleNamespace(input_tokens=10, output_tokens=2, total_tokens=12),
            )
        return _resp(
            [_msg_item("IDERA: PASS\nLiability: FLAG (US$750M)\nNot legal advice.")],
            response_id="resp_2",
            usage=SimpleNamespace(input_tokens=15, output_tokens=5, total_tokens=20),
        )

    with patch.object(provider.client.responses, "create", side_effect=fake_create):
        result = provider.tool_runner(
            system="You are a test sub-agent.",
            messages=[{"role": "user", "content": "review the lease"}],
            tools=[read_document],
            max_iterations=4,
        )

    assert "IDERA" in result.text
    assert result.iterations >= 2
    assert result.response_id == "resp_2"
    # Local tool was actually invoked, and its output appended on the next turn.
    assert len(calls) == 2
    second_call_input = calls[1]["input"]
    assert any(item.get("type") == "function_call_output" for item in second_call_input)
    fco = next(item for item in second_call_input if item.get("type") == "function_call_output")
    assert fco["call_id"] == "call_1"
    # Tool actually returned the file content.
    assert "AIRCRAFT OPERATING LEASE" in fco["output"] or "IDERA" in fco["output"]


def test_hosted_web_search_call_recorded():
    settings = load_settings(refresh=True)
    provider = OpenAIProvider(settings)

    def fake_create(**kwargs):
        items = [
            SimpleNamespace(type="web_search_call", id="ws_1"),
            _msg_item("Cited FAA source confirms AD 2026-09-51."),
        ]
        return _resp(items, response_id="resp_ws", usage=SimpleNamespace(input_tokens=5, output_tokens=3))

    with patch.object(provider.client.responses, "create", side_effect=fake_create):
        result = provider.tool_runner(
            system="Find an FAA AD.",
            messages=[{"role": "user", "content": "AD 2026-09-51"}],
            tools=[{"type": "web_search"}],
            max_iterations=2,
        )

    assert any(c.name == "web_search_call" for c in result.hosted_tool_calls)
    assert "Cited FAA source" in result.text


def test_tool_loop_ceiling_forces_a_final_synthesis_pass(monkeypatch):
    """A loop cut off by max_iterations must not ship an empty answer.

    Every zero-length answer observed in production sat at
    iterations == max_iterations: the model kept asking for tools and never
    got a turn in which it had to write prose.
    """
    settings = load_settings(refresh=True)
    provider = OpenAIProvider(settings)

    from legal_helper.tools.documents import read_document

    calls: list = []

    def fake_create(**kwargs):
        calls.append(kwargs)
        # Always ask for another tool — the model never volunteers prose.
        if kwargs.get("tools"):
            return _resp(
                [_fc_item(f"call_{len(calls)}", "read_document", {"path": "README.md"})],
                response_id=f"resp_{len(calls)}",
                usage=SimpleNamespace(input_tokens=10, output_tokens=2, total_tokens=12),
            )
        # The recovery pass: no tools offered, so it must answer.
        return _resp(
            [_msg_item("Answer assembled from the material already gathered.")],
            response_id="resp_final",
            usage=SimpleNamespace(input_tokens=20, output_tokens=8, total_tokens=28),
        )

    with patch.object(provider.client.responses, "create", side_effect=fake_create):
        result = provider.tool_runner(
            system="You are a test sub-agent.",
            messages=[{"role": "user", "content": "review it"}],
            tools=[read_document],
            max_iterations=3,
        )

    assert result.text.strip(), "ceiling exit must not return an empty answer"
    assert "already gathered" in result.text
    # 3 tool-armed iterations, then exactly one tool-free recovery call.
    assert len(calls) == 4
    assert not calls[-1].get("tools")
    assert calls[-1]["input"][-1]["content"][0]["text"].startswith("You have reached")


def test_no_synthesis_pass_when_the_loop_ends_normally():
    """The recovery call is spent only on a real ceiling exit."""
    settings = load_settings(refresh=True)
    provider = OpenAIProvider(settings)

    calls: list = []

    def fake_create(**kwargs):
        calls.append(kwargs)
        return _resp(
            [_msg_item("Done in one pass.")],
            response_id="resp_1",
            usage=SimpleNamespace(input_tokens=5, output_tokens=3),
        )

    with patch.object(provider.client.responses, "create", side_effect=fake_create):
        result = provider.tool_runner(
            system="s",
            messages=[{"role": "user", "content": "q"}],
            tools=[],
            max_iterations=3,
        )

    assert result.text == "Done in one pass."
    assert len(calls) == 1
