from __future__ import annotations

from legal_helper.chat_models import ChatMessage
from legal_helper.config import load_settings
from legal_helper.context import (
    context_window_for,
    estimate_tokens,
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
