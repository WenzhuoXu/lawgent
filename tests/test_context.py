from __future__ import annotations

from legal_helper.chat_models import ChatMessage
from legal_helper.config import load_settings
from legal_helper.context import (
    compact_verification_appendix,
    context_budget_for,
    context_window_for,
    estimate_tokens,
    per_message_cap_for,
    render_recent,
    select_recent_within_budget,
    should_compact,
)
from legal_helper.workflow import _build_context_block


def _m(role: str, content: str) -> ChatMessage:
    return ChatMessage(id="x", chat_id="c", role=role, content=content, created_at="t")


def test_estimate_tokens_cjk_vs_ascii():
    assert estimate_tokens("") == 0
    # 100 ASCII chars ≈ 26 tokens; 100 CJK chars ≈ 101 tokens
    assert estimate_tokens("a" * 100) < estimate_tokens("中" * 100)
    assert estimate_tokens("中" * 100) >= 100


def test_select_recent_keeps_newest_drops_oldest():
    # 6 messages of ~3000 tokens each (12000 chars ascii ≈ 3000 tokens); budget 8000
    msgs = [_m("user", "a" * 12000) for _ in range(6)]
    w = select_recent_within_budget(msgs, 8000)
    # ~3000 tokens/message → 2 fit under 8000 (3rd would exceed)
    assert 1 <= len(w.kept) <= 3
    assert len(w.dropped) == len(msgs) - len(w.kept)
    assert w.used_tokens <= 8000
    # kept is the newest, in chronological order; dropped are the oldest
    assert w.kept[-1] is msgs[-1]
    assert w.dropped == msgs[: len(w.dropped)]


def test_huge_message_is_capped_not_dropped():
    msgs = [_m("user", "x" * 400000), _m("user", "small recent")]
    w = select_recent_within_budget(msgs, 16000)
    assert len(w.kept) == 2  # both kept
    assert w.used_tokens <= 16000  # the huge one was capped, not allowed to blow budget


def test_render_recent_truncates_long_message():
    long = "word " * 5000  # ~6250 tokens
    out = render_recent([_m("user", long)], per_message_cap_tokens=1000)
    assert out.startswith("USER: ")
    assert estimate_tokens(out) <= 1100
    assert out.rstrip().endswith("…")


def test_should_compact_threshold():
    s = load_settings(refresh=True)
    window = context_window_for(s)
    small = [_m("user", "hi")]
    assert should_compact(small, s) is False
    # build a transcript ~70% of the window
    big_chars = int(window * 0.7 * 4)  # ascii ≈ 4 chars/token
    big = [_m("user", "a" * big_chars)]
    assert should_compact(big, s, threshold=0.6) is True


def test_build_context_block_budgeted_with_drop_note():
    # many ~3000-token messages so older ones drop out of a small budget
    msgs = [_m("user" if i % 2 == 0 else "assistant", f"turn {i} " + "a" * 12000) for i in range(8)]
    block = _build_context_block("prior summary text", msgs, budget_tokens=8000)
    assert "Current chat memory summary:" in block
    assert "prior summary text" in block
    assert "captured in the memory summary" in block  # drop note present
    assert estimate_tokens(block) <= 9000  # stays within budget (+summary)
    # newest turn is always present
    assert "turn 7" in block


def test_build_context_block_small_chat_no_drop_note():
    msgs = [_m("user", "hello"), _m("assistant", "hi there")]
    block = _build_context_block("", msgs, budget_tokens=16000)
    assert "captured in the memory summary" not in block
    assert "hello" in block and "hi there" in block


def test_context_budget_scales_with_model_window():
    s = load_settings(refresh=True)
    window = context_window_for(s)
    budget = context_budget_for(s)
    # A share of the window, never below the configured floor.
    assert budget >= s.chat_context_token_budget
    assert budget <= window
    # A 1M-window model must not get the same digest as a 200K one.
    big = s.model_copy(update={"chat_context_window_fraction": 0.25})
    assert context_budget_for(big) >= int(0.25 * window)


def test_per_message_cap_derives_from_budget():
    # Small budget keeps the old floor; a large budget lets a full prior answer
    # through instead of truncating it mid-table.
    assert per_message_cap_for(16_000) == 4_000
    assert per_message_cap_for(250_000) > 50_000


def test_compact_verification_appendix_collapses_table():
    answer = (
        "## 结论\n\n正文引用 [中华人民共和国民法典](https://example.com/a) 第 465 条。\n\n"
        "## 资料来源与核验\n\n"
        "| # | 主张/Claim | 依据/Pinpoint | 在线核验 | 备注/Note |\n"
        "|---|---|---|:---:|---|\n"
        "| 1 | 合同依法成立即生效 | [民法典, 第 465 条](https://example.com/b) | ✔ | 无 |\n"
        "| 2 | 格式条款提示说明义务 | [民法典, 第 496 条](https://example.com/c) | ✔ | 无 |\n"
    )
    out = compact_verification_appendix(answer)
    # Claim + pinpoint survive; table syntax, status and note columns do not.
    assert "合同依法成立即生效" in out and "第 465 条" in out
    assert "格式条款提示说明义务" in out and "第 496 条" in out
    assert "|---" not in out
    assert "在线核验" not in out
    assert estimate_tokens(out) < estimate_tokens(answer)
    # The body above the appendix is untouched, inline links included.
    assert "[中华人民共和国民法典](https://example.com/a)" in out


def test_compact_verification_appendix_leaves_body_tables_alone():
    answer = (
        "## 对比\n\n"
        "| 类别 | 处理 |\n|---|---|\n| A | B |\n\n"
        "没有资料来源章节。\n"
    )
    assert compact_verification_appendix(answer) == answer


def test_render_recent_compacts_only_assistant_appendix():
    appendix = (
        "答复正文。\n\n## 资料来源与核验\n\n"
        "| # | 主张/Claim | 依据/Pinpoint |\n|---|---|---|\n| 1 | 主张甲 | 来源甲 |\n"
    )
    out = render_recent([_m("assistant", appendix), _m("user", appendix)], 8000)
    assistant_part, user_part = out.split("USER: ")
    # assistant copy compacted, user copy replayed verbatim
    assert "|---" not in assistant_part and "- 主张甲 — 来源甲" in assistant_part
    assert "|---" in user_part


def test_build_context_block_puts_volatile_summary_after_stable_transcript():
    msgs = [_m("user", "hello"), _m("assistant", "hi there")]
    block = _build_context_block("rolling summary text", msgs, budget_tokens=16000)
    assert block.index("Recent visible chat transcript:") < block.index(
        "Current chat memory summary:"
    )


def test_compaction_threshold_is_model_tier_aware():
    """Strong models ride longer (sub-agent isolation); fast tier folds early."""
    from legal_helper.config import load_settings
    from legal_helper.context import (
        FAST_COMPACTION_THRESHOLD,
        FRONTIER_COMPACTION_THRESHOLD,
        compaction_threshold_for,
        is_fast_tier,
    )

    assert is_fast_tier("claude-haiku-4-5") is True
    assert is_fast_tier("gpt-5.6-luna") is True
    assert is_fast_tier("claude-opus-5") is False
    assert is_fast_tier(None) is False

    base = load_settings(refresh=True)
    frontier = base.model_copy(
        update={"provider": "anthropic", "anthropic_model": "claude-opus-5"}
    )
    fast = base.model_copy(
        update={"provider": "anthropic", "anthropic_model": "claude-haiku-4-5"}
    )
    assert compaction_threshold_for(frontier) == FRONTIER_COMPACTION_THRESHOLD
    assert compaction_threshold_for(fast) == FAST_COMPACTION_THRESHOLD

    # An explicit setting still wins over the tier default.
    pinned = frontier.model_copy(update={"chat_compaction_threshold": 0.42})
    assert compaction_threshold_for(pinned) == 0.42


def test_opus_5_gets_the_full_window():
    from legal_helper.config import load_settings
    from legal_helper.context import context_window_for

    s = load_settings(refresh=True).model_copy(
        update={"provider": "anthropic", "anthropic_model": "claude-opus-5"}
    )
    assert context_window_for(s) == 1_000_000
