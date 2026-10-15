"""One output contract, rendered from one module (review Y-7, proposal 7)."""

from __future__ import annotations

import re
from datetime import datetime, timezone
from pathlib import Path

import pytest

from legal_helper import answer_contract as contract
from legal_helper.config import load_settings
from legal_helper.skills import internal_skill_names, skill_path

_PKG = Path(__file__).resolve().parent.parent / "legal_helper"

# Literals that used to be copied into each prompt and drifted apart. They may
# appear in the contract module (which renders them) and nowhere else in code
# that builds prompts.
_CONTRACT_LITERALS = (
    "资料来源与核验",
    "Sources & Verification",
    "主张/Claim",
    "在线核验/Online-checked",
    "Be comprehensive",
    "reasonable extension",
    "not by the person who asked for it",
    "goes in your reply next to the file's link",
)
_PROMPT_MODULES = ("workflow.py", "multi_agent.py", "agent.py", "authoring.py", "tools/documents.py")


@pytest.mark.parametrize("module", _PROMPT_MODULES)
def test_prompt_modules_do_not_restate_the_contract(module):
    text = (_PKG / module).read_text(encoding="utf-8")
    leaks = [lit for lit in _CONTRACT_LITERALS if lit in text]
    assert not leaks, f"{module} restates contract literals: {leaks}"


@pytest.mark.parametrize("name", [n for n in internal_skill_names() if n != "cite-check"])
def test_skills_do_not_set_their_own_length_ceiling(name):
    text = skill_path(name).read_text(encoding="utf-8")
    assert not re.search(r"≤\s*\d[\d,]*\s*characters", text), (
        f"{name}/SKILL.md sets its own bundle ceiling; the bundle contract owns length"
    )


def test_language_follows_script_share():
    assert contract.detect_language("公司拖欠工资三个月，员工能否解除合同？") == "Chinese"
    assert contract.detect_language("Is a non-compete enforceable in California?") == "English"
    # A Chinese request naming English instruments stays Chinese.
    assert (
        contract.detect_language("请分析 EU261 和 Montreal Convention 下航班取消的赔偿责任")
        == "Chinese"
    )
    # URLs do not count.
    assert contract.detect_language("看看这个 https://example.com/some/long/english/path") == "Chinese"
    assert contract.detect_language("", default="English") == "English"


def test_status_words_are_per_language():
    assert contract.STATUS["zh"]["no_pinpoint"] == "无法获得具体条款"
    assert contract.STATUS["en"]["no_pinpoint"] == "pinpoint unavailable"
    rules = contract.author_rules()
    for token in (*contract.STATUS["zh"].values(), *contract.STATUS["en"].values()):
        assert token in rules
    assert "## 资料来源" in rules and "## Sources" in rules
    # No claim column, and no disclaimer line in the body.
    assert "no claim" in rules.lower()
    assert "not-legal-advice line" in rules


def test_length_tier_is_the_pinned_tier_or_auto():
    assert contract.length_tier("brief") == "brief"
    assert contract.length_tier("full") == "full"
    assert contract.length_tier(None) == "auto"
    assert contract.length_tier("auto") == "auto"
    assert contract.VERBOSITY["brief"] == "low"
    assert contract.VERBOSITY["auto"] is None
    sentence = contract.length_instruction("brief")
    assert "wins" in sentence  # an explicit request overrides the tier


def test_author_tail_carries_the_volatile_parts():
    tail = contract.author_tail(user_language="Chinese", tier="brief")
    assert "Today's date:" in tail
    assert "Answer language: Chinese." in tail
    assert "Length: brief" in tail


def test_every_authoring_path_carries_the_deliverable_rules_once():
    from legal_helper.authoring import author_system_prompt
    from legal_helper.multi_agent import _GENERAL_SPECIALIST_PROMPT

    rules = contract.deliverable_rules()
    settings = load_settings()
    for prompt in (author_system_prompt(settings), _GENERAL_SPECIALIST_PROMPT):
        assert prompt.count(rules) == 1


def test_deliverable_rules_separate_the_file_from_the_reply():
    rules = contract.deliverable_rules()
    assert "do not paste the document body" in rules
    assert "requester's voice" in rules
    assert "example or a template" in rules
    assert "goes in your reply" in rules


def test_today_line_is_shanghai_date():
    late_utc = datetime(2026, 10, 7, 20, 0, tzinfo=timezone.utc)  # 04:00 next day in CST
    assert contract.today_line(late_utc).startswith("Today's date: 2026-10-08")


def test_specialist_contract_is_parseable_and_carries_one_label_set():
    text = contract.specialist_contract()
    for heading in ("## Findings", "## Issues", "## Artefacts", "## Out of scope", "## Sources"):
        assert heading in text
    assert "ICAO" not in text  # a domain label used to sit in the generic set
    for label in ("statute", "judicial-interpretation", "case"):
        assert label in text


def test_repair_rules_use_the_answer_language():
    assert "无法获得具体条款" in contract.repair_rules("Chinese")
    assert "## 资料来源" in contract.repair_rules("Chinese")
    assert "pinpoint unavailable" in contract.repair_rules("English")


# --- aviation strings never reach a generic prompt (CLAUDE.md:98) ------------

_AVIATION_TOKENS = (
    "FAA", "EASA", "ICAO", "IATA", "CAAC", "IDERA", "Cape Town", "Chicago Convention",
    "air services agreement", "ecaa_", "faa_", "easa_", "drs_search", "airline", "aircraft",
)


def _prompts_without_packs() -> dict[str, str]:
    from legal_helper.agent import SkillAgent
    from legal_helper.authoring import author_system_prompt
    from legal_helper.config import override_current_settings
    from legal_helper.multi_agent import _GENERAL_SPECIALIST_PROMPT
    from legal_helper.tools.skill_resources import render_skill

    settings = load_settings(refresh=True).model_copy(update={"active_domain_packs": []})

    class _Provider:
        name = "mock"
        model = "mock"

    prompts = {
        "author": author_system_prompt(settings),
        "general-specialist": _GENERAL_SPECIALIST_PROMPT,
        "specialist-contract": contract.specialist_contract(),
    }
    with override_current_settings(settings):
        for name in internal_skill_names():
            if name != "cite-check":
                prompts[f"load_skill:{name}"] = render_skill(name)
            prompts[f"specialist:{name}"] = SkillAgent(name, _Provider(), settings)._system_prompt()
    return prompts


def test_no_aviation_tokens_in_prompts_when_the_pack_is_inactive():
    leaks = {
        name: [tok for tok in _AVIATION_TOKENS if re.search(rf"(?<![A-Za-z]){re.escape(tok)}", prompt)]
        for name, prompt in _prompts_without_packs().items()
    }
    leaks = {k: v for k, v in leaks.items() if v}
    assert not leaks, f"aviation strings in generic prompts: {leaks}"


def test_a_pinned_aviation_pack_reaches_the_author_prompt():
    from legal_helper.authoring import author_system_prompt

    settings = load_settings(refresh=True).model_copy(update={"active_domain_packs": ["aviation"]})
    prompt = author_system_prompt(settings)
    assert "faa_title14_search" in prompt
    assert "`aviation` pack — research sources" in prompt
