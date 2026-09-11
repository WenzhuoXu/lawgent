"""Trajectory-level failure taxonomy for the multi-step legal workflow.

Outcome-level checks — did the answer cite something, did the citation exist —
cannot see the failures that matter most in an agentic legal pipeline. The two
that motivate this module:

* **Right-Answer-Wrong-Reason** — a correct output reached through a broken
  chain, invisible to any check that only reads the final text
  (LexAgentHallu, arXiv 2609.09754).
* **Cascading hallucination** — a bad retrieval or inference at step *k* that
  every later step builds on, where each step looks locally plausible
  (CHARM, arXiv 2606.04435).

LexAgentHallu's second finding is the one that makes labelling worth the
trouble: hallucination subclasses **cluster** rather than scatter, forming
distinct per-framework, per-task and per-category profiles. Once each failure
event carries a subclass, the dominant failure mode *for this harness* becomes
a thing you can read off a run instead of guess at.

The workflow already emits structured events for everything below. This module
does not add instrumentation — it gives the existing events a shared vocabulary
and aggregates them into a per-run profile.

Two layers, following the paper's dual-layer design:

* ``Layer.SUBSTANTIVE`` — the legal content is wrong (bad authority, bad
  grounding, unsupported claim).
* ``Layer.PROCEDURAL`` — the *agent* misbehaved (plan malformed, specialist
  dropped, tool path degraded), regardless of whether the content survived.

A procedural failure with a correct-looking answer is precisely the
Right-Answer-Wrong-Reason case, so ``profile()`` reports the layers separately
rather than summing them.
"""

from __future__ import annotations

from collections import Counter
from dataclasses import dataclass
from enum import Enum
from typing import Any, Iterable, Optional


class Layer(str, Enum):
    SUBSTANTIVE = "substantive"
    PROCEDURAL = "procedural"


class Category(str, Enum):
    """High-level failure categories."""

    # substantive
    CITATION = "citation"            # authority missing, unverifiable, or wrong
    GROUNDING = "grounding"          # quote/claim not supported by its source
    # procedural
    PLANNING = "planning"            # decomposition malformed or degraded
    DELEGATION = "delegation"        # specialist work dropped, capped, or failed
    EXECUTION = "execution"          # tool/provider path degraded mid-run
    OUTPUT = "output"                # answer empty or structurally unusable


@dataclass(frozen=True)
class Subclass:
    key: str
    layer: Layer
    category: Category
    description: str


def _s(key: str, layer: Layer, category: Category, description: str) -> Subclass:
    return Subclass(key=key, layer=layer, category=category, description=description)


# Workflow event name → subclass. Only *failure or degradation* events are
# mapped; ``*_started`` / ``*_finished`` events are progress, not failure, and
# deliberately have no entry.
EVENT_SUBCLASSES: dict[str, Subclass] = {
    # ---- substantive · citation -------------------------------------------
    "citation_audit_failed": _s(
        "citation.audit_failed", Layer.SUBSTANTIVE, Category.CITATION,
        "The citation audit rejected the answer.",
    ),
    "citation_audit_auto_pass": _s(
        "citation.unaudited_pass", Layer.SUBSTANTIVE, Category.CITATION,
        "Passed without an audit because no citations were extractable.",
    ),
    "citation_audit_zero_extraction_override": _s(
        "citation.extractor_miss", Layer.SUBSTANTIVE, Category.CITATION,
        "Citation-shaped text the extractor could not parse; full check forced.",
    ),
    "citation_audit_skipped": _s(
        "citation.audit_skipped", Layer.SUBSTANTIVE, Category.CITATION,
        "Audit skipped as a non-legal turn.",
    ),
    "workflow_cite_check_fast_fallback": _s(
        "citation.degraded_checker", Layer.SUBSTANTIVE, Category.CITATION,
        "Cite-check ran on the fast tier after the primary model was unavailable.",
    ),
    # ---- procedural · planning --------------------------------------------
    "workflow_plan_fallback": _s(
        "planning.fallback", Layer.PROCEDURAL, Category.PLANNING,
        "Planner failed; heuristic plan substituted.",
    ),
    "workflow_planner_fast_fallback": _s(
        "planning.degraded_planner", Layer.PROCEDURAL, Category.PLANNING,
        "Planning ran on the fast tier after the primary model was unavailable.",
    ),
    "workflow_plan_router_fallback": _s(
        "planning.router_fallback", Layer.PROCEDURAL, Category.PLANNING,
        "Complexity router failed; default complexity assumed.",
    ),
    "workflow_plan_router_low_quality": _s(
        "planning.router_low_quality", Layer.PROCEDURAL, Category.PLANNING,
        "Router returned a low-confidence route.",
    ),
    "workflow_plan_dependency_cycle_broken": _s(
        "planning.dependency_cycle", Layer.PROCEDURAL, Category.PLANNING,
        "Task graph contained a cycle that had to be broken.",
    ),
    "workflow_plan_agent_capped": _s(
        "planning.scope_capped", Layer.PROCEDURAL, Category.PLANNING,
        "Planned specialist count exceeded the cap and was truncated.",
    ),
    # ---- procedural · delegation ------------------------------------------
    "agent_task_failed": _s(
        "delegation.task_failed", Layer.PROCEDURAL, Category.DELEGATION,
        "A specialist task failed outright.",
    ),
    "agent_task_rate_limited": _s(
        "delegation.rate_limited", Layer.PROCEDURAL, Category.DELEGATION,
        "A specialist task was rate limited.",
    ),
    "workflow_degraded_missing_specialists": _s(
        "delegation.specialist_dropped", Layer.PROCEDURAL, Category.DELEGATION,
        "Synthesis ran without specialists the plan mandated — the mandate-drop "
        "case, and the likeliest source of Right-Answer-Wrong-Reason.",
    ),
    "workflow_degraded": _s(
        "delegation.degraded_run", Layer.PROCEDURAL, Category.DELEGATION,
        "Run completed with reduced coverage.",
    ),
    # ---- procedural · execution -------------------------------------------
    "workflow_agent_rate_limit_degraded": _s(
        "execution.rate_limit_degraded", Layer.PROCEDURAL, Category.EXECUTION,
        "Run degraded under provider rate limits.",
    ),
    "workflow_integration_provider_fallback": _s(
        "execution.provider_fallback", Layer.PROCEDURAL, Category.EXECUTION,
        "Integration turn fell back to the secondary provider.",
    ),
    "workflow_agent_concurrency_reduced": _s(
        "execution.concurrency_reduced", Layer.PROCEDURAL, Category.EXECUTION,
        "Specialist concurrency was reduced mid-run.",
    ),
    # ---- procedural · output ----------------------------------------------
    "empty_answer_guarded": _s(
        "output.empty_answer", Layer.PROCEDURAL, Category.OUTPUT,
        "A stage produced no answer and was guarded.",
    ),
}


def classify_event(event: str) -> Optional[Subclass]:
    """Subclass for a workflow event name, or ``None`` if it is not a failure."""
    return EVENT_SUBCLASSES.get(event)


def profile(events: Iterable[Any]) -> dict[str, Any]:
    """Aggregate a run's events into a trajectory failure profile.

    Accepts event names, ``(name, payload)`` pairs, or dicts carrying an
    ``event``/``name`` key — whatever shape the caller has to hand.

    Reports layers separately on purpose: a run with procedural failures and no
    substantive ones is the Right-Answer-Wrong-Reason shape, and summing the two
    would hide exactly that.
    """
    by_subclass: Counter[str] = Counter()
    by_category: Counter[str] = Counter()
    by_layer: Counter[str] = Counter()
    for item in events:
        name = _event_name(item)
        sub = classify_event(name) if name else None
        if sub is None:
            continue
        by_subclass[sub.key] += 1
        by_category[sub.category.value] += 1
        by_layer[sub.layer.value] += 1
    substantive = by_layer.get(Layer.SUBSTANTIVE.value, 0)
    procedural = by_layer.get(Layer.PROCEDURAL.value, 0)
    return {
        "total": substantive + procedural,
        "by_layer": dict(by_layer),
        "by_category": dict(by_category),
        "by_subclass": dict(by_subclass),
        "dominant_subclass": by_subclass.most_common(1)[0][0] if by_subclass else None,
        # Correct-looking output reached through a broken chain.
        "right_answer_wrong_reason": bool(procedural and not substantive),
    }


def _event_name(item: Any) -> Optional[str]:
    if isinstance(item, str):
        return item
    if isinstance(item, dict):
        for key in ("event", "name", "type"):
            val = item.get(key)
            if isinstance(val, str):
                return val
        return None
    if isinstance(item, (tuple, list)) and item:
        first = item[0]
        return first if isinstance(first, str) else None
    return None


__all__ = [
    "Layer",
    "Category",
    "Subclass",
    "EVENT_SUBCLASSES",
    "classify_event",
    "profile",
]
