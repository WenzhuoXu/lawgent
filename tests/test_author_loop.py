"""Every turn is answered by the chat's own model, in one tool loop.

A fast-tier router used to read each turn first and, for 48% of turns on
2026-10-08..14, answer it itself — on the fast model, at low effort, with no
tools — while the message carried the chat model's name. One of those answers
was a wrong LSAT necessary-assumption answer. These tests pin the replacement:
no planning call on any turn, one author request holding every tool, methods
loaded on demand, and the workflow as a tool.
"""

from __future__ import annotations

import re
from pathlib import Path

import pytest

from legal_helper.chat_models import ChatMessage
from legal_helper.config import load_settings, override_current_settings
from legal_helper.providers import StreamEvent
from legal_helper.workflow import WorkflowExecutor, _turn_language, author_tool_surface


class ScriptedProvider:
    """Streams one scripted answer per call; any one-shot `run` is a planner call."""

    name = "openai"
    model = "gpt-6.1-sol"

    def __init__(self, answer: str = "ANSWER") -> None:
        self.answer = answer
        self.streams: list[dict] = []

    def run(self, *args, **kwargs):
        raise AssertionError("a planning call was made")

    def tool_runner(self, *args, **kwargs):
        raise AssertionError("a planning call was made")

    def stream(self, system, messages, tools=(), *, max_iterations=None, verbosity=None):
        self.streams.append(
            {"system": system, "content": messages[-1]["content"], "tools": list(tools), "verbosity": verbosity}
        )
        yield StreamEvent("delta", {"text": self.answer})
        yield StreamEvent("done", {"text": self.answer, "usage": {"input_tokens": 9, "output_tokens": 2}})


def _settings(tmp_path, **updates):
    base = {"outputs_dir": tmp_path / "out", "state_dir": tmp_path / "state", "enable_cite_check": False}
    base.update(updates)
    return load_settings(refresh=True).model_copy(update=base)


def _names(tools) -> list[str]:
    return [getattr(t, "name", None) or (t.get("name") or t.get("type")) for t in tools]


def _text(content) -> str:
    if isinstance(content, str):
        return content
    return "\n".join(block.get("text", "") for block in content if isinstance(block, dict))


def _msg(role: str, content: str, n: int) -> ChatMessage:
    return ChatMessage(
        id=f"m{n}", chat_id="c", role=role, content=content, created_at=f"2026-10-0{1 + n % 6}T08:00:00+00:00"
    )


@pytest.mark.parametrize(
    "message",
    [
        "你好",
        "5. Barnes: The two newest employees ... Which one of the following is an assumption?",
        "劳动合同到期不续签，用人单位要支付经济补偿吗？",
        "请用多智能体工作流分析外资准入和数据出境两个问题",
    ],
)
def test_every_turn_is_one_author_stream_on_the_chat_model(tmp_path, message):
    provider = ScriptedProvider()
    executor = WorkflowExecutor(settings=_settings(tmp_path), provider=provider)
    events = list(executor.stream(message))
    assert len(provider.streams) == 1
    assert events[-1].kind == "done"
    names = _names(provider.streams[0]["tools"])
    for required in ("run_workflow", "load_skill", "read_skill_file", "read_document", "write_docx"):
        assert required in names


def test_done_names_the_model_that_wrote_the_answer(tmp_path):
    executor = WorkflowExecutor(settings=_settings(tmp_path), provider=ScriptedProvider())
    done = [e for e in executor.stream("hi") if e.kind == "done"][0]
    assert done.data["model"] == "gpt-6.1-sol"
    assert done.data["provider"] == "openai"
    assert done.data["usage"]["input_tokens"] == 9


def test_the_request_carries_the_dated_tail_the_digest_and_the_message(tmp_path):
    provider = ScriptedProvider()
    executor = WorkflowExecutor(settings=_settings(tmp_path), provider=provider)
    recent = [_msg("user", "我们公司在上海。", 1), _msg("assistant", "好的。", 2)]
    list(executor.stream("员工能否解除合同？", recent_messages=recent, memory_summary="## Goals\n- 劳动争议"))
    content = _text(provider.streams[0]["content"])
    assert "Today's date:" in content
    assert "Answer language: Chinese." in content
    assert "我们公司在上海。" in content
    assert "劳动争议" in content
    assert content.rstrip().endswith("Current user request:\n员工能否解除合同？")


def test_the_author_prompt_is_the_same_on_every_turn(tmp_path):
    provider = ScriptedProvider()
    settings = _settings(tmp_path)
    for message in ("hi", "合同违约怎么办？", "Translate this sentence."):
        list(WorkflowExecutor(settings=settings, provider=provider).stream(message))
    systems = {call["system"] for call in provider.streams}
    assert len(systems) == 1
    system = systems.pop()
    assert "Today's date" not in system  # per-turn content lives in the tail
    assert "# Legal methods" in system and "`review-contract`" in system
    assert "`run_workflow`" in system and "mandatory" in system


def test_a_method_the_user_picked_is_inlined_in_the_tail(tmp_path):
    provider = ScriptedProvider()
    executor = WorkflowExecutor(settings=_settings(tmp_path), provider=provider)
    list(executor.stream("Review the attached NDA", skill_hint="triage-nda"))
    content = _text(provider.streams[0]["content"])
    assert '<skill_content name="triage-nda">' in content
    # The method body is in the request, never in the cached system prompt.
    assert '<skill_content name="triage-nda">' not in provider.streams[0]["system"]
    # The bundle contract is for specialists; the author writes the answer.
    assert "Output Contract" not in content


def test_a_pinned_length_sets_verbosity_and_auto_sends_none(tmp_path):
    provider = ScriptedProvider()
    list(WorkflowExecutor(settings=_settings(tmp_path), provider=provider).stream("hi"))
    list(WorkflowExecutor(settings=_settings(tmp_path, answer_length="brief"), provider=provider).stream("hi"))
    assert provider.streams[0]["verbosity"] is None
    assert provider.streams[1]["verbosity"] == "low"
    assert "Length: fit the request" in _text(provider.streams[0]["content"])


def test_language_comes_from_the_request_script():
    assert _turn_language("请分析一下", [], "en") == "Chinese"
    assert _turn_language("Please analyse the clause", [], "zh") == "English"
    # Too short to tell: follow the user's last substantive message.
    recent = [_msg("user", "这份合同的违约金条款是否有效？", 1)]
    assert _turn_language("ok", recent, "en") == "Chinese"


def test_a_specialist_surface_never_holds_the_workflow():
    settings = load_settings(refresh=True)
    assert "run_workflow" in _names(author_tool_surface(settings))
    general = _names(author_tool_surface(settings, legal=False, workflow=False))
    assert "run_workflow" not in general and "load_skill" not in general
    from legal_helper.tools import skill_tools_for_task

    for skill in ("brief", "review-contract", "litigation-analysis"):
        assert "run_workflow" not in _names(skill_tools_for_task(skill))


def test_load_skill_returns_the_method_without_the_bundle_contract():
    from legal_helper.tools.skill_resources import load_skill, read_skill_file

    settings = load_settings(refresh=True).model_copy(update={"active_domain_packs": []})
    with override_current_settings(settings):
        body = load_skill.call({"name": "brief"})
    assert body.startswith('<skill_content name="brief">')
    assert "## Workflow" in body
    assert "Output Contract" not in body
    assert "pack overlay" not in body
    assert load_skill.call({"name": "made-up"}).startswith("ERROR: Unknown method")
    escape = read_skill_file.call({"name": "brief", "path": "../review-contract/SKILL.md"})
    assert escape.startswith("ERROR")


def test_load_skill_appends_the_active_pack_overlay():
    from legal_helper.tools.skill_resources import load_skill

    settings = load_settings(refresh=True).model_copy(update={"active_domain_packs": ["aviation"]})
    with override_current_settings(settings):
        body = load_skill.call({"name": "brief"})
    assert "`aviation` pack overlay for `brief`" in body


def test_the_author_can_call_every_tool_its_method_and_pack_name():
    """A tool a SKILL.md or an active pack's playbook tells the author to use
    must be on its surface (PKULaw is the documented exception, review R-6)."""
    import legal_helper
    from legal_helper.skills import internal_skill_names, load_skill_text
    from legal_helper.tools import _external_skill_tools, skill_tools_for_task
    from legal_helper.workflow import _document_io_tools

    def names(tools):
        return {getattr(t, "name", getattr(t, "__name__", "")) for t in tools}

    pack_root = Path(legal_helper.__file__).resolve().parent / "domains" / "aviation"
    for packs in ([], ["aviation"]):
        settings = load_settings(refresh=True).model_copy(update={"active_domain_packs": packs})
        with override_current_settings(settings):
            author = names(author_tool_surface(settings))
            known = names(_document_io_tools()) | names(_external_skill_tools())
            for skill in internal_skill_names():
                known |= names(skill_tools_for_task(skill, active_packs=packs))
            texts = {s: load_skill_text(s) for s in internal_skill_names() if s != "cite-check"}
            if packs:
                for doc in ("playbook.md", "research_sources.md"):
                    texts[doc] = (pack_root / doc).read_text(encoding="utf-8")
            for source, text in texts.items():
                named = set(re.findall(r"`([a-z][a-z0-9_]+)(?:\(|`)", text)) & known
                # A specialist's own skill-reference tools are `read_skill_file`
                # on the author's surface.
                named -= {"list_skill_references", "read_skill_reference"}
                missing = {n for n in named - author if not n.startswith("pkulaw_")}
                assert not missing, (packs, source, sorted(missing))
