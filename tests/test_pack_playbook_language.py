"""Orchestrator-lane contracts: runtime pack-playbook surfacing + language policy.

Covers two behaviors owned by workflow.py/agent.py:

- Domain-pack playbooks are consumed at runtime: SkillAgent._system_prompt and
  _build_parent_addendum surface the active pack's playbook path plus a compact
  §-heading index, so overlay references like ``playbook §4`` resolve instead of
  being guessed at. The playbook BODY stays out of the prompt (compactness
  guard mirrors test_provider_parity's "Aviation Playbook" assertion).
- The language policy is relaxed per CLAUDE.md: internal work (specialist
  tasks, sub-agent instructions and replies) may use whichever language fits
  the material; only the structural output headings stay English for harness
  parsing, and sources are still quoted in their original language.
"""

from __future__ import annotations

from legal_helper.config import load_settings


AVIATION_PLAYBOOK_REL = "legal_helper/domains/aviation/playbook.md"


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


class _NullProvider:
    name = "mock"
    model = "mock"


def test_parent_addendum_inlines_active_pack_playbook_and_sources():
    from legal_helper.agent import _build_parent_addendum

    addendum = _build_parent_addendum(_settings(["aviation"]))
    assert "`aviation` pack playbook" in addendum
    assert "`aviation` pack — research sources" in addendum
    assert "faa_title14_search" in addendum

    no_pack = _build_parent_addendum(_settings([]))
    assert "pack playbook" not in no_pack
    assert "faa_title14_search" not in no_pack


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


def test_author_prompt_language_contract_relaxed():
    from legal_helper.answer_contract import author_tail
    from legal_helper.authoring import author_system_prompt

    body = " ".join(author_system_prompt(_settings([])).split())
    assert "must be written in English" not in body
    assert "English only" not in body
    # Internal work may use whichever language fits; the answer's language is
    # set per turn in the request tail.
    assert "whichever language best fits the material" in body
    assert "Answer language: Chinese." in author_tail(user_language="Chinese", tier="brief")


def test_sub_agent_contract_relaxed_but_structurally_parseable():
    from legal_helper.agent import SkillAgent

    p = " ".join(SkillAgent("brief", _NullProvider(), _settings([]))._system_prompt().split())
    assert "Work in English only" not in p
    assert "Any Chinese (or other non-English) output" not in p
    # Structural headings stay literal for _has_findings/_has_sources parsing.
    assert "## Findings" in p
    assert "verbatim in English" in p
    # Quotes-of-sources convention preserved.
    assert "original language" in p
