"""Anthropic prompt-cache breakpoints, per-turn usage accumulation, and the
OpenAI output ceiling — all against mocked SDK surfaces."""

from __future__ import annotations

from types import SimpleNamespace
from unittest.mock import patch

from legal_helper.config import load_settings
from legal_helper.providers.anthropic_provider import (
    AnthropicProvider,
    _automatic_cache_kwarg,
    _cached_system,
    _tools_with_cache_breakpoint,
)
from legal_helper.providers.openai_provider import OpenAIProvider
from legal_helper.usage import month_summary

ONE_HOUR = {"type": "ephemeral", "ttl": "1h"}


def _usage(inp=0, out=0, cread=0, cwrite=0) -> SimpleNamespace:
    return SimpleNamespace(
        input_tokens=inp,
        output_tokens=out,
        cache_read_input_tokens=cread,
        cache_creation_input_tokens=cwrite,
    )


def _text_block(text: str) -> SimpleNamespace:
    return SimpleNamespace(type="text", text=text)


def _tool_use_block(name: str, block_id: str) -> SimpleNamespace:
    return SimpleNamespace(type="tool_use", name=name, input={"q": "x"}, id=block_id, text=None)


class _FakeMessageRunner:
    """Non-streaming tool runner: yields one message per model turn."""

    def __init__(self, messages):
        self.messages = messages

    def __iter__(self):
        yield from self.messages

    def until_done(self):
        return self.messages[-1]


class _FakeTurnStream:
    def __init__(self, events, final_message):
        self.events = events
        self.final_message = final_message

    def __iter__(self):
        yield from self.events

    def get_final_message(self):
        return self.final_message


class _FakeStreamRunner:
    def __init__(self, streams):
        self.streams = streams

    def __iter__(self):
        yield from self.streams

    def until_done(self):
        return self.streams[-1].final_message


def _provider(tmp_path, runner, captured: dict) -> AnthropicProvider:
    provider = AnthropicProvider.__new__(AnthropicProvider)
    provider.settings = SimpleNamespace(
        max_tokens=1000, max_iterations=3, state_dir=tmp_path, openai_reasoning_effort="medium"
    )
    provider.model = "claude-test"

    def tool_runner(**kwargs):
        captured.update(kwargs)
        return runner

    provider.client = SimpleNamespace(
        beta=SimpleNamespace(messages=SimpleNamespace(tool_runner=tool_runner))
    )
    return provider


# ----- cache_control breakpoints -------------------------------------------


def test_cached_system_wraps_prompt_with_one_hour_breakpoint():
    # The stable prefix outlives the 5-minute default between turns; a 1h
    # entry is read when the user comes back 5–60 minutes later.
    assert _cached_system("You are a PRC legal assistant.") == [
        {
            "type": "text",
            "text": "You are a PRC legal assistant.",
            "cache_control": ONE_HOUR,
        }
    ]
    # Empty prompts pass through untouched (an empty text block would 400).
    assert _cached_system("") == ""


def test_tools_breakpoint_annotates_last_dict_tool_without_mutating_input():
    web = {"type": "web_search_20250305", "name": "web_search"}
    tools = [{"name": "a"}, web]
    out = _tools_with_cache_breakpoint(tools)
    assert out[-1]["cache_control"] == ONE_HOUR
    assert "cache_control" not in web  # original never mutated
    assert out[0] is tools[0]  # non-last tools untouched
    assert _tools_with_cache_breakpoint([]) == []


def test_tools_breakpoint_copies_beta_function_tool():
    from anthropic.lib.tools._beta_functions import BetaFunctionTool

    def lookup(q: str) -> str:
        """Look up a statute."""
        return q

    tool = BetaFunctionTool(lookup)
    out = _tools_with_cache_breakpoint([tool])
    assert out[0] is not tool
    assert out[0].to_dict()["cache_control"] == ONE_HOUR
    assert tool._cache_control is None  # shared tool object never mutated
    assert out[0].call({"q": "民法典"}) == "民法典"  # copy still executes


def _lookup_tool():
    from anthropic.lib.tools import beta_tool

    @beta_tool
    def lookup(q: str) -> str:
        """Look up a statute."""
        return q

    return lookup


def test_run_and_stream_kwargs_carry_cache_breakpoints(tmp_path):
    final = SimpleNamespace(content=[_text_block("ok")], usage=_usage(1, 1), id="m1")
    captured: dict = {}
    provider = _provider(tmp_path, _FakeMessageRunner([final]), captured)
    provider.run("sys prompt", [{"role": "user", "content": "hi"}], tools=[_lookup_tool()], max_iterations=0)
    assert captured["system"][0]["cache_control"] == ONE_HOUR
    assert captured["tools"][-1].to_dict()["cache_control"] == ONE_HOUR
    # The fourth breakpoint follows the conversation, so each round of a tool
    # loop reads the previous round from cache.
    assert captured["cache_control"] == {"type": "ephemeral"}

    captured_stream: dict = {}
    stream_runner = _FakeStreamRunner([_FakeTurnStream([], final)])
    provider2 = _provider(tmp_path, stream_runner, captured_stream)
    list(provider2.stream("sys prompt", [{"role": "user", "content": "hi"}], tools=[_lookup_tool()]))
    assert captured_stream["system"][0]["cache_control"] == ONE_HOUR
    assert captured_stream["tools"][-1].to_dict()["cache_control"] == ONE_HOUR
    assert captured_stream["cache_control"] == {"type": "ephemeral"}


def test_one_shot_calls_get_no_automatic_breakpoint(tmp_path):
    """A summary or title call has no second round to read it; an automatic
    breakpoint there only bills the write premium."""
    final = SimpleNamespace(content=[_text_block("ok")], usage=_usage(1, 1), id="m1")
    for tools, max_iterations in (([], 0), ([{"type": "web_search_20250305", "name": "web_search"}], 0), ([_lookup_tool()], 1)):
        captured: dict = {}
        provider = _provider(tmp_path, _FakeMessageRunner([final]), captured)
        provider.run("sys prompt", [{"role": "user", "content": "hi"}], tools=tools, max_iterations=max_iterations)
        assert "cache_control" not in captured, (tools, max_iterations)
        assert captured["system"][0]["cache_control"] == ONE_HOUR  # the stable prefix still caches


def test_automatic_breakpoint_is_added_only_while_a_slot_is_free():
    system = _cached_system("sys")
    tools = _tools_with_cache_breakpoint([{"name": "t"}])
    digest = {
        "role": "user",
        "content": [
            {"type": "text", "text": "digest", "cache_control": {"type": "ephemeral"}},
            {"type": "text", "text": "question"},
        ],
    }
    # tools + system + digest = 3 explicit; the automatic one is the fourth.
    assert _automatic_cache_kwarg(tools, system, [digest]) == {"cache_control": {"type": "ephemeral"}}
    # A fourth explicit marker (here inside a tool_result) leaves no slot; a
    # fifth breakpoint would be a 400.
    marked_result = {
        "role": "user",
        "content": [
            {
                "type": "tool_result",
                "tool_use_id": "tu_1",
                "content": [{"type": "text", "text": "r", "cache_control": {"type": "ephemeral"}}],
            }
        ],
    }
    assert _automatic_cache_kwarg(tools, system, [digest, marked_result]) == {}


# ----- per-turn usage + tool_use accumulation ------------------------------


def test_run_accumulates_usage_and_tool_calls_across_turns(tmp_path):
    turn1 = SimpleNamespace(
        content=[_tool_use_block("search_article", "tu_1")],
        usage=_usage(inp=100, out=10, cread=5000, cwrite=200),
        id="m1",
    )
    turn2 = SimpleNamespace(
        content=[_text_block("答案")],
        usage=_usage(inp=40, out=25, cread=5200),
        id="m2",
        stop_reason="end_turn",
    )
    provider = _provider(tmp_path, _FakeMessageRunner([turn1, turn2]), {})

    result = provider.run("sys", [{"role": "user", "content": "hi"}], tools=[])

    # Cache reads are verified through usage.cache_read_input_tokens.
    assert result.usage["cache_read_input_tokens"] == 10_200
    assert result.usage["cache_creation_input_tokens"] == 200
    assert result.usage["input_tokens"] == 140
    assert result.usage["output_tokens"] == 35
    # Intermediate turns' tool_use blocks are no longer dropped.
    assert [c.name for c in result.tool_calls] == ["search_article"]
    assert result.text == "答案"

    # The ledger sees the accumulated totals (cache buckets now populate).
    summary = month_summary(state_dir=tmp_path)
    assert summary["totals"]["cache_read_tokens"] == 10_200
    assert summary["totals"]["cache_write_tokens"] == 200
    assert summary["totals"]["input_tokens"] == 140


def test_stream_accumulates_usage_per_turn_without_double_count(tmp_path):
    delta = SimpleNamespace(
        type="content_block_delta", delta=SimpleNamespace(type="text_delta", text="hello")
    )
    turn1_final = SimpleNamespace(content=[], usage=_usage(inp=50, cread=3000), id="m1")
    turn2_final = SimpleNamespace(
        content=[_text_block("hello")], usage=_usage(inp=20, out=8, cread=3100), id="m2"
    )
    runner = _FakeStreamRunner(
        [_FakeTurnStream([], turn1_final), _FakeTurnStream([delta], turn2_final)]
    )
    provider = _provider(tmp_path, runner, {})

    events = list(provider.stream("sys", [{"role": "user", "content": "hi"}], tools=[]))

    done = events[-1]
    assert done.kind == "done"
    # Per-turn sums; the final message (turn 2) is not counted twice.
    assert done.data["usage"]["cache_read_input_tokens"] == 6_100
    assert done.data["usage"]["input_tokens"] == 70
    assert done.data["usage"]["output_tokens"] == 8


# ----- OpenAI output ceiling ------------------------------------------------


def test_openai_run_passes_max_output_tokens():
    settings = load_settings(refresh=True)
    provider = OpenAIProvider(settings)
    captured: list[dict] = []

    def fake_create(**kwargs):
        captured.append(kwargs)
        return SimpleNamespace(
            id="resp_1",
            output=[SimpleNamespace(type="message", content=[SimpleNamespace(text="done")])],
            output_text="done",
            usage=None,
        )

    with patch.object(provider.client.responses, "create", side_effect=fake_create):
        provider.tool_runner("sys", [{"role": "user", "content": "hi"}], [], max_iterations=1)

    assert captured[0]["max_output_tokens"] == settings.max_tokens


def test_openai_stream_passes_max_output_tokens():
    settings = load_settings(refresh=True)
    provider = OpenAIProvider(settings)
    captured: list[dict] = []

    class _FakeCM:
        def __enter__(self):
            final = SimpleNamespace(id="resp_1", output=[], usage=None)
            return iter([SimpleNamespace(type="response.completed", response=final)])

        def __exit__(self, *args):
            return False

    def fake_stream(**kwargs):
        captured.append(kwargs)
        return _FakeCM()

    with patch.object(provider.client.responses, "stream", side_effect=fake_stream):
        list(provider.stream("sys", [{"role": "user", "content": "hi"}], []))

    assert captured[0]["max_output_tokens"] == settings.max_tokens


def test_build_user_content_marks_digest_as_anthropic_cache_breakpoint():
    """The chat digest must carry an explicit Anthropic cache breakpoint.

    The provider only marks `system` + `tools`; anything in `messages` sits
    past the last breakpoint. Without this, the whole context digest is
    re-billed at full input rate on every turn.
    """
    from legal_helper.attachments import build_user_content

    content = build_user_content(
        "Current user request:\nnew question",
        [],
        "anthropic",
        cache_prefix="Recent visible chat transcript:\nUSER: ...\nASSISTANT: ...",
    )
    assert isinstance(content, list) and len(content) == 2
    prefix, volatile = content
    assert prefix["cache_control"] == {"type": "ephemeral"}
    assert "chat transcript" in prefix["text"]
    # The volatile current request must sit AFTER the breakpoint, unmarked.
    assert "cache_control" not in volatile
    assert "new question" in volatile["text"]


def test_build_user_content_openai_splits_without_cache_control():
    """OpenAI caches prefixes automatically; a cache_control key would be an
    unknown field on an `input_text` block."""
    from legal_helper.attachments import build_user_content

    content = build_user_content("req", [], "openai", cache_prefix="digest")
    assert [b["type"] for b in content] == ["input_text", "input_text"]
    assert all("cache_control" not in b for b in content)


def test_build_user_content_back_compat_plain_string():
    from legal_helper.attachments import build_user_content

    assert build_user_content("just text", [], "anthropic") == "just text"
    assert build_user_content("just text", [], "openai", cache_prefix="") == "just text"


# --- the property the breakpoints depend on -----------------------------
# A cache_control breakpoint only pays off while the prefix in front of it is
# byte-identical between requests. ZCode's context builder keeps that true by
# splitting its system prompt into "stable" and "dynamic" blocks and pushing
# volatile content (the date, workspace instructions, the skills listing) out of
# the system block entirely. This harness gets there a different way — every
# per-turn value lives in `messages`, never in the system prompt — so the
# property is worth asserting rather than assuming.


def test_system_prompts_carry_no_per_turn_content():
    """A date, a path or an id in the system block re-bills the whole prefix."""
    import re

    from legal_helper.agent import SkillAgent
    from legal_helper.authoring import author_system_prompt
    from legal_helper.config import current_settings
    from legal_helper.providers import build_provider

    settings = current_settings()
    provider = build_provider(settings)
    prompts = {
        # The date, the answer language and the length tier go in the request
        # tail (answer_contract.author_tail), never here.
        "author": author_system_prompt(settings),
        "legal-response": SkillAgent("legal-response", provider)._system_prompt(),
        "cite-check": SkillAgent("cite-check", provider)._system_prompt(),
    }
    volatile = re.compile(
        r"20\d\d-\d\d-\d\d"  # an ISO date
        r"|/storage/|/tmp/|outputs/chat_artifacts"  # a machine-specific path
        r"|chat_id|run_id",  # a per-turn identifier
        re.IGNORECASE,
    )
    for name, prompt in prompts.items():
        found = volatile.search(prompt)
        assert found is None, f"{name} system prompt carries per-turn content: {found.group(0)!r}"


def test_a_system_prompt_is_byte_identical_across_builds():
    from legal_helper.agent import SkillAgent
    from legal_helper.config import current_settings
    from legal_helper.providers import build_provider

    provider = build_provider(current_settings())
    first = SkillAgent("legal-response", provider)._system_prompt()
    second = SkillAgent("legal-response", provider)._system_prompt()
    assert first == second


# --- OpenAI: the stable prefix as a cacheable developer message -------------
# `instructions` cannot carry an explicit breakpoint, so a one-shot call (the
# summariser, the title) never reached a cached prefix: in September a one-shot
# luna call read 0.7% of its input from cache.


def _openai(model: str = "gpt-6.1-sol") -> OpenAIProvider:
    settings = load_settings(refresh=True).model_copy(update={"openai_model": model})
    return OpenAIProvider(settings)


def _fake_response(n: int, items=None, text: str = "done"):
    items = items if items is not None else [SimpleNamespace(type="message", content=[SimpleNamespace(text=text)])]
    return SimpleNamespace(id=f"resp_{n}", output=items, output_text=text if items and items[0].type == "message" else "", usage=None)


def test_openai_large_system_prompt_moves_to_a_developer_breakpoint():
    provider = _openai()
    calls: list[dict] = []

    def fake_create(**kwargs):
        calls.append(kwargs)
        if len(calls) == 1:
            fc = SimpleNamespace(type="function_call", call_id="c1", id="fc1", name="nope", arguments="{}")
            return _fake_response(1, [fc])
        return _fake_response(2)

    big_system = "你是一名中国法律研究助手。请逐条引用法律条文并注明条款。" * 60  # ~1.7K tokens
    with patch.object(provider.client.responses, "create", side_effect=fake_create):
        provider.tool_runner(big_system, [{"role": "user", "content": "q"}], [], max_iterations=0)

    first = calls[0]
    assert "instructions" not in first
    dev = first["input"][0]
    assert dev["role"] == "developer"
    assert dev["content"][0]["text"] == big_system
    assert dev["content"][0]["prompt_cache_breakpoint"] == {"mode": "explicit"}
    # Implicit mode stays on: an explicit-only mode would switch off the
    # automatic breakpoint the in-turn tool loop is cached by.
    assert "prompt_cache_options" not in first
    # previous_response_id carries the developer message; re-sending it (or
    # `instructions`) on a chained request would duplicate the system prompt.
    second = calls[1]
    assert second["previous_response_id"] == "resp_1"
    assert "instructions" not in second
    assert all(item.get("role") != "developer" for item in second["input"])


def test_openai_small_system_prompt_stays_in_instructions():
    # Below the 1,024-token minimum a breakpoint writes nothing.
    provider = _openai()
    calls: list[dict] = []

    def fake_create(**kwargs):
        calls.append(kwargs)
        return _fake_response(1)

    with patch.object(provider.client.responses, "create", side_effect=fake_create):
        provider.tool_runner("You write short chat titles.", [{"role": "user", "content": "q"}], [], max_iterations=1)

    assert calls[0]["instructions"] == "You write short chat titles."
    assert calls[0]["input"] == [{"role": "user", "content": "q"}]


def test_openai_unverified_model_keeps_instructions_even_when_large():
    provider = _openai("gpt-5.4")
    calls: list[dict] = []

    def fake_create(**kwargs):
        calls.append(kwargs)
        return _fake_response(1)

    with patch.object(provider.client.responses, "create", side_effect=fake_create):
        provider.tool_runner("rule. " * 2000, [{"role": "user", "content": "q"}], [], max_iterations=1)

    assert "instructions" in calls[0]
    assert calls[0]["input"][0]["role"] == "user"


def test_one_hour_cache_writes_are_kept_apart_for_pricing(tmp_path):
    """1h writes bill at 2x, 5m writes at 1.25x; the total alone cannot price them."""
    usage = SimpleNamespace(
        input_tokens=4,
        output_tokens=100,
        cache_read_input_tokens=0,
        cache_creation_input_tokens=5_028,
        cache_creation=SimpleNamespace(ephemeral_1h_input_tokens=4_909, ephemeral_5m_input_tokens=119),
    )
    final = SimpleNamespace(content=[_text_block("ok")], usage=usage, id="m1")
    provider = _provider(tmp_path, _FakeMessageRunner([final]), {})
    result = provider.run("sys", [{"role": "user", "content": "hi"}], tools=[])
    assert result.usage["cache_creation_input_tokens"] == 5_028
    assert result.usage["cache_creation_1h_input_tokens"] == 4_909
    # The ledger's math is unchanged: it still records the total as before.
    summary = month_summary(state_dir=tmp_path)
    assert summary["totals"]["cache_write_tokens"] == 5_028
