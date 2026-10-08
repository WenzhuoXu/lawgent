"""Planning, authoring and integration behaviour from the 2026-10-07 review
(proposals 3, 4, 10, 12, 13)."""

from __future__ import annotations

import json
from pathlib import Path

from legal_helper.chat_models import AgentTask, ChatMessage, WorkflowPlan
from legal_helper.config import load_settings
from legal_helper.providers import RunResult, StreamEvent
from legal_helper.workflow import (
    WorkflowExecutor,
    _attachments_for_task,
    _is_low_information_task,
    _routing_view,
    _task_coverage,
    _turn_language,
)


class ScriptedProvider:
    """Router/decomposer replies in order; records every stream call."""

    name = "mock"
    model = "mock"
    supports_hosted_web_search = False
    supports_file_search = False
    supports_structured_outputs = False

    def __init__(self, plans: list[str], answer: str = "ANSWER") -> None:
        self.plans = list(plans)
        self.answer = answer
        self.prompts: list[str] = []
        self.streams: list[dict] = []

    def run(self, system=None, messages=None, tools=(), **kwargs):
        if messages:
            self.prompts.append(messages[-1]["content"])
        text = self.plans.pop(0) if self.plans else "{}"
        return RunResult(text=text, provider=self.name, model=self.model)

    def tool_runner(self, *args, **kwargs):
        return self.run(*args, **kwargs)

    def stream(self, system, messages, tools=(), *, max_iterations=None, verbosity=None):
        self.streams.append(
            {"system": system, "content": messages[-1]["content"], "tools": list(tools), "verbosity": verbosity}
        )
        yield StreamEvent("delta", {"text": self.answer})
        yield StreamEvent("done", {"text": self.answer})


def _settings(tmp_path):
    return load_settings(refresh=True).model_copy(update={"outputs_dir": tmp_path / "out"})


def _msg(role: str, content: str, n: int) -> ChatMessage:
    return ChatMessage(
        id=f"m{n}", chat_id="c", role=role, content=content, created_at=f"2026-10-0{1 + n % 6}T08:00:00+00:00"
    )


# --- M-2: the router reads the newest turns -------------------------------------


def test_routing_view_keeps_the_newest_turns_and_the_summary():
    old = [_msg("user" if i % 2 == 0 else "assistant", "旧的长篇讨论。" * 900, i) for i in range(12)]
    newest = _msg("user", "补充事实：合同于2025年3月6日解除，对方已支付一半货款。", 99)
    view = _routing_view(
        "摘要：用户为卖方，争议金额50万元。", [*old, newest], budget_tokens=4_000
    )
    assert "2025年3月6日解除" in view  # the newest turn survives
    assert "争议金额50万元" in view  # the summary survives
    assert view.index("摘要") < view.index("2025年3月6日")


def test_router_prompt_carries_the_newest_turn_not_a_head_slice(tmp_path):
    provider = ScriptedProvider(
        [json.dumps({"execution_mode": "agent_workflow", "complexity": "simple",
                     "agent_tasks": [{"skill_name": "brief", "title": "t", "task": "answer"}]})]
    )
    old = [_msg("assistant", "A" * 40_000, i) for i in range(3)]
    newest = _msg("user", "NEWEST-FACT-MARKER", 9)
    WorkflowExecutor(settings=_settings(tmp_path), provider=provider).plan(
        "继续", recent_messages=[*old, newest], memory_summary="SUMMARY-MARKER"
    )
    assert "NEWEST-FACT-MARKER" in provider.prompts[0]
    assert "SUMMARY-MARKER" in provider.prompts[0]


# --- M-1 / M-3: one planning call; coverage read in Chinese -----------------------


def test_simple_turn_needs_one_planning_call(tmp_path):
    provider = ScriptedProvider(
        [json.dumps({"execution_mode": "agent_workflow", "complexity": "standard",
                     "agent_tasks": [{"skill_name": "litigation-analysis", "title": "Limitation",
                                      "task": "Check the limitation period."}],
                     "integration_instructions": "State whether the claim is time-barred."})]
    )
    plan = WorkflowExecutor(settings=_settings(tmp_path), provider=provider).plan("诉讼时效过了吗？")
    assert len(provider.prompts) == 1
    assert [t.skill_name for t in plan.agent_tasks] == ["litigation-analysis"]
    assert plan.user_language == "Chinese"


def test_parser_default_title_is_not_a_low_information_task():
    task = AgentTask(id="task_1", skill_name="brief", title="Task 1", task="Research the 2023 amendment.")
    assert not _is_low_information_task(task)
    assert _is_low_information_task(AgentTask(id="t", skill_name="brief", title="x", task="source_verification"))


def test_coverage_line_is_read_after_a_chinese_full_stop():
    task = AgentTask(
        id="t", skill_name="brief", title="x",
        task="研究设立登记要求。coverage：establishment、foreign-investment，tax",
    )
    assert _task_coverage(task) == {"establishment", "foreign-investment", "tax"}


def test_tasks_without_coverage_are_not_merged_by_skill_name(tmp_path):
    plan = WorkflowPlan(
        title="x",
        complexity="complex",
        agent_tasks=[
            AgentTask(id="task_1", skill_name="brief", title="A", task="Research tax."),
            AgentTask(id="task_2", skill_name="brief", title="B", task="Research labour."),
        ],
    )
    normalized = WorkflowExecutor(settings=_settings(tmp_path), provider=ScriptedProvider([]))._normalize_plan(plan)
    assert [t.id for t in normalized.agent_tasks] == ["task_1", "task_2"]


# --- A-2: language and empty direct answers ---------------------------------------


def test_language_comes_from_the_request_script():
    assert _turn_language("请分析一下", [], "en") == "Chinese"
    assert _turn_language("Please analyse the clause", [], "zh") == "English"
    # Too short to tell: follow the user's last substantive message.
    recent = [_msg("user", "这份合同的违约金条款是否有效？", 1)]
    assert _turn_language("ok", recent, "en") == "Chinese"


def test_empty_direct_answer_falls_through_to_the_author(tmp_path):
    provider = ScriptedProvider(
        [json.dumps({"execution_mode": "direct_answer", "direct_response": "", "complexity": "simple"})]
    )
    plan = WorkflowExecutor(settings=_settings(tmp_path), provider=provider).plan("Is clause 7 enforceable?")
    assert plan.execution_mode == "agent_workflow"
    assert len(plan.agent_tasks) == 1


def test_direct_answer_with_legal_substance_is_rerouted(tmp_path):
    provider = ScriptedProvider(
        [json.dumps({"execution_mode": "direct_answer", "complexity": "simple",
                     "direct_response": "依据《劳动合同法》第三十八条，可以解除。"})]
    )
    plan = WorkflowExecutor(settings=_settings(tmp_path), provider=provider).plan("可以解除吗")
    assert plan.execution_mode == "agent_workflow"


def test_greeting_direct_answer_is_kept(tmp_path):
    provider = ScriptedProvider(
        [json.dumps({"execution_mode": "direct_answer", "complexity": "simple", "direct_response": "你好！"})]
    )
    plan = WorkflowExecutor(settings=_settings(tmp_path), provider=provider).plan("你好呀")
    assert plan.execution_mode == "direct_answer"


# --- R-1 / Y-1: the author gets the routed method, the tail gets the turn ------------


def test_single_pass_author_gets_the_routed_method_and_a_dated_tail(tmp_path):
    provider = ScriptedProvider(
        [json.dumps({"execution_mode": "agent_workflow", "complexity": "simple",
                     "agent_tasks": [{"skill_name": "litigation-analysis", "title": "Limitation",
                                      "task": "Check limitation."}],
                     "fact_conflicts": ["accident date given as 3月6日 and 3月7日"]})]
    )
    events = list(WorkflowExecutor(settings=_settings(tmp_path), provider=provider).stream("诉讼时效过了吗？"))
    assert [ev.kind for ev in events if ev.kind == "done"] == ["done"]
    call = provider.streams[-1]
    system = call["system"]
    assert "# General legal playbook" in system
    assert "# Method for this request (`litigation-analysis`)" in system
    assert "Output Contract (binding)" not in system  # bundle contract stripped
    assert "Today's date" not in system  # volatile content stays out of the cached prompt
    content = json.dumps(call["content"], ensure_ascii=False)
    assert "Today's date:" in content
    assert "Answer language: Chinese." in content
    assert "Length: brief" in content
    assert "3月6日" in content  # the contradiction reaches the author
    assert call["verbosity"] == "low"
    names = {getattr(t, "name", "") for t in call["tools"]}
    assert "read_skill_reference" in names


def test_integrator_has_tools_open_gaps_and_issues(tmp_path, monkeypatch):
    router = json.dumps({"execution_mode": "agent_workflow", "complexity": "complex", "agent_tasks": []})
    planner = json.dumps(
        {
            "issues": [{"id": "I1", "issue": "Whether the clause is valid"}],
            "agent_tasks": [
                {"id": "task_1", "skill_name": "brief", "title": "Validity", "issue_ids": ["I1"],
                 "task": "Validity.\ncoverage: validity"},
                {"id": "task_2", "skill_name": "compliance-check", "title": "Filing", "task": "Filing.\ncoverage: filing"},
            ],
        }
    )
    provider = ScriptedProvider([router, planner], answer="FINAL")

    class Specialist(ScriptedProvider):
        def stream(self, system, messages, tools=(), **kwargs):
            body = (
                "## Findings\n- valid (label: statute)\n\n## Out of scope\n- penalty calculation\n\n## Sources\n- x"
                if "name: brief" in system
                else ""
            )
            yield StreamEvent("delta", {"text": body})
            yield StreamEvent("done", {"text": body})

    import legal_helper.workflow as workflow_mod

    monkeypatch.setattr(workflow_mod, "build_provider", lambda *a, **k: Specialist([]))
    events = list(WorkflowExecutor(settings=_settings(tmp_path), provider=provider).stream("Is the clause valid and must we file?"))
    finished = {ev.data["task_id"]: ev.data["outcome"] for ev in events if ev.kind == "agent_task_finished"}
    assert finished == {"task_1": "completed", "task_2": "empty"}
    call = provider.streams[-1]
    names = {getattr(t, "name", "") for t in call["tools"]}
    assert "read_document" in names  # the integrator can research and write files
    assert "Specialists have already researched it" in call["system"]
    content = json.dumps(call["content"], ensure_ascii=False)
    assert "Issues to resolve" in content and "Whether the clause is valid" in content
    assert "Open gaps" in content
    assert "penalty calculation" in content  # an Out-of-scope item becomes a gap
    assert "returned nothing" in content  # the empty specialist is disclosed


# --- M-5: attachments go to the specialist that names them -------------------------


def test_attachments_go_to_the_task_that_names_them():
    lease, invoice = Path("/x/lease_2024.docx"), Path("/x/invoice.pdf")
    a = AgentTask(id="a", skill_name="review-contract", title="Lease", task="Review lease_2024.docx")
    b = AgentTask(id="b", skill_name="brief", title="Tax", task="Research VAT.")
    assert _attachments_for_task(a, [lease, invoice], [a, b]) == [lease]
    assert _attachments_for_task(b, [lease, invoice], [a, b]) == []
    # Nobody names a file: everyone gets them.
    c = AgentTask(id="c", skill_name="brief", title="Tax", task="Research VAT.")
    assert _attachments_for_task(c, [lease], [c]) == [lease]


def test_a_trailing_comma_in_a_plan_does_not_cost_the_routing_decision():
    from legal_helper.workflow import WorkflowExecutor, _strip_trailing_commas

    class _P:
        name = "mock"
        model = "mock"

    raw = '{\n "execution_mode": "agent_workflow",\n "complexity": "simple",\n "agent_tasks": [{"skill_name": "brief", "task": "a, b ,}"},],\n}'
    plan = WorkflowExecutor(provider=_P())._parse_plan_json(raw)
    assert plan.complexity == "simple"
    # Commas inside strings are content, escaped quotes included.
    assert _strip_trailing_commas('{"a": "x,}", "b": "q\\",]",}') == '{"a": "x,}", "b": "q\\",]"}'


def test_the_author_can_call_every_tool_its_method_and_pack_name():
    """Simple and standard turns run on the author's surface, so a tool a
    SKILL.md or an active pack's playbook tells it to use must be on it: the
    flowchart method named render_flowchart_image and the aviation playbook
    named retrieve_legal for the ICAO and treaty collections, and the author
    held neither. PKULaw is the documented exception (review R-6)."""
    import re

    import legal_helper
    from legal_helper.config import load_settings, override_current_settings
    from legal_helper.skills import internal_skill_names, load_skill_text
    from legal_helper.tools import _external_skill_tools, skill_tools_for_task
    from legal_helper.workflow import WorkflowExecutor, _document_io_tools

    def names(tools):
        return {getattr(t, "name", getattr(t, "__name__", "")) for t in tools}

    class _P:
        name = "openai"
        model = "mock"

    pack_root = Path(legal_helper.__file__).resolve().parent / "domains" / "aviation"
    for packs in ([], ["aviation"]):
        settings = load_settings(refresh=True).model_copy(update={"active_domain_packs": packs})
        with override_current_settings(settings):
            executor = WorkflowExecutor(settings=settings, provider=_P())
            executor._resolved = settings
            author = names(executor._author_tools(legal=True, skill_method=True))
            known = names(_document_io_tools()) | names(_external_skill_tools())
            for skill in internal_skill_names():
                known |= names(skill_tools_for_task(skill, active_packs=packs))
            texts = {s: load_skill_text(s) for s in internal_skill_names() if s != "cite-check"}
            if packs:
                for doc in ("playbook.md", "research_sources.md"):
                    texts[doc] = (pack_root / doc).read_text(encoding="utf-8")
            for source, text in texts.items():
                named = set(re.findall(r"`([a-z][a-z0-9_]+)(?:\(|`)", text)) & known
                missing = {n for n in named - author if not n.startswith("pkulaw_")}
                assert not missing, (packs, source, sorted(missing))
