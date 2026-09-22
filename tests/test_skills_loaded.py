"""All generic SKILL.md files load with valid frontmatter and disclaimer.

The aviation-specific assertions moved to ``tests/test_domain_pack.py`` and
are exercised through the aviation overlay files there.
"""

from __future__ import annotations

from legal_helper.skills import (
    internal_skill_names,
    list_skills,
    load_playbook,
    load_skill_body,
    load_skill_frontmatter,
    load_skill_text,
)


_EXPECTED_SKILLS = {
    "brief",
    "cite-check",
    "compliance-check",
    "docx-redline",
    "draft-agreement",
    "flowchart",
    "legal-response",
    "legal-risk-assessment",
    "litigation-analysis",
    "meeting-briefing",
    "review-contract",
    "signature-request",
    "tabular-review",
    "triage-nda",
    "vendor-check",
}


def test_skill_set_matches_registry():
    # The shipped legal skills are a fixed set; discovery may add external
    # procedural skills on top, which this contract deliberately ignores.
    assert set(internal_skill_names()) == _EXPECTED_SKILLS


def test_each_skill_loads_with_frontmatter_and_disclaimer():
    for name in internal_skill_names():
        text = load_skill_text(name)
        assert text.startswith("---\n"), f"{name} missing frontmatter"
        fm = load_skill_frontmatter(name)
        assert fm.get("name") == name, f"{name} frontmatter name mismatch"
        assert fm.get("description"), f"{name} missing description"

        body = load_skill_body(name)
        lowered = " ".join(body.lower().split())
        assert "not legal advice" in lowered or "not provide legal advice" in lowered, (
            f"{name} missing disclaimer"
        )


def test_skill_references_general_playbook():
    """Each generic skill points at the general playbook for citation + sanity rules."""
    for name in internal_skill_names():
        body = load_skill_body(name).lower()
        assert "general_playbook" in body or "playbook/general_playbook" in body, (
            f"{name} should point at /playbook/general_playbook.md"
        )


def test_list_skills_returns_dicts():
    """`list_skills` reports every discovered skill, in-tree and external.

    It is the catalogue a caller enumerates, so it is a superset of the
    shipped legal skills rather than equal to them, and it says which entries
    came from outside the package.
    """
    items = list_skills()
    names = {item["name"] for item in items}
    assert _EXPECTED_SKILLS <= names
    for item in items:
        assert "name" in item and "description" in item
        assert "external" in item
    internal = {item["name"] for item in items if not item["external"]}
    assert internal == _EXPECTED_SKILLS


def test_playbook_loads():
    txt = load_playbook()
    assert "playbook" in txt.lower() or "Legal Playbook" in txt


def test_general_playbook_has_jurisdiction_and_citation_rules():
    txt = load_playbook()
    lowered = " ".join(txt.lower().split())
    # Jurisdiction & source hierarchy
    assert "jurisdiction" in lowered
    assert "prc" in lowered or "china" in lowered
    # Language rule (working language free per material; user-facing follows user)
    assert "english" in lowered and "chinese" in lowered
    # Citation standard
    assert "citation" in lowered and ("pinpoint" in lowered or "## sources" in lowered.replace("# sources", "## sources"))
    # Sanity check contract
    assert "sanity" in lowered


def test_active_pack_aviation_loads_overlay_for_each_skill():
    from legal_helper.domains import load_pack

    pack = load_pack("aviation")
    # flowchart / docx-redline are document-tool skills with no aviation overlay.
    expected_with_overlay = _EXPECTED_SKILLS - {"flowchart", "docx-redline"}
    for skill in expected_with_overlay:
        overlay = pack.overlay_for(skill)
        assert overlay is not None and overlay.is_file(), (
            f"aviation pack missing overlay for {skill}"
        )
