"""One model response's tool calls: side by side where tool_policy allows it.

These pin the contract both providers rely on: concurrent-safe calls overlap,
anything else is a barrier that runs alone in its original position, results
and the started/finished hooks come back in call order, a concurrency group's
members never overlap, and worker threads see the caller's contextvars.
"""

from __future__ import annotations

import contextvars
import threading
import time

from legal_helper.providers.tool_exec import run_tool_calls

_marker: contextvars.ContextVar[str] = contextvars.ContextVar("tool_exec_test_marker", default="unset")


def test_safe_calls_overlap_and_results_keep_call_order():
    names = ["inspect_docx", "flk_npc_search", "pkulaw_fatiao__get_law_item_content"]
    # Each call waits for all three to be running at once: only a concurrent
    # execution gets past the barrier.
    barrier = threading.Barrier(len(names), timeout=5)
    hooks: list[tuple[str, int]] = []

    def work(i: int) -> str:
        barrier.wait()
        time.sleep(0.01 * (len(names) - i))  # finish in reverse order
        return f"result-{i}"

    results = run_tool_calls(
        names,
        work,
        before=lambda i: hooks.append(("before", i)),
        after=lambda i, r: hooks.append(("after", i)),
    )
    assert results == ["result-0", "result-1", "result-2"]
    # Started/finished pair up first-in, first-out, as the UI matches them.
    assert hooks == [("before", 0), ("before", 1), ("before", 2), ("after", 0), ("after", 1), ("after", 2)]


def test_an_unsafe_call_runs_alone_in_its_original_position():
    names = ["inspect_pdf", "write_docx", "inspect_xlsx"]
    timeline: list[str] = []
    lock = threading.Lock()

    def work(i: int) -> int:
        with lock:
            timeline.append(f"start-{names[i]}")
        time.sleep(0.02)
        with lock:
            timeline.append(f"end-{names[i]}")
        return i

    assert run_tool_calls(names, work) == [0, 1, 2]
    assert timeline == [
        "start-inspect_pdf",
        "end-inspect_pdf",
        "start-write_docx",
        "end-write_docx",
        "start-inspect_xlsx",
        "end-inspect_xlsx",
    ]


def test_members_of_a_concurrency_group_never_overlap():
    # Renders share PDFium, which is not thread-safe; a lookup may overlap them.
    names = ["render_docx_pages", "render_pdf_pages", "render_pptx_slides", "inspect_docx"]
    active = {"renders": 0, "max_renders": 0, "lookup_overlapped": False}
    lock = threading.Lock()

    def work(i: int) -> str:
        is_render = names[i].startswith("render_")
        with lock:
            if is_render:
                active["renders"] += 1
                active["max_renders"] = max(active["max_renders"], active["renders"])
            elif active["renders"]:
                active["lookup_overlapped"] = True
        time.sleep(0.03)
        with lock:
            if is_render:
                active["renders"] -= 1
        return names[i]

    assert run_tool_calls(names, work) == names
    assert active["max_renders"] == 1


def test_worker_threads_see_the_callers_context():
    token = _marker.set("turn-42")
    try:
        seen = run_tool_calls(["inspect_docx", "inspect_pdf"], lambda i: (_marker.get(), threading.current_thread().name))
    finally:
        _marker.reset(token)
    assert [value for value, _ in seen] == ["turn-42", "turn-42"]
    # And they really were worker threads, not the caller.
    assert all(name != threading.current_thread().name for _, name in seen)


def test_a_single_call_runs_inline():
    caller = threading.current_thread().name
    assert run_tool_calls(["inspect_docx"], lambda i: threading.current_thread().name) == [caller]
