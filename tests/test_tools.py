"""Schema and behavior tests for @beta_tool helpers."""

from __future__ import annotations

import json

import pytest

from legal_helper.tools import (
    diff_xlsx,
    edit_pptx_text,
    edit_xlsx_cells,
    edit_xlsx_cells_checked,
    extract_clauses,
    inspect_pptx,
    inspect_xlsx,
    inspect_xlsx_range,
    legal_source_search,
    list_skill_sections,
    orchestrator_tools,
    read_document,
    read_playbook_section,
    read_skill_section,
    render_pptx_slides,
    run_skill,
    skill_tools,
    skill_tools_for_task,
    write_docx,
    write_pdf,
    write_pptx,
    write_xlsx,
)


def test_tool_input_schemas_have_required_fields():
    schema = write_docx.input_schema
    assert schema["type"] == "object"
    props = schema["properties"]
    assert "filename" in props
    assert "title" in props
    assert "sections" in props
    assert set(schema["required"]) >= {"filename", "title", "sections"}

    schema = write_pdf.input_schema
    assert set(schema["required"]) >= {"filename", "title", "body_markdown"}

    schema = write_xlsx.input_schema
    assert set(schema["required"]) >= {"filename", "sheets"}

    schema = write_pptx.input_schema
    assert set(schema["required"]) >= {"filename", "title", "slides"}

    schema = inspect_xlsx.input_schema
    assert set(schema["required"]) >= {"path"}

    schema = inspect_xlsx_range.input_schema
    assert set(schema["required"]) >= {"path", "sheet", "cell_range"}

    schema = edit_xlsx_cells.input_schema
    assert set(schema["required"]) >= {"source_path", "filename", "edits"}

    schema = edit_xlsx_cells_checked.input_schema
    assert set(schema["required"]) >= {"source_path", "filename", "edits"}

    schema = diff_xlsx.input_schema
    assert set(schema["required"]) >= {"source_path", "target_path"}

    schema = inspect_pptx.input_schema
    assert set(schema["required"]) >= {"path"}

    schema = edit_pptx_text.input_schema
    assert set(schema["required"]) >= {"source_path", "filename", "replacements"}

    schema = render_pptx_slides.input_schema
    assert set(schema["required"]) >= {"path"}

    schema = read_document.input_schema
    assert set(schema["required"]) >= {"path"}

    schema = run_skill.input_schema
    assert set(schema["required"]) >= {"skill_name", "task"}
    # The skill_name enum should be present
    skill_prop = schema["properties"]["skill_name"]
    enum = skill_prop.get("enum") or skill_prop.get("anyOf", [{}])[0].get("enum") or []
    if enum:
        assert "review-contract" in enum

    schema = legal_source_search.input_schema
    assert set(schema["required"]) >= {"source", "query"}


def test_aviation_dispatcher_exposes_caac_sources():
    from legal_helper.tools import aviation_source_search

    schema = aviation_source_search.input_schema
    source_prop = schema["properties"]["source"]
    enum = source_prop.get("enum") or source_prop.get("anyOf", [{}])[0].get("enum") or []
    assert "caac_local" in enum
    assert "caac_local_fetch" in enum
    assert "caac_hybrid" in enum


def test_extract_clauses_finds_aviation_categories():
    """Aviation tags surface only when the aviation pack is active.

    Generic commercial categories (insurance, governing_law, etc.) live in
    ``tools/contract.py``; aviation-specific tags (IDERA / Cape Town /
    AD-SB / hull-liability) are pulled from ``domains/aviation/contract_tags.py``
    once ``active_domain_packs`` contains ``aviation``.
    """
    from legal_helper.config import current_settings, override_current_settings

    contract = """
ARTICLE 4 — INSURANCE

Lessee shall maintain hull all-risks insurance and combined single-limit aviation liability.
Lessor shall be named as additional insured with AVN52E endorsement.

ARTICLE 8 — IDERA AND CAPE TOWN

The parties shall execute an Irrevocable Deregistration and Export Request Authorization in
the form prescribed by the Cape Town Convention.

ARTICLE 5 — AIRWORTHINESS DIRECTIVES

Lessee bears the cost of all Airworthiness Directives and Mandatory Service Bulletins.
"""
    aviation_settings = current_settings().model_copy(
        update={"active_domain_packs": ["aviation"]}
    )
    with override_current_settings(aviation_settings):
        raw = extract_clauses.call({"text": contract})
    assert isinstance(raw, str)
    parsed = json.loads(raw)
    headings = " ".join(c["heading"] for c in parsed)
    assert "INSURANCE" in headings
    assert "IDERA" in headings
    assert "AIRWORTHINESS" in headings
    all_cats = {cat for c in parsed for cat in c["categories"]}
    assert "insurance_hull_liability" in all_cats
    assert "idera_cape_town" in all_cats
    assert "ad_sb_compliance" in all_cats


def test_extract_clauses_no_aviation_tags_in_generic_mode():
    """Without the aviation pack, aviation-specific tags must not leak."""
    contract = """
ARTICLE 4 — INSURANCE

Lessee shall maintain hull all-risks insurance with AVN52E endorsement.

ARTICLE 8 — IDERA AND CAPE TOWN

Cape Town Convention.

ARTICLE 6 — GOVERNING LAW

This Agreement is governed by the laws of New York.
"""
    raw = extract_clauses.call({"text": contract})
    parsed = json.loads(raw)
    all_cats = {cat for c in parsed for cat in c["categories"]}
    assert "insurance_hull_liability" not in all_cats
    assert "idera_cape_town" not in all_cats
    assert "ad_sb_compliance" not in all_cats
    # Generic categories still surface.
    assert "insurance" in all_cats
    assert "governing_law" in all_cats


def test_read_document_reads_markdown(tmp_path):
    p = tmp_path / "memo.md"
    p.write_text("# Hello\n\nThis lease references IDERA and Cape Town.")
    text = read_document.call({"path": str(p)})
    assert "IDERA" in text
    assert "Hello" in text


def test_read_document_includes_docx_comments(tmp_path):
    import zipfile
    from xml.etree import ElementTree as ET

    from docx import Document

    w_ns = "http://schemas.openxmlformats.org/wordprocessingml/2006/main"
    docx_path = tmp_path / "commented.docx"
    doc = Document()
    doc.add_paragraph("本人理解并愿意承担在提供医疗救助过程中可能产生的风险和责任。")
    doc.save(docx_path)

    with zipfile.ZipFile(docx_path) as zin:
        files = {name: zin.read(name) for name in zin.namelist()}

    root = ET.fromstring(files["word/document.xml"])
    para = next(root.iter(f"{{{w_ns}}}p"))
    first_run = next(para.iter(f"{{{w_ns}}}r"))
    start = ET.Element(f"{{{w_ns}}}commentRangeStart", {f"{{{w_ns}}}id": "0"})
    end = ET.Element(f"{{{w_ns}}}commentRangeEnd", {f"{{{w_ns}}}id": "0"})
    ref_run = ET.Element(f"{{{w_ns}}}r")
    ref = ET.SubElement(ref_run, f"{{{w_ns}}}commentReference", {f"{{{w_ns}}}id": "0"})
    assert ref is not None
    children = list(para)
    idx = children.index(first_run)
    para.insert(idx, start)
    para.insert(idx + 2, end)
    para.insert(idx + 3, ref_run)
    files["word/document.xml"] = ET.tostring(root, encoding="utf-8", xml_declaration=True)
    files["word/comments.xml"] = (
        '<?xml version="1.0" encoding="UTF-8" standalone="yes"?>'
        '<w:comments xmlns:w="http://schemas.openxmlformats.org/wordprocessingml/2006/main">'
        '<w:comment w:id="0" w:author="Reviewer">'
        "<w:p><w:r><w:t>此条建议重新修订，是否效仿东航和南航由航司兜底？</w:t></w:r></w:p>"
        "</w:comment></w:comments>"
    ).encode("utf-8")

    with zipfile.ZipFile(docx_path, "w", compression=zipfile.ZIP_DEFLATED) as zout:
        for name, data in files.items():
            zout.writestr(name, data)

    text = read_document.call({"path": str(docx_path)})
    assert "## Word comments / annotations" in text
    assert "此条建议重新修订" in text
    assert "Anchor/context" in text


def test_write_docx_returns_path(tmp_path, monkeypatch):
    monkeypatch.setenv("LEGAL_HELPER_OUTPUTS_DIR", str(tmp_path / "out"))
    from legal_helper import config as cfg

    cfg._CACHED = None
    path = write_docx.call(
        {
            "filename": "test_redline.docx",
            "title": "Test Redline",
            "sections": [
                {"heading": "Summary", "body_markdown": "All good."},
                {"heading": "Findings", "body_markdown": "- one\n- two"},
            ],
        }
    )
    assert path.endswith(".docx")
    from pathlib import Path

    assert Path(path).is_file()


def test_write_xlsx_and_read_document_round_trip(tmp_path, monkeypatch):
    monkeypatch.setenv("LEGAL_HELPER_OUTPUTS_DIR", str(tmp_path / "out"))
    from legal_helper import config as cfg

    cfg._CACHED = None
    path = write_xlsx.call(
        {
            "filename": "pending_suggestions.xlsx",
            "sheets": [
                {
                    "name": "待落实建议",
                    "rows": [
                        ["模块", "待落实建议"],
                        ["审批审核", "建议完善志愿者准入审核流程"],
                    ],
                }
            ],
        }
    )
    assert path.endswith(".xlsx")
    text = read_document.call({"path": path})
    assert "待落实建议" in text
    assert "审批审核" in text
    assert "建议完善志愿者准入审核流程" in text


def test_inspect_and_edit_xlsx_cells(tmp_path, monkeypatch):
    monkeypatch.setenv("LEGAL_HELPER_OUTPUTS_DIR", str(tmp_path / "out"))
    from legal_helper import config as cfg

    cfg._CACHED = None
    source = write_xlsx.call(
        {
            "filename": "source.xlsx",
            "sheets": [
                {
                    "name": "待落实建议",
                    "rows": [["序号", "待落实建议"], [1, ""], [2, ""]],
                }
            ],
        }
    )
    inspected = inspect_xlsx.call({"path": source, "preview_rows": 3})
    assert "待落实建议" in inspected
    assert "row_count" in inspected

    edited = edit_xlsx_cells.call(
        {
            "source_path": source,
            "filename": "edited.xlsx",
            "edits": [
                {
                    "sheet": "待落实建议",
                    "cell": "B2",
                    "value": "建议重新修订医生志愿者风险责任表述",
                }
            ],
        }
    )
    text = read_document.call({"path": edited})
    assert "建议重新修订医生志愿者风险责任表述" in text
    assert "待落实建议" in text


def test_xlsx_range_diff_and_checked_edit(tmp_path, monkeypatch):
    monkeypatch.setenv("LEGAL_HELPER_OUTPUTS_DIR", str(tmp_path / "out"))
    from legal_helper import config as cfg

    cfg._CACHED = None
    source = write_xlsx.call(
        {
            "filename": "range_source.xlsx",
            "sheets": [
                {
                    "name": "A类",
                    "rows": [
                        ["一级流程", "二级流程", "文件编号及名称"],
                        ["旅客服务", "", "PSMM1.1_服务战略管理办法"],
                        ["", "服务战略管理", "PSMM2.1_服务文化管理办法"],
                    ],
                }
            ],
        }
    )

    raw_range = inspect_xlsx_range.call({"path": source, "sheet": "A类", "cell_range": "A1:C3"})
    payload = json.loads(raw_range)
    assert payload["rows"][1][2]["cell"] == "C2"
    assert payload["rows"][1][2]["value"] == "PSMM1.1_服务战略管理办法"

    edited_raw = edit_xlsx_cells_checked.call(
        {
            "source_path": source,
            "filename": "range_edited.xlsx",
            "edits": [
                {
                    "sheet": "A类",
                    "cell": "B2",
                    "value": "基础服务",
                    "check_expected": True,
                    "expected_value": None,
                }
            ],
            "protected_ranges": [{"sheet": "A类", "range": "C1:C3"}],
        }
    )
    edited_payload = json.loads(edited_raw)
    edited = edited_payload["path"]
    assert edited_payload["applied_count"] == 1

    raw_diff = diff_xlsx.call(
        {
            "source_path": source,
            "target_path": edited,
            "ranges": [{"sheet": "A类", "range": "A1:C3"}],
        }
    )
    diff_payload = json.loads(raw_diff)
    assert diff_payload["diff_count"] == 1
    assert diff_payload["diffs"][0]["cell"] == "B2"
    assert diff_payload["diffs"][0]["target_value"] == "基础服务"

    protected_diff = diff_xlsx.call(
        {
            "source_path": source,
            "target_path": edited,
            "ranges": [{"sheet": "A类", "range": "C1:C3"}],
        }
    )
    assert json.loads(protected_diff)["diff_count"] == 0

    stale = edit_xlsx_cells_checked.call(
        {
            "source_path": source,
            "filename": "stale.xlsx",
            "edits": [
                {
                    "sheet": "A类",
                    "cell": "B3",
                    "value": "错误写入",
                    "check_expected": True,
                    "expected_value": "not the current value",
                }
            ],
        }
    )
    assert stale.startswith("ERROR editing")
    assert "Precondition failed" in stale


def test_write_inspect_and_edit_pptx(tmp_path, monkeypatch):
    from legal_helper.documents.writers.pptx import pptxgenjs_available

    if not pptxgenjs_available():
        pytest.skip("pptxgenjs renderer unavailable; run `npm i` in the repo root")
    monkeypatch.setenv("LEGAL_HELPER_OUTPUTS_DIR", str(tmp_path / "out"))
    from legal_helper import config as cfg

    cfg._CACHED = None
    source = write_pptx.call(
        {
            "filename": "deck.pptx",
            "title": "Legal Briefing",
            "slides": [
                {
                    "title": "Issue",
                    "bullets": ["Facts", "Law", "Recommendation"],
                    "speaker_notes": "Discuss exposure and next steps.",
                }
            ],
        }
    )
    inspected = inspect_pptx.call({"path": source, "include_text_runs": True})
    assert "Legal Briefing" not in inspected
    assert "Issue" in inspected
    assert "Discuss exposure" in inspected

    edited = edit_pptx_text.call(
        {
            "source_path": source,
            "filename": "edited_deck.pptx",
            "replacements": [{"slide": 1, "find": "Recommendation", "replace": "Action Plan"}],
        }
    )
    text = read_document.call({"path": edited})
    assert "Action Plan" in text
    assert "Recommendation" not in text


def test_render_pptx_slides(tmp_path, monkeypatch):
    import shutil

    from legal_helper.documents.writers.pptx import pptxgenjs_available

    if not shutil.which("soffice") or not shutil.which("pdftoppm"):
        pytest.skip("soffice and pdftoppm are required for slide rendering")
    if not pptxgenjs_available():
        pytest.skip("pptxgenjs renderer unavailable; run `npm i` in the repo root")

    monkeypatch.setenv("LEGAL_HELPER_OUTPUTS_DIR", str(tmp_path / "out"))
    from legal_helper import config as cfg

    cfg._CACHED = None
    source = write_pptx.call(
        {
            "filename": "render_me.pptx",
            "title": "Render Me",
            "slides": [{"title": "Render", "bullets": ["Check"], "speaker_notes": ""}],
        }
    )
    raw = render_pptx_slides.call({"path": source, "filename_prefix": "rendered", "dpi": 72})
    payload = json.loads(raw)
    assert payload["count"] >= 1
    assert payload["images"][0].endswith(".jpg")


def test_orchestrator_and_skill_tool_lists():
    orch = orchestrator_tools()
    names = {getattr(t, "name", None) for t in orch}
    assert {
        "run_skill",
        "write_docx",
        "write_pdf",
        "write_xlsx",
        "write_pptx",
        "inspect_xlsx",
        "inspect_xlsx_range",
        "edit_xlsx_cells",
        "edit_xlsx_cells_checked",
        "diff_xlsx",
        "inspect_pptx",
        "edit_pptx_text",
        "render_pptx_slides",
        "read_document",
    } <= names

    rc_tools = skill_tools("review-contract")
    rc_names = {getattr(t, "name", None) for t in rc_tools}
    assert {"list_skill_sections", "read_skill_section", "read_playbook_section"} <= rc_names
    assert "legal_source_search" in rc_names
    assert {
        "read_document",
        "write_docx",
        "write_pdf",
        "write_xlsx",
        "write_pptx",
        "inspect_xlsx",
        "inspect_xlsx_range",
        "edit_xlsx_cells",
        "edit_xlsx_cells_checked",
        "diff_xlsx",
        "inspect_pptx",
        "edit_pptx_text",
        "render_pptx_slides",
        "fetch_url_to_artifact",
    } <= rc_names
    assert "extract_clauses" in rc_names

    doc_tools = skill_tools_for_task(
        "review-contract",
        "Read attachments to read with `read_document` and produce a docx redline deliverable.",
    )
    doc_names = {getattr(t, "name", None) for t in doc_tools}
    assert {
        "read_document",
        "extract_clauses",
        "write_docx",
        "write_pdf",
        "write_xlsx",
        "write_pptx",
        "inspect_xlsx",
        "inspect_xlsx_range",
        "edit_xlsx_cells",
        "edit_xlsx_cells_checked",
        "diff_xlsx",
        "inspect_pptx",
        "edit_pptx_text",
        "render_pptx_slides",
        "fetch_url_to_artifact",
    } <= doc_names

    rb_tools = skill_tools_for_task("brief", "Research treaty source support with citations.")
    rb_names = {getattr(t, "name", None) for t in rb_tools}
    assert "legal_source_search" in rb_names
    assert {
        "read_document",
        "write_docx",
        "write_pdf",
        "write_xlsx",
        "write_pptx",
        "inspect_xlsx",
        "inspect_xlsx_range",
        "edit_xlsx_cells",
        "edit_xlsx_cells_checked",
        "diff_xlsx",
        "inspect_pptx",
        "edit_pptx_text",
        "render_pptx_slides",
        "fetch_url_to_artifact",
    } <= rb_names
    assert "extract_clauses" not in rb_names


def test_pkulaw_surfaces_as_primary_prc_source(monkeypatch):
    """PKULaw MCP must surface in every skill that lists ``pkulaw_*`` in its
    allow-list — even without a live ``tools/list`` probe (no token, no
    network). Per user contract: PKULaw is primary, flk_npc is fallback.
    """
    # Force the live probe to fail so we exercise the static manifest path.
    from legal_helper.tools import mcp_tools as mt

    mt._cached_tools_list.cache_clear()
    monkeypatch.setattr(mt, "http_list_tools", lambda spec: [])

    tools = skill_tools_for_task(
        "cite-check",
        "Validate citations in attached memo.",
        jurisdictions=["CN"],
        active_packs=[],
    )
    names = {getattr(t, "name", None) for t in tools}
    # PKULaw must appear ahead of (or alongside) flk_npc — primary source.
    assert "flk_npc_search" in names
    pkulaw_tools = {n for n in names if isinstance(n, str) and n.startswith("pkulaw_")}
    # Every documented PKULaw sub-service must be reachable.
    expected = {
        "pkulaw_law_search__search_article",
        "pkulaw_law_search__get_article",
        "pkulaw_fatiao__get_law_item_content",
        "pkulaw_case_search__search_case",
        "pkulaw_case_list__get_case_list",
        "pkulaw_anhao__anhao_recognition",
        "pkulaw_law_recognition__law_recognition",
        "pkulaw_citation_validator__adjust_provisions",
        "pkulaw_doc_link__get_linked_content",
        "pkulaw_nl_search__ai_pkulaw_search",
    }
    missing = expected - pkulaw_tools
    assert not missing, f"PKULaw tools missing from cite-check surface: {missing}"


def test_run_skill_enum_includes_cite_check():
    """Regression for the orchestrator dispatch surface — cite-check must be
    a routable skill_name, not just a SKILL.md file on disk."""
    schema = run_skill.input_schema
    skill_prop = schema["properties"]["skill_name"]
    enum = skill_prop.get("enum") or skill_prop.get("anyOf", [{}])[0].get("enum") or []
    assert "cite-check" in enum, f"cite-check missing from run_skill enum: {enum}"


def test_allowed_connectors_frontmatter_filters_connector_surface():
    """`allowed_connectors` in SKILL.md frontmatter must narrow the connector
    function-tool list. signature-request declares
    `[ecfr_search, eurlex_search, flk_npc_search]`, so even with all
    jurisdictions on it should not see CourtListener or Federal Register.
    """
    tools = skill_tools_for_task(
        "signature-request",
        "Prepare a multi-party closing checklist.",
        jurisdictions=["CN", "US", "EU"],
        active_packs=[],
    )
    names = {getattr(t, "name", None) for t in tools}
    # In allow-list
    assert "ecfr_search" in names
    assert "eurlex_search" in names
    assert "flk_npc_search" in names
    # Not in allow-list — must be filtered out even though jurisdictions
    # would otherwise make them visible.
    assert "courtlistener_search" not in names
    assert "federal_register_search" not in names


def test_skill_resource_tools_read_current_agent_sections():
    from legal_helper.logging_setup import agent_name_var

    token = agent_name_var.set("vendor-check")
    try:
        raw = list_skill_sections.call({"include_playbook": True})
        payload = json.loads(raw)
        assert payload["skill_name"] == "vendor-check"
        assert "skill_sections" in payload
        assert "playbook_sections" in payload

        section = read_skill_section.call({"heading": "Workflow"})
        # generic vendor-check skill has a Workflow section; it no longer
        # mentions "aviation" — the aviation overlay does.
        assert section.lower().startswith("## workflow")

        playbook = read_playbook_section.call({"heading": "Citation Standard"})
        assert "citation" in playbook.lower()
    finally:
        agent_name_var.reset(token)
