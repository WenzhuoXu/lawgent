"""Run one model response's tool calls, side by side where that is safe.

A model that asks for five statute lookups in one response used to wait for
them one after another: both providers executed the calls of a response
serially, which since August cost ~461 s of pure waiting. Whether two calls may
overlap is a property of the tools, declared in ``tool_policy`` — not something
to infer here, and not ``is_mutating``: two page renders mutate nothing and
still must not overlap.

The rule this module applies:

- a maximal run of consecutive concurrent-safe calls executes together;
- any other call executes alone, in its original position, so a write and a
  later read of the same file keep their order;
- results come back in the order the model asked for them;
- the ``before`` / ``after`` hooks run in the caller's thread and in that same
  order, so "started" / "finished" events pair up exactly as they did when
  everything ran serially (the UI matches them first-in, first-out per tool).

Worker threads get a copy of the caller's context. The server's SSE event sink,
the run and agent ids and the usage attribution all live in contextvars;
without the copy a tool's events would vanish and its spend would be orphaned.
"""

from __future__ import annotations

import contextvars
import threading
from concurrent.futures import ThreadPoolExecutor
from typing import Any, Callable, Optional, Sequence, TypeVar

from ..tool_policy import concurrency_group, is_concurrent_safe

T = TypeVar("T")

# Upper bound on calls in flight for one response. Lookups are network-bound,
# so this is about not opening an unbounded number of sockets at once, not CPU.
MAX_PARALLEL_TOOL_CALLS = 8

_GROUP_LOCKS: dict[str, threading.RLock] = {}
_GROUP_LOCKS_GUARD = threading.Lock()


def _group_lock(group: str) -> threading.RLock:
    with _GROUP_LOCKS_GUARD:
        lock = _GROUP_LOCKS.get(group)
        if lock is None:
            lock = _GROUP_LOCKS[group] = threading.RLock()
        return lock


def _guarded(name: str, work: Callable[[], T]) -> T:
    """Run ``work`` holding its concurrency group's lock, when it has one.

    Taken on the serial path too: the hazard is process-wide library state, so
    a render in one sub-agent must also wait for a render in another.
    """
    group = concurrency_group(name)
    if group is None:
        return work()
    with _group_lock(group):
        return work()


def _segments(names: Sequence[str]) -> list[list[int]]:
    """Split call indices into runs that may execute together."""
    out: list[list[int]] = []
    for i, name in enumerate(names):
        if is_concurrent_safe(name) and out and len(out[-1]) and is_concurrent_safe(names[out[-1][-1]]):
            out[-1].append(i)
        else:
            out.append([i])
    return out


def run_tool_calls(
    names: Sequence[str],
    work: Callable[[int], T],
    *,
    before: Optional[Callable[[int], Any]] = None,
    after: Optional[Callable[[int, T], Any]] = None,
) -> list[T]:
    """Execute ``work(i)`` for every call; return results in call order.

    ``work`` must turn its own failures into a result (both providers already
    hand tool errors back to the model rather than raising); an exception that
    escapes anyway is re-raised once its segment has finished.
    """
    results: list[Any] = [None] * len(names)
    for segment in _segments(names):
        for i in segment:
            if before is not None:
                before(i)
        if len(segment) == 1:
            i = segment[0]
            results[i] = _guarded(names[i], lambda i=i: work(i))
        else:
            with ThreadPoolExecutor(
                max_workers=min(len(segment), MAX_PARALLEL_TOOL_CALLS),
                thread_name_prefix="tool-call",
            ) as pool:
                futures = {
                    i: pool.submit(
                        contextvars.copy_context().run,
                        _guarded,
                        names[i],
                        lambda i=i: work(i),
                    )
                    for i in segment
                }
            # Leaving the `with` waited for every future, so .result() is ready.
            for i in segment:
                results[i] = futures[i].result()
        for i in segment:
            if after is not None:
                after(i, results[i])
    return results


__all__ = ["MAX_PARALLEL_TOOL_CALLS", "run_tool_calls"]
