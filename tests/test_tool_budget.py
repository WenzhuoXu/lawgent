"""Tool results must not enter the context at whatever size the source was.

The 2026-09 ledger: 40 of 667 requests crossed OpenAI's 272K long-context
threshold at a mean input of 646K tokens and carried ~55% of the month's spend.
The `[:1200]` slices in both providers were log previews, not budgets. These
tests pin what replaced them: a per-tool result budget and the exemptions that
keep deliverables whole. There is deliberately no per-turn cumulative ceiling —
it stopped work mid-task; `test_turn_compaction.py` covers what replaced it.
"""

from __future__ import annotations

from legal_helper.context import estimate_tokens
from legal_helper.tool_budget import (
    DEFAULT_BUDGET,
    apply_result_budget,
    budget_for,
    budget_log_fields,
)

# CJK on purpose: a Chinese statute body is ~1 token per character, so it hits
# a token budget four times sooner than the same byte count of English.
_STATUTE = "第一条 合同当事人应当遵循诚实信用原则。" * 2_000
_OPINION = "The court held that the carrier's liability is limited. " * 4_000


def test_a_statute_body_is_cut_and_spilled_to_a_file(tmp_path):
    result = apply_result_budget("pkulaw_fatiao__get_law_item_content", _STATUTE)
    assert result.applied
    assert result.original_tokens > 30_000
    assert result.kept_tokens <= 13_000
    assert result.spill_path is not None and result.spill_path.is_file()
    # The full text stays reachable, and the model is told how to reach it.
    assert result.spill_path.read_text(encoding="utf-8") == _STATUTE
    assert "read_document" in result.text
    assert str(result.spill_path) in result.text


def test_a_search_result_list_keeps_its_head():
    """The head of a ranked list is the useful part, so the tail is what goes."""
    result = apply_result_budget("legal_source_search", _OPINION)
    assert result.applied
    assert result.text.startswith("The court held")


def test_an_unlisted_tool_gets_the_default_budget():
    assert budget_for("some_new_tool") == DEFAULT_BUDGET
    small = "short result"
    unchanged = apply_result_budget("some_new_tool", small)
    assert not unchanged.applied
    assert unchanged.text == small


def test_deliverables_are_never_budgeted():
    """Truncating these would shorten the memo, not the evidence behind it."""
    for tool in ("run_skill", "cite_check_report_tool", "ground_answer_tool"):
        assert budget_for(tool) is None
        result = apply_result_budget(tool, _STATUTE)
        assert not result.applied
        assert result.text == _STATUTE


def test_a_result_inside_its_budget_is_returned_untouched():
    body = "第一条 " * 100
    result = apply_result_budget("read_document", body)
    assert not result.applied
    assert result.text == body
    assert result.spill_path is None


def test_many_results_are_never_replaced_by_a_stop_notice():
    """No cumulative cap: the 60th result is budgeted like the first."""
    for _ in range(60):
        result = apply_result_budget("legal_source_search", _STATUTE)
        assert result.applied
        assert "exhausted" not in result.text
        assert result.text.startswith(_STATUTE[:20])


def test_budget_log_fields_are_empty_when_nothing_was_cut():
    untouched = apply_result_budget("run_skill", "short")
    assert budget_log_fields(untouched) == {}
    assert budget_log_fields(None) == {}
    cut = apply_result_budget("legal_source_search", _STATUTE)
    fields = budget_log_fields(cut)
    assert fields["result_budget_applied"] is True
    assert fields["result_tokens_before"] > fields["result_tokens_after"]
    assert "result_spill_path" in fields


def test_non_string_and_empty_results_are_safe():
    assert apply_result_budget("legal_source_search", "").text == ""
    assert apply_result_budget("legal_source_search", None).text is None  # type: ignore[arg-type]


def test_estimate_is_the_meter_not_bytes():
    """Same byte count, different token cost — the budget must see tokens."""
    cjk = "第" * 20_000
    latin = "a" * 20_000
    assert estimate_tokens(cjk) > 3 * estimate_tokens(latin)
