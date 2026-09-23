"""Model-facing size budget for tool results.

A tool result used to enter the model's context at whatever size the source
happened to be. The `[:1200]` slices in both providers are log previews, not
budgets, so a PKULaw statute body, a CourtListener opinion or a wide RAG
retrieval went in whole and stayed there for the rest of the turn. Measured on
the 2026-09 ledger: 40 of 667 requests crossed OpenAI's 272K long-context
threshold at a mean input of 646K tokens, and the 2x input / 1.5x output
surcharge on them was ~55% of the month's spend.

Each tool therefore declares how much of its output may reach the model:

- ``truncate`` keeps a head (or tail) window and says how much was dropped;
- ``artifact`` writes the full result to a file and hands the model a preview
  plus the path, so the content is still reachable through ``read_document``
  (with ``offset`` / ``limit`` to page it) instead of being resident.

Budgets are measured in **estimated tokens, not bytes**: the same 50 KB is
~12K tokens of English and ~50K tokens of Chinese, and this harness reads both.

Two exemption rules matter more than the numbers:

- a result carrying inline images is never budgeted — the providers split the
  text half out before this runs, and re-serialising the JSON would break the
  image protocol;
- a result that *is* the deliverable (a sub-agent answer from ``run_skill``, a
  cite-check report the workflow parses) is never budgeted, because truncating
  it would silently shorten the memo rather than the evidence behind it.
"""

from __future__ import annotations

import hashlib
from dataclasses import dataclass
from pathlib import Path
from typing import Literal, Optional

from .context import estimate_tokens, truncate_to_tokens

Strategy = Literal["truncate", "artifact"]
Preview = Literal["head", "tail"]


@dataclass(frozen=True)
class ResultBudget:
    max_tokens: int
    strategy: Strategy = "truncate"
    preview: Preview = "head"


@dataclass(frozen=True)
class BudgetedResult:
    text: str
    original_tokens: int
    kept_tokens: int
    spill_path: Optional[Path] = None
    applied: bool = False


# Generous enough that an ordinary tool never notices it. How many such results
# a loop may accumulate is not capped: `turn_compaction` clears old ones when
# the transcript nears the turn ceiling.
DEFAULT_BUDGET = ResultBudget(max_tokens=16_000, strategy="truncate", preview="head")

# Result *is* the deliverable, or is a structured payload another layer parses.
EXEMPT_TOOLS = frozenset(
    {
        "run_skill",  # a sub-agent's finished answer — becomes the memo
        "cite_check_report_tool",  # workflow parses the report markdown
        "ground_answer_tool",  # grounding verdicts feed the verification table
        "quote_roundtrip_tool",
        "provenance_audit_tool",
        "verification_log_append_tool",
        "extract_citations_tool",
        "validate_citations_tool",
        "view_image",
        "read_deck_stylesheet",
    }
)

# Statute and opinion bodies: keep a working window resident, spill the rest.
_DOCUMENT_BODY = ResultBudget(max_tokens=12_000, strategy="artifact", preview="head")
# Result lists: the head is the ranked part, so a tail preview would be worse.
_SOURCE_LIST = ResultBudget(max_tokens=8_000, strategy="artifact", preview="head")
# Structured inspections: truncating is fine, the model re-inspects a narrower
# range instead of paging a file.
_INSPECTION = ResultBudget(max_tokens=12_000, strategy="truncate", preview="head")

_EXACT_BUDGETS: dict[str, ResultBudget] = {
    "read_document": _DOCUMENT_BODY,
    "retrieve_legal": _SOURCE_LIST,
    "legal_source_search": _SOURCE_LIST,
    "aviation_source_search": _SOURCE_LIST,
    "courtlistener_search": _SOURCE_LIST,
    "ecfr_search": _SOURCE_LIST,
    "eurlex_search": _SOURCE_LIST,
    "federal_register_search": _SOURCE_LIST,
    "govinfo_search": _SOURCE_LIST,
    "flk_npc_search": _SOURCE_LIST,
    "extract_pdf_tables": _INSPECTION,
    "inspect_xlsx": _INSPECTION,
    "inspect_xlsx_range": _INSPECTION,
    "inspect_docx": _INSPECTION,
    "inspect_pdf": _INSPECTION,
    "inspect_pptx": _INSPECTION,
    "project_memory_search": _INSPECTION,
}

# Prefix rules, applied after the exact map. Namespaced MCP tools arrive as
# ``<server>__<tool>``, so one entry covers every sub-service of a provider.
_PREFIX_BUDGETS: tuple[tuple[str, ResultBudget], ...] = (
    ("pkulaw_fatiao__", _DOCUMENT_BODY),
    ("pkulaw_law_search__", _DOCUMENT_BODY),
    ("pkulaw_doc_link__", _DOCUMENT_BODY),
    ("pkulaw_case_search__", _SOURCE_LIST),
    ("pkulaw_case_list__", _SOURCE_LIST),
    ("pkulaw_nl_search__", _SOURCE_LIST),
    ("pkulaw_", _INSPECTION),  # recognition / validator services: short payloads
)


def budget_for(tool_name: str) -> Optional[ResultBudget]:
    """The budget governing ``tool_name``, or None when it is exempt."""
    name = (tool_name or "").strip()
    if not name or name in EXEMPT_TOOLS:
        return None
    exact = _EXACT_BUDGETS.get(name)
    if exact is not None:
        return exact
    for prefix, budget in _PREFIX_BUDGETS:
        if name.startswith(prefix):
            return budget
    return DEFAULT_BUDGET


def _spill_dir() -> Path:
    from .config import current_settings

    return current_settings().state_dir / "tool_results"


def _spill(tool_name: str, text: str) -> Optional[Path]:
    """Write the full result to a file; return its path, or None on failure.

    Spills live under ``state/`` rather than ``outputs/`` on purpose: they are
    machine intermediates, and the artifact scan that populates the chat's
    file list walks ``outputs/`` recursively.
    """
    try:
        digest = hashlib.sha256(text.encode("utf-8", "replace")).hexdigest()[:12]
        safe = "".join(c if c.isalnum() or c in "-_" else "_" for c in tool_name)[:60]
        target = _spill_dir() / f"{safe}-{digest}.md"
        if not target.exists():
            target.parent.mkdir(parents=True, exist_ok=True)
            target.write_text(text, encoding="utf-8")
        return target
    except Exception:  # noqa: BLE001 — a failed spill must degrade to truncation
        return None


def apply_result_budget(tool_name: str, text: str) -> BudgetedResult:
    """Cut ``text`` down to the budget for ``tool_name``.

    Returns the original text unchanged when the tool is exempt or already
    inside its budget, so the common case costs one token estimate.
    """
    if not isinstance(text, str) or not text:
        return BudgetedResult(text=text, original_tokens=0, kept_tokens=0)

    budget = budget_for(tool_name)
    original = estimate_tokens(text)
    if budget is None:
        return BudgetedResult(text=text, original_tokens=original, kept_tokens=original)

    if original <= budget.max_tokens:
        return BudgetedResult(text=text, original_tokens=original, kept_tokens=original)

    spill_path = _spill(tool_name, text) if budget.strategy == "artifact" else None
    window = truncate_to_tokens(text, budget.max_tokens, keep=budget.preview)
    dropped = max(0, original - estimate_tokens(window))
    if spill_path is not None:
        notice = (
            f"\n\n[Result truncated to ~{budget.max_tokens:,} of ~{original:,} tokens. "
            f"The complete result is saved at {spill_path}. "
            f"Read the rest with read_document(path=\"{spill_path}\", offset=<line>, limit=<lines>) "
            f"— do not re-run this tool to get it.]"
        )
    else:
        notice = (
            f"\n\n[Result truncated to ~{budget.max_tokens:,} of ~{original:,} tokens "
            f"(~{dropped:,} dropped). Narrow the query or the requested range to see more.]"
        )
    out = window + notice
    kept = estimate_tokens(out)
    return BudgetedResult(
        text=out,
        original_tokens=original,
        kept_tokens=kept,
        spill_path=spill_path,
        applied=True,
    )


def budget_log_fields(budgeted: Optional[BudgetedResult]) -> dict[str, object]:
    """Log fields describing a budget cut, empty when nothing was cut.

    Both providers log through this so a truncation is visible in the same
    shape regardless of which one served the turn.
    """
    if budgeted is None or not budgeted.applied:
        return {}
    fields: dict[str, object] = {
        "result_budget_applied": True,
        "result_tokens_before": budgeted.original_tokens,
        "result_tokens_after": budgeted.kept_tokens,
    }
    if budgeted.spill_path is not None:
        fields["result_spill_path"] = str(budgeted.spill_path)
    return fields


__all__ = [
    "ResultBudget",
    "BudgetedResult",
    "DEFAULT_BUDGET",
    "EXEMPT_TOOLS",
    "budget_for",
    "apply_result_budget",
    "budget_log_fields",
]
