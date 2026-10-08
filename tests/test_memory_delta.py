"""Rolling memory is maintained by delta, not rewrite (ACE context collapse)."""

from __future__ import annotations

from legal_helper.memory import (
    apply_delta,
    parse_delta,
    parse_summary,
    render_summary,
)


def test_parse_and_render_roundtrip():
    text = "## Goals\n- win\n## Facts\n- A leases to B"
    assert render_summary(parse_summary(text)) == text


def test_legacy_freeform_summary_is_preserved_into_facts():
    sections = parse_summary("an old unstructured blob")
    assert sections["Facts"] == ["an old unstructured blob"]


def test_delta_appends_without_touching_existing_detail():
    sections = parse_summary("## Facts\n- A leases to B\n- lease dated 2026-03-01")
    ops = parse_delta("+ Facts | governing law is PRC\nnot a delta line")
    merged = apply_delta(sections, ops)
    # The point of delta updates: nothing pre-existing is lost.
    assert merged["Facts"] == [
        "A leases to B",
        "lease dated 2026-03-01",
        "governing law is PRC",
    ]


def test_delta_removes_only_what_it_names_and_dedupes_adds():
    sections = parse_summary("## Open\n- need CCAR-121 text\n- confirm lessor entity")
    ops = parse_delta(
        "- Open | need CCAR-121\n+ Open | confirm lessor entity\n+ Open | check 民航法 第124条"
    )
    merged = apply_delta(sections, ops)
    assert merged["Open"] == ["confirm lessor entity", "check 民航法 第124条"]


def test_unknown_section_lands_in_facts_rather_than_being_dropped():
    merged = apply_delta(parse_summary(""), parse_delta("+ Nonsense | keep me"))
    assert merged["Facts"] == ["keep me"]


def test_section_cap_evicts_oldest_first():
    from legal_helper.memory import _MAX_BULLETS_PER_SECTION

    ops = parse_delta("\n".join(f"+ Facts | fact {i}" for i in range(_MAX_BULLETS_PER_SECTION + 5)))
    merged = apply_delta(parse_summary(""), ops)
    assert len(merged["Facts"]) == _MAX_BULLETS_PER_SECTION
    assert merged["Facts"][0] == "fact 5"
    assert merged["Facts"][-1] == f"fact {_MAX_BULLETS_PER_SECTION + 4}"


def test_housekeeping_calls_run_at_pinned_low_effort(monkeypatch):
    """Summaries and titles run after the user is released, but they still bill."""
    import legal_helper.memory as memory
    from legal_helper.chat_models import ChatMessage
    from legal_helper.config import load_settings

    seen: list[tuple[str, bool]] = []

    class _Provider:
        def run(self, **kwargs):
            from legal_helper.providers import RunResult

            return RunResult(text="+ Facts | x", provider="openai", model="m")

    def fake_build(settings, fast=False):
        seen.append((settings.openai_reasoning_effort, fast))
        return _Provider()

    monkeypatch.setattr(memory, "build_provider", fake_build)
    settings = load_settings(refresh=True).model_copy(update={"openai_reasoning_effort": "xhigh"})
    msgs = [ChatMessage(id="1", chat_id="c", role="user", content="hello", created_at="", metadata={})]
    memory.summarize_with_fast_model(settings, msgs, "")
    memory.summarize_title_with_fast_model(settings, msgs)
    for provider_name in ("anthropic",):
        memory.summarize_title_with_fast_model(settings.model_copy(update={"provider": provider_name}), msgs)
    assert seen == [("low", True), ("none", True), ("none", True)]
