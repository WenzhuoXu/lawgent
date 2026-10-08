"""Doctrine guards for the legal-method text the model reads.

The 2026-10-07 capability review (R-3, proposal 13) found the general
playbook ranking 规范性文件 above 司法解释, putting treaties at the top of a
universal stack, and the aviation pack ranking ICAO Annexes above national
law. Every corrected rule was verified at an official source; these tests
keep the corrections from silently reverting. They assert the presence of
rules, not prose.
"""

from __future__ import annotations

import re
from pathlib import Path

import pytest
import yaml

from legal_helper.domains import load_pack
from legal_helper.skills import load_playbook, load_playbook_section, skill_path

ROOT = Path(__file__).resolve().parents[1]
STAGES = {"arises", "extinguished", "exercisable", "quantum"}
_PINPOINT_RE = re.compile(r"^第[一二三四五六七八九十百千零]+条(第[一二三四五六七八九十]+款)?$")


def test_playbook_section0_separates_legislation_from_judicial_authority():
    section = load_playbook_section("Jurisdiction & Source Hierarchy")
    playbook = load_playbook()
    assert "binding stack" not in playbook
    assert "International / supranational" not in playbook
    assert "规范性文件 → 司法解释" not in playbook
    for marker in ("立法法 2023修正", "宪法 > 法律", "部门规章 = 地方政府规章", "司法解释工作规定",
                   "裁判依据", "法释〔2009〕14号", "行政诉讼法"):
        assert marker in section, marker


def test_playbook_section0_case_weight_labels():
    section = load_playbook_section("Jurisdiction & Source Hierarchy")
    for label in ("`应当参照 / must-follow`", "`参考 / reference`", "`persuasive / 说服性`",
                  "`binding / 拘束性`"):
        assert label in section, label
    assert "人民法院案例库" in section and "类案检索意见" in section


def test_playbook_section0_conflicts_treaties_choice_of_law_and_time():
    section = load_playbook_section("Jurisdiction & Source Hierarchy")
    for marker in ("上位法优于下位法", "第103条", "第104条", "第105、106条", "name the rule",
                   "domestic hook", "法释〔2023〕15号", "声明保留", "涉外民事关系法律适用法",
                   "法院地法", "Date before text", "法释〔2020〕15号"):
        assert marker in section, marker


def test_playbook_reasoning_section_ties_steps_to_section0():
    section = load_playbook_section("3a. Structured legal reasoning")
    lowered = section.lower()
    for marker in ("operative date", "conflict of norms", "weight-label", "domestic hook",
                   "element by element", "adverse authority", "contradict"):
        assert marker in lowered, marker
    assert "§4" not in section
    assert "规范性文件 → 司法解释" not in section


def test_claude_md_hierarchy_sentence_follows_the_playbook():
    text = (ROOT / "CLAUDE.md").read_text(encoding="utf-8")
    assert "规范性文件 → 司法解释" not in text
    assert "general playbook §0" in text


def test_aviation_pack_routes_rank_through_the_domestic_hook():
    text = load_pack("aviation").playbook_path.read_text(encoding="utf-8")
    assert "international convention\n  / ICAO Annex →" not in text
    assert "domestic hook" in text
    # 民用航空法 2025 revision (in force 2026-07-01) moved the treaty clause.
    assert "第二百六十条" in text and "第一百八十四条" in text
    assert "art. 37" in text and "art. 38" in text  # SARPs bind states; filed differences
    assert "49 USC § 40105" not in text


PRESET_DIR = skill_path("litigation-analysis").parent / "resources" / "element_tables"


@pytest.mark.parametrize("preset_file", sorted(PRESET_DIR.glob("*.yaml")), ids=lambda p: p.stem)
def test_presets_have_lifecycle_stages_and_well_formed_pinpoints(preset_file):
    preset = yaml.safe_load(preset_file.read_text(encoding="utf-8"))
    for defence in preset.get("defenses") or []:
        assert defence.get("stage") in STAGES, f"{preset_file.name}: {defence['id']}"
    entries = (preset.get("elements") or []) + (preset.get("defenses") or [])
    pinpoints = [b["pinpoint"] for b in preset.get("legal_basis") or []]
    pinpoints += [a["pinpoint"] for e in entries for a in e.get("authority") or []]
    for pinpoint in pinpoints:
        assert _PINPOINT_RE.match(pinpoint), f"{preset_file.name}: {pinpoint!r}"


def test_new_presets_ship():
    names = {p.stem for p in PRESET_DIR.glob("*.yaml")}
    assert {"sale_price", "lease_rent", "guarantee", "unjust_enrichment",
            "property_service_fee"} <= names


def test_issue_element_reference_carries_method_and_attribution():
    ref = skill_path("litigation-analysis").parent / "references" / "issue_element_method.md"
    text = ref.read_text(encoding="utf-8")
    for marker in ("产生", "未消灭", "可行使", "否认", "真伪不明", "Three search rounds",
                   "类案检索", "last three years", "八个一致", "类案 matrix"):
        assert marker in text, marker
    for licence in ("CC BY 4.0", "MIT", "Apache-2.0"):
        assert licence in text, licence
