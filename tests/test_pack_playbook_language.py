"""Orchestrator-lane contracts: runtime pack-playbook surfacing + language policy.

Covers two behaviors owned by workflow.py/agent.py:

- Domain-pack playbooks are consumed at runtime: SkillAgent._system_prompt and
  _build_parent_addendum surface the active pack's playbook path plus a compact
  §-heading index, so overlay references like ``playbook §4`` resolve instead of
  being guessed at. The playbook BODY stays out of the prompt (compactness
  guard mirrors test_provider_parity's "Aviation Playbook" assertion).
- The language policy is relaxed per CLAUDE.md: internal work (planner task
  bodies, sub-agent instructions and replies) may use whichever language fits
  the material; only the structural output headings stay English for harness
  parsing, and sources are still quoted in their original language.
"""

from __future__ import annotations

from legal_helper.config import load_settings
from legal_helper.providers import RunResult


AVIATION_PLAYBOOK_REL = "legal_helper/domains/aviation/playbook.md"


class _CapturingPlanner:
    name = "mock"
    model = "mock-advanced"
    supports_hosted_web_search = False
    supports_file_search = False
    supports_structured_outputs = False

    def __init__(self) -> None:
        self.prompts: list[str] = []

    def run(self, system=None, messages=None, *args, **kwargs):
        if messages:
            self.prompts.append(messages[-1]["content"])
        return RunResult(text="{}", provider=self.name, model=self.model)

    def stream(self, *args, **kwargs):
        yield from ()

    def tool_runner(self, *args, **kwargs):
        return self.run(*args, **kwargs)


def _settings(packs: list[str]):
    return load_settings(refresh=True).model_copy(update={"active_domain_packs": packs})


# --- runtime pack-playbook surfacing ----------------------------------------


def test_skill_agent_prompt_surfaces_pack_playbook_with_section_index():
    from legal_helper.agent import SkillAgent

    sp = SkillAgent("review-contract", provider=None, settings=_settings(["aviation"]))._system_prompt()
    assert "Active domain-pack resources for this run:" in sp
    # Overlay hint still present alongside the new playbook hint.
    assert "overlays/review-contract.md" in sp
    assert AVIATION_PLAYBOOK_REL in sp
    # §-heading index makes overlay references like `playbook §2` resolvable.
    assert "§1. Insurance" in sp
    assert "§2. Cape Town Convention / IDERA" in sp
    # The playbook BODY is now inlined rather than fetched. Telling the model
    # to `read_document` it cost 35 whole-file reads of one static 18KB file in
    # a month — 80% of all instruction-fetch output — one tool iteration each,
    # then re-billed on every later iteration of the turn. A pack is only
    # active when its defaults are needed, so they belong in the prompt.
    assert "AVN52E" in sp
    assert f'read_document("{AVIATION_PLAYBOOK_REL}")' not in sp


def test_skill_agent_prompt_without_pack_has_no_playbook_hint():
    from legal_helper.agent import SkillAgent

    sp = SkillAgent("review-contract", provider=None, settings=_settings([]))._system_prompt()
    assert "Active domain-pack resources" not in sp
    assert "playbook §N" not in sp
    # The SKILL.md body is inlined now, and ten SKILL.md files hard-code an
    # aviation overlay pointer against CLAUDE.md's "no aviation strings outside
    # `domains/aviation/`" rule. Inlining must not leak that onto a run where
    # the pack is inactive.
    assert "domains/aviation" not in sp
    assert "AVN52E" not in sp


def test_parent_addendum_lists_pack_playbooks():
    from legal_helper.agent import _build_parent_addendum

    addendum = _build_parent_addendum(_settings(["aviation"]))
    assert "Pack playbooks" in addendum
    assert AVIATION_PLAYBOOK_REL in addendum
    assert "read_document" in addendum

    no_pack = _build_parent_addendum(_settings([]))
    assert "Pack playbooks" not in no_pack


def test_pack_playbook_section_index_numbered_and_compact():
    from legal_helper.agent import _pack_playbook_section_index
    from legal_helper.domains import load_pack

    index = _pack_playbook_section_index(load_pack("aviation"))
    assert "§4. AD / SB Compliance" in index
    assert "§0a." in index  # letter-suffixed numbered sections included
    assert "Aviation Playbook" not in index  # H1 title excluded
    assert "Hull All-Risks" not in index  # unnumbered sub-headings excluded
    assert len(index) < 900  # stays a compact one-line index


# --- language policy: internal language free per material --------------------


def test_parent_prompt_language_contract_relaxed():
    from legal_helper.agent import _PARENT_PROMPT_BODY

    body = " ".join(_PARENT_PROMPT_BODY.split())
    assert "must be written in English" not in body
    assert "English only" not in body
    assert "whichever language fits" in body
    # User-facing convention unchanged.
    assert "Default user-facing output to Chinese" in body


def test_sub_agent_contract_relaxed_but_structurally_parseable():
    from legal_helper.agent import _SUB_AGENT_LANGUAGE_AND_VERIFICATION_PROMPT as p

    assert "Work in English only" not in p
    assert "Any Chinese (or other non-English) output" not in p
    # Structural headings stay literal for _has_findings/_has_sources parsing.
    assert "## Findings" in p
    assert "verbatim in English" in p
    # Quotes-of-sources convention preserved.
    assert "original language" in p


def test_decomposition_prompt_allows_material_fit_language(tmp_path):
    from legal_helper.workflow import WorkflowExecutor

    provider = _CapturingPlanner()
    settings = load_settings(refresh=True).model_copy(update={"outputs_dir": tmp_path / "out"})
    executor = WorkflowExecutor(settings=settings, provider=provider)
    executor._agent_workflow_plan(
        "Compare PRC and US cross-border data-transfer rules.", complexity="complex"
    )
    prompt = provider.prompts[0]
    assert "ONLY working language" not in prompt
    assert "whichever language fits the material" in prompt
    # `user_language` is still the final-answer language signal.
    assert "user_language" in prompt
    # Quotes-of-sources convention preserved.
    assert "original language" in prompt


def test_heuristic_plan_tasks_have_no_english_only_instruction():
    from legal_helper.workflow import heuristic_plan

    plan = heuristic_plan("What does the Chicago Convention require for overflight permits?")
    assert plan.agent_tasks
    for task in plan.agent_tasks:
        assert "Work in English" not in task.task
        assert "in English" not in task.task


def test_skill_started_event_reports_free_task_language():
    import legal_helper.agent as agent_mod

    events: list[tuple[str, dict]] = []

    class _Provider:
        name = "mock"
        model = "mock"

        def tool_runner(self, *args, **kwargs):
            return RunResult(text="## Findings\n- ok", provider="mock", model="mock")

    orig = agent_mod.log_workflow_event
    agent_mod.log_workflow_event = lambda name, payload: events.append((name, payload))
    try:
        agent_mod.SkillAgent("brief", _Provider(), _settings([])).execute("task body")
    finally:
        agent_mod.log_workflow_event = orig
    started = [payload for name, payload in events if name == "skill_started"]
    assert started and started[0]["task_language"] == "free per material"
