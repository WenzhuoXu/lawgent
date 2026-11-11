"""The Claude Code engine against the installed claude-agent-sdk, offline.

What an SDK upgrade can break without a model call showing it: the options we
pass (a renamed or removed field raises here), the in-process MCP server, the
SDK's own parsing of the CLI's stream-json lines into the message types the
translator reads, and the shape of the plan-usage answer. Run before and after
every upgrade (see scripts/upgrade_agent_sdk.md); the zero-quota live half is
scripts/agent_sdk_check.py.
"""

from __future__ import annotations

import json
from pathlib import Path

import pytest

claude_agent_sdk = pytest.importorskip("claude_agent_sdk")

from claude_agent_sdk._internal.message_parser import parse_message  # noqa: E402

from legal_helper import logging_setup as ls  # noqa: E402
from legal_helper.claude_code import prompt as cc_prompt  # noqa: E402
from legal_helper.claude_code import runtime  # noqa: E402
from legal_helper.claude_code.executor import _Translator  # noqa: E402
from legal_helper.claude_code.tools import legal_tool_server, legal_tools  # noqa: E402
from legal_helper.config import load_settings  # noqa: E402
from legal_helper.turn_context import TurnContext, open_turn  # noqa: E402

FIXTURES = Path(__file__).parent / "fixtures" / "claude_code"
SESSION = "6f1c2d8e-0000-4000-8000-000000000001"


@pytest.fixture()
def settings(tmp_path, monkeypatch):
    s = load_settings().model_copy(update={"state_dir": tmp_path / "state", "outputs_dir": tmp_path / "out"})
    plugin = tmp_path / "plugin"
    plugin.mkdir()
    monkeypatch.setattr(runtime, "plugin_dir", lambda _s: plugin)
    return s


def test_options_build_with_every_field_we_pass(settings, tmp_path):
    with open_turn(TurnContext(settings=settings)):
        opts = runtime.options(
            settings,
            model="claude-opus-5-5",
            system_prompt=cc_prompt.author_system_prompt(settings, uploads_dir=tmp_path),
            cwd=tmp_path / "cwd",
            mcp_server=legal_tool_server(settings),
            resume=SESSION,
            add_dirs=(tmp_path,),
            agents=cc_prompt.agents(settings),
            persist=False,
        )
    assert isinstance(opts, claude_agent_sdk.ClaudeAgentOptions)
    assert opts.permission_mode == "dontAsk"
    assert opts.setting_sources == [] and opts.strict_mcp_config
    assert opts.mcp_servers["legal"]["type"] == "sdk"
    assert f"mcp__{runtime.SERVER_NAME}" in opts.allowed_tools
    assert set(opts.agents) == {cc_prompt.SPECIALIST_AGENT, cc_prompt.DECK_AGENT}
    assert all(a.model == "inherit" and a.effort for a in opts.agents.values())
    assert "no-session-persistence" in opts.extra_args


def test_the_legal_server_carries_the_research_and_document_tools(settings):
    with open_turn(TurnContext(settings=settings)):
        names = {t.name for t in legal_tools(settings)}
        server = legal_tool_server(settings)
    assert {"legal_search", "legal_fetch", "legal_sources", "read_document", "write_docx"} <= names
    assert not names & {"load_skill", "run_workflow", "create_deck"}
    assert server["name"] == runtime.SERVER_NAME


def _turn_lines() -> list[dict]:
    """One turn as the CLI writes it with --output-format stream-json and partial messages."""
    def ev(event, parent=None):
        return {"type": "stream_event", "uuid": "u", "session_id": SESSION, "event": event,
                "parent_tool_use_id": parent}

    return [
        {"type": "system", "subtype": "init", "session_id": SESSION, "model": "claude-opus-5-5",
         "apiKeySource": "none", "claude_code_version": "2.1.321", "permissionMode": "dontAsk",
         "tools": ["Read", "WebFetch", "mcp__legal__legal_search"], "skills": ["legal:brief"],
         "mcp_servers": [{"name": "legal", "status": "connected"}]},
        ev({"type": "message_start", "message": {"usage": {"input_tokens": 3, "cache_read_input_tokens": 9000,
                                                             "cache_creation_input_tokens": 1000}}}),
        ev({"type": "content_block_start", "index": 0, "content_block": {"type": "text", "text": ""}}),
        ev({"type": "content_block_delta", "index": 0, "delta": {"type": "text_delta", "text": "先查原文。"}}),
        ev({"type": "content_block_stop", "index": 0}),
        ev({"type": "content_block_start", "index": 1,
            "content_block": {"type": "tool_use", "id": "toolu_1", "name": "mcp__legal__legal_search", "input": {}}}),
        ev({"type": "content_block_stop", "index": 1}),
        {"type": "assistant", "session_id": SESSION, "parent_tool_use_id": None,
         "message": {"role": "assistant", "model": "claude-opus-5-5", "content": [
             {"type": "text", "text": "先查原文。"},
             {"type": "tool_use", "id": "toolu_1", "name": "mcp__legal__legal_search",
              "input": {"jurisdiction": "CN", "query": "民法典 无因管理"}}]}},
        {"type": "user", "session_id": SESSION, "parent_tool_use_id": None,
         "message": {"role": "user", "content": [
             {"type": "tool_result", "tool_use_id": "toolu_1",
              "content": [{"type": "text", "text": "{\"count\": 1}"}], "is_error": False}]}},
        {"type": "rate_limit_event", "uuid": "r", "session_id": SESSION,
         "rate_limit_info": {"status": "allowed", "rateLimitType": "seven_day", "utilization": 0.18}},
        ev({"type": "message_start", "message": {"usage": {"input_tokens": 2, "cache_read_input_tokens": 11000}}}),
        ev({"type": "content_block_start", "index": 0, "content_block": {"type": "text", "text": ""}}),
        ev({"type": "content_block_delta", "index": 0, "delta": {"type": "text_delta", "text": "第九百七十九条规定了无因管理。"}}),
        ev({"type": "content_block_stop", "index": 0}),
        {"type": "assistant", "session_id": SESSION, "parent_tool_use_id": None,
         "message": {"role": "assistant", "model": "claude-opus-5-5", "content": [
             {"type": "text", "text": "第九百七十九条规定了无因管理。"}]}},
        {"type": "result", "subtype": "success", "duration_ms": 4200, "duration_api_ms": 3900,
         "is_error": False, "num_turns": 2, "session_id": SESSION, "result": "第九百七十九条规定了无因管理。",
         "usage": {"input_tokens": 5, "output_tokens": 120, "cache_read_input_tokens": 20000,
                   "cache_creation_input_tokens": 1000}},
    ]


def test_sdk_parses_the_cli_stream_into_what_the_translator_reads(settings):
    events: list[tuple[str, dict]] = []
    token = ls.workflow_event_sink_var.set(lambda rec: events.append((rec.get("event_type"), rec.get("data", {}))))
    try:
        translator = _Translator(settings, "claude-opus-5-5", "Chinese")
        streamed = []
        for line in _turn_lines():
            msg = parse_message(line)
            assert msg is not None, line["type"]
            streamed += list(translator.on_message(msg))
        answer_events = []
        gen = translator.finish()
        try:
            while True:
                answer_events.append(next(gen))
        except StopIteration as stop:
            answer = stop.value
    finally:
        ls.workflow_event_sink_var.reset(token)

    kinds = [e.kind for e in streamed]
    assert "commentary" in kinds
    assert next(e.data["text"] for e in streamed if e.kind == "commentary") == "先查原文。"
    assert answer == "第九百七十九条规定了无因管理。"
    logged = dict((k, d) for k, d in events)
    assert logged["claude_code_session"]["version"] == "2.1.321"
    assert logged["claude_code_session"]["auth"] == "subscription"
    assert logged["claude_code_session"]["mcp_servers"] == [{"name": "legal", "status": "connected"}]
    started = logged["local_tool_call_started"]
    assert started["tool_name"] == "legal_search" and started["call_id"] == "toolu_1"
    finished = logged["local_tool_call_finished"]
    assert finished["call_id"] == "toolu_1" and finished["output_preview"] == "{\"count\": 1}"
    assert logged["rate_limit"]["rate_limit_type"] == "seven_day"
    assert translator.session_id == SESSION
    assert translator.error_message() is None
    usage = translator.usage_payload()
    assert usage["output_tokens"] == 120 and usage["last_request_input_tokens"] == 11002


def test_an_error_result_reaches_the_user_as_its_own_message(settings):
    translator = _Translator(settings, "claude-opus-5-5", "Chinese")
    msg = parse_message({"type": "result", "subtype": "error_during_execution", "duration_ms": 1,
                         "duration_api_ms": 1, "is_error": True, "num_turns": 1, "session_id": SESSION,
                         "errors": ["API Error: EPROTO"]})
    list(translator.on_message(msg))
    assert translator.error_message() == "API Error: EPROTO"


def test_plan_usage_answer_still_has_the_shape_we_read():
    raw = json.loads((FIXTURES / "get_usage.json").read_text(encoding="utf-8"))
    usage = runtime.normalize_plan_usage(raw)
    assert usage["subscription_type"] == raw["subscription_type"]
    assert usage["available"] is True
    assert usage["limits"] and all(isinstance(lim["percent"], (int, float)) for lim in usage["limits"])
    assert {lim["group"] for lim in usage["limits"]} == {"weekly"}
    assert usage["extra_usage"]["currency"] == "USD"


def test_plan_usage_still_goes_through_the_private_control_channel():
    # runtime.request_plan_usage calls client._query._send_control_request:
    # an SDK release that renames it breaks the usage panel silently.
    from claude_agent_sdk._internal.query import Query

    assert hasattr(Query, "_send_control_request")
