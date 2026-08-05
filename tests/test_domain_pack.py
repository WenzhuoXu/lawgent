"""Domain-pack loader, overlay resolution, pack-aware tool filtering."""

from __future__ import annotations

from pathlib import Path

import pytest

from legal_helper.domains import available_packs, load_pack


def test_aviation_pack_is_available() -> None:
    assert "aviation" in available_packs()


def test_aviation_pack_loads() -> None:
    pack = load_pack("aviation")
    assert pack.name == "aviation"
    assert pack.display_name
    assert "CN" in pack.jurisdictions
    assert pack.playbook_path.is_file()
    assert pack.overlay_dir.is_dir()
    # references_dir is optional (created on demand by ingestion / authoring)


def test_aviation_overlay_for_every_skill() -> None:
    pack = load_pack("aviation")
    for skill in (
        "review-contract",
        "signature-request",
        "compliance-check",
        "meeting-briefing",
        "triage-nda",
        "legal-response",
        "legal-risk-assessment",
        "vendor-check",
        "brief",
    ):
        overlay = pack.overlay_for(skill)
        assert overlay is not None and overlay.is_file(), (
            f"aviation pack missing overlay for {skill}"
        )


def test_unknown_pack_raises() -> None:
    with pytest.raises(FileNotFoundError):
        load_pack("does-not-exist")


def test_connectors_filter_by_pack() -> None:
    from legal_helper.connectors import filter_for

    cn_only = {e.name for e in filter_for(["CN"], [])}
    multi_aviation = {e.name for e in filter_for(["CN", "US", "EU", "ICAO"], ["aviation"])}
    multi_generic = {e.name for e in filter_for(["CN", "US", "EU", "ICAO"], [])}

    assert "flk_npc_search" in cn_only
    # Aviation pack flips on the aviation-tagged connectors that match the
    # active jurisdiction set. Live ``ccar_search`` / ``ccar_fetch`` remain
    # disabled, but the local CAAC corpus connector is available.
    assert "caac_local_search" in multi_aviation
    assert "caac_local_fetch" in multi_aviation
    assert "faa_title14_search" in multi_aviation
    assert "drs_search" in multi_aviation
    assert "easa_ad_search" in multi_aviation
    # Without the pack, aviation-tagged connectors must not leak in.
    assert "faa_title14_search" not in multi_generic
    assert "caac_local_search" not in multi_generic
    assert "drs_search" not in multi_generic
    assert "easa_ad_search" not in multi_generic


def test_tool_registry_responds_to_active_packs() -> None:
    from legal_helper.tools import skill_tools_for_task

    generic = skill_tools_for_task(
        "review-contract",
        "attachments to read; deliverable docx",
        jurisdictions=["CN", "US"],
        active_packs=[],
    )
    aviation = skill_tools_for_task(
        "review-contract",
        "attachments to read; deliverable docx",
        jurisdictions=["CN", "US"],
        active_packs=["aviation"],
    )
    assert len(aviation) > len(generic), "aviation pack should add tools to the surface"
