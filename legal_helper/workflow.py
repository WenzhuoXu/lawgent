"""Planner-driven workflow executor for the web chat surface."""

from __future__ import annotations

import json
import queue
import re
import contextvars
import threading
import time
import uuid
from pathlib import Path
from typing import Any, Callable, Iterable, Iterator, Optional

from . import answer_contract
from .agent import SkillAgent, _build_user_message
from .answer_lint import AnswerLinter
from .attachments import build_user_content
from .authoring import author_system_prompt
from .chat_models import AgentTask, ChatMessage, WorkflowPlan
from .citations import extract_citations
from .tools.citations import take_last_cite_check_payload
from .config import Settings, current_settings, load_settings, override_current_settings
from .documents import write_docx_document, write_pdf_document
from .errors import OUT_OF_MONEY_MESSAGE, is_out_of_money_error, is_rate_limit_error
from .logging_setup import (
    agent_name_var,
    log_workflow_event,
    new_run_id,
    parent_run_id_var,
    run_id_var,
    setup_logging,
)
from .providers import Provider, StreamEvent, build_provider
from .providers.base import JSON_OBJECT
from .runs import RunStore, load_run
from .usage import usage_context_var
from .skills import SKILL_NAMES
from .tools.documents import (
    copy_xlsx_sheet,
    edit_docx_text,
    edit_pptx_text,
    edit_xlsx_cells,
    edit_xlsx_cells_checked,
    extract_pdf_tables,
    diff_xlsx,
    inspect_docx,
    inspect_pdf,
    inspect_pptx,
    inspect_xlsx,
    inspect_xlsx_range,
    merge_pdfs,
    read_document,
    render_docx_pages,
    render_pdf_pages,
    render_pptx_slides,
    render_xlsx_pages,
    reshape_docx,
    reshape_pptx,
    reshape_xlsx,
    rotate_pdf_pages,
    split_pdf,
    view_image,
    write_docx,
    write_pdf,
    write_xlsx,
)
from .tools.connectors_tools import visible_connector_tools
from .tools.dates import compute_period
from .tools.decks import create_deck
from .tools.diagrams import render_flowchart_image
from .tools.procedures import procedure_tool
from .tools.fetch_attach import fetch_url_to_artifact
from .tools.format_recipes import list_format_recipes, read_format_recipe
from .tools.redline_tools import compare_docx, redline_docx
from .tools.legal_search import legal_source_search
from .tools.search import hosted_search_tools_for_provider


GENERAL_TASK_SKILL = "general-answer"

# One transient-failure retry per specialist, after a short pause. Rate limits
# are handled separately by the adaptive-concurrency path in `_stream_tasks`.
_SPECIALIST_RETRY_BACKOFF_SECONDS = 2.0


def _document_io_tools() -> list[Any]:
    """Safe baseline document tools for authoring/final-synthesis turns."""
    tools: list[Any] = [
        write_docx,
        write_pdf,
        inspect_docx,
        edit_docx_text,
        reshape_docx,
        redline_docx,
        compare_docx,
        render_docx_pages,
        inspect_pdf,
        extract_pdf_tables,
        render_pdf_pages,
        merge_pdfs,
        split_pdf,
        rotate_pdf_pages,
        write_xlsx,
        # Every deck — a full presentation, one slide, a diagram slide — is
        # made by ppt-master through this one tool.
        create_deck,
        # A diagram as a picture, for a Word document or the chat. The
        # flowchart method names it, and since simple and standard turns go to
        # this surface, a diagram request otherwise had no way to draw.
        render_flowchart_image,
        inspect_xlsx,
        inspect_xlsx_range,
        edit_xlsx_cells,
        edit_xlsx_cells_checked,
        reshape_xlsx,
        copy_xlsx_sheet,
        diff_xlsx,
        render_xlsx_pages,
        inspect_pptx,
        edit_pptx_text,
        reshape_pptx,
        render_pptx_slides,
        read_document,
        # Four instruction sources tell the model to reach for `view_image`
        # when it needs fine detail on one page, but it was absent from this
        # surface — so the only way to look closely was another full render.
        view_image,
        fetch_url_to_artifact,
        list_format_recipes,
        read_format_recipe,
        # Deadlines are arithmetic over a holiday calendar; the model used to
        # count them in prose with no calendar at all.
        compute_period,
    ]
    # An installed external procedure other than the deck skill (a 公文
    # generator, say); absent when none is installed.
    procedure = procedure_tool()
    if procedure is not None:
        tools.append(procedure)
    return tools

_GENERAL_SYSTEM_PROMPT = """You are a practical general-purpose assistant.
Answer ordinary non-legal user requests directly and helpfully.

Use hosted web search when the request depends on current facts, availability,
prices, schedules, news, recent product details, or source-backed research.
If web search is not available, say what you can answer from general knowledge
and what should be verified. Do not add legal disclaimers unless the
user's request actually raises a legal issue.

Pick the right tool for what the user actually wants — read the tool
descriptions carefully. Never refuse a request that one of your tools can
fulfil; if you have a tool for it, use it. When the user asks for a file
deliverable, write it with the matching tool and reply with the artifact
path — never paste the document body into chat as a substitute.

For Office/PDF work, use the document tools directly in this general response
path. For Excel, inspect the real workbook coordinates before editing — do not
derive edit coordinates from markdown table exports, where blank rows or merged
headers shift the row numbers. `read_format_recipe` is available when you want
the deeper recipe for a format; it is not a required first step.
"""


def _detect_language(text: str) -> str:
    return answer_contract.detect_language(text)


class _ToolXmlStripper:
    """Stream-safe filter that drops pseudo tool-call / tool-result XML blocks
    that the model occasionally emits as plain text during the final-synthesis
    turn (when no tools are offered, the model still sometimes writes
    ``<tool_call>{...}</tool_call>`` or ``<tool_result>{...}</tool_result>``
    instead of being told to invoke a function).

    Handles chunked streaming correctly: if a partial opening tag arrives
    split across two chunks (e.g. ``"<too"`` then ``"l_call>..."``), the
    suffix is buffered until the next chunk so legitimate prose containing
    ``<`` is not corrupted.
    """

    _OPEN_TAGS: tuple[str, ...] = (
        "<tool_call",
        "<tool_result",
        "<function_call",
        "<function_result",
        "<invoke",
        "<parameters",
    )

    def __init__(self) -> None:
        self._buf: str = ""
        self._inside_tag: bool = False
        self._end_marker: str = ""

    def feed(self, chunk: str) -> str:
        """Append chunk; return text safe to emit immediately."""
        self._buf += chunk
        out: list[str] = []
        while self._buf:
            if self._inside_tag:
                idx = self._buf.find(self._end_marker)
                if idx == -1:
                    # Still inside the suppressed block \u2014 drop pending content
                    # and wait for the closing tag in a future chunk.
                    self._buf = ""
                    return "".join(out)
                self._buf = self._buf[idx + len(self._end_marker) :]
                self._inside_tag = False
                self._end_marker = ""
                continue

            earliest = -1
            matched_tag: Optional[str] = None
            for t in self._OPEN_TAGS:
                i = self._buf.find(t)
                if i != -1 and (earliest == -1 or i < earliest):
                    earliest = i
                    matched_tag = t

            if earliest == -1:
                last_lt = self._buf.rfind("<")
                if last_lt == -1:
                    out.append(self._buf)
                    self._buf = ""
                    return "".join(out)
                suffix = self._buf[last_lt:]
                if any(t.startswith(suffix) for t in self._OPEN_TAGS):
                    out.append(self._buf[:last_lt])
                    self._buf = suffix
                    return "".join(out)
                out.append(self._buf)
                self._buf = ""
                return "".join(out)

            out.append(self._buf[:earliest])
            assert matched_tag is not None
            tag_name = matched_tag[1:]  # strip leading "<"
            self._end_marker = f"</{tag_name}>"
            self._buf = self._buf[earliest:]
            self._inside_tag = True
        return "".join(out)

    def flush(self) -> str:
        """Final flush: drop any unclosed suppressed block, emit safe tail."""
        if self._inside_tag:
            self._buf = ""
            return ""
        tail = self._buf
        self._buf = ""
        return tail


def _make_task(skill_name: str, title: str, task: str, depends_on: Optional[list[str]] = None) -> AgentTask:
    return AgentTask(
        id=f"task_{uuid.uuid4().hex[:8]}",
        skill_name=skill_name,
        title=title,
        task=task,
        depends_on=depends_on or [],
    )


_EMPTY_ANSWER_NOTICE = (
    "> ⚠️ 本轮未能生成正文答复（模型结束时没有写出答案）。本轮生成的文件（如有）"
    "已保存并列在下方；请再发一次消息，我会基于已收集的材料直接作答。\n\n"
    "> ⚠️ This turn produced no written answer — the model finished without "
    "composing one. Any files generated this turn are saved and listed below; "
    "send the message again and I will answer from the material already gathered."
)


def _guard_empty_answer(text: str, stage: str) -> str:
    """Never ship an empty assistant message.

    Shipping the blank is the worst option: the run's artifacts were written to
    disk, so the user sees an empty reply next to files they did receive.
    """
    if text and text.strip():
        return text
    log_workflow_event("empty_answer_guarded", {"stage": stage})
    return _EMPTY_ANSWER_NOTICE


def _provider_kwargs(fn: Callable[..., Any], **kwargs: Any) -> dict[str, Any]:
    """The optional keyword arguments ``fn`` accepts, with None values dropped.

    Lets a per-call option (verbosity) reach providers that support it without
    breaking an injected provider or a test double that predates it.
    """
    import inspect

    try:
        params = inspect.signature(fn).parameters
    except (TypeError, ValueError):
        return {}
    accepts_any = any(p.kind is inspect.Parameter.VAR_KEYWORD for p in params.values())
    return {
        key: value
        for key, value in kwargs.items()
        if value is not None and (accepts_any or key in params)
    }


_CONTEXT_OVERFLOW_RE = re.compile(
    r"context_length_exceeded|maximum context length|prompt is too long|"
    r"input (?:is )?too long|too many tokens|context window",
    re.IGNORECASE,
)


def _is_context_overflow(exc: BaseException) -> bool:
    code = str(getattr(exc, "code", "") or "")
    return code == "context_length_exceeded" or bool(_CONTEXT_OVERFLOW_RE.search(str(exc)))


def _attachments_for_task(
    task: AgentTask, attachments: list[Path], all_tasks: Iterable[AgentTask]
) -> list[Path]:
    """The turn's attachments a specialist should receive.

    Every attachment used to go to every specialist and to the integrator.
    A task gets the files its title or body names; when no task names any
    file, every task gets them all (the planner did not route the files, so
    no specialist may be left without them).
    """
    if not attachments:
        return []

    def names(candidate: AgentTask) -> list[Path]:
        text = f"{candidate.title}\n{candidate.task}".lower()
        return [
            p for p in attachments
            if p.name.lower() in text or (len(p.stem) >= 4 and p.stem.lower() in text)
        ]

    if not any(names(other) for other in all_tasks):
        return list(attachments)
    return names(task)


def _make_general_task(user_message: str) -> AgentTask:
    return _make_task(
        GENERAL_TASK_SKILL,
        "General answer",
        (
            "Answer this ordinary non-legal user request as a general assistant. "
            "Use hosted web search when current facts, recent information, products, "
            "prices, schedules, recommendations, or source-backed factual claims are "
            "needed. Keep the answer practical and in the user's language. Do not use "
            "legal skill resources or add a legal-advice disclaimer unless a "
            "legal issue becomes relevant.\n\n"
            f"User request:\n{user_message}"
        ),
    )


from .domains import detect_packs, merge_active_packs


_GENERIC_LEGAL_MARKERS = (
    # English
    "compliance",
    "contract",
    "agreement",
    "non-disclosure",
    "nda",
    "legal",
    "regulation",
    "regulator",
    "regulatory",
    "counsel",
    "statute",
    "court",
    "judgment",
    "judgement",
    "litigation",
    "arbitration",
    "indemnity",
    "warrant",
    "warranty",
    "intellectual property",
    "trademark",
    "patent",
    "copyright",
    "license",
    "licence",
    "employment",
    "termination",
    "due diligence",
    "data protection",
    "privacy",
    "sanctions",
    "subpoena",
    "discovery",
    # Chinese
    "合同",
    "协议",
    "法律",
    "法规",
    "合规",
    "责任",
    "公约",
    "监管",
    "判决",
    "裁判",
    "诉讼",
    "仲裁",
    "赔偿",
    "知识产权",
    "商标",
    "专利",
    "著作权",
    "许可",
    "员工",
    "雇佣",
    "尽职调查",
    "数据保护",
    "隐私",
    "制裁",
    "保密",
)

def _looks_like_legal_request(
    text: str,
    skill_hint: Optional[str] = None,
    active_packs: Optional[list[str]] = None,
) -> bool:
    """Generic legal request detection. A request matches if either:

    - the orchestrator already proposed a skill via ``skill_hint``,
    - the text mentions a generic legal marker, or
    - any domain pack's markers (declared in ``pack.yaml``) fire on the text.

    ``active_packs`` is accepted for back-compat with the legacy aviation
    alias; it does NOT short-circuit the text check (pre-activating a pack
    doesn't mean every prompt is legal).
    """
    if skill_hint in SKILL_NAMES:
        return True
    lower = text.lower()
    if any(marker in lower for marker in _GENERIC_LEGAL_MARKERS):
        return True
    if detect_packs(text):
        return True
    return re.search(r"\b(law|nda|m&a|mna)\b", lower) is not None


_FINAL_SYNTHESIS_RE = re.compile(
    r"\b(final answer|final response|final synthesis|final summary|final draft|"
    r"final legal analysis|final chinese|final english|chinese final|english final|"
    r"draft final|prepare the final|produce the final|write the final|"
    r"synthesize all prior|synthesize the|integrate all|integrate the specialist|"
    r"using the outputs of|consolidate (?:the )?specialist|"
    r"structured legal memorandum|"
    r"chinese[- ]language (?:legal )?(?:analysis|response|answer|draft|memo)|"
    r"translate (?:the )?(?:final|answer|response|specialist)|"
    r"translation (?:into|to) chinese|"
    r"chinese[- ](?:final )?(?:legal )?(?:analysis|response|answer|draft))\b",
    re.IGNORECASE,
)

_CITATION_QC_RE = re.compile(
    r"\b("
    r"source verification|citation verification|citation quality|"
    r"citation (?:check|audit|qc)|pinpoint (?:audit|verification|check)|"
    r"verify (?:all )?(?:primary )?(?:legal )?(?:source|citation)"
    r")\b",
    re.IGNORECASE,
)

_LEGAL_CLAIM_RE = re.compile(
    r"\b("
    r"article|annex|standard|recommended practice|section|paragraph|page|"
    r"convention|icao|iata|faa|easa|caac|cfr|usc|regulation|treaty|"
    r"liability|compliance|jurisdiction|authority|must|shall|may|prohibit|"
    r"legal advice|counsel|citation|sources?"
    r")\b|[\u4e00-\u9fff].*(法律|法规|公约|责任|合规|来源|资料)",
    re.IGNORECASE,
)


# `_LEGAL_CLAIM_RE` above detects legal markers in a produced ANSWER. Deciding
# whether an incoming request about an uploaded file is legal work needs the
# request-side vocabulary instead — the words a user types, in either language.
_LEGAL_REQUEST_RE = re.compile(
    r"\b("
    r"contract|agreement|clause|lease|indemnit\w*|warrant(?:y|ies)|liabilit\w*|"
    r"enforceab\w*|breach|terminat\w*|nda|confidentialit\w*|governing law|"
    r"arbitrat\w*|litigat\w*|dispute|jurisdiction|complian\w*|regulat\w*|"
    r"statut\w*|legal|lawful|unlawful|counsel|due diligence|redline|"
    r"terms and conditions|policy|policies|obligation|licen[cs]\w*|tariff"
    r")\b"
    r"|(合同|协议|条款|条文|租赁|租约|赔偿|责任|违约|解约|终止|保密|竞业|"
    r"管辖|仲裁|诉讼|争议|合规|法律|法规|规章|监管|审查|尽调|尽职调查|"
    r"红线|批注|义务|许可|授权|条例|办法|规定)",
    re.IGNORECASE,
)


# Intake confidence below which a turn asks instead of guessing, and the number
# of consecutive clarifying turns allowed before the workflow proceeds on stated
# assumptions. A question is useful once, occasionally twice; a third in a row is
# the model stuck asking rather than working, and the user has to type the same
# facts again each time. Both are enforced in `_normalize_plan`, not asked for in
# the prompt, so a hedging planner cannot talk its way past them.
CLARIFY_CONFIDENCE_THRESHOLD = 0.8
MAX_CLARIFY_ROUNDS = 2


class WorkflowCancelled(Exception):
    """Raised internally when a web chat run is cancelled by the user."""


def _is_final_synthesis_task(task: AgentTask) -> bool:
    """Planner cleanup: final synthesis belongs to the orchestrator, not a sub-agent."""
    return bool(_FINAL_SYNTHESIS_RE.search(f"{task.title}\n{task.task}"))


def _is_citation_qc_task(task: AgentTask) -> bool:
    """Citation QC is fired post-synthesis as a /cite-check sub-agent, so
    drop any planner-emitted citation-QC task — the orchestrator owns it."""
    return bool(_CITATION_QC_RE.search(f"{task.title}\n{task.task}"))


# `coverage:` anywhere on a line, with an ASCII or full-width colon. The old
# pattern accepted it only at line start or after an ASCII ". ", and a Chinese
# task body ends its sentence with 。 — 248 of 431 coverage lines went unread,
# and dedup then merged 12 of 29 complex plans (8 collapsed to one specialist)
# whose tokens were in fact disjoint.
_COVERAGE_LINE_RE = re.compile(r"(?im)coverage\s*[:：]\s*([^\n]+?)\s*$")

# Jurisdiction fingerprints for the merge veto below. A task that clearly targets
# one legal system must never be dedup-merged into a sibling targeting a
# different one — comparative multi-jurisdiction work is this product's core
# case, and merging China/US/EU research into a single `brief` bundle collapses
# the whole multi-specialist path (and the FIG1 CoT display) on the fast tier.
_JURISDICTION_PATTERNS: dict[str, re.Pattern[str]] = {
    "CN": re.compile(
        r"\b(china|chinese|prc|p\.r\.c\.|caac)\b|中国|中华人民共和国|民航局|"
        r"民用航空法|国务院|全国人大",
        re.IGNORECASE,
    ),
    "US": re.compile(
        r"\b(u\.?s\.?|usa|united states|american|federal|circuit|scotus|"
        r"c\.?f\.?r\.?|u\.?s\.?c\.?|courtlistener|ntsb|faa)\b|美国|联邦",
        re.IGNORECASE,
    ),
    "EU": re.compile(
        r"\b(eu|e\.u\.|european union|european|eur-?lex|regulation\s*\(ec\)|"
        r"directive|ecj|cjeu|brussels)\b|欧盟|欧洲|欧共体",
        re.IGNORECASE,
    ),
    "UK": re.compile(r"\b(uk|u\.k\.|united kingdom|british|england|wales)\b|英国", re.IGNORECASE),
    "HK": re.compile(r"\b(hong kong|hk|hksar)\b|香港", re.IGNORECASE),
    "ET": re.compile(r"\b(ethiopia|ethiopian|ecaa)\b|埃塞俄比亚", re.IGNORECASE),
}


def _task_jurisdictions(task: AgentTask) -> set[str]:
    """Detect the legal systems a task targets, from its title + body.

    Used as a hard veto in coverage-dedup: two tasks whose jurisdiction sets are
    both non-empty and disjoint are distinct evidence bases and must not merge,
    even when they share a skill name and carry no explicit `coverage:` line."""
    text = f"{task.title}\n{task.task}"
    return {code for code, pat in _JURISDICTION_PATTERNS.items() if pat.search(text)}


def _task_coverage(task: AgentTask) -> set[str]:
    """Extract a normalised set of coverage tokens from a task's `coverage:` line.

    A planner that follows the instructions emits a line like
    ``coverage: schengen-borders, carrier-liability, evidence-preservation``
    inside the task body. We use that to detect overlapping mandates between
    sibling tasks during plan normalisation. Returns an empty set when no
    coverage line is present.
    """
    match = _COVERAGE_LINE_RE.search(task.task or "")
    if not match:
        return set()
    tokens = re.split(r"[,;/、，；]+", match.group(1))
    return {re.sub(r"\s+", "-", tok.strip().lower()).strip("-") for tok in tokens if tok.strip()}


def _strip_coverage_lines(text: str) -> str:
    """Remove `coverage:` line(s) from a task body.

    Used when merging overlapping mandates so the rewritten body carries a
    single unioned coverage line instead of several stale ones.
    """
    return _COVERAGE_LINE_RE.sub("", text or "").strip()


def _is_low_information_task(task: AgentTask) -> bool:
    """A task body too thin to execute: empty, or a bare label.

    The title is not evidence. The parser fills a missing title with a default,
    and flagging that default produced all 286 `workflow_plan_router_low_quality`
    events — 172 of them on tasks with a real body.
    """
    body = task.task.strip()
    return not body or bool(re.fullmatch(r"[A-Za-z][A-Za-z0-9_-]{0,40}", body))


def _should_audit_direct_response(text: str) -> bool:
    """Avoid citation-audit noise for pure greetings/status replies."""
    if not text.strip():
        return False
    if re.search(r"https?://|\bSources?\b|资料来源|来源", text, re.IGNORECASE):
        return True
    return bool(_LEGAL_CLAIM_RE.search(text))


def _is_general_plan(plan: WorkflowPlan) -> bool:
    return bool(plan.agent_tasks) and all(task.skill_name == GENERAL_TASK_SKILL for task in plan.agent_tasks)


def _should_run_cite_check(plan: Optional[WorkflowPlan], final_text: str) -> bool:
    """Skip the post-summary /cite-check skill for generic enquiries.

    Mirrors the legacy `audit_citations` skip semantics: chit-chat,
    greetings, status replies, and pure-general plans without any legal
    keyword get a free pass. The cite-check skill is fired only when the
    final answer plausibly contains legal substance.
    """
    if not final_text or not final_text.strip():
        return False
    if plan is not None and _is_general_plan(plan):
        return _should_audit_direct_response(final_text)
    if plan is None:
        return _should_audit_direct_response(final_text)
    return True


_CITE_CHECK_TASK_TEMPLATE = (
    "Audit the final answer below for citation accuracy with `stakes={stakes!r}`. "
    "Follow your SKILL.md workflow. Return the resulting Markdown "
    "verification report as your entire output.\n\n"
    "---\nFINAL ANSWER TO AUDIT\n---\n{final_text}"
)

# Map a report section's severity header → the ReportItem.severity value the
# frontend expects. Matches the English + 中文 headers from report-format.md.
_SEVERITY_SECTION_RE = re.compile(
    r"^#{1,4}\s*(CRITICAL|NUANCED|MODEL[-\s]?ONLY)\b", re.IGNORECASE | re.MULTILINE
)
_SEVERITY_KEY = {"CRITICAL": "critical", "NUANCED": "nuanced", "MODELONLY": "model_only"}
# A markdown table separator row: `|---|---|` (with optional alignment colons).
# This unambiguously marks a real table, so we key table detection off it rather
# than a fixed column-name header — models vary the columns (3-col
# Location|Issue|Assessment, 5-col Location|Issue|Claimed|Source says|Fix, and
# renamed variants like "Source says / verification status").
_TABLE_SEPARATOR_RE = re.compile(r"^\|(?:\s*:?-{1,}:?\s*\|)+\s*$")


def _map_report_columns(header_cells: list[str]) -> dict[str, int]:
    """Map a verdict-table header to column indices by name, with positional
    fallback. Handles the canonical 5-col table and degraded variants."""
    lower = [c.lower() for c in header_cells]

    def find(*kw: str) -> int:
        for i, h in enumerate(lower):
            if any(k in h for k in kw):
                return i
        return -1

    n = len(header_cells)
    loc = find("location", "位置", "出处")
    issue = find("issue", "问题", "类型")
    claimed = find("claim", "assessment", "proposition", "主张", "论述")
    # source / fix are name-only (may be -1); the row parser fills them from
    # trailing cells positionally when the data carries more columns than named.
    source = find("source", "status", "verification", "来源", "原文", "核验")
    fix = find("fix", "action", "remedi", "required", "correct", "修正", "处理")
    return {
        "location": loc if loc >= 0 else 0,
        "issue": issue if issue >= 0 else (1 if n > 1 else 0),
        "claimed": claimed if claimed >= 0 else (2 if n > 2 else n - 1),
        "source": source,  # may be -1 (absent)
        "fix": fix,  # may be -1 (absent) — resolved positionally per row
    }


def _row_to_item(
    cells: list[str], colmap: dict[str, int], severity: str
) -> Optional[dict[str, Any]]:
    """Turn one verdict-table data row into a structured item.

    Columns follow the canonical report order (Location · Issue · Claimed ·
    Source says · Fix). Named columns win; any extra trailing cells beyond
    ``claimed`` are assigned to fix (last) then source_says (positionally), which
    recovers rows whose data is wider than the header names (a common
    fast-model quirk, e.g. a 3-col header over 4-cell rows)."""

    def at(idx: int) -> str:
        if idx is None or idx < 0 or idx >= len(cells):
            return ""
        return _strip_md_emphasis(cells[idx])

    loc_i, iss_i, clm_i = colmap["location"], colmap["issue"], colmap["claimed"]
    src_i, fix_i = colmap["source"], colmap["fix"]
    location, issue, claimed = at(loc_i), at(iss_i), at(clm_i)
    source_says, fix = at(src_i), at(fix_i)

    used = {loc_i, iss_i, clm_i}
    if src_i >= 0:
        used.add(src_i)
    if fix_i >= 0:
        used.add(fix_i)
    trailing = [i for i in range(len(cells)) if i > clm_i and i not in used]
    if trailing and not fix:
        fix = _strip_md_emphasis(cells[trailing.pop()])
    if trailing and not source_says:
        source_says = _strip_md_emphasis(cells[trailing.pop()])

    # Skip an echoed column-header row that leaked into the body.
    if location.lower() == "location" and issue.lower() == "issue":
        return None
    if not (location or claimed or issue):
        return None
    return {
        "location": location,
        "issue_type": issue,
        "severity": severity,
        "claimed": claimed,
        "source_says": source_says,
        "fix": fix or None,
    }


def _parse_verdict_tables(chunk: str, severity: str) -> list[dict[str, Any]]:
    """Parse every markdown table in a severity chunk, keyed off the ``|---|``
    separator row: the nearest preceding pipe line is the header (→ column map),
    the lines below are data rows until a blank line / heading / next table.

    Keying off the separator (not a fixed column-name header) tolerates every
    observed shape — 3-col ``Location|Issue|Assessment``, 5-col
    ``Location|Issue|Claimed|Source says|Fix``, renamed variants, and echoed
    section headers that carry no separator of their own."""
    out: list[dict[str, Any]] = []
    lines = chunk.splitlines()
    n = len(lines)
    i = 0
    while i < n:
        if not _TABLE_SEPARATOR_RE.match(lines[i].strip()):
            i += 1
            continue
        # Header = nearest preceding non-blank pipe line (not itself a separator).
        header_cells: Optional[list[str]] = None
        j = i - 1
        while j >= 0:
            prev = lines[j].strip()
            if not prev:
                j -= 1
                continue
            if prev.startswith("|") and not _TABLE_SEPARATOR_RE.match(prev):
                header_cells = _split_md_table_row(lines[j])
            break
        if not header_cells:
            i += 1
            continue
        colmap = _map_report_columns(header_cells)
        # Consume data rows below the separator.
        k = i + 1
        while k < n:
            row = lines[k].strip()
            if not row or not row.startswith("|"):
                break
            if _TABLE_SEPARATOR_RE.match(row):
                k += 1
                continue
            item = _row_to_item(_split_md_table_row(lines[k]), colmap, severity)
            if item:
                out.append(item)
            k += 1
        i = k
    return out
_COVERAGE_COUNTS_RE = re.compile(
    r"Checked\s+(\d+)\s+of\s+(\d+)\s+citations\.\s*"
    r"(\d+)\s+confirmed;\s*"
    r"(\d+)\s+could not be retrieved;\s*"
    r"(\d+)\s+flagged as potential miscitations;\s*"
    r"(\d+)\s+flagged as misgrounded",
    re.IGNORECASE | re.DOTALL,
)
_COVERAGE_COUNTS_ZH_RE = re.compile(
    r"已核\s*(\d+)\s*条\s*/\s*共\s*(\d+)\s*条引证："
    r"(\d+)\s*条确认；\s*"
    r"(\d+)\s*条无法核验；\s*"
    r"(\d+)\s*条疑似错引；\s*"
    r"(\d+)\s*条疑似",
)
# The total-checked count, read separately for the coverage denominator.
_COVERAGE_TOTAL_RE = re.compile(
    r"Checked\s+(?:\w+\s+){0,2}?(\d+)(?:\s+of\s+(?:the\s+)?(\d+))?"
    r"|已核\s*(\d+)\s*条(?:\s*/\s*共\s*(\d+)\s*条)?",
    re.IGNORECASE,
)
# Keyword-anchored fuzzy coverage regex. Fast models paraphrase the canonical
# line ("9 supported" for "9 confirmed", "3 could not be verified" for
# "could not be retrieved", …) but keep the prescribed ORDER (confirmed ·
# could-not-check · miscited · misgrounded — report-format.md). We require a
# number IMMEDIATELY followed (within a few words) by each of the four verdict
# keywords, in order. This accepts order-preserving paraphrases and safely
# REJECTS non-canonical summaries (e.g. one that front-loads a "24 propositions"
# count or omits categories) rather than misreading their integers positionally.
_COVERAGE_FUZZY_RE = re.compile(
    r"(\d+)\D{0,40}?(?:confirmed|supported|verified|确认|支持)"
    r".{0,90}?(\d+)\D{0,45}?"
    r"(?:could\s*(?:not|n't)|unable|unverif|not\s+(?:be\s+)?(?:retrieved|verified|checked)|无法|未能)"
    r".{0,110}?(\d+)\D{0,55}?"
    r"(?:miscit|mis-?cit|flagged|correction|narrow|错引|疑似错引)"
    r".{0,110}?(\d+)\D{0,55}?"
    r"(?:misground|mis-?ground|unsupported|未支持|不支持|疑似)",
    re.IGNORECASE | re.DOTALL,
)


def _coverage_is_substantive(cov: Optional[dict[str, Any]]) -> bool:
    """True when a coverage object carries any non-zero count.

    An all-zero coverage is the hollow shell a fast model leaves when it calls
    ``cite_check_report_tool`` with skeleton args but writes the real analysis
    as prose; treat it as absent so the markdown parser can recover the truth."""
    if not cov:
        return False
    keys = ("total_cites", "confirmed", "could_not_check", "miscited", "misgrounded")
    return any(int(cov.get(k, 0) or 0) for k in keys)


def _split_md_table_row(line: str) -> list[str]:
    """Split a markdown table row `| a | b |` into trimmed cell strings."""
    s = line.strip()
    if s.startswith("|"):
        s = s[1:]
    if s.endswith("|"):
        s = s[:-1]
    return [c.strip() for c in s.split("|")]


def _parse_report_items_from_markdown(body: str) -> list[dict[str, Any]]:
    """Recover per-citation `items[]` from the rendered verification report.

    The structured JSON from ``cite_check_report_tool`` is the authoritative
    source, but fast models sometimes compose the report as free prose without
    calling the tool (so the ContextVar side-channel stays empty). The rendered
    markdown always carries the same severity-sectioned verdict tables
    (report-format.md), so we parse them here as the robust fallback. Best-effort
    and never raises — a parse miss simply yields no rows.
    """
    if not body:
        return []
    items: list[dict[str, Any]] = []
    # Split the report into severity sections, then parse every verdict table
    # within each (a section may hold an echoed section-header line plus the
    # real data table). Table detection is separator-driven, so variable column
    # counts are all handled.
    sections = list(_SEVERITY_SECTION_RE.finditer(body))
    if not sections:
        # No severity headings — parse the whole body as unlabelled tables.
        return _parse_verdict_tables(body, "nuanced")
    for idx, sec in enumerate(sections):
        raw_sev = sec.group(1).upper().replace("-", "").replace(" ", "")
        severity = _SEVERITY_KEY.get(raw_sev, "nuanced")
        start = sec.end()
        end = sections[idx + 1].start() if idx + 1 < len(sections) else len(body)
        items.extend(_parse_verdict_tables(body[start:end], severity))
    return items


def _strip_md_emphasis(cell: str) -> str:
    """Drop surrounding markdown bold/italic markers from a table cell."""
    s = cell.strip()
    # Collapse **bold** / *italic* markers but keep the inner text.
    return re.sub(r"\*\*(.+?)\*\*", r"\1", s).replace("**", "").strip()


def _parse_coverage_from_markdown(body: str) -> Optional[dict[str, int]]:
    """Recover the structured `coverage` counts from the report's coverage line.

    Two-tier: first the strict canonical regexes (EN + 中文); then a
    keyword-anchored fuzzy match that requires each of the four verdict counts
    (confirmed · could-not-check · miscited · misgrounded) to appear as a number
    immediately followed by its category keyword, in the prescribed order. Fast
    models paraphrase the wording but keep that order, so the fuzzy match
    recovers coverage the strict regex misses — while rejecting non-canonical
    summaries rather than misreading their integers positionally. Bold/italic
    emphasis is stripped first because models routinely bold the counts."""
    if not body:
        return None
    text = _strip_md_emphasis(body)
    m = _COVERAGE_COUNTS_RE.search(text) or _COVERAGE_COUNTS_ZH_RE.search(text)
    if m:
        checked, total, confirmed, cnc, miscited, misgrounded = (int(g) for g in m.groups())
        return {
            "total_cites": total,
            "confirmed": confirmed,
            "could_not_check": cnc,
            "miscited": miscited,
            "misgrounded": misgrounded,
        }
    fuzzy = _COVERAGE_FUZZY_RE.search(text)
    if not fuzzy:
        return None
    confirmed, cnc, miscited, misgrounded = (int(g) for g in fuzzy.groups())
    counts_total = confirmed + cnc + miscited + misgrounded
    # Denominator: prefer an explicit "of M" total when present and consistent;
    # otherwise sum the verdict counts.
    total = counts_total
    tm = _COVERAGE_TOTAL_RE.search(text)
    if tm:
        checked_en, of_en, checked_zh, of_zh = tm.groups()
        explicit = of_en or of_zh or checked_en or checked_zh
        if explicit and int(explicit) >= counts_total:
            total = int(explicit)
    return {
        "total_cites": total,
        "confirmed": confirmed,
        "could_not_check": cnc,
        "miscited": miscited,
        "misgrounded": misgrounded,
    }


def _parse_cite_check_payload(
    report_markdown: str,
    structured: Optional[dict[str, Any]] = None,
) -> dict[str, Any]:
    """Derive a citation_audit payload from the /cite-check skill's output.

    Back-compat surface: keeps ``ok`` and ``warnings`` so existing web-UI
    code (``citation_audit`` event handler) keeps working; adds richer
    fields (``report_markdown``, ``coverage_line``, ``do_not_file``) for
    consumers that want the full audit.

    ``structured`` is the exact JSON payload ``cite_check_report_tool``
    produced (recovered via the ContextVar side-channel, un-truncated). When
    present it authoritatively supplies ``items[]`` and ``coverage`` (the
    per-citation rows the FIG4 inspector renders) and lets ``do_not_file`` come
    straight from the report object instead of a markdown regex. The
    markdown-regex path remains the fallback for degraded / legacy output where
    no structured payload was captured.
    """
    body = report_markdown or ""
    coverage_match = re.search(
        r"(Checked\s+\d+\s+of\s+\d+\s+citations\..+?\.)|"
        r"(已核\s+\d+\s+条.+?。)",
        body,
        re.IGNORECASE | re.DOTALL,
    )
    coverage_line = ""
    if coverage_match:
        coverage_line = (coverage_match.group(0) or "").strip()
    has_critical = bool(re.search(
        r"^##\s*CRITICAL[\s\S]{0,1200}?\|\s*char\s",
        body,
        re.IGNORECASE | re.MULTILINE,
    ))
    # do_not_file is fail-safe: OR the structured flag with the rendered banner.
    # A fast model may call cite_check_report_tool with do_not_file=False yet
    # still write a "⚠️ DO NOT FILE" banner in its prose (hollow tool call); we
    # must never silently downgrade a DO-NOT-FILE the reader can plainly see.
    banner_dnf = bool(re.search(r"DO\s+NOT\s+FILE", body, re.IGNORECASE))
    structured_dnf = bool(structured.get("do_not_file")) if structured else False
    do_not_file = structured_dnf or banner_dnf
    items = list(structured.get("items") or []) if structured else []
    coverage = structured.get("coverage") if structured else None
    # Fallback (B1): when the tool JSON wasn't captured — fast models often
    # compose the report as prose without calling cite_check_report_tool — parse
    # the same items[]/coverage out of the rendered markdown so the FIG4 rows
    # still render. Structured payload wins ONLY when it is substantive; a
    # hollow (all-zero / empty) structured shell defers to the markdown.
    if not items:
        items = _parse_report_items_from_markdown(body)
    if not _coverage_is_substantive(coverage):
        # Drop a hollow all-zero structured shell so the UI never shows "0/0";
        # keep only genuinely substantive coverage from the markdown fallback.
        coverage = _parse_coverage_from_markdown(body) or None
    if not coverage_line and _coverage_is_substantive(coverage):
        confirmed = coverage.get("confirmed", 0)
        total = coverage.get("total_cites", 0)
        coverage_line = f"{confirmed}/{total} 通过 / confirmed"
    # A critical structured item is as authoritative as the markdown ## CRITICAL
    # section for the pass/fail signal.
    has_critical_item = any(
        (it.get("severity") == "critical") for it in items
    )
    ok = not has_critical and not has_critical_item and not do_not_file
    warnings: list[str] = []
    if coverage_line:
        warnings.append(coverage_line)
    if do_not_file:
        warnings.insert(0, "DO NOT FILE — pre-filing escalation threshold breached.")
    payload: dict[str, Any] = {
        "ok": ok,
        "warnings": warnings[:20],
        "report_markdown": body,
        "coverage_line": coverage_line,
        "do_not_file": do_not_file,
        "skill": "cite-check",
        "skipped": False,
    }
    if items:
        payload["items"] = items
    if _coverage_is_substantive(coverage):
        payload["coverage"] = coverage
    dnf_reason = structured.get("do_not_file_reason") if structured else None
    if not dnf_reason and do_not_file:
        # Pull the reason from the rendered banner ("⚠️ DO NOT FILE — <reason>")
        # when the structured payload didn't carry one (prose-only DNF).
        bm = re.search(
            r"DO\s+NOT\s+FILE[^\n—:-]*[—:-]+\s*([^\n]{3,300})",
            body,
            re.IGNORECASE,
        )
        if bm:
            dnf_reason = _strip_md_emphasis(bm.group(1)).strip(" *·—-")
    if dnf_reason:
        payload["do_not_file_reason"] = dnf_reason
    if structured is not None and structured.get("stakes"):
        payload["stakes"] = structured["stakes"]
    return payload


_FILED_STAKES_RE = re.compile(
    r"\b("
    r"motion to|complaint|brief|filed|filing|"
    r"oral argument|cert\.? petition|petitioner|respondent|"
    r"FRCP|FRAP|FRE\s*\d+|rule\s*11|"
    r"submitted to (?:the )?(?:court|tribunal|agency|commission|regulator)|"
    r"regulatory submission|"
    r"opinion letter|"
    r"起诉状|答辩状|上诉状|再审申请|司法建议|监管报送"
    r")\b",
    re.IGNORECASE,
)


def _audit_is_repairable(audit: Optional[dict[str, Any]]) -> bool:
    """True when the audit failed in a way a revision pass can act on.

    A skipped audit (no citations, disabled, generic enquiry) has nothing to
    repair, and a pass needs no repair. Everything else — a critical item, a
    DO-NOT-FILE verdict — is actionable, because the report names the claims.
    """
    if not audit or audit.get("skipped") or audit.get("ok"):
        return False
    return bool(audit.get("items")) or bool(audit.get("do_not_file")) or bool(
        (audit.get("report_markdown") or "").strip()
    )


def _infer_cite_check_stakes(plan: Optional[WorkflowPlan], final_text: str) -> str:
    """Choose `filed` vs `internal` from the answer and plan.

    `filed` triggers the strictest cite-check pass plus the DO NOT FILE
    escalation threshold inside `cite_check_report_tool`. We only escalate
    when the final text references a filing artefact or the original plan
    routed through a litigation skill.
    """
    if _FILED_STAKES_RE.search(final_text or ""):
        return "filed"
    if plan is not None:
        for task in plan.agent_tasks:
            # Only genuine drafting-for-submission skills escalate to `filed`.
            # `brief` is the generic research/topic skill behind almost every
            # legal answer, so escalating on it made nearly every advisory memo
            # trip the strict DO-NOT-FILE threshold (a false positive — an
            # internal route-launch/market-entry memo is not a court filing).
            # Genuine filing intent is still caught by `_FILED_STAKES_RE` above.
            if task.skill_name in {"legal-response", "litigation-analysis"}:
                return "filed"
    return "internal"


def _skipped_cite_check_payload(reason: str) -> dict[str, Any]:
    return {
        "ok": True,
        "warnings": [],
        "report_markdown": "",
        "coverage_line": "",
        "do_not_file": False,
        "skill": "cite-check",
        "skipped": True,
        "skip_reason": reason,
    }


_CITE_CHECK_LOOKUP_TOOLS = {
    # PRC primary + fallback
    "flk_npc_search",
    # US
    "courtlistener_search",
    "ecfr_search",
    "federal_register_search",
    "govinfo_search",
    # EU
    "eurlex_search",
    # Hosted web (Claude / GPT both expose as "web_search" / "web_fetch")
    "web_search",
    "web_fetch",
}


def _short_query_arg(arguments: dict[str, Any]) -> str:
    """Pull the most descriptive arg (query / keyword / text) and trim it."""
    if not isinstance(arguments, dict):
        return ""
    for key in ("query", "keyword", "law_name", "ad_number", "text", "url"):
        val = arguments.get(key)
        if isinstance(val, str) and val.strip():
            snippet = val.strip().replace("\n", " ")
            return snippet[:60] + ("…" if len(snippet) > 60 else "")
    return ""


# Source-retrieval tools whose calls contribute a "retrieved source" chip to a
# specialist's chain-of-thought step (FIG1 cot-srb). Mirrors the frontend
# SOURCE_TOOL_NAMES set in ``src/lib/toolLabels.ts`` — keep the two in sync.
_SOURCE_RETRIEVAL_TOOLS = {
    "web_search", "web_fetch", "web_search_call", "file_search_call",
    "web_search_tool_result", "web_fetch_tool_result", "retrieve_legal",
    "legal_source_search", "aviation_source_search", "drs_search", "drs_fetch",
    "easa_ad_search", "easa_ad_fetch", "easa_ear_index", "faa_title14_search",
    "ecfr_search", "federal_register_search", "govinfo_search",
    "courtlistener_search", "eurlex_search", "flk_npc_search", "flk_npc_fetch",
    "ccar_search", "ccar_fetch", "caac_local_search", "caac_local_fetch",
}


def _source_chip_from_tool_call(tool_call_data: dict[str, Any]) -> Optional[dict[str, str]]:
    """A retrieved-source chip (``{label, kind}``) for a specialist CoT step.

    Faithful to what the worker can observe: the provider runs tools inside its
    own loop, so tool *results* never surface here — the query keyword plus the
    source system is the honest signal. Returns ``None`` for non-source tool
    calls or calls without a descriptive query arg.
    """
    # Local tool_call events use "name"; hosted (web_search) events use
    # "tool_name" — accept either so both providers' source calls yield chips.
    name = (tool_call_data.get("name") or tool_call_data.get("tool_name") or "").strip()
    if not name:
        return None
    bare = name.split("__", 1)[0]
    is_source = (
        name in _SOURCE_RETRIEVAL_TOOLS
        or bare in _SOURCE_RETRIEVAL_TOOLS
        or bare.startswith("pkulaw_")
    )
    if not is_source:
        return None
    label = _short_query_arg(tool_call_data.get("arguments") or {})
    if not label:
        return None
    return {"label": label, "kind": bare}


def _cite_check_progress_from_tool_call(
    tool_call_data: dict[str, Any],
    state: dict[str, int],
    total_claims: int,
) -> Optional[dict[str, Any]]:
    """Translate a sub-agent tool_call into a singleton progress update.

    Returns ``None`` for tool calls that don't carry meaningful progress
    semantics (e.g. ``read_skill_reference`` while the agent is bootstrapping).
    The front-end keeps a single progress card and replaces it each time;
    these events are deliberately *not* append-only activity rows.
    """
    name = (tool_call_data.get("name") or "").strip()
    if not name:
        return None
    arguments = tool_call_data.get("arguments") or {}

    # Bare-name + namespaced (``pkulaw_law_search__search_article``) lookup
    # tools both count as a primary-source fetch.
    bare = name.split("__", 1)[0]
    is_lookup = (
        name in _CITE_CHECK_LOOKUP_TOOLS
        or bare in _CITE_CHECK_LOOKUP_TOOLS
        or bare.startswith("pkulaw_")
    )

    if name == "read_document":
        return {
            "stage": "reading_draft",
            "current": 0,
            "total": total_claims,
            "message": "Reading the draft attachment.",
            "tool_name": name,
        }
    if name == "extract_citations_tool":
        return {
            "stage": "extracting",
            "current": 0,
            "total": total_claims,
            "message": f"Extracting citations from the draft (≈{total_claims} expected).",
            "tool_name": name,
        }
    if name == "validate_citations_tool":
        return {
            "stage": "validating",
            "current": 0,
            "total": total_claims,
            "message": "Validating citation structure (reporter / 法释 / CELEX form).",
            "tool_name": name,
        }
    if is_lookup:
        state["fetched"] = state.get("fetched", 0) + 1
        current = min(state["fetched"], total_claims or state["fetched"])
        snippet = _short_query_arg(arguments)
        label = f"Fetching authority {current} of {total_claims or '?'}"
        if snippet:
            label = f"{label} — {bare}: {snippet}"
        else:
            label = f"{label} — {bare}"
        return {
            "stage": "fetching",
            "current": current,
            "total": total_claims,
            "message": label,
            "tool_name": name,
        }
    if name == "quote_roundtrip_tool":
        state["roundtrips"] = state.get("roundtrips", 0) + 1
        return {
            "stage": "roundtripping",
            "current": state["roundtrips"],
            "total": total_claims,
            "message": f"Round-tripping quote {state['roundtrips']} against primary source.",
            "tool_name": name,
        }
    if name == "provenance_audit_tool":
        return {
            "stage": "auditing",
            "current": state.get("roundtrips", 0),
            "total": total_claims,
            "message": "Auditing provenance tags for untagged claims.",
            "tool_name": name,
        }
    if name == "cite_check_report_tool":
        return {
            "stage": "composing",
            "current": total_claims,
            "total": total_claims,
            "message": "Composing the verification report.",
            "tool_name": name,
        }
    if name == "verification_log_append_tool":
        state["verified"] = state.get("verified", 0) + 1
        return {
            "stage": "logging",
            "current": state["verified"],
            "total": total_claims,
            "message": f"Logged {state['verified']} verified citation(s).",
            "tool_name": name,
        }
    return None


def _zero_citation_payload(reason: str) -> dict[str, Any]:
    """Fast-path payload when the final text contains zero extractable citations.

    Avoids spinning up a SkillAgent for a turn with nothing to audit. Still
    emits a ``citation_audit`` event so the front-end shows the audit ran.
    """
    return {
        "ok": True,
        "warnings": [],
        "report_markdown": (
            "# Citation Verification Report\n\n"
            "No citations detected in the final answer; skill auto-passed.\n"
        ),
        "coverage_line": "Checked 0 of 0 citations.",
        "do_not_file": False,
        "skill": "cite-check",
        "skipped": False,
        "skip_reason": reason,
    }


_CITATION_SURFACE_PATTERNS: tuple[tuple[str, "re.Pattern[str]"], ...] = (
    ("url", re.compile(r"https?://")),
    (
        "sources_heading",
        re.compile(
            r"资料来源|来源与核验|Sources\s*&\s*Verification|^\s*#{1,6}\s*Sources?\b",
            re.IGNORECASE | re.MULTILINE,
        ),
    ),
    # 《民法典》-style instrument titles.
    ("title_brackets", re.compile(r"《[^《》\n]{2,60}》")),
    # 法释〔2024〕5号 / （2023）京01民终12345号 / 国办发〔2023〕12号 / 第5号.
    (
        "document_number",
        re.compile(
            r"[〔\[（(]\s*(?:19|20)\d{2}\s*[〕\]）)][^\n]{0,40}?号"
            r"|第?\s*[0-9０-９一二三四五六七八九十百千万零]+\s*号"
        ),
    ),
    # 第X条/款/项 pinpoints (Arabic or CJK numerals).
    ("cjk_pinpoint", re.compile(r"第\s*[0-9０-９一二三四五六七八九十百千万零]+\s*[条款项]")),
)


def _citation_surface_signals(text: str) -> list[str]:
    """Citation-shaped surface signals the regex extractor may have missed.

    The zero-extraction fast path must not auto-pass an answer that visibly
    carries sources (资料来源 heading, 《…》 instrument titles, …号 document
    numbers, 第…条 pinpoints, URLs) just because ``extract_citations`` found
    nothing — canonical PRC typography is exactly where the extractor is
    weakest. Returns the matched signal names (empty list → safe to auto-pass).
    """
    if not text:
        return []
    return [name for name, pattern in _CITATION_SURFACE_PATTERNS if pattern.search(text)]


def _simple_direct_response(user_message: str) -> Optional[str]:
    """Local guardrail: never spend specialist tokens on obvious chat turns."""
    text = " ".join(user_message.strip().split())
    if not text:
        return "What would you like help with?"
    lower = text.lower().strip(" ?!.。！？")
    greeting_patterns = (
        r"^(hi|hello|hey|yo|good morning|good afternoon|good evening)$",
        r"^(hi|hello|hey|yo)[,\s].*",
        r"^how are you( today)?$",
        r"^how's it going$",
        r"^how is it going$",
        r"^what's up$",
        r"^thanks?$",
        r"^thank you$",
    )
    if any(re.match(pattern, lower) for pattern in greeting_patterns):
        return "I'm doing okay, and ready to help. What would you like to work on?"
    if re.fullmatch(r"(你好|您好|嗨|哈喽|在吗|谢谢|谢了)[。！？!?\s]*", text):
        return "我在，随时可以帮你处理法律问题、合同或文件。你想先看哪一件事？"
    return None


def heuristic_plan(user_message: str, skill_hint: Optional[str] = None) -> WorkflowPlan:
    """Fallback plan when routing fails: a greeting, or one single-pass task.

    It used to emit a fixed three-specialist aviation-operations template
    (airline crew, carrier liability) whenever "international" or "convention"
    appeared anywhere in the digest. A failed planner is a reason to let the
    author work from the request directly, not to guess a decomposition.
    """
    language = _detect_language(user_message)
    direct_response = _simple_direct_response(user_message)
    if direct_response:
        return WorkflowPlan(
            title="Direct response",
            user_language=language,
            execution_mode="direct_answer",
            complexity="simple",
            direct_response=direct_response,
            routing_reason="Local fallback recognized a simple conversational turn.",
        )
    if skill_hint in SKILL_NAMES and skill_hint != GENERAL_TASK_SKILL:
        task = _make_task(skill_hint, f"{skill_hint} analysis", user_message)
    elif _looks_like_legal_request(user_message, skill_hint):
        task = _make_task("brief", "Legal question", user_message)
    else:
        task = _make_general_task(user_message)
    return WorkflowPlan(
        title="Legal question" if task.skill_name != GENERAL_TASK_SKILL else "General answer",
        user_language=language,
        complexity="standard",
        routing_reason="Planner unavailable; answering in one pass from the request.",
        agent_tasks=[task],
    )


def _render_available_files(
    available_files: Optional[Iterable[dict]],
    generated_files: Optional[Iterable[dict]] = None,
) -> str:
    """List the chat's files that are NOT inlined this turn, with how to open them.

    Uploads and generated deliverables both. Generated files used to be absent
    from the digest, so the model could not tell the latest version of a
    deliverable from a superseded one (an edit in one chat started from an
    older workbook and lost three rows). Each generated file names what it was
    derived from, newest first. The listing sits in the stable, cacheable part
    of the digest; the bytes are fetched only when a tool asks for them.
    """
    blocks: list[str] = []
    uploads = [entry for entry in (available_files or []) if str(entry.get("path", "")).strip()]
    if uploads:
        lines = [
            "Files the user uploaded in this chat. They are NOT inlined in this "
            "turn — open the ones you need with `read_document`, `inspect_docx`, "
            "`inspect_xlsx`, `view_image`, or `render_pdf_pages`:"
        ]
        for entry in uploads:
            path = str(entry.get("path", "")).strip()
            name = entry.get("name") or Path(path).name
            size = entry.get("size")
            size_note = f" ({size:,} bytes)" if isinstance(size, int) and size > 0 else ""
            lines.append(f"- {name}{size_note} — {path}")
        blocks.append("\n".join(lines))
    generated = [entry for entry in (generated_files or []) if str(entry.get("path", "")).strip()]
    if generated:
        lines = [
            "Files this chat has produced, newest first. When the user refers to "
            "\"the document\" or asks for another change, start from the newest "
            "version unless they name an older one:"
        ]
        for entry in generated:
            path = str(entry.get("path", "")).strip()
            name = entry.get("name") or Path(path).name
            when = str(entry.get("created_at") or "")[:16].replace("T", " ")
            origin = entry.get("derived_from") or []
            note = f" (from {', '.join(Path(o).name for o in origin)})" if origin else ""
            stamp = f" [{when}]" if when else ""
            lines.append(f"- {name}{stamp}{note} — {path}")
        blocks.append("\n".join(lines))
    return "\n\n".join(blocks)


def _build_context_parts(
    memory_summary: str,
    recent_messages: Iterable[ChatMessage],
    *,
    budget_tokens: Optional[int] = None,
    available_files: Optional[Iterable[dict]] = None,
    generated_files: Optional[Iterable[dict]] = None,
) -> tuple[str, str]:
    """The within-chat context digest as (stable prefix, volatile tail).

    The prefix — project block, files, recent transcript — only grows from turn
    to turn, so it is sent as a cacheable block ahead of the request. The
    rolling summary is rewritten after every turn, so it goes in the volatile
    tail: inside the prefix it changed the cached bytes every turn. The recent
    transcript fills what the budget leaves newest-first (older turns drop out —
    they survive in the summary); token-budgeted rather than a fixed message
    count so long pasted documents stay intact.
    """
    from .context import (
        context_budget_for,
        estimate_tokens,
        per_message_cap_for,
        render_recent,
        select_recent_within_budget,
    )

    if budget_tokens is None:
        try:
            budget_tokens = context_budget_for(current_settings())
        except Exception:  # noqa: BLE001
            budget_tokens = 16_000

    # Durable cross-chat project context (brief + salient memory + rolling
    # summary): standing jurisdiction pins and prior decisions for every author.
    try:
        from .projects import active_project_context_block

        project_block = active_project_context_block().strip()
    except Exception:  # noqa: BLE001 — context injection must never break a run
        project_block = ""

    lines: list[str] = []
    if project_block:
        lines.append(project_block)
    files_block = _render_available_files(available_files, generated_files)
    if files_block:
        # Changes only when a file is uploaded or produced: stable enough to
        # cache with the transcript.
        lines.append(files_block)
    summary = memory_summary.strip()
    remaining = max(
        1_000,
        budget_tokens
        - estimate_tokens(summary)
        - estimate_tokens(project_block)
        - estimate_tokens(files_block),
    )
    windowed = select_recent_within_budget(recent_messages, remaining)
    if windowed.kept:
        header = "Recent visible chat transcript:"
        if windowed.dropped:
            header += (
                f" (showing the {len(windowed.kept)} most recent of "
                f"{len(windowed.kept) + len(windowed.dropped)} turns; "
                "earlier turns are captured in the memory summary)"
            )
        lines.append(header)
        lines.append(render_recent(windowed.kept, per_message_cap_for(budget_tokens)))
    volatile = f"Current chat memory summary:\n{summary}" if summary else ""
    return "\n\n".join(lines), volatile


def _build_context_block(
    memory_summary: str,
    recent_messages: Iterable[ChatMessage],
    *,
    budget_tokens: Optional[int] = None,
    available_files: Optional[Iterable[dict]] = None,
    generated_files: Optional[Iterable[dict]] = None,
) -> str:
    """The whole digest as one text: the stable prefix, then the summary."""
    stable, volatile = _build_context_parts(
        memory_summary,
        recent_messages,
        budget_tokens=budget_tokens,
        available_files=available_files,
        generated_files=generated_files,
    )
    return "\n\n".join(part for part in (stable, volatile) if part)


#: Token budgets for the planner's view of the chat. The router classifies
#: one turn; the decomposer splits a complex one and needs a little more.
ROUTER_CONTEXT_TOKENS = 4_000
DECOMPOSER_CONTEXT_TOKENS = 8_000


def _routing_view(
    memory_summary: str,
    recent_messages: Iterable[ChatMessage],
    *,
    budget_tokens: int,
    available_files: Optional[Iterable[dict]] = None,
    generated_files: Optional[Iterable[dict]] = None,
) -> str:
    """The chat as the router and decomposer see it: newest turns first.

    Both used to take the author's digest and cut it from the head
    (`[:12000]` / `[:16000]`). That digest runs oldest-first and ends with the
    memory summary, so 31% of router calls and 21% of decomposer calls lost
    the newest turns and the summary — one clarify turn asked for facts the
    user had given in the turn before. Here the budget is spent on the summary
    and the newest turns first, older turns drop out whole, and nothing is
    char-sliced.
    """
    from .context import estimate_tokens, render_recent, select_recent_within_budget

    summary = (memory_summary or "").strip()
    names = [
        str(entry.get("name") or Path(str(entry.get("path", ""))).name)
        for entry in [*(generated_files or []), *(available_files or [])]
        if entry.get("path")
    ]
    files_line = ("Files in this chat: " + ", ".join(names[:30])) if names else ""
    summary_budget = min(estimate_tokens(summary), budget_tokens // 3)
    if summary and estimate_tokens(summary) > summary_budget:
        # Keep the summary's newest part: rolling summaries append at the end.
        keep_chars = max(200, int(len(summary) * summary_budget / max(1, estimate_tokens(summary))))
        summary = "…" + summary[-keep_chars:]
    remaining = max(500, budget_tokens - estimate_tokens(summary) - estimate_tokens(files_line))
    windowed = select_recent_within_budget(recent_messages, remaining)
    parts: list[str] = []
    if summary:
        parts.append(f"Chat memory summary:\n{summary}")
    if files_line:
        parts.append(files_line)
    if windowed.kept:
        header = "Most recent turns (oldest of these first, newest last):"
        if windowed.dropped:
            header += f" [{len(windowed.dropped)} earlier turns omitted; see the summary]"
        parts.append(header + "\n" + render_recent(windowed.kept, max(400, remaining // 2)))
    return "\n\n".join(parts)


def _artifact_url(settings: Settings, path: str) -> str:
    p = Path(path)
    try:
        rel = p.resolve().relative_to(load_settings().outputs_dir.resolve())
        return f"/api/artifacts/{rel.as_posix()}"
    except Exception:
        pass
    try:
        rel = p.resolve().relative_to(settings.outputs_dir.resolve())
        return f"/api/artifacts/{rel.as_posix()}"
    except Exception:
        return f"/api/artifacts/{p.name}"


def write_followup_pdf(
    settings: Settings,
    source_text: str,
    title: str = "",
    *,
    watermark: Optional[str] = None,
) -> dict[str, str]:
    """Export an answer as a PDF in the answer's own language.

    The title is the caller's, else the answer's first heading (which is then
    not printed twice), else a generic title in the answer's language; the
    not-legal-advice notice is the PDF's Subject metadata in that language.
    18 of 19 exported DOCX and 5 of 5 PDFs used to open with an English
    "Legal Analysis" and an English disclaimer whatever the answer said.
    """
    from .documents.writers._text import detect_lang, disclaimer_for, export_title, strip_title_heading

    lang = detect_lang(source_text)
    title = (title or "").strip() or export_title(source_text, lang=lang)
    settings.outputs_dir.mkdir(parents=True, exist_ok=True)
    filename = f"legal_analysis_{uuid.uuid4().hex[:8]}.pdf"
    output_path = settings.outputs_dir / filename
    write_pdf_document(
        output_path=output_path,
        title=title,
        body_markdown=strip_title_heading(source_text, title),
        disclaimer=disclaimer_for(lang),
        lang=lang,
        watermark=watermark,
    )
    return {
        "filename": filename,
        "path": str(output_path.resolve()),
        "url": _artifact_url(settings, str(output_path)),
    }


def write_followup_docx(
    settings: Settings,
    source_text: str,
    title: str = "",
    *,
    watermark: Optional[str] = None,
) -> dict[str, str]:
    """Export an answer as a DOCX in the answer's own language.

    Same title rule as :func:`write_followup_pdf`; the notice goes into the
    document properties, never the body.
    """
    from .documents.writers._text import detect_lang, disclaimer_for, export_title, strip_title_heading

    lang = detect_lang(source_text)
    title = (title or "").strip() or export_title(source_text, lang=lang)
    settings.outputs_dir.mkdir(parents=True, exist_ok=True)
    filename = f"legal_analysis_{uuid.uuid4().hex[:8]}.docx"
    output_path = settings.outputs_dir / filename
    write_docx_document(
        output_path=output_path,
        title=title,
        sections=[("", strip_title_heading(source_text, title))],
        disclaimer=disclaimer_for(lang),
        lang=lang,
        watermark=watermark,
    )
    return {
        "filename": filename,
        "path": str(output_path.resolve()),
        "url": _artifact_url(settings, str(output_path)),
    }


def _turn_language(
    user_message: str,
    recent_messages: Optional[Iterable[ChatMessage]],
    default_language: str = "zh",
) -> str:
    """The language this turn's answer is written in.

    From the request's own script share; a request too short to tell ("ok",
    a bare filename) follows the user's last substantive message, then the
    configured default. Never "English because the planner omitted it".
    """
    default = "Chinese" if default_language == "zh" else "English"

    def signal(text: str) -> int:
        cleaned = re.sub(r"https?://\S+|\S+\.(?:docx?|xlsx?|pptx?|pdf|txt|md)\b", " ", text or "")
        return len(re.findall(r"[一-鿿]", cleaned)) + len(re.findall(r"[A-Za-z]{2,}", cleaned))

    if signal(user_message) >= 3:
        return answer_contract.detect_language(user_message, default)
    for message in reversed(list(recent_messages or [])):
        if message.role == "user" and signal(message.content) >= 3:
            return answer_contract.detect_language(message.content, default)
    return default


# Citation-shaped legal substance in a router-written direct answer: an
# instrument title, an article/section pinpoint, a document number. Narrower
# than `_LEGAL_CLAIM_RE`, which also fires on "may", "must", "authority" and
# "sources" and matched 49 of 101 direct answers.
_DIRECT_LEGAL_SUBSTANCE_RE = re.compile(
    r"《[^《》\n]{2,40}(?:法|条例|规定|办法|解释|公约|协定|规则)》"
    r"|第\s*[0-9０-９一二三四五六七八九十百千零]+\s*条"
    r"|\b(?:Article|Art\.|Section|§)\s*\d+"
    r"|\b\d+\s+(?:U\.S\.C\.|C\.F\.R\.)"
    r"|[〔\[（(]\s*(?:19|20)\d{2}\s*[〕\]）)][^\n]{0,20}?号",
    re.IGNORECASE,
)


def _strip_trailing_commas(text: str) -> str:
    """``text`` without commas that directly precede ``}`` or ``]`` outside strings."""
    out: list[str] = []
    in_string = escaped = False
    for index, char in enumerate(text):
        if in_string:
            if escaped:
                escaped = False
            elif char == "\\":
                escaped = True
            elif char == '"':
                in_string = False
        elif char == '"':
            in_string = True
        elif char == "," and text[index + 1 :].lstrip()[:1] in ("}", "]"):
            continue
        out.append(char)
    return "".join(out)


def _direct_answer_has_legal_substance(text: str) -> bool:
    return bool(_DIRECT_LEGAL_SUBSTANCE_RE.search(text or ""))


def _routable_skills() -> list[tuple[str, str]]:
    """(name, one-line purpose) for each skill a plan may name.

    In-tree legal methods plus `general-answer`. `cite-check` runs on every
    answer automatically, and an external procedure (ppt-master) is reached
    through its own tool (`create_deck`), so neither is offered as a plan task.
    """
    from .skills import internal_skill_names, skill_manifest

    out: list[tuple[str, str]] = []
    for name in internal_skill_names():
        if name == "cite-check":
            continue
        try:
            description = str(skill_manifest(name).get("description") or "")
        except Exception:  # noqa: BLE001
            description = ""
        first = re.split(r"(?<=[.。])\s", description.strip(), maxsplit=1)[0]
        out.append((name, first[:220]))
    out.append((GENERAL_TASK_SKILL, "Non-legal work: documents, spreadsheets, decks, general research and questions."))
    return out


def _skills_block() -> str:
    return "\n".join(f"- `{name}` — {purpose}" for name, purpose in _routable_skills())


_ROUTER_SYSTEM = (
    "You route and plan one turn of a legal assistant's chat. Return only valid JSON."
)

_ROUTER_INSTRUCTIONS = f"""Route this chat turn and plan it.

Execution modes:
- direct_answer: greetings, questions about this assistant, or a short follow-up
  you can answer with confidence from the conversation alone. Never a legal
  proposition that needs a source, and never a request that needs a tool.
- clarify: a fact that changes the answer is missing, and the answer can
  neither proceed on a stated assumption nor briefly cover each reading. Ask
  only for that fact.
- agent_workflow: everything else — legal research, reading or editing files,
  drafting, any file deliverable, anything that needs a tool. If unsure
  whether the system can do it, choose agent_workflow; never refuse on
  capability grounds.

Complexity of an agent_workflow turn:
- simple: one narrow question, one instrument or one issue, a quick lookup, a
  short edit, or a follow-up that extends one prior answer.
- standard: a focused analysis with related sub-parts that share one evidence
  base (one contract, one jurisdiction, one incident).
- complex: several distinct issues that each need their own sustained research
  in a different body of law or jurisdiction — a multi-section memo, a
  market-entry analysis, a long outline of distinct topics. A short comparison
  (a few jurisdictions, one question each) is standard, and so is any request
  that asks for a short answer.

For a simple or standard turn, return exactly one agent_task: the assignment
for the one author who will answer.
- skill_name: the method that fits, from the list below.
- title: a short title.
- task: what to do, in one or two sentences, grounded in the newest turns.
For a complex turn, leave agent_tasks empty: a planner splits it next.

integration_instructions: the points the answer must cover, taken from the
user's own words and the newest turns, in the user's order.
fact_conflicts: facts in the request or the recent turns that contradict each
other (dates, amounts, parties, sequence of events), one line each; [] if none.
intake_confidence (0.0-1.0): how confident you are that you have the facts the
work needs (jurisdiction, governing law, the party's role, the operative date,
the document in question). Below {CLARIFY_CONFIDENCE_THRESHOLD} a clarify turn
asks; at or above it the turn proceeds on stated assumptions. It is read as a
number.

JSON shape:
{{
  "title": "short title",
  "execution_mode": "agent_workflow | direct_answer | clarify",
  "complexity": "simple | standard | complex",
  "direct_response": "the exact reply (direct_answer) or the question (clarify), in the user's language; empty otherwise",
  "routing_reason": "brief reason",
  "intake_confidence": 0.0,
  "agent_tasks": [{{"skill_name": "brief", "title": "…", "task": "…"}}],
  "integration_instructions": "…",
  "fact_conflicts": []
}}
"""

_DECOMPOSER_SYSTEM = (
    "You plan the parallel research for a complex legal request. Return only valid JSON."
)

_DECOMPOSER_INSTRUCTIONS = """Plan this complex request for parallel research.

1. issues — the distinct legal issues (争点) the answer must resolve, each:
   {"id": "I1", "issue": "the precise question", "claim_basis": "the claim
   basis or rule to test (请求权基础 / legal basis)", "elements": ["…"],
   "facts_needed": ["facts the outcome depends on"], "facts_assumed":
   ["assumptions the answer must state"], "jurisdiction": "CN | US | EU | …",
   "evidence_surface": "where the law and facts live: statutes, cases, a
   regulator's site, the uploaded contract …"}
2. agent_tasks — group the issues into specialists whose evidence surfaces
   are disjoint, so each researches its slice in depth: target 2-3, hard max
   3, all parallel unless one genuinely needs another's findings. Each:
   {"id": "task_1", "skill_name": "…", "title": "…", "issue_ids": ["I1"],
   "task": "what to research and produce, self-contained, ending with a line
   `coverage: token1, token2`", "depends_on": []}
   - coverage tokens are short kebab-case names of what the specialist owns;
     no token appears in two tasks.
   - A specialist works through its issues element by element, records the
     strongest counter-reading and the authorities it considered and
     excluded; do not instruct it to write polished prose.
   - No synthesis, final-answer, translation or citation-check specialist: the
     author integrates the bundles, and a citation audit runs automatically.
   - Task text may be in whichever language fits the sources (中文 for PRC
     law); quote sources in their original language.
3. integration_instructions — the points the final answer must cover, in the
   user's order.
4. fact_conflicts — contradicting facts, one line each; [] if none.

JSON shape:
{"title": "…", "routing_reason": "…", "issues": [ … ], "agent_tasks": [ … ],
 "integration_instructions": "…", "fact_conflicts": []}
"""


class WorkflowExecutor:
    _run_store: Optional[RunStore] = None

    def __init__(self, settings: Optional[Settings] = None, provider: Optional[Provider] = None) -> None:
        setup_logging()
        self.settings = settings or current_settings()
        # Per-stream resolved settings (with auto-detected packs merged in).
        # Reset at the top of every stream() call.
        self._resolved = self.settings
        self.provider = provider or build_provider(self.settings)
        if provider is not None:
            # An injected provider (tests, embedders) serves every role.
            self.planner_provider = provider
            self.decomposer_provider = provider
            self.cite_check_provider = provider
            self.integration_provider = provider
            self.author_provider = provider
        else:
            self.planner_provider = self._role_provider("router")
            self.decomposer_provider = self._role_provider("decomposer")
            self.cite_check_provider = self._role_provider("cite_check")
            self.integration_provider = self._role_provider("integrator")
            # The default row is the chat's own model and effort, which is the
            # primary provider itself; an override in role_profiles now applies.
            self.author_provider = self._role_provider("author")

    def _role_provider(self, role: str) -> Provider:
        """The provider for one role, per ``Settings.role_profiles``."""
        role_settings, fast = self.settings.for_role(role)
        if role_settings is self.settings and not fast:
            return self.provider
        try:
            return build_provider(role_settings, fast=fast)
        except Exception as exc:  # noqa: BLE001 — a missing fast tier degrades to the main model
            log_workflow_event(
                "workflow_role_provider_fallback", {"role": role, "reason": str(exc)[:500]}
            )
            return self.provider

    def plan(
        self,
        user_message: str,
        *,
        skill_hint: Optional[str] = None,
        recent_messages: Optional[list[ChatMessage]] = None,
        memory_summary: str = "",
        available_files: Optional[list[dict]] = None,
        generated_files: Optional[list[dict]] = None,
        has_attachments: bool = False,
        clarify_rounds_used: int = 0,
        user_language: Optional[str] = None,
    ) -> WorkflowPlan:
        """One routing call; a second (decomposition) call only for complex turns.

        Every agent_workflow turn used to pay for a decomposition call (p50
        12.3 s, 8.7% of all run time) that the single-pass author mostly
        ignored, and the router's schema showed `"agent_tasks": []` with no item
        shape, so its tasks never passed the quality check. The router now
        returns the one task a simple or standard turn needs.
        """
        language = user_language or _turn_language(
            user_message, recent_messages, self.settings.default_language
        )
        view = _routing_view(
            memory_summary,
            recent_messages or [],
            budget_tokens=ROUTER_CONTEXT_TOKENS,
            available_files=available_files,
            generated_files=generated_files,
        )
        try:
            result = self._run_planner(
                self._routing_prompt(
                    user_message,
                    skill_hint=skill_hint,
                    context_block=view,
                    has_attachments=has_attachments,
                ),
                system=_ROUTER_SYSTEM,
            )
            routed = self._resolve_clarify(
                self._normalize_plan(self._parse_plan_result(result)),
                clarify_rounds_used,
            ).model_copy(update={"user_language": language})
            log_workflow_event(
                "workflow_plan_routed",
                {
                    "execution_mode": routed.execution_mode,
                    "intake_confidence": routed.intake_confidence,
                    "complexity": routed.complexity,
                    "routing_reason": routed.routing_reason,
                    "agent_count": len(routed.agent_tasks),
                    "user_language": language,
                    "provider": result.provider,
                    "model": result.model,
                    "usage": result.usage,
                },
            )
        except Exception as exc:
            if is_out_of_money_error(exc):
                raise
            log_workflow_event("workflow_plan_router_fallback", {"reason": str(exc)[:500]})
            return self._normalize_plan(heuristic_plan(user_message, skill_hint)).model_copy(
                update={"user_language": language}
            )

        if routed.execution_mode == "direct_answer":
            reroute = ""
            if not routed.direct_response.strip():
                # An empty direct answer is a routing failure, not a reply: it
                # shipped "I can help with that." in English.
                reroute = "empty_direct_response"
            elif _direct_answer_has_legal_substance(routed.direct_response):
                # The router writes direct answers on the fast model with no
                # tools; a legal proposition belongs to the author, who can
                # check it.
                reroute = "legal_substance"
            if reroute:
                log_workflow_event("workflow_direct_answer_rerouted", {"reason": reroute})
                routed = routed.model_copy(
                    update={"execution_mode": "agent_workflow", "direct_response": ""}
                )
        if routed.execution_mode != "agent_workflow":
            return routed
        if routed.complexity == "complex":
            return self._agent_workflow_plan(
                user_message,
                routed=routed,
                skill_hint=skill_hint,
                context_block=_routing_view(
                    memory_summary,
                    recent_messages or [],
                    budget_tokens=DECOMPOSER_CONTEXT_TOKENS,
                    available_files=available_files,
                    generated_files=generated_files,
                ),
            )
        return self._single_task_plan(routed, user_message, skill_hint)

    def _single_task_plan(
        self, routed: WorkflowPlan, user_message: str, skill_hint: Optional[str]
    ) -> WorkflowPlan:
        """A simple/standard plan with exactly one usable task."""
        usable = [task for task in routed.agent_tasks if not _is_low_information_task(task)]
        if usable:
            return routed.model_copy(update={"agent_tasks": usable[:1]})
        hint = skill_hint if skill_hint in SKILL_NAMES and skill_hint != "cite-check" else None
        skill = hint or (
            routed.agent_tasks[0].skill_name
            if routed.agent_tasks
            else ("brief" if _looks_like_legal_request(user_message, skill_hint) else GENERAL_TASK_SKILL)
        )
        task = _make_task(skill, routed.title or "Answer", user_message)
        log_workflow_event(
            "workflow_plan_task_filled",
            {"skill_name": skill, "router_task_count": len(routed.agent_tasks)},
        )
        return routed.model_copy(update={"agent_tasks": [task]})

    def _run_planner(self, prompt: str, *, system: str, provider: Optional[Provider] = None) -> Any:
        # JSON mode rather than a strict schema: OpenAI strict mode requires
        # additionalProperties=false through every nested object, and the local
        # parser/normalizer validates and sanitizes the result anyway. JSON mode
        # only guarantees the syntax, which is what kept failing.
        return (provider or self.planner_provider).run(
            system=system,
            messages=[{"role": "user", "content": prompt}],
            tools=[],
            max_iterations=1,
            structured_output=JSON_OBJECT,
        )

    def _routing_prompt(
        self,
        user_message: str,
        *,
        skill_hint: Optional[str] = None,
        context_block: str = "",
        has_attachments: bool = False,
    ) -> str:
        # Static text first so the prefix caches; the turn-specific parts after.
        prompt = _ROUTER_INSTRUCTIONS + "\nAvailable skill_name values:\n" + _skills_block() + "\n"
        if has_attachments:
            # The router sees filenames, not bytes. A turn with attachments
            # always needs tools; classify the skill on the merits.
            prompt += (
                "\nATTACHMENTS: this turn carries uploaded file(s), listed with the "
                "request. You see their names, not their contents, so choose "
                "agent_workflow. A legal question about a file, or reviewing, "
                "drafting or checking a legal document, is legal work, not "
                "general-answer.\n"
            )
        if skill_hint:
            prompt += f"\nUser selected skill hint: {skill_hint}\n"
        if context_block:
            prompt += f"\nConversation so far:\n{context_block}\n"
        return f"{prompt}\nCurrent user request:\n{user_message}"

    def _agent_workflow_plan(
        self,
        user_message: str,
        *,
        routed: Optional[WorkflowPlan] = None,
        skill_hint: Optional[str] = None,
        context_block: str = "",
        complexity: str = "complex",
    ) -> WorkflowPlan:
        """Decompose a complex turn into issues and 2-3 parallel specialists."""
        prompt = _DECOMPOSER_INSTRUCTIONS + "\nAvailable skill_name values:\n" + _skills_block() + "\n"
        if skill_hint:
            prompt += f"\nThe user selected this skill hint: {skill_hint}\n"
        if context_block:
            prompt += f"\nConversation so far:\n{context_block}\n"
        if routed is not None and routed.integration_instructions.strip():
            prompt += f"\nThe router noted the answer must cover:\n{routed.integration_instructions.strip()}\n"
        prompt += f"\nCurrent user request:\n{user_message}"
        language = routed.user_language if routed is not None else _detect_language(user_message)
        try:
            result = self._run_planner(
                prompt, system=_DECOMPOSER_SYSTEM, provider=self.decomposer_provider
            )
            parsed = self._parse_plan_result(result)
            update: dict[str, Any] = {
                "execution_mode": "agent_workflow",
                "complexity": complexity,
                "user_language": language,
            }
            if routed is not None:
                update["intake_confidence"] = routed.intake_confidence
                update["fact_conflicts"] = list(
                    dict.fromkeys([*routed.fact_conflicts, *parsed.fact_conflicts])
                )
                if not parsed.integration_instructions.strip():
                    update["integration_instructions"] = routed.integration_instructions
            parsed = parsed.model_copy(update=update)
            if parsed.agent_tasks:
                log_workflow_event(
                    "workflow_plan_decomposed",
                    {
                        "agent_count": len(parsed.agent_tasks),
                        "issue_count": len(parsed.issues),
                        "complexity": complexity,
                        "provider": result.provider,
                        "model": result.model,
                        "usage": result.usage,
                    },
                )
                return self._normalize_plan(parsed)
        except Exception as exc:
            if is_out_of_money_error(exc):
                raise
            log_workflow_event("workflow_plan_fallback", {"reason": str(exc)[:500]})
        # A failed decomposition answers in one pass rather than guessing a split.
        fallback = routed if routed is not None else heuristic_plan(user_message, skill_hint)
        return self._single_task_plan(
            fallback.model_copy(update={"user_language": language}), user_message, skill_hint
        )

    def _parse_plan_result(self, result: Any) -> WorkflowPlan:
        if getattr(result, "structured_payload", None):
            return self._parse_plan_data(result.structured_payload)
        return self._parse_plan_json(result.text)

    def _parse_plan_json(self, text: str) -> WorkflowPlan:
        raw = text.strip()
        if raw.startswith("```"):
            raw = re.sub(r"^```(?:json)?\s*", "", raw)
            raw = re.sub(r"\s*```$", "", raw)
        match = re.search(r"\{.*\}", raw, re.DOTALL)
        if match:
            raw = match.group(0)
        try:
            data = json.loads(raw)
        except json.JSONDecodeError:
            # A trailing comma is the one slip that recurs in plain-JSON plans
            # (luna, 2026-10-08); it cost the turn its routing decision.
            data = json.loads(_strip_trailing_commas(raw))
        return self._parse_plan_data(data)

    def _parse_plan_data(self, data: dict[str, Any]) -> WorkflowPlan:
        from .skills import internal_skill_names

        if hasattr(data, "model_dump"):
            data = data.model_dump()
        data = dict(data)
        data.setdefault("title", "Legal workflow")
        # The answer language is decided from the request's script, not here.
        data["user_language"] = str(data.get("user_language") or "Chinese")
        mode = data.get("execution_mode")
        if mode not in {"agent_workflow", "direct_answer", "clarify"}:
            mode = "agent_workflow"
        data["execution_mode"] = mode
        complexity = str(data.get("complexity") or "").strip().lower()
        if complexity not in {"simple", "standard", "complex"}:
            complexity = "standard"
        data["complexity"] = complexity
        data["direct_response"] = str(data.get("direct_response") or "")
        data["routing_reason"] = str(data.get("routing_reason") or "")
        legal_skills = set(internal_skill_names()) - {"cite-check"}
        issues = []
        for index, issue in enumerate(data.get("issues") or [], start=1):
            if not isinstance(issue, dict):
                continue
            issue_id = re.sub(r"[^A-Za-z0-9_-]+", "_", str(issue.get("id") or f"I{index}"))
            issues.append(
                {
                    "id": issue_id,
                    "issue": str(issue.get("issue") or issue.get("title") or ""),
                    "claim_basis": str(issue.get("claim_basis") or ""),
                    "elements": [str(e) for e in (issue.get("elements") or []) if str(e).strip()],
                    "facts_needed": [str(e) for e in (issue.get("facts_needed") or []) if str(e).strip()],
                    "facts_assumed": [str(e) for e in (issue.get("facts_assumed") or []) if str(e).strip()],
                    "jurisdiction": str(issue.get("jurisdiction") or ""),
                    "evidence_surface": str(issue.get("evidence_surface") or ""),
                }
            )
        data["issues"] = issues
        tasks = []
        for index, task in enumerate(data.get("agent_tasks") or [], start=1):
            if not isinstance(task, dict):
                continue
            skill = task.get("skill_name")
            if skill != GENERAL_TASK_SKILL and skill not in legal_skills:
                # An external procedure is reached through its own tool, and an
                # unknown name falls back to the research method.
                skill = GENERAL_TASK_SKILL if skill in SKILL_NAMES else "brief"
            task_id = re.sub(r"[^A-Za-z0-9_-]+", "_", str(task.get("id") or f"task_{index}"))
            body = str(task.get("task") or "")
            tasks.append(
                {
                    "id": task_id,
                    "skill_name": skill,
                    # A missing title is the parser's default, not a defect of
                    # the task (see `_is_low_information_task`).
                    "title": str(task.get("title") or "").strip() or (body[:40] or skill),
                    "task": body,
                    "issue_ids": [
                        re.sub(r"[^A-Za-z0-9_-]+", "_", str(i)) for i in (task.get("issue_ids") or [])
                    ],
                    "depends_on": [
                        re.sub(r"[^A-Za-z0-9_-]+", "_", str(dep))
                        for dep in (task.get("depends_on") or [])
                    ],
                }
            )
        data["agent_tasks"] = tasks
        data["fact_conflicts"] = [
            str(c).strip() for c in (data.get("fact_conflicts") or []) if str(c).strip()
        ]
        data["integration_instructions"] = str(data.get("integration_instructions") or "")
        data["citation_requirements"] = str(data.get("citation_requirements") or "")
        data.pop("artifact_format", None)
        try:
            confidence = data.get("intake_confidence")
            data["intake_confidence"] = None if confidence is None else float(confidence)
        except (TypeError, ValueError):
            data["intake_confidence"] = None
        return WorkflowPlan(**{k: v for k, v in data.items() if k in WorkflowPlan.model_fields})

    def _resolve_clarify(self, plan: WorkflowPlan, clarify_rounds_used: int) -> WorkflowPlan:
        """Decide whether a `clarify` routing actually gets to ask.

        Two gates, both arithmetic rather than instruction, because a planner
        that is inclined to hedge will hedge past any wording:

        - **Confidence.** `clarify` with `intake_confidence` at or above the
          threshold is a hedge, not a blocker: the model says it knows enough
          and then asks anyway. It proceeds instead.
        - **Rounds.** After `MAX_CLARIFY_ROUNDS` consecutive clarifying turns,
          the next one proceeds on stated assumptions. Asking a third time
          costs the user the same typing again and produces nothing.
        """
        if plan.execution_mode != "clarify":
            return plan

        confident = (
            plan.intake_confidence is not None
            and plan.intake_confidence >= CLARIFY_CONFIDENCE_THRESHOLD
        )
        exhausted = clarify_rounds_used >= MAX_CLARIFY_ROUNDS
        if not confident and not exhausted:
            return plan

        reason = "confidence_at_threshold" if confident else "clarify_rounds_exhausted"
        log_workflow_event(
            "clarify_overridden",
            {
                "reason": reason,
                "intake_confidence": plan.intake_confidence,
                "threshold": CLARIFY_CONFIDENCE_THRESHOLD,
                "clarify_rounds_used": clarify_rounds_used,
                "max_rounds": MAX_CLARIFY_ROUNDS,
            },
        )
        return plan.model_copy(
            update={
                "execution_mode": "agent_workflow",
                "direct_response": "",
                "routing_reason": (
                    f"{plan.routing_reason} [proceeding without further questions: {reason}]"
                ).strip(),
                # The question the router wanted to ask becomes the assumption
                # the work has to state, so the answer still surfaces the gap.
                "integration_instructions": "\n".join(
                    filter(
                        None,
                        [
                            plan.integration_instructions,
                            (
                                "Essential facts were not supplied. State the "
                                "assumptions you proceed on at the top of the "
                                "answer, and answer for the most likely reading. "
                                "Where an assumption changes the conclusion, give "
                                "the alternative outcome too."
                                + (
                                    f" The open question was: {plan.direct_response.strip()}"
                                    if plan.direct_response.strip()
                                    else ""
                                )
                            ),
                        ],
                    )
                ),
            }
        )

    def _normalize_plan(self, plan: WorkflowPlan) -> WorkflowPlan:
        """Drop redundant aggregator/QC sub-agents; preserve real specialist dependencies.

        LLM planners sometimes create a dependent "draft final answer" agent and then
        the orchestrator performs integration again, or a "source verification" agent
        that duplicates the orchestrator's automatic citation audit. Both double
        latency and produce partial final-looking output before the real final
        answer. We strip those tasks and keep the rest of the dependency graph
        intact so genuine inter-specialist dependencies still serialize correctly.
        """
        if plan.execution_mode != "agent_workflow":
            if plan.agent_tasks or plan.issue_decomposition:
                log_workflow_event(
                    "workflow_plan_normalized",
                    {
                        "execution_mode": plan.execution_mode,
                        "direct_route_tasks_removed": len(plan.agent_tasks),
                        "agent_count_before": len(plan.agent_tasks),
                        "agent_count_after": 0,
                    },
                )
            return plan.model_copy(update={"agent_tasks": [], "issue_decomposition": []})

        removed_synthesis: list[AgentTask] = []
        removed_qc: list[AgentTask] = []
        kept: list[AgentTask] = []
        for task in plan.agent_tasks:
            if _is_final_synthesis_task(task):
                removed_synthesis.append(task)
            elif _is_citation_qc_task(task):
                removed_qc.append(task)
            else:
                kept.append(task)

        # Merge only on proven overlap: two tasks merge when their `coverage:`
        # tokens intersect and they do not target different legal systems. A
        # task without a coverage line is never merged — the old rule merged
        # any two such tasks that shared a skill name, which is not overlap.
        merged_overlap: list[dict[str, str]] = []
        deduped: list[AgentTask] = []
        coverage_by_kept_id: dict[str, set[str]] = {}
        jurisdictions_by_kept_id: dict[str, set[str]] = {}
        absorbed_by_target: dict[str, list[AgentTask]] = {}
        for task in kept:
            coverage = _task_coverage(task)
            task_juris = _task_jurisdictions(task)
            overlap_target: Optional[str] = None
            if coverage:
                for prior_id, prior_coverage in coverage_by_kept_id.items():
                    if prior_coverage & coverage:
                        overlap_target = prior_id
                        break
            # Jurisdiction veto: comparative CN/US/EU work is distinct evidence
            # per jurisdiction even when a token repeats.
            if overlap_target is not None and task_juris:
                target_juris = jurisdictions_by_kept_id.get(overlap_target, set())
                if target_juris and not (task_juris & target_juris):
                    overlap_target = None
            if overlap_target is not None:
                merged_overlap.append(
                    {
                        "dropped_task_id": task.id,
                        "dropped_title": task.title,
                        "dropped_skill": task.skill_name,
                        "merged_into": overlap_target,
                        "reason": "coverage_overlap",
                    }
                )
                absorbed_by_target.setdefault(overlap_target, []).append(task)
                coverage_by_kept_id[overlap_target] |= coverage
                if task_juris:
                    jurisdictions_by_kept_id.setdefault(overlap_target, set()).update(task_juris)
                continue
            deduped.append(task)
            coverage_by_kept_id[task.id] = set(coverage)
            jurisdictions_by_kept_id[task.id] = set(task_juris)

        # Hard ceiling on specialist count — each is a full research loop on the
        # main model. Extra tasks are merged into the closest kept task (most
        # shared coverage, then shared jurisdiction, then the lightest), never
        # dropped: the old `kept[:3]` truncation threw a planned mandate away.
        MAX_SPECIALISTS = 3
        while len(deduped) > MAX_SPECIALISTS:
            extra = deduped.pop()
            extra_cov = coverage_by_kept_id.pop(extra.id, set())
            extra_juris = jurisdictions_by_kept_id.pop(extra.id, set())
            for absorbed in absorbed_by_target.pop(extra.id, []):
                absorbed_by_target.setdefault("__pending__", []).append(absorbed)

            def closeness(candidate: AgentTask) -> tuple[int, int, int]:
                cov = coverage_by_kept_id.get(candidate.id, set())
                juris = jurisdictions_by_kept_id.get(candidate.id, set())
                return (len(cov & extra_cov), len(juris & extra_juris), -len(cov))

            target = max(deduped, key=closeness)
            merged_overlap.append(
                {
                    "dropped_task_id": extra.id,
                    "dropped_title": extra.title,
                    "dropped_skill": extra.skill_name,
                    "merged_into": target.id,
                    "reason": "specialist_cap",
                }
            )
            absorbed_by_target.setdefault(target.id, []).append(extra)
            absorbed_by_target[target.id].extend(absorbed_by_target.pop("__pending__", []))
            coverage_by_kept_id.setdefault(target.id, set()).update(extra_cov)
            jurisdictions_by_kept_id.setdefault(target.id, set()).update(extra_juris)

        if merged_overlap:
            # A merged mandate must be executed, not dropped: the target's body
            # carries the absorbed instructions, one unioned `coverage:` line,
            # the absorbed issues and dependencies.
            rewritten: list[AgentTask] = []
            for task in deduped:
                absorbed = absorbed_by_target.get(task.id)
                if not absorbed:
                    rewritten.append(task)
                    continue
                body = _strip_coverage_lines(task.task)
                merged_deps = list(task.depends_on)
                merged_issues = list(task.issue_ids)
                for dropped in absorbed:
                    dropped_body = _strip_coverage_lines(dropped.task)
                    if dropped_body:
                        body += (
                            "\n\nAdditional mandate merged from planner task "
                            f"'{dropped.title}' ({dropped.id}) — cover it fully:\n"
                            f"{dropped_body}"
                        )
                    for dep in dropped.depends_on:
                        if dep not in merged_deps:
                            merged_deps.append(dep)
                    for issue_id in dropped.issue_ids:
                        if issue_id not in merged_issues:
                            merged_issues.append(issue_id)
                union = coverage_by_kept_id.get(task.id) or set()
                if union:
                    body += "\n\ncoverage: " + ", ".join(sorted(union))
                rewritten.append(
                    task.model_copy(
                        update={"task": body, "depends_on": merged_deps, "issue_ids": merged_issues}
                    )
                )
            deduped = rewritten
            log_workflow_event(
                "workflow_plan_coverage_deduped",
                {
                    "agent_count_before_dedup": len(kept),
                    "agent_count_after_dedup": len(deduped),
                    "merged": merged_overlap,
                    "rewritten_target_ids": sorted(k for k in absorbed_by_target if k != "__pending__"),
                },
            )
        kept = deduped

        kept_ids = {task.id for task in kept}
        # Rewrite any dependency that pointed at a dropped task to the merge target.
        dropped_to_target = {m["dropped_task_id"]: m["merged_into"] for m in merged_overlap}
        # A task can be merged into one that is later merged itself (overlap,
        # then the cap): follow the chain to the survivor.
        for dropped_id in list(dropped_to_target):
            seen: set[str] = set()
            target = dropped_to_target[dropped_id]
            while target in dropped_to_target and target not in seen:
                seen.add(target)
                target = dropped_to_target[target]
            dropped_to_target[dropped_id] = target
        dangling_edges_pruned = 0
        normalized: list[AgentTask] = []
        for task in kept:
            valid_deps: list[str] = []
            for dep in task.depends_on:
                redirected = dropped_to_target.get(dep, dep)
                if redirected in kept_ids and redirected not in valid_deps and redirected != task.id:
                    valid_deps.append(redirected)
                else:
                    dangling_edges_pruned += 1
            normalized.append(task.model_copy(update={"depends_on": valid_deps}))

        # Break dependency cycles so execution can always make progress. A
        # cyclic plan previously aborted the whole run in `_stream_tasks` with
        # "unresolved task dependencies"; instead, drop the intra-cycle edges
        # (the cycle members then run in parallel) and log what was broken.
        deps_by_id = {task.id: set(task.depends_on) for task in normalized}
        resolvable: set[str] = set()
        progressed = True
        while progressed:
            progressed = False
            for tid, deps in deps_by_id.items():
                if tid not in resolvable and deps <= resolvable:
                    resolvable.add(tid)
                    progressed = True
        cycle_ids = {tid for tid in deps_by_id if tid not in resolvable}
        # Trim tasks that are merely downstream of a cycle (no unresolved task
        # depends on them): they keep their dependencies and become runnable
        # once the actual cycle members are unblocked below.
        trimmed = True
        while trimmed and cycle_ids:
            trimmed = False
            depended_on: set[str] = set()
            for tid in cycle_ids:
                depended_on |= deps_by_id[tid] & cycle_ids
            fringe = cycle_ids - depended_on
            if fringe:
                cycle_ids -= fringe
                trimmed = True
        if cycle_ids:
            broken_edges: list[list[str]] = []
            uncycled: list[AgentTask] = []
            for task in normalized:
                if task.id not in cycle_ids:
                    uncycled.append(task)
                    continue
                keep_deps = [dep for dep in task.depends_on if dep not in cycle_ids]
                broken_edges.extend([task.id, dep] for dep in task.depends_on if dep in cycle_ids)
                uncycled.append(task.model_copy(update={"depends_on": keep_deps}))
            normalized = uncycled
            log_workflow_event(
                "workflow_plan_dependency_cycle_broken",
                {"cycle_task_ids": sorted(cycle_ids), "removed_edges": broken_edges},
            )

        integration_instructions = plan.integration_instructions
        if removed_synthesis:
            removed_requirements = "\n".join(
                f"- {task.title}: {task.task[:900]}" for task in removed_synthesis
            )
            integration_instructions = (
                f"{integration_instructions}\n\nFinal synthesis requirements moved from "
                f"removed planner task(s):\n{removed_requirements}"
            ).strip()

        if removed_synthesis or removed_qc or dangling_edges_pruned:
            log_workflow_event(
                "workflow_plan_normalized",
                {
                    "removed_final_synthesis_tasks": [t.id for t in removed_synthesis],
                    "removed_citation_qc_tasks": [t.id for t in removed_qc],
                    "dangling_dependency_edges_pruned": dangling_edges_pruned,
                    "agent_count_before": len(plan.agent_tasks),
                    "agent_count_after": len(normalized),
                },
            )

        return plan.model_copy(
            update={
                "agent_tasks": normalized,
                # The planner's issues, when it gave any; task titles are only
                # the fallback (they used to overwrite the issue list).
                "issue_decomposition": (
                    [issue.issue for issue in plan.issues if issue.issue]
                    or [task.title for task in normalized]
                ),
                "integration_instructions": integration_instructions,
            }
        )

    def stream(
        self,
        user_message: str,
        *,
        attachments: Optional[Iterable[Path]] = None,
        available_files: Optional[list[dict]] = None,
        generated_files: Optional[list[dict]] = None,
        recent_messages: Optional[list[ChatMessage]] = None,
        memory_summary: str = "",
        skill_hint: Optional[str] = None,
        cancel_event: Optional[threading.Event] = None,
        clarify_rounds_used: int = 0,
        resume_run_id: Optional[str] = None,
    ) -> Iterator[StreamEvent]:
        run_id = new_run_id()
        token_run = run_id_var.set(run_id)
        token_parent = parent_run_id_var.set(None)
        token_agent = agent_name_var.set("orchestrator")
        # Durable run record. Opened before any model call so a crash in
        # routing is still visible, and reused when resuming: the research a
        # previous attempt completed is the expensive part to lose.
        chat_id = (usage_context_var.get() or {}).get("chat_id")
        self._run_store = RunStore(self.settings, run_id, chat_id=chat_id)
        # Set at every exit that actually answered. The direct-answer and
        # clarify branches return early, so the terminal status is settled in
        # `finally` from this flag rather than at one happy-path line.
        answered = False
        resume_outputs: dict[str, str] = {}
        if resume_run_id:
            previous = load_run(self.settings, resume_run_id)
            if previous is not None:
                resume_outputs = previous.resumable_outputs()
                self._run_store.append_event(
                    "resumed_from",
                    {"run_id": resume_run_id, "reused_tasks": sorted(resume_outputs)},
                )
                log_workflow_event(
                    "workflow_resumed",
                    {
                        "from_run_id": resume_run_id,
                        "reused_task_count": len(resume_outputs),
                        "previous_status": previous.snapshot.status,
                    },
                )

        # Auto-detect domain packs from the user message + recent messages and
        # merge with explicit settings.active_domain_packs. The resolved
        # settings live for the duration of this turn via
        # override_current_settings, so SkillAgent, the author prompt, and the
        # connector/MCP filters all see the merged set.
        recent_text = " ".join(m.content for m in (recent_messages or [])[-3:])
        detection_text = f"{user_message}\n{recent_text}".strip()
        merged_packs, detected_packs = merge_active_packs(
            self.settings.active_domain_packs, detection_text
        )
        if list(merged_packs) != list(self.settings.active_domain_packs):
            self._resolved = self.settings.model_copy(
                update={"active_domain_packs": list(merged_packs)}
            )
        else:
            self._resolved = self.settings
        if detected_packs:
            log_workflow_event(
                "pack_autodetected",
                {
                    "explicit": list(self.settings.active_domain_packs),
                    "detected": detected_packs,
                    "merged": merged_packs,
                },
            )

        def raise_if_cancelled() -> None:
            if cancel_event is not None and cancel_event.is_set():
                raise WorkflowCancelled()

        attachment_paths = [Path(p) for p in (attachments or [])]

        # Per-turn telemetry surfaced on the final `done` event: wall-clock
        # duration + the last provider turn's token usage.
        turn_start = time.monotonic()
        turn_usage: dict[str, Any] = {}

        def _capture_usage(ev: StreamEvent) -> None:
            u = ev.data.get("usage") if ev.data else None
            if u:
                # Last provider turn wins — its input_tokens best represents how
                # full the main-thread context window is at answer time.
                turn_usage.clear()
                turn_usage.update(u)

        def _done_payload(text: str, audit_payload: Any) -> dict[str, Any]:
            payload: dict[str, Any] = {"text": text, "citation_audit": audit_payload}
            if turn_usage:
                payload["usage"] = dict(turn_usage)
            payload["duration_ms"] = int((time.monotonic() - turn_start) * 1000)
            return payload

        try:
            raise_if_cancelled()
            # The planner sees a text representation (filenames as hints) so it
            # routes cheaply without paying for multimodal content.
            content = _build_user_message(user_message, attachment_paths)
            # The digest's stable part is the cacheable prefix of every request
            # this turn; the rolling summary travels in the volatile tail.
            context_block, context_tail = _build_context_parts(
                memory_summary,
                recent_messages or [],
                available_files=available_files,
                generated_files=generated_files,
            )
            language = _turn_language(
                user_message, recent_messages, self.settings.default_language
            )
            if merged_packs:
                # Surface active-pack(s) to the UI so it can render a banner.
                yield StreamEvent(
                    "active_domain_packs",
                    {
                        "packs": list(merged_packs),
                        "detected": list(detected_packs),
                    },
                )
            plan = self.plan(
                content,
                skill_hint=skill_hint,
                recent_messages=recent_messages,
                memory_summary=memory_summary,
                available_files=available_files,
                generated_files=generated_files,
                has_attachments=bool(attachment_paths),
                clarify_rounds_used=clarify_rounds_used,
                user_language=language,
            )
            raise_if_cancelled()

            if plan.execution_mode in {"direct_answer", "clarify"} and attachment_paths:
                # The router sees filenames, not bytes: a turn with attachments
                # needs the author to read them. Carry an explicit legal/general
                # classification instead of an absent one.
                prior_mode = plan.execution_mode
                forced_signal = "\n".join([user_message, *(p.name for p in attachment_paths)])
                forced_task = (
                    _make_general_task(user_message)
                    if not _LEGAL_REQUEST_RE.search(forced_signal)
                    else _make_task("brief", plan.title or "Attached document review", user_message)
                )
                plan = plan.model_copy(update={
                    "execution_mode": "agent_workflow",
                    "direct_response": "",
                    "agent_tasks": [forced_task],
                    "routing_reason": (
                        (plan.routing_reason or "")
                        + f" (forced agent_workflow: attachments present; "
                        f"skill={forced_task.skill_name})"
                    ),
                })
                log_workflow_event(
                    "workflow_plan_attachment_override",
                    {
                        "from_mode": prior_mode,
                        "skill_name": forced_task.skill_name,
                        "attachment_count": len(attachment_paths),
                    },
                )

            if self._run_store is not None:
                self._run_store.record_plan(plan)
            # Emitted after the attachment override so the plan the UI shows is
            # the plan that actually runs.
            log_workflow_event(
                "workflow_plan_created",
                {"plan": plan.model_dump(), "agent_count": len(plan.agent_tasks)},
            )
            yield StreamEvent("workflow_plan", plan.model_dump())

            tier = answer_contract.length_tier(
                plan.complexity,
                None if self.settings.answer_length == "auto" else self.settings.answer_length,
            )

            if plan.execution_mode in {"direct_answer", "clarify"}:
                final_text = plan.direct_response.strip() or (
                    "请补充会影响结论的关键事实，我再据此作答。"
                    if answer_contract.language_code(plan.user_language) == "zh"
                    else "Could you add the missing facts that change the answer?"
                )
                log_workflow_event(
                    "workflow_direct_output",
                    {
                        "execution_mode": plan.execution_mode,
                        "routing_reason": plan.routing_reason,
                        "response_chars": len(final_text),
                    },
                )
                yield StreamEvent("delta", {"text": final_text})
                audit_payload = None
                for ev in self._run_cite_check_after_summary(
                    plan=plan,
                    final_text=final_text,
                    attachments=attachment_paths,
                    cancel_event=cancel_event,
                ):
                    if ev.kind == "citation_audit":
                        audit_payload = ev.data
                    yield ev
                answered = True
                yield StreamEvent("done", _done_payload(final_text, audit_payload))
                return

            # One author answers every simple and standard turn, and any plan
            # that ended with a single task: there is no value in a
            # specialist→synthesis double pass for one bundle. Only a complex
            # plan with several specialists fans out.
            if plan.complexity != "complex" or len(plan.agent_tasks) <= 1:
                task = plan.agent_tasks[0] if plan.agent_tasks else None
                is_general = (
                    task.skill_name == GENERAL_TASK_SKILL
                    if task is not None
                    else not _looks_like_legal_request(user_message, skill_hint)
                )
                skill_name = None if (is_general or task is None) else task.skill_name
                if is_general:
                    direct_system = _GENERAL_SYSTEM_PROMPT
                    direct_tools = self._author_tools(legal=False)
                    tail = "\n\n".join(
                        [answer_contract.today_line(), f"Answer language: {plan.user_language}."]
                    )
                else:
                    direct_system = author_system_prompt(
                        self._resolved, mode="single_pass", skill_name=skill_name
                    )
                    direct_tools = self._author_tools(legal=True, skill_method=bool(skill_name))
                    tail = answer_contract.author_tail(
                        user_language=plan.user_language,
                        tier=tier,
                        must_cover=plan.integration_instructions,
                        fact_conflicts=plan.fact_conflicts,
                    )
                if plan.citation_requirements.strip() and not is_general:
                    tail += "\n\n" + plan.citation_requirements.strip()
                # The chat digest is the stable, growing prefix of every turn in
                # this chat; the tail and the current request are volatile.
                if context_tail:
                    tail = f"{context_tail}\n\n{tail}"
                direct_user_content = build_user_content(
                    f"{tail}\n\nCurrent user request:\n{user_message}",
                    attachment_paths,
                    self.author_provider.name,
                    cache_prefix=context_block,
                )
                log_workflow_event(
                    "workflow_direct_llm_started",
                    {
                        "mode": "general" if is_general else "single_pass_legal",
                        "complexity": plan.complexity,
                        "skill_method": skill_name,
                        "length_tier": None if is_general else tier,
                        "tool_count": len(direct_tools),
                        "attachment_count": len(attachment_paths),
                        "user_language": plan.user_language,
                    },
                )
                final_text, audit_payload = yield from self._verified_synthesis(
                    system=direct_system,
                    content=direct_user_content,
                    plan=plan,
                    attachments=attachment_paths,
                    capture_usage=_capture_usage,
                    cancel_event=cancel_event,
                    stage="single_pass",
                    provider=self.author_provider,
                    tools=direct_tools,
                    verbosity=None if is_general else answer_contract.VERBOSITY.get(tier),
                )
                raise_if_cancelled()
                answered = True
                yield StreamEvent("done", _done_payload(final_text, audit_payload))
                return

            task_outputs, failed_tasks = yield from self._stream_tasks(
                plan,
                attachments=attachment_paths,
                cancel_event=cancel_event,
                resume_outputs=resume_outputs,
                cache_prefix=context_block,
                context_tail=context_tail,
            )
            raise_if_cancelled()
            if failed_tasks:
                # Surface the degradation so the UI can flag partial coverage.
                yield StreamEvent(
                    "workflow_degraded",
                    {
                        "failed_task_ids": sorted(failed_tasks),
                        "message": (
                            "Some specialist research failed; the answer covers "
                            "only the completed research bundles."
                        ),
                    },
                )

            general = _is_general_plan(plan)
            integration_text = self._integration_prompt(
                user_message=user_message,
                plan=plan,
                task_outputs=task_outputs,
                failed_tasks=failed_tasks,
                tier=tier,
            )
            # The digest is the cacheable prefix here too; the specialists'
            # attachments are not re-sent (the largest re-send was 483,525
            # characters) — the files listing names them and `read_document`
            # opens one if the author needs it.
            if context_tail:
                integration_text = f"{context_tail}\n\n{integration_text}"
            integration_content = build_user_content(
                integration_text, [], self.provider.name, cache_prefix=context_block
            )
            log_workflow_event(
                "integration_started",
                {
                    "agent_count": len(plan.agent_tasks),
                    "complexity": plan.complexity,
                    "user_language": plan.user_language,
                    "length_tier": tier,
                    "issue_count": len(plan.issues),
                },
            )
            final_text, audit_payload = yield from self._verified_synthesis(
                system=(
                    _GENERAL_SYSTEM_PROMPT
                    if general
                    else author_system_prompt(self._resolved, mode="integration")
                ),
                content=integration_content,
                plan=plan,
                attachments=[],
                capture_usage=_capture_usage,
                cancel_event=cancel_event,
                provider=self.integration_provider,
                tools=self._author_tools(legal=not general),
                verbosity=None if general else answer_contract.VERBOSITY.get(tier),
            )
            raise_if_cancelled()
            answered = True
            yield StreamEvent("done", _done_payload(final_text, audit_payload))
        except WorkflowCancelled:
            # Paused, not lost: the completed research bundles are on disk and a
            # resumed run reuses them.
            if self._run_store is not None:
                self._run_store.finish("paused", pause_reason="cancelled")
            yield StreamEvent(
                "cancelled",
                {"message": "Cancelled by user.", "run_id": run_id, "resumable": True},
            )
        except Exception as exc:
            out_of_money = is_out_of_money_error(exc)
            if self._run_store is not None:
                self._run_store.finish(
                    "paused" if out_of_money else "failed",
                    pause_reason="provider_unavailable" if out_of_money else "error",
                    error=str(exc)[:2000],
                )
            if out_of_money:
                yield StreamEvent("delta", {"text": OUT_OF_MONEY_MESSAGE})
                yield StreamEvent("error", {"message": OUT_OF_MONEY_MESSAGE, "out_of_money": True})
            else:
                yield StreamEvent("error", {"message": str(exc), "run_id": run_id})
        finally:
            # A generator abandoned mid-turn (the client disconnected) never
            # reaches an except clause, so a run left "running" here is not a
            # completed one — it is resumable work nobody is waiting for.
            if self._run_store is not None and self._run_store.snapshot.status == "running":
                if answered:
                    self._run_store.finish("complete")
                else:
                    self._run_store.finish("paused", pause_reason="abandoned")
            run_id_var.reset(token_run)
            parent_run_id_var.reset(token_parent)
            agent_name_var.reset(token_agent)

    def _author_tools(self, *, legal: bool, skill_method: bool = False) -> list[Any]:
        """The tool surface of whoever writes the answer.

        The same for the single-pass author and the integrator: the integrator
        was told "no tools are available" while holding 31 document tools,
        and could neither close a gap a specialist named nor research it.
        """
        tools: list[Any] = [
            *hosted_search_tools_for_provider(self._resolved),
            *_document_io_tools(),
        ]
        if legal:
            from .tools import _active_pack_jurisdictions
            from .tools.aviation_search import aviation_source_search
            from .tools.rag_tools import retrieve_legal

            packs = list(self._resolved.active_domain_packs)
            # The same widening a specialist gets: an active pack reaches the
            # jurisdictions it declares (the aviation pack's ET-scoped ECAA
            # tools, its EU-scoped EASA tools), and its playbook names them.
            jurisdictions = list(
                dict.fromkeys(
                    [
                        self._resolved.default_jurisdiction,
                        *self._resolved.secondary_jurisdictions,
                        *_active_pack_jurisdictions(packs),
                    ]
                )
            )
            tools.extend(visible_connector_tools(jurisdictions, packs))
            tools.append(legal_source_search)
            if "aviation" in packs:
                tools.append(aviation_source_search)
            # The local corpus (ICAO documents and treaties among it) is reached
            # only through RAG; every legal method declares rag_collections.
            # PKULaw stays off this surface until its gateway answers again
            # (capability review 2026-10-07, R-6).
            tools.append(retrieve_legal)
            if skill_method:
                from .tools.skill_resources import list_skill_references, read_skill_reference

                tools.extend([list_skill_references, read_skill_reference])
        try:
            from .projects import active_project_id

            if active_project_id():
                from .tools.project_tools import (
                    project_brief_read,
                    project_memory_search,
                    project_memory_write,
                )

                tools.extend([project_memory_write, project_memory_search, project_brief_read])
        except Exception:  # noqa: BLE001 — project tools are optional
            pass
        return tools


    # --- verified synthesis -------------------------------------------------
    # A citation audit that only reports is a warning label; the answer still
    # ships with the bad cite in it. These three methods turn the audit into a
    # repair: synthesise into a buffer, audit, fix exactly what the audit
    # flagged, then reveal. Bounded by ``max_repair_rounds`` so a model that
    # cannot satisfy its own auditor cannot loop.

    def _synthesis_pass(
        self,
        *,
        system: str,
        content: Any,
        reveal: bool,
        capture_usage: Callable[[StreamEvent], None],
        cancel_event: Optional[threading.Event],
        stage: str = "integration",
        provider: Optional[Provider] = None,
        tools: Optional[list[Any]] = None,
        verbosity: Optional[str] = None,
        plan: Optional[WorkflowPlan] = None,
    ) -> Iterator[StreamEvent]:
        """One synthesis turn. Yields provider events, returns the answer text.

        With ``reveal=False`` the prose is buffered rather than streamed, so a
        verification pass can run before the reader sees a single citation.
        Tool calls, sources and phase events still stream either way. The text
        passes through the answer lint (whole lines at a time) on its way out.
        """
        chunks: list[str] = []
        stripper = _ToolXmlStripper()
        linter = AnswerLinter(
            language=answer_contract.language_code(plan.user_language if plan else "")
        )
        got_delta = False

        def emit_text(clean: str) -> Iterator[StreamEvent]:
            if not clean:
                return
            chunks.append(clean)
            if reveal:
                yield StreamEvent("delta", {"text": clean})

        active = provider or self.integration_provider
        for ev in active.stream(
            system=system,
            messages=[{"role": "user", "content": content}],
            tools=_document_io_tools() if tools is None else tools,
            max_iterations=self.settings.parent_max_iterations,
            **_provider_kwargs(active.stream, verbosity=verbosity),
        ):
            if cancel_event is not None and cancel_event.is_set():
                raise WorkflowCancelled()
            if ev.kind == "delta":
                got_delta = True
                clean = stripper.feed(ev.data.get("text", ""))
                if clean:
                    yield from emit_text(linter.feed(clean))
            elif ev.kind == "done":
                capture_usage(ev)
                if not got_delta and ev.data.get("text"):
                    clean = stripper.feed(ev.data.get("text", ""))
                    if clean:
                        yield from emit_text(linter.feed(clean))
            else:
                yield ev
        tail = stripper.flush()
        if tail:
            yield from emit_text(linter.feed(tail))
        yield from emit_text(linter.flush())
        if linter.counts.any():
            log_workflow_event("answer_lint", {"stage": stage, **linter.counts.as_dict()})
        return _guard_empty_answer("".join(chunks), stage)

    def _audit_pass(
        self,
        *,
        plan: Optional[WorkflowPlan],
        final_text: str,
        attachments: Optional[list[Path]],
        cancel_event: Optional[threading.Event],
        emit_result: bool = True,
    ) -> Iterator[StreamEvent]:
        """Run the citation audit, returning its payload.

        ``emit_result=False`` keeps the progress events but withholds the
        ``citation_audit`` verdict, so an intermediate audit of a draft that is
        about to be repaired never reaches the UI as the answer's verdict.
        """
        payload: Optional[dict[str, Any]] = None
        for ev in self._run_cite_check_after_summary(
            plan=plan,
            final_text=final_text,
            attachments=attachments,
            cancel_event=cancel_event,
        ):
            if ev.kind == "citation_audit":
                payload = ev.data
                if not emit_result:
                    continue
            yield ev
        return payload

    def _verified_synthesis(
        self,
        *,
        system: str,
        content: Any,
        plan: WorkflowPlan,
        attachments: Optional[list[Path]],
        capture_usage: Callable[[StreamEvent], None],
        cancel_event: Optional[threading.Event],
        stage: str = "integration",
        provider: Optional[Provider] = None,
        tools: Optional[list[Any]] = None,
        verbosity: Optional[str] = None,
    ) -> Iterator[StreamEvent]:
        """Synthesise, audit, repair what the audit flagged, then reveal.

        Returns ``(final_text, audit_payload)``. When verification is disabled
        for this plan the prose streams as it is generated and no repair runs —
        repairing an answer the reader has already seen would append a second
        copy rather than correct the first.
        """
        buffered = self._verify_before_reveal(plan)
        rounds = max(0, int(getattr(self.settings, "max_repair_rounds", 1))) if buffered else 0
        draft = yield from self._synthesis_pass(
            system=system,
            content=content,
            reveal=not buffered,
            capture_usage=capture_usage,
            cancel_event=cancel_event,
            stage=stage,
            provider=provider,
            tools=tools,
            verbosity=verbosity,
            plan=plan,
        )
        if self._run_store is not None:
            self._run_store.set_phase(stage)
            self._run_store.write_artifact(f"{stage}-draft", draft, title="Draft answer")
        audit: Optional[dict[str, Any]] = None
        for attempt in range(rounds + 1):
            last = attempt >= rounds
            audit = yield from self._audit_pass(
                plan=plan,
                final_text=draft,
                attachments=attachments,
                cancel_event=cancel_event,
                emit_result=last,
            )
            if self._run_store is not None and audit is not None:
                self._run_store.write_artifact(
                    f"citation-audit-{attempt + 1}",
                    (audit.get("report_markdown") or "").strip()
                    or json.dumps(audit, ensure_ascii=False, indent=2, default=str),
                    title=f"Citation audit (round {attempt + 1})",
                )
            if last or not _audit_is_repairable(audit):
                if not last and audit is not None:
                    # Passed on an earlier round: emit the verdict we withheld.
                    yield StreamEvent("citation_audit", audit)
                break
            items = [
                it for it in (audit.get("items") or [])
                if str(it.get("severity", "")).lower() in {"critical", "major"}
            ]
            log_workflow_event(
                "answer_repair_started",
                {
                    "round": attempt + 1,
                    "flagged_items": len(items),
                    "do_not_file": bool(audit.get("do_not_file")),
                },
            )
            yield StreamEvent(
                "answer_repair",
                {
                    "round": attempt + 1,
                    "rounds_allowed": rounds,
                    "flagged_items": len(items),
                    "do_not_file": bool(audit.get("do_not_file")),
                },
            )
            repaired = yield from self._synthesis_pass(
                system=system,
                content=build_user_content(
                    self._repair_prompt(draft, audit, plan),
                    attachments or [],
                    self.provider.name,
                ),
                reveal=False,
                capture_usage=capture_usage,
                cancel_event=cancel_event,
                stage="repair",
                provider=provider,
                tools=tools,
                verbosity=verbosity,
                plan=plan,
            )
            if not repaired.strip():
                log_workflow_event("answer_repair_abandoned", {"round": attempt + 1})
                break
            draft = repaired
            if self._run_store is not None:
                self._run_store.snapshot.repair_rounds = attempt + 1
                self._run_store.write_artifact(
                    f"repair-{attempt + 1}", draft, title=f"Repaired answer (round {attempt + 1})"
                )
        if buffered and draft:
            yield StreamEvent("delta", {"text": draft})
        return draft, audit

    def _verify_before_reveal(self, plan: Optional[WorkflowPlan]) -> bool:
        """True when this answer is audited before the reader sees it.

        Only substantive legal workflow answers: a greeting or a UI question
        has nothing to verify, and holding it back would add latency for
        nothing.
        """
        if not getattr(self.settings, "verify_before_reveal", True):
            return False
        if not self.settings.enable_cite_check:
            return False
        if plan is None or _is_general_plan(plan):
            return False
        return plan.execution_mode == "agent_workflow"

    def _repair_prompt(
        self,
        draft: str,
        audit: dict[str, Any],
        plan: Optional[WorkflowPlan],
    ) -> str:
        """Instructions to fix exactly what the audit flagged, and nothing else."""
        items = [
            it for it in (audit.get("items") or [])
            if str(it.get("severity", "")).lower() in {"critical", "major"}
        ]
        lines = [
            "The draft answer below failed its citation audit. Repair it.",
            "",
            "## Draft",
            draft,
            "",
            "## Audit findings",
            (audit.get("report_markdown") or "").strip() or "(no report body)",
        ]
        if items:
            lines += [
                "",
                "## Findings to fix, in order",
                *[
                    f"{n}. [{it.get('severity', '?')}] {it.get('claim') or it.get('cite') or ''}"
                    f" — {it.get('note') or it.get('verdict') or 'unsupported'}"
                    for n, it in enumerate(items[:20], 1)
                ],
            ]
        lines += ["", answer_contract.repair_rules((plan.user_language if plan else "") or "Chinese")]
        return "\n".join(lines)

    def _run_cite_check_after_summary(
        self,
        *,
        plan: Optional[WorkflowPlan],
        final_text: str,
        attachments: Optional[list[Path]],
        cancel_event: Optional[threading.Event],
    ) -> Iterator[StreamEvent]:
        """Fire the /cite-check skill on the final synthesized answer.

        Replaces the legacy in-process `audit_citations` call. The skill is
        invoked as a real sub-agent (SkillAgent) so the orchestrator routes
        through the standard skill machinery, not a hard-coded regex check.

        Skips entirely for generic enquiries (chit-chat / general-plan
        without legal substance) — see `_should_run_cite_check`.

        Streams the cite-check sub-agent's events to the caller and yields
        a final ``citation_audit`` event with a back-compat payload
        (``ok``, ``warnings``) plus richer fields (``report_markdown``,
        ``coverage_line``, ``do_not_file``).
        """
        def raise_if_cancelled() -> None:
            if cancel_event is not None and cancel_event.is_set():
                raise WorkflowCancelled()

        if not self.settings.enable_cite_check:
            payload = _skipped_cite_check_payload(reason="cite-check disabled (enable_cite_check=false)")
            log_workflow_event("citation_audit_skipped", {"reason": payload["skip_reason"]})
            yield StreamEvent("citation_audit", payload)
            return

        if not _should_run_cite_check(plan, final_text):
            payload = _skipped_cite_check_payload(
                reason="generic enquiry — no legal citations expected"
            )
            log_workflow_event("citation_audit_skipped", {"reason": payload["skip_reason"]})
            yield StreamEvent("citation_audit", payload)
            return

        # Fast path: zero extractable citations → no point in spinning up the
        # SkillAgent. Emit an auto-pass payload and move on.
        try:
            cites = extract_citations(final_text)
        except Exception:  # noqa: BLE001
            cites = []
        if not cites:
            surface_signals = _citation_surface_signals(final_text)
            if not surface_signals:
                payload = _zero_citation_payload(
                    reason="no citations detected in final answer"
                )
                log_workflow_event("citation_audit_auto_pass", {"reason": payload["skip_reason"]})
                yield StreamEvent("citation_audit", payload)
                return
            # Citation-shaped text is present even though the extractor found
            # nothing (e.g. PRC typography the regex misses). Run the full
            # cite-check anyway instead of auto-passing an unaudited answer.
            log_workflow_event(
                "citation_audit_zero_extraction_override",
                {"surface_signals": surface_signals},
            )

        # Decide stakes from the plan: explicit court-filing / regulatory
        # signals → strict pass, otherwise internal.
        stakes = _infer_cite_check_stakes(plan, final_text)

        log_workflow_event(
            "citation_audit_started",
            {
                "citations_detected": len(cites),
                "stakes": stakes,
                "skill": "cite-check",
            },
        )
        # Surface an "auditing" phase to the UI status line (B2). Distinct from
        # the log event above: this one reaches the SSE stream + is persisted,
        # so the status reads "核验引证 / Verifying citations" live and on reload.
        yield StreamEvent(
            "citation_audit_started",
            {"citations_detected": len(cites), "stakes": stakes},
        )

        # Clear any structured payload left by a prior turn so a run where the
        # tool never fires cannot inherit stale items[].
        take_last_cite_check_payload()

        # Fire the skill on the fast model — the cite-check work is
        # tool-driven (extract_citations_tool, validate_citations_tool,
        # cite_check_report_tool) and doesn't need the parent's full model.
        agent = SkillAgent("cite-check", self.cite_check_provider, self._resolved)
        task_text = _CITE_CHECK_TASK_TEMPLATE.format(stakes=stakes, final_text=final_text)

        # Per-tool progress so the UI can show "fetching authority 3 of 12"
        # without per-token spam. ``cite_check_progress`` is a singleton
        # event the front-end mutates in place rather than appending.
        total_claims = len(cites)
        progress_state = {
            "fetched": 0,
            "roundtrips": 0,
            "verified": 0,
        }

        def _initial_progress() -> StreamEvent:
            return StreamEvent(
                "cite_check_progress",
                {
                    "stage": "starting",
                    "current": 0,
                    "total": total_claims,
                    "message": (
                        f"Spinning up cite-check sub-agent for {total_claims} citation(s)."
                        if total_claims
                        else (
                            "Spinning up cite-check sub-agent — citation-shaped "
                            "text present; extractor count unavailable."
                        )
                    ),
                    "tool_name": None,
                },
            )

        yield _initial_progress()

        chunks: list[str] = []
        try:
            for ev in agent.stream(task_text, attachments=list(attachments or [])):
                raise_if_cancelled()
                if ev.kind == "delta":
                    # Buffer the cite-check report internally — token-level
                    # deltas are noise in the activity feed and would also
                    # leak into the assistant message if propagated as the
                    # default ``delta`` event. The audit surfaces as a
                    # single ``citation_audit`` event at the end of this
                    # method with the parsed payload.
                    chunks.append(ev.data.get("text", ""))
                    continue
                if ev.kind == "done":
                    continue
                if ev.kind == "tool_call":
                    progress = _cite_check_progress_from_tool_call(
                        ev.data, progress_state, total_claims
                    )
                    if progress is not None:
                        yield StreamEvent("cite_check_progress", progress)
                    # Fall through to also yield the raw tool_call so the
                    # detailed activity log still shows the underlying call
                    # for users who want it.
                # Non-delta events (tool_call, hosted_tool_call, skill_*, …)
                # flow through unchanged so the activity feed still shows
                # which tools the cite-check sub-agent invoked.
                yield ev
        except WorkflowCancelled:
            raise
        except Exception as exc:  # noqa: BLE001
            log_workflow_event(
                "citation_audit_failed",
                {"reason": str(exc)[:500], "skill": "cite-check"},
            )
            yield StreamEvent(
                "citation_audit",
                {
                    "ok": False,
                    "warnings": [f"cite-check skill failed: {exc!s}"],
                    "report_markdown": "",
                    "coverage_line": "",
                    "do_not_file": False,
                    "skill": "cite-check",
                    "skipped": False,
                    "error": str(exc)[:500],
                },
            )
            return

        body = "".join(chunks)
        # Recover the full structured report the cite-check tool produced
        # (un-truncated) so the audit carries per-citation items[] + coverage.
        structured = take_last_cite_check_payload()
        # The tool renders its report to markdown internally; when the skill
        # returned only tool JSON (no prose recap), fall back to that markdown.
        if not body and structured and structured.get("report_markdown"):
            body = structured["report_markdown"]
        payload = _parse_cite_check_payload(body, structured)
        log_workflow_event("citation_audit_finished", {
            "ok": payload["ok"],
            "do_not_file": payload["do_not_file"],
            "coverage_line": payload["coverage_line"],
            "report_chars": len(payload["report_markdown"]),
            "item_count": len(payload.get("items") or []),
        })
        yield StreamEvent("citation_audit", payload)

    def _record_task(
        self,
        plan: WorkflowPlan,
        task_id: str,
        output: str,
        *,
        error: Optional[str] = None,
    ) -> None:
        """Persist one specialist's bundle to the run record."""
        if self._run_store is None:
            return
        task = next((item for item in plan.agent_tasks if item.id == task_id), None)
        self._run_store.task_finished(
            task_id,
            output,
            skill_name=getattr(task, "skill_name", ""),
            title=getattr(task, "title", ""),
            error=error,
        )

    def _stream_tasks(
        self,
        plan: WorkflowPlan,
        *,
        attachments: Optional[list[Path]] = None,
        cancel_event: Optional[threading.Event] = None,
        resume_outputs: Optional[dict[str, str]] = None,
        cache_prefix: str = "",
        context_tail: str = "",
    ) -> Iterator[StreamEvent]:
        """Run the plan's specialists, honouring dependencies and concurrency.

        Returns ``(outputs, failures)``; each task's typed outcome (completed,
        empty, truncated, failed) is kept in ``self._task_outcomes`` for the
        integration prompt.
        """
        self._task_outcomes: dict[str, str] = {}
        outputs: dict[str, str] = {}
        # task_id → error message for specialists that failed after retry.
        # Failed tasks count as completed for dependency purposes (dependents
        # run without the missing upstream bundle) and are reported to the
        # integration turn as failed coverage instead of aborting the run.
        failures: dict[str, str] = {}
        pending = {task.id: task for task in plan.agent_tasks}
        completed: set[str] = set()
        # A resumed run inherits the research a previous attempt finished: the
        # tasks are already complete, so only what was left runs again.
        for task_id, text in (resume_outputs or {}).items():
            if task_id in pending and text.strip():
                outputs[task_id] = text
                completed.add(task_id)
                pending.pop(task_id, None)
        if resume_outputs:
            reused = sorted(completed)
            if reused:
                log_workflow_event("workflow_tasks_reused", {"task_ids": reused})
                yield StreamEvent("workflow_tasks_reused", {"task_ids": reused})
        adaptive_max_concurrent = self.settings.effective_max_concurrent_agents()
        rate_limit_retried: set[str] = set()

        def raise_if_cancelled() -> None:
            if cancel_event is not None and cancel_event.is_set():
                raise WorkflowCancelled()

        while pending:
            raise_if_cancelled()
            ready_all = [
                task
                for task in pending.values()
                if all(dep in completed for dep in task.depends_on)
            ]
            if not ready_all:
                raise RuntimeError("Workflow plan has unresolved task dependencies")

            max_concurrent = max(1, adaptive_max_concurrent)
            ready = ready_all[:max_concurrent]
            if len(ready_all) > len(ready):
                log_workflow_event(
                    "workflow_agent_concurrency_limited",
                    {
                        "ready_agent_count": len(ready_all),
                        "started_agent_count": len(ready),
                        "max_concurrent_agents": max_concurrent,
                        "deferred_task_ids": [task.id for task in ready_all[max_concurrent:]],
                    },
                )

            q: queue.Queue[tuple[str, str, Optional[StreamEvent], Optional[str]]] = queue.Queue()

            def worker(task: AgentTask) -> None:
                with override_current_settings(self._resolved):
                    provider_name = self._resolved.provider
                    provider_model = self._resolved.model_for_provider()

                    def run_once() -> None:
                        nonlocal provider_name, provider_model
                        chunks: list[str] = []
                        task_start = time.monotonic()
                        tool_count = 0
                        # Retrieved-source chips (FIG1 cot-srb), de-duplicated by
                        # label so repeated queries don't spam the step.
                        sources: list[dict[str, str]] = []
                        seen_sources: set[str] = set()
                        # Specialists run on the main model (the fast tier ends
                        # fetch-driven research early and returns hedged
                        # findings), at the effort the "specialist" role caps.
                        role_settings, role_fast = self._resolved.for_role("specialist")
                        provider = (
                            build_provider(role_settings, fast=True)
                            if role_fast
                            else build_provider(role_settings)
                        )
                        provider_name = getattr(provider, "name", provider_name)
                        provider_model = getattr(provider, "model", provider_model)
                        q.put((task.id, "event", StreamEvent("agent_task_started", task.model_dump()), None))
                        task_prompt = task.task
                        if context_tail:
                            task_prompt = f"{context_tail}\n\n# Your task\n{task_prompt}"
                        if task.depends_on:
                            upstream_sections = []
                            for dep_id in task.depends_on:
                                dep_task = next(
                                    (t for t in plan.agent_tasks if t.id == dep_id), None
                                )
                                dep_title = dep_task.title if dep_task else dep_id
                                dep_skill = dep_task.skill_name if dep_task else ""
                                dep_output = outputs.get(dep_id, "").strip()
                                if not dep_output:
                                    continue
                                upstream_sections.append(
                                    f"## Upstream output — {dep_title} "
                                    f"({dep_skill}, {dep_id})\n{dep_output}"
                                )
                            if upstream_sections:
                                task_prompt = (
                                    f"{task_prompt}\n\n"
                                    "# Upstream specialist outputs (use as inputs to your own "
                                    "analysis — do not merely restate or summarize them):\n\n"
                                    + "\n\n".join(upstream_sections)
                                )
                        sub_attachments = _attachments_for_task(
                            task, list(attachments or []), plan.agent_tasks
                        )
                        if task.skill_name == GENERAL_TASK_SKILL:
                            event_iter = provider.stream(
                                system=_GENERAL_SYSTEM_PROMPT,
                                messages=[
                                    {
                                        "role": "user",
                                        "content": build_user_content(
                                            task_prompt,
                                            sub_attachments,
                                            getattr(provider, "name", "anthropic"),
                                            cache_prefix=cache_prefix,
                                        ),
                                    }
                                ],
                                tools=self._author_tools(legal=False),
                                max_iterations=self.settings.sub_agent_max_iterations,
                            )
                        else:
                            agent = SkillAgent(task.skill_name, provider, self._resolved)
                            event_iter = agent.stream(
                                task_prompt, attachments=sub_attachments, cache_prefix=cache_prefix
                            )
                        # Throttled live-preview state: emit a rolling tail of the
                        # sub-agent's output as ``agent_delta`` at most every 250ms
                        # so the UI can show what a specialist is producing without
                        # forwarding every token (which would flood N parallel agents).
                        reasoning_chars: list[str] = []
                        last_preview = 0.0
                        truncated = False
                        refused = False

                        def _emit_preview(phase: str, full_text: str, force: bool = False) -> None:
                            nonlocal last_preview
                            now = time.monotonic()
                            if not force and (now - last_preview) < 0.25:
                                return
                            last_preview = now
                            q.put((
                                task.id,
                                "event",
                                StreamEvent(
                                    "agent_delta",
                                    {
                                        "task_id": task.id,
                                        "skill_name": task.skill_name,
                                        "tail": full_text[-300:],
                                        "total_chars": len(full_text),
                                        "phase": phase,
                                    },
                                ),
                                None,
                            ))

                        for ev in event_iter:
                            if cancel_event is not None and cancel_event.is_set():
                                return
                            if ev.kind == "delta":
                                chunks.append(ev.data.get("text", ""))
                                _emit_preview("answer", "".join(chunks))
                            elif ev.kind == "reasoning_delta":
                                # Sub-agent reasoning: fold into the live preview as a
                                # reasoning-phase tail; never persist it as a plain event.
                                reasoning_chars.append(ev.data.get("text", ""))
                                _emit_preview("reasoning", "".join(reasoning_chars))
                            elif ev.kind == "done":
                                # Sub-agent completion is internal. Forwarding it as
                                # workflow ``done`` makes the chat save a partial answer.
                                if not chunks and ev.data.get("text"):
                                    chunks.append(ev.data.get("text", ""))
                                stop = ev.data.get("stop_reason")
                                if stop == "max_tokens":
                                    truncated = True
                                elif stop == "refusal":
                                    refused = True
                                continue
                            else:
                                data = dict(ev.data)
                                data.setdefault("task_id", task.id)
                                data.setdefault("skill_name", task.skill_name)
                                # Both local tool_call and hosted_tool_call (Anthropic
                                # web_search / web_fetch) count toward tool_count and
                                # can contribute a source chip. Hosted searches emit
                                # TWO blocks — the query-bearing server_tool_use and a
                                # result echo (web_*_tool_result) with no args; the
                                # chip helper returns None for the echo, and we skip
                                # counting it so tool_count reflects real calls.
                                is_hosted_echo = ev.kind == "hosted_tool_call" and (
                                    (data.get("raw_type") or "").endswith("_tool_result")
                                )
                                if ev.kind in ("tool_call", "hosted_tool_call") and not is_hosted_echo:
                                    tool_count += 1
                                    chip = _source_chip_from_tool_call(data)
                                    if chip and chip["label"] not in seen_sources:
                                        seen_sources.add(chip["label"])
                                        sources.append(chip)
                                        # Surface the newly retrieved source on a
                                        # preview tick so the CoT step's chips grow
                                        # live, not only at completion.
                                        q.put((
                                            task.id,
                                            "event",
                                            StreamEvent(
                                                "agent_delta",
                                                {
                                                    "task_id": task.id,
                                                    "skill_name": task.skill_name,
                                                    "tool_count": tool_count,
                                                    "sources": list(sources),
                                                    "phase": "research",
                                                },
                                            ),
                                            None,
                                        ))
                                q.put((task.id, "event", StreamEvent(ev.kind, data), None))
                        # Final flush of the answer preview before the finished receipt.
                        if chunks:
                            _emit_preview("answer", "".join(chunks), force=True)
                        text = "".join(chunks)
                        # Typed outcome: an empty bundle or one cut off at the
                        # output ceiling is not research the author can rely on.
                        if refused:
                            outcome = "refused"
                        elif not text.strip():
                            outcome = "empty"
                        else:
                            outcome = "truncated" if truncated else "completed"
                        q.put(
                            (
                                task.id,
                                "done",
                                StreamEvent(
                                    "agent_task_finished",
                                    {
                                        "task_id": task.id,
                                        "skill_name": task.skill_name,
                                        "outcome": outcome,
                                        "response_chars": len(text),
                                        "tool_count": tool_count,
                                        "duration_ms": int((time.monotonic() - task_start) * 1000),
                                        "sources": list(sources),
                                    },
                                ),
                                text,
                            )
                        )

                    last_error = ""
                    for attempt in range(2):
                        try:
                            run_once()
                            return
                        except Exception as exc:
                            if is_out_of_money_error(exc):
                                q.put((task.id, "error", StreamEvent("error", {"message": OUT_OF_MONEY_MESSAGE, "out_of_money": True}), None))
                                return
                            if is_rate_limit_error(exc):
                                # Rate limits are batch-level signals: the consumer
                                # reduces concurrency and re-runs the task, so no
                                # in-worker retry here.
                                q.put(
                                    (
                                        task.id,
                                        "rate_limit",
                                        StreamEvent(
                                            "agent_task_rate_limited",
                                            {
                                                "task_id": task.id,
                                                "skill_name": task.skill_name,
                                                "provider": provider_name,
                                                "model": provider_model,
                                                "message": str(exc),
                                            },
                                        ),
                                        None,
                                    )
                                )
                                return
                            last_error = str(exc)
                            if _is_context_overflow(exc):
                                # Not transient: the same request overflows again.
                                break
                            if attempt == 0:
                                # One retry after a short pause covers transient
                                # provider/network hiccups before degrading.
                                q.put(
                                    (
                                        task.id,
                                        "event",
                                        StreamEvent(
                                            "agent_task_retried",
                                            {
                                                "task_id": task.id,
                                                "skill_name": task.skill_name,
                                                "message": last_error[:500],
                                            },
                                        ),
                                        None,
                                    )
                                )
                                if cancel_event is not None:
                                    if cancel_event.wait(_SPECIALIST_RETRY_BACKOFF_SECONDS):
                                        return
                                else:
                                    time.sleep(_SPECIALIST_RETRY_BACKOFF_SECONDS)
                    # Fail-soft: the specialist stays failed, the run continues —
                    # completed sibling bundles reach integration with an explicit
                    # failed-coverage note instead of the whole run aborting.
                    q.put(
                        (
                            task.id,
                            "failed",
                            StreamEvent(
                                "agent_task_failed",
                                {
                                    "task_id": task.id,
                                    "skill_name": task.skill_name,
                                    "title": task.title,
                                    "message": last_error[:500],
                                    "attempts": 2,
                                },
                            ),
                            None,
                        )
                    )

            # Python threads do NOT auto-copy contextvars. Without this, the
            # workflow_event_sink_var set on the streaming endpoint's context
            # is invisible to sub-agent threads, so log_workflow_event writes
            # to JSONL but never reaches the SQLite sink or the UI — which is
            # why failure signals never make it to the chip color logic. Each
            # thread gets its own snapshot of the parent context.
            threads = [
                threading.Thread(
                    target=contextvars.copy_context().run,
                    args=(worker, task),
                    daemon=True,
                )
                for task in ready
            ]
            for thread in threads:
                thread.start()

            finished_ready: set[str] = set()
            rate_limited_ready: set[str] = set()
            while len(finished_ready) < len(ready):
                raise_if_cancelled()
                try:
                    task_id, status, ev, text = q.get(timeout=0.2)
                except queue.Empty:
                    continue
                if ev is None:
                    continue
                yield ev
                if status == "done":
                    outputs[task_id] = text or ""
                    finished_ready.add(task_id)
                    completed.add(task_id)
                    self._task_outcomes[task_id] = str(ev.data.get("outcome") or "completed")
                    self._record_task(plan, task_id, text or "")
                elif status == "error":
                    # Only unrecoverable run-level errors (out of money) arrive
                    # as "error" now; per-task failures degrade via "failed".
                    raise RuntimeError(ev.data.get("message", "Agent task failed"))
                elif status == "failed":
                    failures[task_id] = ev.data.get("message", "specialist failed")
                    self._task_outcomes[task_id] = "failed"
                    outputs.setdefault(task_id, "")
                    finished_ready.add(task_id)
                    self._record_task(
                        plan, task_id, outputs.get(task_id, ""), error=failures[task_id]
                    )
                    # Counts as completed for dependency purposes: dependents
                    # still run, minus the missing upstream bundle.
                    completed.add(task_id)
                elif status == "rate_limit":
                    rate_limited_ready.add(task_id)
                    finished_ready.add(task_id)

            for thread in threads:
                thread.join()
            if rate_limited_ready:
                retryable = rate_limited_ready - rate_limit_retried
                if retryable and adaptive_max_concurrent > 1:
                    rate_limit_retried.update(retryable)
                    old_limit = adaptive_max_concurrent
                    adaptive_max_concurrent = 1
                    log_workflow_event(
                        "workflow_agent_concurrency_reduced",
                        {
                            "old_max_concurrent_agents": old_limit,
                            "new_max_concurrent_agents": adaptive_max_concurrent,
                            "provider": self.settings.provider,
                            "model": self.settings.model_for_provider(),
                            "retry_task_ids": sorted(retryable),
                        },
                    )
                    yield StreamEvent(
                        "agent_concurrency_reduced",
                        {
                            "old_max_concurrent_agents": old_limit,
                            "new_max_concurrent_agents": adaptive_max_concurrent,
                            "provider": self.settings.provider,
                            "model": self.settings.model_for_provider(),
                            "retry_task_ids": sorted(retryable),
                        },
                    )
                    for task in ready:
                        if task.id not in rate_limited_ready:
                            pending.pop(task.id, None)
                    continue
                # Concurrency already reduced (or reduction impossible) and the
                # provider still rate-limits: degrade the rate-limited tasks to
                # failed coverage instead of discarding completed sibling
                # bundles by aborting the whole run.
                for task in ready:
                    if task.id in rate_limited_ready and task.id not in completed:
                        failures[task.id] = (
                            "Provider rate limit persisted after the workflow "
                            "reduced concurrency."
                        )
                        outputs.setdefault(task.id, "")
                        completed.add(task.id)
                        yield StreamEvent(
                            "agent_task_failed",
                            {
                                "task_id": task.id,
                                "skill_name": task.skill_name,
                                "title": task.title,
                                "message": failures[task.id],
                                "rate_limited": True,
                            },
                        )
                log_workflow_event(
                    "workflow_agent_rate_limit_degraded",
                    {"failed_task_ids": sorted(rate_limited_ready)},
                )
            for task in ready:
                pending.pop(task.id, None)

        if failures:
            succeeded = [
                tid for tid in outputs
                if tid not in failures and (outputs[tid] or "").strip()
            ]
            if not succeeded:
                # Nothing to degrade to — with zero completed bundles the
                # integration turn would have to invent findings.
                raise RuntimeError(
                    "All specialist tasks failed: "
                    + "; ".join(f"{tid}: {msg}" for tid, msg in sorted(failures.items()))
                )
            log_workflow_event(
                "workflow_degraded_missing_specialists",
                {
                    "failed": [
                        {"task_id": tid, "error": msg[:500]}
                        for tid, msg in sorted(failures.items())
                    ],
                    "completed_task_ids": sorted(succeeded),
                },
            )
        return outputs, failures

    def _integration_prompt(
        self,
        *,
        user_message: str,
        plan: WorkflowPlan,
        task_outputs: dict[str, str],
        failed_tasks: Optional[dict[str, str]] = None,
        tier: str = "full",
    ) -> str:
        """The integrator's request: the turn's directives, the plan's issues,
        the bundles, and the gaps. The rules for using them live in the cached
        author system prompt (``authoring._INTEGRATION_RULES``)."""
        failed_tasks = failed_tasks or {}
        outcomes = getattr(self, "_task_outcomes", {}) or {}
        general = _is_general_plan(plan)
        parts: list[str] = []
        if general:
            parts.append(answer_contract.today_line())
            parts.append(f"Answer language: {plan.user_language}.")
            if plan.integration_instructions.strip():
                parts.append(plan.integration_instructions.strip())
            parts.append(
                "Author one practical answer from the task outputs below. Keep useful "
                "source links; do not add legal framing the user did not ask for."
            )
        else:
            parts.append(
                answer_contract.author_tail(
                    user_language=plan.user_language,
                    tier=tier,
                    must_cover=plan.integration_instructions,
                    fact_conflicts=plan.fact_conflicts,
                )
            )
            if plan.issues:
                issue_lines = []
                for issue in plan.issues:
                    line = f"- {issue.id}: {issue.issue}"
                    if issue.claim_basis:
                        line += f" — basis: {issue.claim_basis}"
                    if issue.facts_assumed:
                        line += " — assumed: " + "; ".join(issue.facts_assumed)
                    issue_lines.append(line)
                parts.append("Issues to resolve, in order:\n" + "\n".join(issue_lines))
            if plan.citation_requirements.strip():
                parts.append(plan.citation_requirements.strip())

        bundles: list[str] = []
        gaps: list[str] = []
        for task in plan.agent_tasks:
            coverage = ", ".join(sorted(_task_coverage(task))) or "unspecified"
            label = f"{task.title} ({task.skill_name}; coverage: {coverage})"
            if task.id in failed_tasks:
                gaps.append(f"- {label}: the specialist FAILED — {failed_tasks[task.id][:300]}")
                continue
            body = (task_outputs.get(task.id) or "").strip()
            outcome = outcomes.get(task.id, "completed" if body else "empty")
            if outcome == "refused":
                gaps.append(f"- {label}: the specialist's model declined the task")
                continue
            if outcome == "empty" or not body:
                gaps.append(f"- {label}: the specialist returned nothing")
                continue
            note = ""
            if outcome == "truncated":
                note = " [cut off at the output limit — its last findings may be incomplete]"
                gaps.append(f"- {label}: its bundle was cut off; check the tail before relying on it")
            issues = f"; issues: {', '.join(task.issue_ids)}" if task.issue_ids else ""
            bundles.append(f"### bundle:{task.id} ({task.skill_name}{issues}){note}\n{body}")
            for item in _out_of_scope_items(body):
                gaps.append(f"- {label} left out of scope: {item}")
        parts.append(
            "Specialist bundles (raw research — never echo verbatim, never label by specialist):\n\n"
            + ("\n\n".join(bundles) if bundles else "(none completed)")
        )
        if gaps:
            parts.append(
                "Open gaps — close each with your own tools if you can; otherwise "
                "say plainly which angles this answer does not cover and must be "
                "re-run before reliance. Never invent findings for them:\n" + "\n".join(gaps)
            )
        parts.append(f"Current user request:\n{user_message}")
        return "\n\n".join(parts)


_OUT_OF_SCOPE_RE = re.compile(r"^##\s*Out of scope\s*$(.*?)(?=^##\s|\Z)", re.MULTILINE | re.DOTALL | re.IGNORECASE)


def _out_of_scope_items(bundle: str) -> list[str]:
    """The bullets of a bundle's `## Out of scope` section (159 such items in
    20 of 21 multi-specialist runs were logged and never acted on)."""
    match = _OUT_OF_SCOPE_RE.search(bundle or "")
    if not match:
        return []
    items = []
    for line in match.group(1).splitlines():
        text = line.strip().lstrip("-*•").strip()
        if text and text.lower() not in {"none", "n/a", "无", "（无）", "(none)"}:
            items.append(text[:300])
    return items[:12]
