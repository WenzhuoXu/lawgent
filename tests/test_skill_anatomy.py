"""Each SKILL.md is small (anatomy contract) and has valid frontmatter."""

from __future__ import annotations

import re
from pathlib import Path

import pytest
import yaml

from legal_helper.skills import SKILL_NAMES, skill_path


_FRONTMATTER_RE = re.compile(r"^---\s*\n(.*?)\n---\s*\n(.*)$", re.DOTALL)


@pytest.mark.parametrize("name", SKILL_NAMES)
def test_skill_file_exists(name: str) -> None:
    assert skill_path(name).is_file(), f"SKILL.md missing for {name}"


@pytest.mark.parametrize("name", SKILL_NAMES)
def test_skill_under_100_lines(name: str) -> None:
    text = skill_path(name).read_text(encoding="utf-8")
    line_count = text.count("\n") + 1
    assert line_count <= 100, f"{name}/SKILL.md is {line_count} lines (target: <=100)"


@pytest.mark.parametrize("name", SKILL_NAMES)
def test_skill_frontmatter_valid(name: str) -> None:
    text = skill_path(name).read_text(encoding="utf-8")
    m = _FRONTMATTER_RE.match(text)
    assert m is not None, f"{name}/SKILL.md is missing YAML frontmatter"
    fm = yaml.safe_load(m.group(1)) or {}
    assert fm.get("name") == name
    assert fm.get("description")


@pytest.mark.parametrize("name", SKILL_NAMES)
def test_skill_has_output_contract(name: str) -> None:
    text = skill_path(name).read_text(encoding="utf-8")
    assert "Output Contract (binding)" in text, f"{name} missing Output Contract section"


_FORBIDDEN_AVIATION_TOKENS = (
    "IDERA",
    "Cape Town",
    "Part-145",
    "AD/SB",
    "ITAR",
    "FOQA",
    "ASAP MOU",
)


@pytest.mark.parametrize("name", SKILL_NAMES)
def test_skill_no_aviation_specific_strings(name: str) -> None:
    text = skill_path(name).read_text(encoding="utf-8")
    # The body may reference the aviation overlay file by path; that's fine.
    body = text.replace("domains/aviation/overlays", "")
    leaks = [t for t in _FORBIDDEN_AVIATION_TOKENS if t in body]
    assert not leaks, f"{name}/SKILL.md still contains aviation-specific tokens: {leaks}"


def test_tabular_review_support_files_exist() -> None:
    root = skill_path("tabular-review").parent
    assert (root / "references" / "methodology.md").is_file()
    templates_dir = root / "resources" / "grid_templates"
    assert (templates_dir / "README.md").is_file()
    templates = sorted(templates_dir.glob("*.yaml"))
    assert templates, "tabular-review ships no grid template presets"
    for template in templates:
        preset = yaml.safe_load(template.read_text(encoding="utf-8"))
        assert preset.get("name") == template.stem
        columns = preset.get("columns") or []
        assert columns, f"{template.name} has no columns"
        for col in columns:
            assert col.get("id") and col.get("question"), f"{template.name}: bad column {col}"
            assert col.get("type") in {"extract", "classify", "flag"}, (
                f"{template.name}: column {col.get('id')} has invalid type"
            )
            if col.get("type") == "classify":
                assert col.get("enum"), f"{template.name}: classify column {col['id']} needs enum"


def test_tabular_review_per_cell_citation_contract() -> None:
    text = skill_path("tabular-review").read_text(encoding="utf-8")
    assert "Per-cell citation contract (binding)" in text
    # The grid pairs every substantive column with a basis/依据 pinpoint cell.
    assert "依据" in text and "pinpoint" in text.lower()
    assert "NOT FOUND" in text and "AMBIGUOUS" in text


def test_draft_agreement_support_files_exist() -> None:
    root = skill_path("draft-agreement").parent
    assert (root / "references" / "methodology.md").is_file()
    library_dir = root / "resources" / "clause_library"
    assert (library_dir / "README.md").is_file()
    presets = sorted(library_dir.glob("*.yaml"))
    preset_names = {p.stem for p in presets}
    # The sketch's four core families ship as library presets.
    assert {"nda", "services", "license", "employment"} <= preset_names
    for preset_file in presets:
        preset = yaml.safe_load(preset_file.read_text(encoding="utf-8"))
        assert preset.get("name") == preset_file.stem
        clauses = preset.get("clauses") or []
        assert clauses, f"{preset_file.name} has no clauses"
        for clause in clauses:
            assert clause.get("id"), f"{preset_file.name}: clause missing id"
            assert clause.get("text_zh") or clause.get("text_en"), (
                f"{preset_file.name}: clause {clause['id']} has no text"
            )
            # legal_basis may be empty (market practice) but never absent,
            # and every entry must carry a source + pinpoint.
            assert "legal_basis" in clause, (
                f"{preset_file.name}: clause {clause['id']} missing legal_basis"
            )
            for basis in clause["legal_basis"] or []:
                assert basis.get("source") and basis.get("pinpoint"), (
                    f"{preset_file.name}: clause {clause['id']} bad basis {basis}"
                )


def test_draft_agreement_clause_basis_contract() -> None:
    text = skill_path("draft-agreement").read_text(encoding="utf-8")
    assert "Clause-basis contract (binding)" in text
    # Every operative clause carries a verified 法律依据 pinpoint.
    assert "法律依据" in text and "pinpoint" in text.lower()
    assert "pkulaw" in text.lower()
    # The draft-review round-trip shares the review-contract bucket vocabulary.
    assert "review-contract" in text


def test_litigation_analysis_support_files_exist() -> None:
    root = skill_path("litigation-analysis").parent
    assert (root / "references" / "methodology.md").is_file()
    tables_dir = root / "resources" / "element_tables"
    assert (tables_dir / "README.md").is_file()
    presets = sorted(tables_dir.glob("*.yaml"))
    preset_names = {p.stem for p in presets}
    # The sketch's core PRC causes of action ship as element presets.
    assert {"contract_breach", "tort_liability", "loan_dispute"} <= preset_names
    for preset_file in presets:
        preset = yaml.safe_load(preset_file.read_text(encoding="utf-8"))
        assert preset.get("name") == preset_file.stem
        assert preset.get("cause_of_action"), f"{preset_file.name} missing 案由"
        for basis in preset.get("legal_basis") or []:
            assert basis.get("source") and basis.get("pinpoint"), (
                f"{preset_file.name}: bad legal_basis {basis}"
            )
        elements = preset.get("elements") or []
        assert elements, f"{preset_file.name} has no elements"
        for entry in elements + (preset.get("defenses") or []):
            assert entry.get("id"), f"{preset_file.name}: entry missing id"
            assert entry.get("element_zh") or entry.get("element_en"), (
                f"{preset_file.name}: entry {entry['id']} has no element text"
            )
            assert entry.get("burden") in {"claimant", "respondent"}, (
                f"{preset_file.name}: entry {entry['id']} has invalid burden"
            )
            # authority may be empty (practice-based), never invented; every
            # present entry carries a source + pinpoint.
            for basis in entry.get("authority") or []:
                assert basis.get("source") and basis.get("pinpoint"), (
                    f"{preset_file.name}: entry {entry['id']} bad authority {basis}"
                )


def test_litigation_analysis_authority_contract() -> None:
    text = skill_path("litigation-analysis").read_text(encoding="utf-8")
    assert "Authority-verification contract (binding)" in text
    # The already-wired PKULaw case tools are the mandated verification path.
    for tool in ("search_case", "get_case_list", "anhao_recognition"):
        assert tool in text, f"litigation-analysis does not reference {tool}"
    assert "案号" in text
    assert "courtlistener_search" in text


def test_litigation_analysis_limitation_check() -> None:
    text = skill_path("litigation-analysis").read_text(encoding="utf-8")
    assert "诉讼时效" in text and "第188条" in text
    assert "中止" in text and "中断" in text
    methodology = (
        skill_path("litigation-analysis").parent / "references" / "methodology.md"
    ).read_text(encoding="utf-8")
    # 中止 / 中断 / excluded-claims / 除斥期间 articles all covered.
    for marker in ("第188条", "第193条", "第194条", "第195条", "第196条", "第199条"):
        assert marker in methodology, f"methodology missing 民法典 {marker}"


def test_draft_agreement_prc_filing_skeletons() -> None:
    filings_dir = skill_path("draft-agreement").parent / "resources" / "prc_filings"
    assert (filings_dir / "README.md").is_file()
    qisuzhuang = (filings_dir / "qisuzhuang.md").read_text(encoding="utf-8")
    for required in ("民事起诉状", "诉讼请求", "事实与理由", "证据和证据来源", "民事诉讼法"):
        assert required in qisuzhuang, f"起诉状 skeleton missing {required}"
    dabianzhuang = (filings_dir / "dabianzhuang.md").read_text(encoding="utf-8")
    for required in ("民事答辩状", "答辩意见", "证据和证据来源", "民事诉讼法"):
        assert required in dabianzhuang, f"答辩状 skeleton missing {required}"
