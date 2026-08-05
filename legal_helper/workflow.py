"""Planner-driven workflow executor for the web chat surface."""

from __future__ import annotations

import queue
import re
import contextvars
import threading
import time
import uuid
from pathlib import Path
from typing import Any, Iterable, Iterator, Optional

from .agent import (
    SkillAgent,
    _build_parent_addendum,
    _build_user_message,
    _PARENT_PROMPT_BODY,
)
from .attachments import build_user_content
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
    write_docx,
    write_pdf,
    write_pptx,
    write_xlsx,
)
from .tools.connectors_tools import visible_connector_tools
from .tools.fetch_attach import fetch_url_to_artifact
from .tools.format_recipes import list_format_recipes, read_format_recipe
from .tools.legal_search import legal_source_search
from .tools.search import hosted_search_tools_for_provider


GENERAL_TASK_SKILL = "general-answer"

# One transient-failure retry per specialist, after a short pause. Rate limits
# are handled separately by the adaptive-concurrency path in `_stream_tasks`.
_SPECIALIST_RETRY_BACKOFF_SECONDS = 2.0


def _document_io_tools() -> list[Any]:
    """Safe baseline document tools for authoring/final-synthesis turns."""
    return [
        write_docx,
        write_pdf,
        inspect_docx,
        edit_docx_text,
        reshape_docx,
        render_docx_pages,
        inspect_pdf,
        extract_pdf_tables,
        render_pdf_pages,
        merge_pdfs,
        split_pdf,
        rotate_pdf_pages,
        write_xlsx,
        write_pptx,
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
        fetch_url_to_artifact,
        list_format_recipes,
        read_format_recipe,
    ]

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
path. Call `read_format_recipe("xlsx" | "docx" | "pptx" | "pdf")` before any
non-trivial edit — the recipe lists every tool you have and the rule for when
to choose `reshape_*` over scalar `edit_*`. For Excel, always inspect the real
workbook coordinates first; reach for `reshape_xlsx` (insert/delete/move rows,
merge, copy_row, style) when the user asks for a structural change — never
simulate a structural reshape with a string of scalar `edit_xlsx_cells`
calls. Finish by re-inspecting and, when layout matters, rendering with
`render_xlsx_pages` for visual QA. Do not derive edit coordinates from
markdown table exports when blank rows or merged headers may shift the row
numbers.
"""


_SINGLE_PASS_LEGAL_SYSTEM = """You are a legal AI helper answering a focused legal
question in a SINGLE pass. **Not legal advice** — remind the user that qualified
counsel must approve before reliance.

You have hosted web search / fetch and document tools directly. There are NO
sub-agents this turn: research the question YOURSELF with your tools, then author
the final user-facing answer. Do not emit `run_skill`, `<tool_call>`, or any
pseudo function-call envelope as text.

# Research, then commit
- Identify the governing instruments and FETCH the primary sources. For any
  cross-border / route / market-entry / transaction question, find the specific
  bilateral or multilateral instrument in force between the named parties (air
  services agreement, tax treaty, BIT, convention + that state's ratification
  status) — never answer from a generic framework convention alone when the
  actual agreement between the parties exists.
- COMMIT to what you verify online with a pinpoint (article / section / clause /
  page): state it as established fact with the pinpoint inline. Reserve hedging
  ('may', 'subject to local-law confirmation', `pinpoint unavailable`) for points
  you genuinely could not confirm. Do not turn the whole answer into a
  '待核验 / to-verify' checklist.

# Authoring
- Directly address every part the user asked, in order, with reasonable extension
  into adjacent points a competent practitioner needs to act. Be comprehensive on
  the named angles; do not drift into unrelated tangents. State each fact at most
  once.
- Default user-facing output to Chinese when the user writes in Chinese; use
  polished professional legal Chinese and keep exact English/source legal terms in
  parentheses. Quote primary sources in their original language with a short gloss.
- Inline citations use markdown link form `[name](URL)`. End with EXACTLY ONE
  appendix titled `资料来源与核验` (Chinese) or `Sources & Verification` (English):
  a single markdown table with columns #, 主张/Claim, 依据/Pinpoint (with URL),
  在线核验/Online-checked (✔ / 未核验 / pinpoint unavailable), 备注/Note. Do not
  emit any other sources/verification section.
"""


def _detect_language(text: str) -> str:
    return "Chinese" if re.search(r"[\u4e00-\u9fff]", text) else "English"


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


# Back-compat alias — `_looks_like_aviation_legal_request` was the original
# name. Existing tests and tools still import it; this delegates to the
# generic implementation with the aviation pack pre-activated.
def _looks_like_aviation_legal_request(
    text: str, skill_hint: Optional[str] = None
) -> bool:
    return _looks_like_legal_request(text, skill_hint, active_packs=["aviation"])


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


class WorkflowCancelled(Exception):
    """Raised internally when a web chat run is cancelled by the user."""


def _is_final_synthesis_task(task: AgentTask) -> bool:
    """Planner cleanup: final synthesis belongs to the orchestrator, not a sub-agent."""
    return bool(_FINAL_SYNTHESIS_RE.search(f"{task.title}\n{task.task}"))


def _is_citation_qc_task(task: AgentTask) -> bool:
    """Citation QC is fired post-synthesis as a /cite-check sub-agent, so
    drop any planner-emitted citation-QC task — the orchestrator owns it."""
    return bool(_CITATION_QC_RE.search(f"{task.title}\n{task.task}"))


_COVERAGE_LINE_RE = re.compile(r"(?im)(?:^|\.\s+)coverage\s*:\s*([^\n]+?)\s*$")

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
    tokens = re.split(r"[,;/、]+", match.group(1))
    return {re.sub(r"\s+", "-", tok.strip().lower()).strip("-") for tok in tokens if tok.strip()}


def _strip_coverage_lines(text: str) -> str:
    """Remove `coverage:` line(s) from a task body.

    Used when merging overlapping mandates so the rewritten body carries a
    single unioned coverage line instead of several stale ones. Preserves a
    sentence-ending period when the coverage line was inline.
    """
    stripped = _COVERAGE_LINE_RE.sub(
        lambda m: "." if m.group(0).startswith(".") else "", text or ""
    )
    return stripped.strip()


def _is_low_information_task(task: AgentTask) -> bool:
    """Detect router-produced placeholders that are not safe to execute."""
    title = task.title.strip()
    body = task.task.strip()
    generic_title = bool(re.fullmatch(r"task[_\s-]*\d+", title, re.IGNORECASE))
    terse_label = bool(re.fullmatch(r"[A-Za-z][A-Za-z0-9_-]{0,40}", body))
    return generic_title or terse_label


def _planner_settings(settings: Settings) -> Settings:
    """Use a cheaper planner profile without changing the actual work model."""
    if settings.provider == "openai" and settings.openai_reasoning_effort != "none":
        return settings.model_copy(update={"openai_reasoning_effort": "low"})
    return settings


def _research_settings(settings: Settings) -> Settings:
    """Reasoning effort for specialist research turns.

    Specialist work is tool-orchestration (decide what to search, read the
    results, extract pinpoints) on the FAST/cheap model — its quality comes from
    the sources it pulls, not from the top reasoning tier. So cap high/xhigh
    down to "medium" here: it removes the dominant latency cost (each of N
    agents at a high tier can take minutes per iteration) and the synthesis
    judgement still happens at integration on the premium model. OpenAI knob
    only; Anthropic keeps its configured profile.
    """
    if settings.provider == "openai" and settings.openai_reasoning_effort in {"high", "xhigh"}:
        return settings.model_copy(update={"openai_reasoning_effort": "medium"})
    return settings


def _integration_settings(settings: Settings) -> Settings:
    """Reasoning effort for the final synthesis turn.

    Integration assembles already-researched specialist findings into prose — it
    does not need the top reasoning tier that research turns use. Capping very
    high effort here removes the dominant latency cost (an xhigh single-turn
    synthesis of a long memo can run many minutes) without hurting research
    depth, which already happened in the specialist turns. Only the OpenAI
    effort knob is adjusted; Anthropic keeps its configured profile.
    """
    if settings.provider == "openai" and settings.openai_reasoning_effort in {"high", "xhigh"}:
        return settings.model_copy(update={"openai_reasoning_effort": "medium"})
    return settings


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
    semantics (e.g. ``list_skill_sections`` while the agent is bootstrapping).
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
        return "我在，随时可以帮你处理航空法律、合同或合规问题。你想先看哪一件事？"
    return None


def heuristic_plan(user_message: str, skill_hint: Optional[str] = None) -> WorkflowPlan:
    """Deterministic fallback planner with no artificial agent-count cap."""
    language = _detect_language(user_message)
    direct_response = _simple_direct_response(user_message)
    if direct_response:
        return WorkflowPlan(
            title="Direct response",
            user_language=language,
            execution_mode="direct_answer",
            direct_response=direct_response,
            routing_reason="Local fallback recognized a simple conversational turn.",
            issue_decomposition=[],
            agent_tasks=[],
            integration_instructions="",
            citation_requirements="",
        )

    lower = user_message.lower()
    tasks: list[AgentTask] = []

    if skill_hint in SKILL_NAMES:
        tasks.append(
            _make_task(
                skill_hint,
                f"{skill_hint} analysis",
                f"Analyze the user's request for later integration:\n\n{user_message}",
            )
        )
    elif any(word in lower for word in ("nda", "non-disclosure", "保密")):
        tasks.append(_make_task("triage-nda", "NDA triage", user_message))
    elif any(word in lower for word in ("lease", "mro", "contract", "agreement", "租赁", "合同")):
        tasks.append(_make_task("review-contract", "Contract review", user_message))
    elif any(word in lower for word in ("risk", "liability", "exposure", "风险", "责任")):
        tasks.append(_make_task("legal-risk-assessment", "Risk and liability assessment", user_message))
    elif any(word in lower for word in ("draft", "response", "letter", "报告", "回复")):
        tasks.append(_make_task("legal-response", "Response drafting analysis", user_message))

    legal_complexity = any(
        word in lower
        for word in (
            "convention",
            "icao",
            "iata",
            "annex",
            "passport",
            "inad",
            "international",
            "公约",
            "护照",
            "航空公司",
            "机上",
        )
    )
    if legal_complexity:
        tasks = [
            _make_task(
                "brief",
                "Treaty and source research",
                "Research the governing statutes, regulations, treaties, and agency "
                "guidance with pinpoint source support for the user's question. "
                f"User request:\n{user_message}",
            ),
            _make_task(
                "compliance-check",
                "Operational compliance analysis",
                "Analyze airline/crew operational requirements, permitted onboard measures, "
                "reporting, handoff, and compliance limits. "
                f"User request:\n{user_message}",
            ),
            _make_task(
                "legal-risk-assessment",
                "Risk and liability assessment",
                "Assess carrier liability, passenger-rights/human-rights limits, evidence "
                "preservation risk, and escalation thresholds. "
                f"User request:\n{user_message}",
            ),
        ]

    if not tasks and _looks_like_aviation_legal_request(user_message, skill_hint):
        tasks.append(
            _make_task(
                "brief",
                "Legal research brief",
                f"Answer the user's legal request with source support:\n\n{user_message}",
            )
        )
    elif not tasks:
        tasks.append(_make_general_task(user_message))

    return WorkflowPlan(
        title="Legal workflow" if tasks[0].skill_name != GENERAL_TASK_SKILL else "General answer",
        user_language=language,
        issue_decomposition=[task.title for task in tasks],
        agent_tasks=tasks,
        integration_instructions=(
            "Integrate all specialist outputs into one polished answer in the user's language. "
            "Preserve citation links and include pinpoint citations wherever available. "
            "Include claim-level sanity check, online evidence verification, and sources for "
            "substantive legal answers."
            if tasks[0].skill_name != GENERAL_TASK_SKILL
            else (
                "Return one practical user-facing answer in the user's language. Include "
                "source links when web search was used or when factual claims need support. "
                "Do not add legal framing unless the user actually asked for it."
            )
        ),
    )


def _build_context_block(
    memory_summary: str,
    recent_messages: Iterable[ChatMessage],
    *,
    budget_tokens: Optional[int] = None,
) -> str:
    """Assemble the within-chat context digest under a token budget.

    The rolling summary is always included; the recent transcript fills the
    remaining budget newest-first (older turns drop out — they survive in the
    summary). Token-budgeted rather than a fixed message count so long pasted
    documents are kept intact instead of being chopped to a fixed char cap.
    """
    from .context import estimate_tokens, render_recent, select_recent_within_budget

    if budget_tokens is None:
        try:
            budget_tokens = current_settings().chat_context_token_budget
        except Exception:  # noqa: BLE001
            budget_tokens = 16_000

    # Durable cross-chat project context (brief + salient memory + rolling
    # summary). Surfacing it here puts standing jurisdiction pins and prior
    # decisions in front of the planner, the decomposition step, and the
    # integration turn — not just the author-time parent addendum.
    try:
        from .projects import active_project_context_block

        project_block = active_project_context_block().strip()
    except Exception:  # noqa: BLE001 — context injection must never break a run
        project_block = ""

    lines: list[str] = []
    if project_block:
        lines.append(project_block)
    summary = memory_summary.strip()
    if summary:
        lines.append(f"Current chat memory summary:\n{summary}")
    remaining = max(
        1_000,
        budget_tokens - estimate_tokens(summary) - estimate_tokens(project_block),
    )
    windowed = select_recent_within_budget(recent_messages, remaining)
    if windowed.kept:
        header = "Recent visible chat transcript:"
        if windowed.dropped:
            header += (
                f" (showing the {len(windowed.kept)} most recent of "
                f"{len(windowed.kept) + len(windowed.dropped)} turns; "
                "earlier turns are captured in the memory summary above)"
            )
        lines.append(header)
        lines.append(render_recent(windowed.kept))
    return "\n\n".join(lines)


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
    title: str = "Legal Analysis",
    *,
    watermark: Optional[str] = None,
) -> dict[str, str]:
    settings.outputs_dir.mkdir(parents=True, exist_ok=True)
    filename = f"legal_analysis_{uuid.uuid4().hex[:8]}.pdf"
    output_path = settings.outputs_dir / filename
    write_pdf_document(
        output_path=output_path,
        title=title,
        body_markdown=source_text,
        disclaimer="Legal AI assistant — not legal advice. All output must be reviewed by qualified counsel before reliance.",
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
    title: str = "Legal Analysis",
    *,
    watermark: Optional[str] = None,
) -> dict[str, str]:
    settings.outputs_dir.mkdir(parents=True, exist_ok=True)
    filename = f"legal_analysis_{uuid.uuid4().hex[:8]}.docx"
    output_path = settings.outputs_dir / filename
    write_docx_document(
        output_path=output_path,
        title=title,
        sections=[("", source_text)],
        disclaimer="Legal AI assistant — not legal advice. All output must be reviewed by qualified counsel before reliance.",
        watermark=watermark,
    )
    return {
        "filename": filename,
        "path": str(output_path.resolve()),
        "url": _artifact_url(settings, str(output_path)),
    }


class WorkflowExecutor:
    def __init__(self, settings: Optional[Settings] = None, provider: Optional[Provider] = None) -> None:
        setup_logging()
        self.settings = settings or current_settings()
        # Per-stream resolved settings (with auto-detected packs merged in).
        # Reset at the top of every stream() call.
        self._resolved = self.settings
        self.provider = provider or build_provider(self.settings)
        if provider is not None:
            self.planner_provider = provider
        else:
            try:
                self.planner_provider = build_provider(_planner_settings(self.settings), fast=True)
            except Exception as exc:
                log_workflow_event("workflow_planner_fast_fallback", {"reason": str(exc)[:500]})
                self.planner_provider = self.provider
        # Cite-check is a regex/tool-heavy post-step; the fast (smaller /
        # cheaper) model is well-suited and keeps the audit from doubling
        # the per-turn cost. Fall back to the main provider if the fast
        # variant cannot be built (e.g. provider has no fast tier).
        if provider is not None:
            self.cite_check_provider = provider
        else:
            try:
                self.cite_check_provider = build_provider(self.settings, fast=True)
            except Exception as exc:
                log_workflow_event(
                    "workflow_cite_check_fast_fallback", {"reason": str(exc)[:500]}
                )
                self.cite_check_provider = self.provider
        # Final-synthesis turn runs the full model but at a capped reasoning
        # effort — synthesis of researched findings is the dominant latency cost
        # at xhigh and gains little from the top tier.
        if provider is not None:
            self.integration_provider = provider
        else:
            integration_settings = _integration_settings(self.settings)
            if integration_settings is self.settings:
                self.integration_provider = self.provider
            else:
                try:
                    self.integration_provider = build_provider(integration_settings)
                except Exception as exc:
                    log_workflow_event(
                        "workflow_integration_provider_fallback", {"reason": str(exc)[:500]}
                    )
                    self.integration_provider = self.provider

    def plan(
        self,
        user_message: str,
        *,
        skill_hint: Optional[str] = None,
        recent_messages: Optional[list[ChatMessage]] = None,
        memory_summary: str = "",
    ) -> WorkflowPlan:
        context_block = _build_context_block(memory_summary, recent_messages or [])
        try:
            result = self._run_planner(
                self._routing_prompt(user_message, skill_hint=skill_hint, context_block=context_block),
                system=(
                    "You are a cost-aware router for an legal assistant. "
                    "Return only valid JSON."
                ),
            )
            routed = self._normalize_plan(self._parse_plan_result(result))
            log_workflow_event(
                "workflow_plan_routed",
                {
                    "execution_mode": routed.execution_mode,
                    "complexity": routed.complexity,
                    "routing_reason": routed.routing_reason,
                    "agent_count": len(routed.agent_tasks),
                    "provider": result.provider,
                    "model": result.model,
                    "usage": result.usage,
                },
            )
            if routed.execution_mode != "agent_workflow":
                return routed
            # A `simple` turn that the cheap router already decomposed into a
            # usable single task needs no second, heavier planning pass.
            if routed.agent_tasks:
                if not any(_is_low_information_task(task) for task in routed.agent_tasks):
                    return routed
                log_workflow_event(
                    "workflow_plan_router_low_quality",
                    {
                        "agent_count": len(routed.agent_tasks),
                        "low_information_tasks": [
                            {"id": task.id, "title": task.title, "task": task.task}
                            for task in routed.agent_tasks
                            if _is_low_information_task(task)
                        ],
                    },
                )
            complexity = routed.complexity
        except Exception as exc:
            if is_out_of_money_error(exc):
                raise
            log_workflow_event("workflow_plan_router_fallback", {"reason": str(exc)[:500]})
            complexity = "standard"

        return self._agent_workflow_plan(
            user_message,
            skill_hint=skill_hint,
            context_block=context_block,
            complexity=complexity,
        )

    def _run_planner(self, prompt: str, *, system: str, provider: Optional[Provider] = None) -> Any:
        # The shared WorkflowPlan schema is intentionally permissive for legacy JSON,
        # while OpenAI strict structured outputs require additionalProperties=false
        # through every nested object. Plain JSON avoids planner schema 400s; the
        # local parser/normalizer still validates and sanitizes the result.
        #
        # `provider` overrides the default fast planner. Cheap routing/classification
        # stays on the fast model; complex DECOMPOSITION is escalated to the main
        # model by the caller, because decomposition quality (how the breadth-first
        # query is split into disjoint, well-scoped specialists) is the single
        # largest driver of final-answer quality — a weak fast model collapses a
        # multi-body-of-law memo into one shallow mega-agent.
        structured_output = None
        return (provider or self.planner_provider).run(
            system=system,
            messages=[{"role": "user", "content": prompt}],
            tools=[],
            max_iterations=1,
            structured_output=structured_output,
        )

    def _routing_prompt(
        self,
        user_message: str,
        *,
        skill_hint: Optional[str] = None,
        context_block: str = "",
    ) -> str:
        prompt = (
            "Route this chat turn before any expensive specialist work.\n\n"
            "Execution modes:\n"
            "- direct_answer: greetings, UI/help questions, simple follow-ups, or "
            "short answers you are confident you can give from your own knowledge "
            "with no tools required.\n"
            "- clarify: essential facts are missing and any specialist work would "
            "be wasteful.\n"
            "- agent_workflow: anything that requires action or tools — legal "
            "research, document review, source verification, drafting/saving "
            "files, downloading or fetching external resources, web search, "
            "reading uploaded files, multi-issue analysis, or any request whose "
            "fulfilment depends on calling a tool you do not yourself have.\n\n"
            "Never use direct_answer to refuse a user request on capability "
            "grounds. If you are uncertain whether the system can do what the "
            "user asked, pick agent_workflow — a specialist with tools will "
            "decide. Direct_answer is only for high-confidence, no-tool replies.\n\n"
            "Also classify the COMPLEXITY of any agent_workflow turn — this sizes "
            "how much machinery runs, so judge it honestly:\n"
            "- simple: a single, self-contained legal/factual question, one "
            "instrument or one narrow issue, a quick lookup, a short document "
            "edit, or a follow-up that extends one prior answer. One specialist "
            "with tools can fully answer it in a single pass.\n"
            "- standard: a focused analysis with a few related sub-parts that "
            "share one evidence base (one contract review, one-jurisdiction "
            "compliance question, one incident). One specialist covers all "
            "sub-parts internally.\n"
            "- complex: a multi-section memo, a country-/market-entry analysis, a "
            "multi-jurisdiction comparison, or a request that enumerates a long "
            "outline of distinct topics (corporate + tax + labour + aviation + "
            "consumer + data, etc.). These need several specialists researching "
            "DISJOINT evidence bases in parallel, then one synthesis. For "
            "direct_answer/clarify use \"simple\".\n\n"
            "Return JSON matching this shape. For direct_answer or clarify, include the exact "
            "user-facing direct_response and leave agent_tasks empty. For agent_workflow, "
            "leave agent_tasks empty unless the turn is `simple` — a heavier "
            "planner decomposes `standard`/`complex` turns.\n"
            "{\n"
            '  "title": "short title",\n'
            '  "user_language": "English or Chinese or other",\n'
            '  "execution_mode": "agent_workflow | direct_answer | clarify",\n'
            '  "complexity": "simple | standard | complex",\n'
            '  "direct_response": "user-facing answer or clarification question",\n'
            '  "routing_reason": "brief reason",\n'
            '  "issue_decomposition": [],\n'
            '  "agent_tasks": [],\n'
            '  "integration_instructions": "",\n'
            '  "citation_requirements": ""\n'
            "}\n\n"
            "Do not use direct_answer for substantive legal claims, current factual claims, "
            "or search/research requests that need source support. "
            "For ordinary non-legal document, spreadsheet, deck, PDF, search, "
            f"or research tasks, use a single `{GENERAL_TASK_SKILL}` "
            "agent task rather than a legal skill. "
            "Default user-facing output to Chinese when the user writes in Chinese.\n"
        )
        if skill_hint:
            prompt += f"\nUser selected skill hint: {skill_hint}\n"
        if context_block:
            prompt += f"\nConversation context available to the router:\n{context_block[:12000]}\n"
        return f"{prompt}\nCurrent user request:\n{user_message}"

    def _agent_workflow_plan(
        self,
        user_message: str,
        *,
        skill_hint: Optional[str] = None,
        context_block: str = "",
        complexity: str = "standard",
    ) -> WorkflowPlan:
        if complexity == "complex":
            fanout_guidance = (
                "COMPLEXITY = complex. This is a multi-section memo / market-entry / "
                "multi-jurisdiction analysis. Decompose it into **a few specialists "
                "(target 2-3, hard max 3)**, each owning a DISJOINT evidence base, all runnable "
                "in parallel (empty depends_on). Derive the clusters from THIS "
                "request's own structure — group the outline topics by the research "
                "surface they share (the same body of law, regulator, jurisdiction, "
                "or level of the source hierarchy), so each specialist goes deep on a "
                "coherent slice rather than skimming everything. Any layer of the "
                "applicable-law stack that is genuinely live for the request "
                "(including controlling international / inter-party instruments where "
                "relevant) is a candidate cluster — let the request decide, do not "
                "impose a fixed template. Do NOT collapse the whole memo into one "
                "agent: a single agent cannot carry pinpoint depth across many "
                "topics. Keep coverage tokens disjoint across agents.\n\n"
            )
        else:
            fanout_guidance = (
                "COMPLEXITY = " + complexity + ". "
                "**Default to ONE specialist agent** unless the question genuinely "
                "splits into independent evidence bases. A focused question with "
                "several related sub-parts is ONE specialist that covers them "
                "internally via coverage tokens — do not fan out per sub-part.\n\n"
            )
        prompt = (
            "Create a careful workflow plan for an legal assistant that can also "
            "handle ordinary non-legal tasks. "
            "The planner decides how many specialist agents are needed; do not force "
            "a fixed count. Size the fan-out to the stated complexity below. For "
            "ordinary non-legal requests, including Office/PDF/spreadsheet/deck work, "
            f"create exactly one `{GENERAL_TASK_SKILL}` task.\n\n"
            + fanout_guidance
            + "Available skill_name values are exactly: "
            f"{', '.join(SKILL_NAMES)}, {GENERAL_TASK_SKILL}.\n\n"
            "Return JSON matching this schema:\n"
            "{\n"
            '  "title": "short workflow title",\n'
            '  "user_language": "English or Chinese or other",\n'
            '  "execution_mode": "agent_workflow",\n'
            '  "direct_response": "",\n'
            '  "routing_reason": "why specialist workflow is needed",\n'
            '  "issue_decomposition": ["issue 1", "issue 2"],\n'
            '  "agent_tasks": [\n'
            '    {"id": "task_1", "skill_name": "brief", "title": "short specialist title", '
            '"task": "agent-specific instructions, in whichever language fits the material. '
            'End the task body with a single line: '
            '`coverage: token1, token2, ...` enumerating the specific issues this specialist owns '
            '(e.g. `coverage: schengen-borders, carrier-liability, evidence-preservation`).", '
            '"depends_on": []}\n'
            "  ],\n"
            '  "integration_instructions": "what angles the final answer must cover and any user-specific framing",\n'
            '  "citation_requirements": "pinpoint citation requirements"\n'
            "}\n\n"
            "AGENT-COUNT TIE-BREAKERS (the COMPLEXITY guidance above sets the target "
            "count; use these to draw the boundaries cleanly):\n"
            "  - Each agent must own a genuinely disjoint evidence base / research "
            "surface (e.g. one pulls PRC statutes from PKULaw, another U.S. eCFR / "
            "Federal Register, another foreign-jurisdiction sources via web_search, "
            "another treaties / bilateral agreements).\n"
            "  - For a `standard` turn whose single question merely enumerates related "
            "angles that share one evidence base, use ONE specialist covering them via "
            "coverage tokens — do not fan out per angle.\n"
            "  - For a `complex` turn, prefer the multi-specialist split even when two "
            "clusters might touch a shared authority — just assign that authority's "
            "coverage token to exactly one agent so outputs stay disjoint.\n\n"
            "COVERAGE LINE — required on every task body:\n"
            "  - End every agent_tasks[].task with `coverage: <comma-separated tokens>`.\n"
            "  - Each token is a short kebab-case issue name.\n"
            "  - No two tasks in the same plan may share a coverage token. If two "
            "prospective tasks would share a token, merge them into one task.\n\n"
            "Every task must be self-contained as an assignment, but not as a reference dump. "
            f"If skill_name is `{GENERAL_TASK_SKILL}`, the task is a plain general assistant "
            "assignment: answer the user's everyday non-legal request, use hosted web search "
            "when current or source-backed facts are needed, use document tools and "
            "format recipes for file production/editing tasks, and do not inspect legal "
            "skills or the playbook. "
            "Do not paste whole playbook passages, SKILL.md procedures, or long source excerpts "
            "into agent_tasks[].task. Specialists have resource tools to inspect exact "
            "SKILL.md/playbook sections on demand. Include concise assumptions, legal issues, "
            "source targets, expected output shape, and named skill/playbook topics to inspect. "
            "Prefer parallelizable tasks with empty "
            "depends_on when they can be researched independently. Use depends_on only when "
            "one specialist genuinely needs another's findings to do its own research; do "
            "not use depends_on to sequence drafting or review steps. Require pinpoint "
            "citations: article, annex, standard/recommended practice, paragraph, section, "
            "page, or explicit `pinpoint unavailable`.\n\n"
            "Specialists emit a compact `Findings` bundle (terse one-sentence bullets with "
            "inline pinpoints), an `Out of scope` list, and a `Sources` list. They do NOT "
            "write user-facing prose, summaries, scenario walkthroughs, evidence logs, or "
            "claim-check sections — the orchestrator is the author of the final answer. "
            "Do not instruct specialists to produce polished or narrative output.\n\n"
            "LANGUAGE — user-facing output vs internal work:\n"
            "  - Sub-agent working language is free: write each agent_tasks[].title and "
            "agent_tasks[].task in whichever language fits the material — 中文 is "
            "encouraged for tasks grounded in PRC statutes, 司法解释, and other "
            "Chinese-language sources; English fits common-law, EU, or "
            "mixed-jurisdiction material. Do not instruct any sub-agent to draft the "
            "user-facing response or to translate anything — specialists gather "
            "findings; the orchestrator authors the final answer.\n"
            "  - `user_language` is purely a signal to the orchestrator's final integration "
            "step — it tells the orchestrator which language the final user-facing answer "
            "must be written in. It does NOT constrain specialists' working language.\n"
            "  - Quote source material verbatim in its original language regardless of "
            "the task's working language — e.g., article text from PRC statutes or "
            "judicial interpretations, treaty clauses, or the user's verbatim question "
            "for context.\n\n"
            "HARD RULES — the orchestrator performs these steps; do NOT create sub-agents for "
            "them:\n"
            "  1. No final-answer / final-summary / final-draft / synthesis / integration / "
            "translation agent. The orchestrator integrates specialist outputs into the "
            "user-facing answer in the user's language. Specialists must only gather and "
            "present the information they were asked to research — never the final response, "
            "and never a translated version of it.\n"
            "  2. No citation-verification / source-QC / pinpoint-audit agent. The "
            "orchestrator runs an automatic citation audit on the final answer. Each "
            "specialist is responsible for the pinpoint quality of its own citations.\n\n"
        )
        if skill_hint:
            prompt += f"The user selected this skill hint: {skill_hint}\n\n"
        if context_block:
            prompt += f"Conversation context available to the planner:\n{context_block[:16000]}\n\n"
        prompt += f"Current user request:\n{user_message}"
        # Decomposition runs on the FAST planner for every tier. Running it on the
        # main model at the user's effort (e.g. xhigh) is a multi-minute,
        # premium-priced turn before research even begins. The explicit
        # "target 2-3, hard max 3" guidance plus the agent-count cap in
        # `_normalize_plan` keep the breakdown disciplined without paying that
        # tax. (Latency + cost lever.)
        decomposition_provider = self.planner_provider
        try:
            result = self._run_planner(
                prompt,
                system=(
                    "You are the senior workflow planner for a multi-agent legal "
                    "research system. Planning quality is critical. Return only valid JSON."
                ),
                provider=decomposition_provider,
            )
            parsed = self._parse_plan_result(result).model_copy(
                update={"execution_mode": "agent_workflow", "complexity": complexity}
            )
            if parsed.agent_tasks:
                log_workflow_event(
                    "workflow_plan_decomposed",
                    {
                        "agent_count": len(parsed.agent_tasks),
                        "complexity": complexity,
                        "decomposition_on_fast_model": True,
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
        fallback_message = f"{context_block}\n\nCurrent user request:\n{user_message}" if context_block else user_message
        return self._normalize_plan(heuristic_plan(fallback_message, skill_hint))

    def _parse_plan_result(self, result: Any) -> WorkflowPlan:
        if getattr(result, "structured_payload", None):
            return self._parse_plan_data(result.structured_payload)
        return self._parse_plan_json(result.text)

    def _parse_plan_json(self, text: str) -> WorkflowPlan:
        import json

        raw = text.strip()
        if raw.startswith("```"):
            raw = re.sub(r"^```(?:json)?\s*", "", raw)
            raw = re.sub(r"\s*```$", "", raw)
        match = re.search(r"\{.*\}", raw, re.DOTALL)
        if match:
            raw = match.group(0)
        data = json.loads(raw)
        return self._parse_plan_data(data)

    def _parse_plan_data(self, data: dict[str, Any]) -> WorkflowPlan:
        if hasattr(data, "model_dump"):
            data = data.model_dump()
        data = dict(data)
        data.setdefault("title", "Legal workflow")
        data.setdefault("user_language", "English")
        # `document_export` was a deprecated keyword-routed mode; if a planner
        # still emits it (cache, old prompt), demote to agent_workflow so the
        # specialist or orchestrator can decide via tools.
        mode = data.get("execution_mode")
        if mode == "document_export":
            mode = "agent_workflow"
        if mode not in {"agent_workflow", "direct_answer", "clarify"}:
            mode = "agent_workflow"
        data["execution_mode"] = mode
        complexity = str(data.get("complexity") or "").strip().lower()
        if complexity not in {"simple", "standard", "complex"}:
            complexity = "standard"
        data["complexity"] = complexity
        data["direct_response"] = str(data.get("direct_response") or "")
        data["routing_reason"] = str(data.get("routing_reason") or "")
        data["artifact_format"] = None
        tasks = []
        for index, task in enumerate(data.get("agent_tasks") or [], start=1):
            skill = task.get("skill_name")
            if skill == GENERAL_TASK_SKILL:
                skill = GENERAL_TASK_SKILL
            elif skill not in SKILL_NAMES:
                skill = "brief"
            task_id = task.get("id") or f"task_{index}"
            task_id = re.sub(r"[^A-Za-z0-9_-]+", "_", task_id)
            tasks.append(
                {
                    "id": task_id,
                    "skill_name": skill,
                    "title": task.get("title") or f"Task {index}",
                    "task": task.get("task") or "",
                    "depends_on": [
                        re.sub(r"[^A-Za-z0-9_-]+", "_", str(dep))
                        for dep in (task.get("depends_on") or [])
                    ],
                }
            )
        data["agent_tasks"] = tasks
        if not data.get("citation_requirements"):
            if tasks and all(task["skill_name"] == GENERAL_TASK_SKILL for task in tasks):
                data["citation_requirements"] = (
                    "For ordinary factual or current-information claims, include useful source "
                    "links when web search was used or when the user needs verification."
                )
            else:
                data["citation_requirements"] = (
                    "Cite document/instrument plus pinpoint where available: article, annex, "
                    "standard/recommended practice, section, paragraph, page, clause, or "
                    "state `pinpoint unavailable`."
                )
        return WorkflowPlan(**data)

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

        merged_overlap: list[dict[str, str]] = []
        deduped: list[AgentTask] = []
        seen_skills_without_coverage: dict[str, str] = {}
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
            elif task.skill_name in seen_skills_without_coverage:
                overlap_target = seen_skills_without_coverage[task.skill_name]
            # Jurisdiction veto: never merge into a target that targets a
            # DIFFERENT, non-overlapping legal system. Comparative CN/US/EU work
            # is distinct evidence per jurisdiction — merging collapses the
            # multi-specialist fan-out (and FIG1 CoT display) into one bundle.
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
                    }
                )
                absorbed_by_target.setdefault(overlap_target, []).append(task)
                if coverage:
                    coverage_by_kept_id[overlap_target] |= coverage
                if task_juris:
                    jurisdictions_by_kept_id.setdefault(overlap_target, set()).update(task_juris)
                continue
            deduped.append(task)
            if coverage:
                coverage_by_kept_id[task.id] = coverage
            else:
                seen_skills_without_coverage.setdefault(task.skill_name, task.id)
            if task_juris:
                jurisdictions_by_kept_id[task.id] = set(task_juris)

        if merged_overlap:
            # A merged mandate must be executed, not dropped: rewrite each merge
            # target's body to append the absorbed task's instructions, carry a
            # single unioned `coverage:` line, and inherit the absorbed task's
            # dependencies (redirected/pruned by the dependency pass below).
            rewritten: list[AgentTask] = []
            for task in deduped:
                absorbed = absorbed_by_target.get(task.id)
                if not absorbed:
                    rewritten.append(task)
                    continue
                body = _strip_coverage_lines(task.task)
                merged_deps = list(task.depends_on)
                for dropped in absorbed:
                    dropped_body = _strip_coverage_lines(dropped.task)
                    if dropped_body:
                        body += (
                            "\n\nAdditional mandate merged from overlapping planner "
                            f"task '{dropped.title}' ({dropped.id}) — cover it fully:\n"
                            f"{dropped_body}"
                        )
                    for dep in dropped.depends_on:
                        if dep not in merged_deps:
                            merged_deps.append(dep)
                union = coverage_by_kept_id.get(task.id) or set()
                if union:
                    body += "\n\ncoverage: " + ", ".join(sorted(union))
                rewritten.append(
                    task.model_copy(update={"task": body, "depends_on": merged_deps})
                )
            deduped = rewritten
            log_workflow_event(
                "workflow_plan_coverage_deduped",
                {
                    "agent_count_before_dedup": len(kept),
                    "agent_count_after_dedup": len(deduped),
                    "merged": merged_overlap,
                    "rewritten_target_ids": sorted(absorbed_by_target),
                },
            )
        kept = deduped

        # Hard ceiling on specialist count. Each specialist is a full
        # (cheap-model) research loop, so an over-eager planner directly
        # multiplies latency and cost. The planner is told to target 2-3; this
        # enforces it regardless of what it emits. Dangling dependencies onto a
        # dropped task are pruned by the dependency pass below.
        MAX_SPECIALISTS = 3
        if len(kept) > MAX_SPECIALISTS:
            dropped_for_cap = kept[MAX_SPECIALISTS:]
            kept = kept[:MAX_SPECIALISTS]
            log_workflow_event(
                "workflow_plan_agent_capped",
                {
                    "cap": MAX_SPECIALISTS,
                    "dropped_task_ids": [t.id for t in dropped_for_cap],
                    "dropped_titles": [t.title for t in dropped_for_cap],
                },
            )

        kept_ids = {task.id for task in kept}
        # Rewrite any dependency that pointed at a dropped task to the merge target.
        dropped_to_target = {m["dropped_task_id"]: m["merged_into"] for m in merged_overlap}
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
                "issue_decomposition": [task.title for task in normalized],
                "integration_instructions": integration_instructions,
            }
        )

    def stream(
        self,
        user_message: str,
        *,
        attachments: Optional[Iterable[Path]] = None,
        recent_messages: Optional[list[ChatMessage]] = None,
        memory_summary: str = "",
        skill_hint: Optional[str] = None,
        cancel_event: Optional[threading.Event] = None,
    ) -> Iterator[StreamEvent]:
        run_id = new_run_id()
        token_run = run_id_var.set(run_id)
        token_parent = parent_run_id_var.set(None)
        token_agent = agent_name_var.set("orchestrator")

        # Auto-detect domain packs from the user message + recent messages and
        # merge with explicit settings.active_domain_packs. The resolved
        # settings live for the duration of this turn via
        # override_current_settings, so SkillAgent, _build_parent_addendum,
        # and the connector/MCP filters all see the merged set.
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
        # duration + the last provider turn's token usage. The Context widget
        # uses input_tokens as a context-window fill proxy; the run header + a
        # duration chip use duration_ms. turn_usage is mutated in the provider
        # `done` branches below (dict mutation is visible through the closure).
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
            # routes cheaply without paying for multimodal content. Real
            # provider content blocks are built where each sub-agent or the
            # integration step actually calls the model.
            content = _build_user_message(user_message, attachment_paths)
            context_block = _build_context_block(memory_summary, recent_messages or [])
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
            )
            raise_if_cancelled()
            log_workflow_event(
                "workflow_plan_created",
                {"plan": plan.model_dump(), "agent_count": len(plan.agent_tasks)},
            )
            yield StreamEvent("workflow_plan", plan.model_dump())

            if plan.execution_mode in {"direct_answer", "clarify"} and attachment_paths:
                # The cheap planner only sees filenames, not the file bytes.
                # When attachments are present, force the workflow to spin up a
                # specialist so the model actually sees the file content.
                plan = plan.model_copy(update={
                    "execution_mode": "agent_workflow",
                    "agent_tasks": [],
                    "routing_reason": (plan.routing_reason or "") + " (forced agent_workflow: attachments present)",
                })

            if plan.execution_mode in {"direct_answer", "clarify"}:
                final_text = plan.direct_response.strip()
                if not final_text:
                    final_text = (
                        "Could you clarify the missing details so I can route this properly?"
                        if plan.execution_mode == "clarify"
                        else "I can help with that."
                    )
                log_workflow_event(
                    "workflow_direct_output",
                    {
                        "execution_mode": plan.execution_mode,
                        "routing_reason": plan.routing_reason,
                        "response_chars": len(final_text),
                        "citation_audit_considered": _should_audit_direct_response(final_text),
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
                yield StreamEvent("done", _done_payload(final_text, audit_payload))
                return

            # Direct-LLM shortcut: when the plan is a single general-answer
            # task (no legal specialists), there is no value in the planner's
            # task rewrite, sub-agent dispatch, or tools-empty integration
            # step — they only strip the user's verbatim intent. Hand the
            # turn directly to the model with the full tool surface and the
            # user's original message + chat context, and stream its answer.
            # Single-pass shortcut: a `simple` turn, or any plan with at most one
            # task — whether an ordinary general task OR a single legal specialist —
            # is answered in ONE tool-armed pass by the main model. There is no
            # value in the specialist→synthesis double pass for a single bundle:
            # the second turn only reformats the first and (for legal) tends to
            # hedge a thin single bundle into a '待核验' checklist. Honour the
            # complexity dial directly so a `simple` classification stays cheap
            # even when the router over-emitted tasks. Reserve the
            # decompose→synthesize path for genuinely multi-specialist work.
            if plan.complexity == "simple" or len(plan.agent_tasks) <= 1:
                is_general_single = _is_general_plan(plan) or not plan.agent_tasks
                direct_tools: list = [
                    *hosted_search_tools_for_provider(self._resolved),
                    *_document_io_tools(),
                ]
                if not is_general_single:
                    direct_tools.extend(
                        visible_connector_tools(
                            [
                                self._resolved.default_jurisdiction,
                                *self._resolved.secondary_jurisdictions,
                            ],
                            self._resolved.active_domain_packs,
                        )
                    )
                    direct_tools.append(legal_source_search)
                directive_parts: list[str] = []
                if context_block:
                    directive_parts.append(context_block)
                if not is_general_single:
                    if (plan.integration_instructions or "").strip():
                        directive_parts.append(
                            "What the final answer must cover:\n"
                            + plan.integration_instructions.strip()
                        )
                    if (plan.citation_requirements or "").strip():
                        directive_parts.append(plan.citation_requirements.strip())
                directive_parts.append(f"Current user request:\n{user_message}")
                direct_user_text = "\n\n".join(directive_parts)
                direct_user_content = build_user_content(
                    direct_user_text, attachment_paths, self.provider.name
                )
                direct_system = (
                    _GENERAL_SYSTEM_PROMPT
                    if is_general_single
                    else _SINGLE_PASS_LEGAL_SYSTEM + _build_parent_addendum(self._resolved)
                )
                direct_chunks: list[str] = []
                stripper = _ToolXmlStripper()
                log_workflow_event(
                    "workflow_direct_llm_started",
                    {
                        "mode": "general" if is_general_single else "single_pass_legal",
                        "complexity": plan.complexity,
                        "tool_count": len(direct_tools),
                        "attachment_count": len(attachment_paths),
                        "user_language": plan.user_language,
                    },
                )
                for ev in self.provider.stream(
                    system=direct_system,
                    messages=[{"role": "user", "content": direct_user_content}],
                    tools=direct_tools,
                    max_iterations=self.settings.parent_max_iterations,
                ):
                    raise_if_cancelled()
                    if ev.kind == "delta":
                        clean = stripper.feed(ev.data.get("text", ""))
                        if clean:
                            direct_chunks.append(clean)
                            yield StreamEvent("delta", {"text": clean})
                    elif ev.kind == "done":
                        _capture_usage(ev)
                        if not direct_chunks and ev.data.get("text"):
                            clean = stripper.feed(ev.data.get("text", ""))
                            if clean:
                                direct_chunks.append(clean)
                                yield StreamEvent("delta", {"text": clean})
                        continue
                    else:
                        yield ev
                tail = stripper.flush()
                if tail:
                    direct_chunks.append(tail)
                    yield StreamEvent("delta", {"text": tail})
                final_text = "".join(direct_chunks)
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
                yield StreamEvent("done", _done_payload(final_text, audit_payload))
                return

            task_outputs, failed_tasks = yield from self._stream_tasks(
                plan, attachments=attachment_paths, cancel_event=cancel_event
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

            integration_prompt = self._integration_prompt(
                original_request=content,
                context_block=context_block,
                plan=plan,
                task_outputs=task_outputs,
                failed_tasks=failed_tasks,
            )
            integration_content = build_user_content(
                integration_prompt, attachment_paths, self.provider.name
            )
            final_chunks: list[str] = []
            stripper = _ToolXmlStripper()
            log_workflow_event(
                "integration_started",
                {
                    "agent_count": len(plan.agent_tasks),
                    "complexity": plan.complexity,
                    "user_language": plan.user_language,
                    "attachment_count": len(attachment_paths),
                },
            )
            for ev in self.integration_provider.stream(
                system=(
                    _GENERAL_SYSTEM_PROMPT
                    if _is_general_plan(plan)
                    else (_PARENT_PROMPT_BODY + _build_parent_addendum(self._resolved))
                ),
                messages=[{"role": "user", "content": integration_content}],
                tools=_document_io_tools(),
                max_iterations=self.settings.parent_max_iterations,
            ):
                raise_if_cancelled()
                if ev.kind == "delta":
                    clean = stripper.feed(ev.data.get("text", ""))
                    if clean:
                        final_chunks.append(clean)
                        yield StreamEvent("delta", {"text": clean})
                elif ev.kind == "done":
                    # We emit our own final done with citation audit below. If a
                    # provider only exposes text at completion, still surface it.
                    _capture_usage(ev)
                    if not final_chunks and ev.data.get("text"):
                        clean = stripper.feed(ev.data.get("text", ""))
                        if clean:
                            final_chunks.append(clean)
                            yield StreamEvent("delta", {"text": clean})
                    continue
                else:
                    yield ev
            tail = stripper.flush()
            raise_if_cancelled()
            if tail:
                final_chunks.append(tail)
                yield StreamEvent("delta", {"text": tail})
            final_text = "".join(final_chunks)
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
            yield StreamEvent("done", _done_payload(final_text, audit_payload))
        except WorkflowCancelled:
            yield StreamEvent("cancelled", {"message": "Cancelled by user."})
        except Exception as exc:
            if is_out_of_money_error(exc):
                yield StreamEvent("delta", {"text": OUT_OF_MONEY_MESSAGE})
                yield StreamEvent("error", {"message": OUT_OF_MONEY_MESSAGE, "out_of_money": True})
            else:
                yield StreamEvent("error", {"message": str(exc)})
        finally:
            run_id_var.reset(token_run)
            parent_run_id_var.reset(token_parent)
            agent_name_var.reset(token_agent)

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

    def _stream_tasks(
        self,
        plan: WorkflowPlan,
        *,
        attachments: Optional[list[Path]] = None,
        cancel_event: Optional[threading.Event] = None,
    ) -> Iterator[StreamEvent]:
        outputs: dict[str, str] = {}
        # task_id → error message for specialists that failed after retry.
        # Failed tasks count as completed for dependency purposes (dependents
        # run without the missing upstream bundle) and are reported to the
        # integration turn as failed coverage instead of aborting the run.
        failures: dict[str, str] = {}
        pending = {task.id: task for task in plan.agent_tasks}
        completed: set[str] = set()
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
                        # Research specialists run on the main (capable) model.
                        # The fast/mini model is not capable enough to do the
                        # thorough, fetch-driven primary-source research this work
                        # needs — it terminates early and returns hedged findings.
                        # Cost is contained by the disciplined fan-out (<=3
                        # specialists) and bounded iterations rather than by
                        # downgrading the model. Effort is capped via
                        # _research_settings.
                        provider = build_provider(_research_settings(self._resolved))
                        provider_name = getattr(provider, "name", provider_name)
                        provider_model = getattr(provider, "model", provider_model)
                        q.put((task.id, "event", StreamEvent("agent_task_started", task.model_dump()), None))
                        task_prompt = task.task
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
                                    f"{task.task}\n\n"
                                    "# Upstream specialist outputs (use as inputs to your own "
                                    "analysis — do not merely restate or summarize them):\n\n"
                                    + "\n\n".join(upstream_sections)
                                )
                        sub_attachments = list(attachments or [])
                        sub_user_content = build_user_content(
                            task_prompt, sub_attachments, getattr(provider, "name", "anthropic")
                        )
                        if task.skill_name == GENERAL_TASK_SKILL:
                            from .tools import fetch_url_to_artifact, read_document
                            general_tools = [
                                *hosted_search_tools_for_provider(self.settings),
                                fetch_url_to_artifact,
                                read_document,
                            ]
                            event_iter = provider.stream(
                                system=_GENERAL_SYSTEM_PROMPT,
                                messages=[{"role": "user", "content": sub_user_content}],
                                tools=general_tools,
                                max_iterations=self.settings.sub_agent_max_iterations,
                            )
                        else:
                            agent = SkillAgent(task.skill_name, provider, self._resolved)
                            event_iter = agent.stream(task_prompt, attachments=sub_attachments)
                        # Throttled live-preview state: emit a rolling tail of the
                        # sub-agent's output as ``agent_delta`` at most every 250ms
                        # so the UI can show what a specialist is producing without
                        # forwarding every token (which would flood N parallel agents).
                        reasoning_chars: list[str] = []
                        last_preview = 0.0

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
                        q.put(
                            (
                                task.id,
                                "done",
                                StreamEvent(
                                    "agent_task_finished",
                                    {
                                        "task_id": task.id,
                                        "skill_name": task.skill_name,
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
                elif status == "error":
                    # Only unrecoverable run-level errors (out of money) arrive
                    # as "error" now; per-task failures degrade via "failed".
                    raise RuntimeError(ev.data.get("message", "Agent task failed"))
                elif status == "failed":
                    failures[task_id] = ev.data.get("message", "specialist failed")
                    outputs.setdefault(task_id, "")
                    finished_ready.add(task_id)
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
        original_request: str,
        context_block: str,
        plan: WorkflowPlan,
        task_outputs: dict[str, str],
        failed_tasks: Optional[dict[str, str]] = None,
    ) -> str:
        failed_tasks = failed_tasks or {}
        parts = [
            "Author the final user-facing answer from the specialist findings below.",
            f"User language: {plan.user_language}.",
            plan.integration_instructions,
            plan.citation_requirements,
            "Original user request:",
            original_request,
        ]
        if context_block:
            parts.extend(["Conversation context:", context_block])
        parts.append("Specialist findings (raw input — never echo verbatim, never label by specialist):")
        for task in plan.agent_tasks:
            if task.id in failed_tasks:
                continue
            parts.append(
                f"\n### bundle:{task.id} ({task.skill_name})\n{task_outputs.get(task.id, '')}"
            )
        if failed_tasks:
            failed_lines = []
            for task in plan.agent_tasks:
                if task.id not in failed_tasks:
                    continue
                coverage = ", ".join(sorted(_task_coverage(task))) or "unspecified"
                failed_lines.append(
                    f"- {task.title} ({task.skill_name}, {task.id}; coverage: {coverage}): "
                    f"{failed_tasks[task.id][:300]}"
                )
            parts.append(
                "SPECIALIST FAILURES — these planned research bundles FAILED and "
                "produced no findings:\n" + "\n".join(failed_lines) + "\n"
                "Do NOT invent findings for the failed coverage. In the final "
                "answer, state explicitly (in the user's language) which angles "
                "could not be researched in this run and must be re-run before "
                "reliance on them."
            )
        if _is_general_plan(plan):
            parts.append(
                "This is an ordinary non-legal request. Give a practical answer in the "
                "user's language, preserve useful source links from the general task output, "
                "and do not add legal framing or legal-advice disclaimers unless "
                "the user actually asked for legal analysis."
            )
        else:
            parts.append(
                "AUTHORING RULES — read before writing a single sentence:\n"
                "- Treat the bundles above as raw `Findings` + `Sources` material. You are "
                "the author. You decide what to keep, merge, drop, reorder, or restate.\n"
                "- Directly address every question the user asked, in the order asked, with "
                "reasonable extension into adjacent points the practitioner needs to act on. "
                "Be comprehensive on the user's named angles. Do not drift into unrelated "
                "tangents.\n"
                "- The same fact appears **at most once** in the final answer. If two "
                "bundles make the same point in different wording, keep one. If two bundles "
                "cite the same authority for different propositions, merge them.\n"
                "- Pick **one** organising axis (the user's named dimensions, OR scenarios, "
                "OR required-vs-recommended). Never present the same content under two axes.\n"
                "- Do NOT add a per-specialist section. The reader does not know which "
                "bundle produced which bullet. Do NOT echo any bundle's `Findings` list "
                "verbatim.\n"
                "- COMMIT to what the specialists actually verified. When a bundle's "
                "`Sources` line carries a pinpoint (article/section/clause/page) AND is "
                "marked online-checked (`online-checked: yes`, `✔`, or a fetched URL), "
                "state that proposition as established fact with the pinpoint inline — do "
                "NOT downgrade it to 'may/typically/待核验'. Reserve hedging ('may', "
                "'subject to local-law confirmation', `pinpoint unavailable`) for claims "
                "the bundles genuinely could not verify. A wall of '待核验/未核验' on "
                "points the specialists DID confirm is a failure, not caution.\n"
                "- GOVERNING INSTRUMENTS FIRST. For any cross-border / route / market-entry "
                "/ transaction question, lead the relevant section with the specific "
                "bilateral or multilateral instrument in force between the named parties "
                "(air services agreement, tax treaty, BIT, convention + that state's "
                "ratification status) with article-level pinpoints when a bundle supplies "
                "them. Never substitute a generic framework convention (e.g. the Chicago "
                "Convention alone) for the actual agreement between the parties when a "
                "bundle found the latter.\n"
                "- End with EXACTLY ONE appendix titled `资料来源与核验` (for Chinese "
                "answers) or `Sources & Verification` (for English answers), as a single "
                "markdown table with columns: #, 主张/Claim, 依据/Pinpoint (with URL), "
                "在线核验/Online-checked (✔ / 未核验 / pinpoint unavailable), 备注/Note. "
                "Build the table from the union of the bundles' `Sources` lines. Do NOT "
                "also emit `在线证据核验记录`, `逐项主张核验`, `Evidence Verification Log`, "
                "`Claim-Level Sanity Check`, or a separate `Sources` section.\n"
                "- Inline citations in the body use markdown link form `[name](URL)` so the "
                "automatic citation audit can read them."
            )
        parts.append(
            "ABSOLUTE OUTPUT RULES:\n"
            "- You are NOT calling tools in this turn. No tools are available.\n"
            "- Do NOT emit any `<tool_call>`, `</tool_call>`, `<tool_result>`, "
            "`</tool_result>`, `<function_call>`, `<function_result>`, "
            "`<invoke>`, or `<parameters>` tags. Do not emit any pseudo-JSON "
            "function-call envelope such as `{\"skill_name\": ..., \"task\": ...}` "
            "as plain text.\n"
            "- The specialist findings above already represent completed tool work. "
            "Author from them; do not re-request them.\n"
            "- Output ONLY the polished user-facing answer in the user's language. "
            "Begin directly with the answer. Do not narrate your process."
        )
        return "\n\n".join(parts)
