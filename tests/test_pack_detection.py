"""Domain-pack auto-detection: detect_packs + merge_active_packs."""

from __future__ import annotations

import pytest

from legal_helper.domains import detect_packs, load_pack, merge_active_packs
from legal_helper.domains.detect import clear_cache


@pytest.fixture(autouse=True)
def _reset_cache():
    clear_cache()
    yield
    clear_cache()


def test_aviation_pack_has_markers_in_yaml() -> None:
    pack = load_pack("aviation")
    assert pack.markers_english, "aviation pack.yaml must declare english markers"
    assert pack.markers_chinese, "aviation pack.yaml must declare chinese markers"


def test_english_aviation_prompts_detect() -> None:
    for prompt in (
        "Review this dry lease against the playbook.",
        "Need a Cape Town / IDERA compliance memo for a 737 transfer.",
        "Switching MRO from EASA Part-145 to a domestic shop.",
        "14 CFR Part 121 ETOPS approval question.",
    ):
        assert detect_packs(prompt) == ["aviation"], f"missed aviation in: {prompt!r}"


def test_chinese_aviation_prompts_detect() -> None:
    for prompt in (
        "我们要把一架B737-800从VP-C转到N籍，怎么办理适航出口？",
        "包机合同里的责任条款是否合规？",
        "民航局适航指令的合规流程",
    ):
        assert detect_packs(prompt) == ["aviation"], f"missed aviation in: {prompt!r}"


def test_non_aviation_prompts_do_not_detect() -> None:
    for prompt in (
        "Draft an employment contract for a Beijing senior engineer.",
        "帮我看一下这份NDA。",
        "Run a cross-border data transfer compliance check under PIPL.",
        "请审查这份普通商业合同。",
    ):
        assert detect_packs(prompt) == [], f"false positive on: {prompt!r}"


def test_partial_word_does_not_false_positive() -> None:
    # "leased" should NOT trigger on "lease" because "lease" is not in the
    # aviation marker list (we removed generic-leasing markers; aviation
    # uses "dry lease" / "wet lease" + 干租/湿租). "Aircraftxx" should not
    # match aircraft.
    for prompt in (
        "Please review the residential leased premises addendum.",
        "Some unrelated text about supercraftsmanship.",
    ):
        assert "aviation" not in detect_packs(prompt), f"false positive on: {prompt!r}"


def test_merge_active_packs_preserves_explicit() -> None:
    merged, new = merge_active_packs(["aviation"], "plain commercial NDA")
    assert merged == ["aviation"]
    assert new == []


def test_merge_active_packs_adds_detected() -> None:
    merged, new = merge_active_packs([], "Review this dry lease and IDERA.")
    assert merged == ["aviation"]
    assert new == ["aviation"]


def test_merge_active_packs_dedupes() -> None:
    merged, new = merge_active_packs(["aviation"], "Dry lease + IDERA.")
    assert merged == ["aviation"]
    assert new == []


def test_workflow_classifier_uses_pack_detection() -> None:
    """``_looks_like_legal_request`` should fire on prompts that only trigger
    a pack marker (e.g. pure aviation phrasing with no generic legal verb)."""
    from legal_helper.workflow import _looks_like_legal_request

    # No generic legal marker — only the aviation-specific term should fire.
    assert _looks_like_legal_request("Switching MRO providers next month") is True
    # Pure non-legal everyday prose should not.
    assert _looks_like_legal_request("Tell me a joke about cats") is False
