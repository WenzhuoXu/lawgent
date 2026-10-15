"""One author loop per chat turn — the executor behind the web chat and the CLI.

The chat's own model reads every message and answers it, with every tool it
may need: hosted search, the legal connectors and corpus, the document tools,
the legal methods (`load_skill`), the domain packs (`load_pack`) and the
multi-agent workflow (`run_workflow`, in ``multi_agent.py``).

After the author finishes, the citation audit (when enabled) runs, repairs
what it flagged and only then reveals the answer.
"""

from __future__ import annotations

import json
import re
import threading
import time
import uuid
from pathlib import Path
from typing import Any, Callable, Iterable, Iterator, Optional

from . import answer_contract
from .agent import SkillAgent
from .answer_lint import AnswerLinter
from .attachments import build_user_content
from .authoring import author_system_prompt
from .chat_models import ChatMessage
from .citations import extract_citations
from .tools.citations import take_last_cite_check_payload
from .config import Settings, current_settings, load_settings, override_current_settings
from .documents import write_docx_document, write_pdf_document
from .errors import OUT_OF_MONEY_MESSAGE, is_out_of_money_error
from .logging_setup import (
    agent_name_var,
    log_workflow_event,
    new_run_id,
    parent_run_id_var,
    run_id_var,
    setup_logging,
)
from .multi_agent import _short_query_arg
from .providers import Provider, StreamEvent, build_provider
from .tool_exposure import mark_deferred
from .turn_context import TurnContext, open_turn
from .runs import RunStore, load_run
from .usage import usage_context_var
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


def _document_io_tools() -> list[Any]:
    """The document tools on the author's surface and a general specialist's."""
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
        # A diagram as a picture, for a Word document or the chat.
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
        # Fine detail on one page without another full render.
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


class _ToolXmlStripper:
    """Stream-safe filter that drops pseudo tool-call / tool-result XML blocks
    that the model occasionally emits as plain text (``<tool_call>{...}
    </tool_call>`` or ``<tool_result>{...}</tool_result>``) instead of invoking
    a function.

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


def _should_run_cite_check(final_text: str) -> bool:
    """Audit an answer that states law; let chit-chat, translations and
    reasoning answers through. Decided from the answer itself."""
    if not final_text or not final_text.strip():
        return False
    if re.search(r"https?://|\bSources?\b|资料来源|来源", final_text, re.IGNORECASE):
        return True
    return bool(_LEGAL_CLAIM_RE.search(final_text))


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

    ``ok`` and ``warnings`` drive the UI's audit card; ``report_markdown``,
    ``coverage_line``, ``do_not_file``, ``items`` and ``coverage`` carry the
    full audit.

    ``structured`` is the exact JSON payload ``cite_check_report_tool``
    produced (recovered via the ContextVar side-channel, un-truncated). When
    present it authoritatively supplies ``items[]`` and ``coverage`` (the
    per-citation rows the FIG4 inspector renders) and lets ``do_not_file`` come
    straight from the report object instead of a markdown regex. The
    markdown-regex path covers a report written as prose, where no structured
    payload was captured.
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

    A skipped audit (disabled, or an answer that states no law) has nothing to
    repair, and a pass needs no repair. Everything else — a critical item, a
    DO-NOT-FILE verdict — is actionable, because the report names the claims.
    """
    if not audit or audit.get("skipped") or audit.get("ok"):
        return False
    return bool(audit.get("items")) or bool(audit.get("do_not_file")) or bool(
        (audit.get("report_markdown") or "").strip()
    )


def _infer_cite_check_stakes(methods_used: Iterable[str], final_text: str) -> str:
    """Choose `filed` vs `internal` from the answer and the methods it used.

    `filed` triggers the strictest cite-check pass plus the DO NOT FILE
    escalation threshold inside `cite_check_report_tool`. Escalate when the
    text references a filing artefact or the turn used a drafting-for-
    submission method. `brief` — the generic research method behind almost
    every legal answer — never escalates: it made nearly every advisory memo
    trip the strict threshold.
    """
    if _FILED_STAKES_RE.search(final_text or ""):
        return "filed"
    if set(methods_used or ()) & {"legal-response", "litigation-analysis"}:
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


def author_tool_surface(settings: Settings, *, legal: bool = True, workflow: bool = True) -> list[Any]:
    """The author's tools — one surface for every turn.

    ``legal=False, workflow=False`` is a general-answer specialist's surface:
    the hosted search and document tools, without the legal connectors, the
    methods, or the workflow (depth is one — a specialist never delegates).
    """
    tools: list[Any] = [
        *hosted_search_tools_for_provider(settings),
        *_document_io_tools(),
    ]
    if legal:
        from .tools import _active_pack_jurisdictions
        from .tools.aviation_search import aviation_source_search
        from .tools.rag_tools import retrieve_legal
        from .tools.skill_resources import load_skill, read_skill_file

        packs = list(settings.active_domain_packs)
        # An active pack reaches the jurisdictions it declares (the aviation
        # pack's ET-scoped ECAA tools, its EU-scoped EASA tools), and its
        # playbook names them.
        jurisdictions = list(
            dict.fromkeys(
                [
                    settings.default_jurisdiction,
                    *settings.secondary_jurisdictions,
                    *_active_pack_jurisdictions(packs),
                ]
            )
        )
        tools.extend(visible_connector_tools(jurisdictions, packs))
        tools.append(legal_source_search)
        if "aviation" in packs:
            tools.append(aviation_source_search)
        # The local corpus (ICAO documents and treaties among it) is reached
        # only through RAG. PKULaw stays off this surface until its gateway
        # answers again (capability review 2026-10-07, R-6).
        tools.append(retrieve_legal)
        tools.extend([load_skill, read_skill_file])
        from .tools.pack_tools import load_pack, loadable_packs, pack_source_tools

        if loadable_packs(settings):
            # Pack source tools are deferred like every other source tool.
            tools.extend([load_pack, *pack_source_tools(settings)])
    if workflow:
        from .multi_agent import run_workflow_tool

        tools.append(run_workflow_tool(settings))
    from .projects import active_project_id

    if active_project_id():
        from .tools.project_tools import (
            project_brief_read,
            project_memory_search,
            project_memory_write,
        )

        tools.extend([project_memory_write, project_memory_search, project_brief_read])
    # Capability-side tools wait behind the provider's tool search.
    return mark_deferred(tools)


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
    configured default.
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


class WorkflowExecutor:
    _run_store: Optional[RunStore] = None

    def __init__(self, settings: Optional[Settings] = None, provider: Optional[Provider] = None) -> None:
        setup_logging()
        self.settings = settings or current_settings()
        self.provider = provider or build_provider(self.settings)
        if provider is not None:
            # An injected provider (tests, embedders) serves every role.
            self.cite_check_provider = provider
            self.author_provider = provider
        else:
            self.cite_check_provider = self._role_provider("cite_check")
            # The default row is the chat's own model and effort, which is the
            # primary provider itself; an override in role_profiles applies.
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
        resume_run_id: Optional[str] = None,
    ) -> Iterator[StreamEvent]:
        """Answer one chat turn: the author loop, then the citation audit."""
        run_id = new_run_id()
        token_run = run_id_var.set(run_id)
        token_parent = parent_run_id_var.set(None)
        token_agent = agent_name_var.set("orchestrator")
        # Durable run record, opened before any model call; reused when
        # resuming, so a paused workflow's finished bundles are not paid twice.
        chat_id = (usage_context_var.get() or {}).get("chat_id")
        self._run_store = RunStore(self.settings, run_id, chat_id=chat_id)
        answered = False
        resume_tasks: dict[str, dict[str, str]] = {}
        if resume_run_id:
            previous = load_run(self.settings, resume_run_id)
            if previous is not None:
                resume_tasks = previous.resumable_tasks()
                self._run_store.append_event(
                    "resumed_from",
                    {"run_id": resume_run_id, "reusable_tasks": sorted(resume_tasks)},
                )
                log_workflow_event(
                    "workflow_resumed",
                    {
                        "from_run_id": resume_run_id,
                        "reusable_task_count": len(resume_tasks),
                        "previous_status": previous.snapshot.status,
                    },
                )

        # Packs pinned in settings, a project or --domain-pack are active from
        # the start. Any other pack the author loads itself (`load_pack`) when
        # it judges the request falls in the pack's domain, so the system
        # prompt stays the same for the whole chat.
        pinned_packs = list(self.settings.active_domain_packs)

        def raise_if_cancelled() -> None:
            if cancel_event is not None and cancel_event.is_set():
                raise WorkflowCancelled()

        attachment_paths = [Path(p) for p in (attachments or [])]

        # Per-turn telemetry surfaced on the final `done` event: wall-clock
        # duration, the last provider request's usage, and who wrote it.
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
            payload: dict[str, Any] = {
                "text": text,
                "citation_audit": audit_payload,
                "provider": getattr(self.author_provider, "name", None),
                "model": getattr(self.author_provider, "model", None),
            }
            if turn_usage:
                payload["usage"] = dict(turn_usage)
            payload["duration_ms"] = int((time.monotonic() - turn_start) * 1000)
            return payload

        try:
            raise_if_cancelled()
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
            if pinned_packs:
                # Surface the pinned pack(s) to the UI so it can render a banner;
                # `load_pack` reports a pack the author loads during the turn.
                yield StreamEvent("active_domain_packs", {"packs": pinned_packs})
            tier = answer_contract.length_tier(self.settings.answer_length)
            turn = TurnContext(
                settings=self.settings,
                user_message=user_message,
                user_language=language,
                context_block=context_block,
                context_tail=context_tail,
                attachments=attachment_paths,
                cancel_event=cancel_event,
                run_store=self._run_store,
                resume_tasks=resume_tasks,
            )
            with open_turn(turn), override_current_settings(self.settings):
                system = author_system_prompt(self.settings)
                tools = author_tool_surface(self.settings)
                tail = answer_contract.author_tail(user_language=language, tier=tier)
                if skill_hint and skill_hint != "cite-check":
                    from .tools.skill_resources import render_skill

                    # A method the user picked in the interface: inlined, not
                    # left to the model to load.
                    picked = render_skill(skill_hint)
                    if not picked.startswith("ERROR"):
                        tail += "\n\nThe user picked this method for the turn — follow it:\n" + picked
                if context_tail:
                    tail = f"{context_tail}\n\n{tail}"
                content = build_user_content(
                    f"{tail}\n\nCurrent user request:\n{user_message}",
                    attachment_paths,
                    self.author_provider.name,
                    cache_prefix=context_block,
                )
                log_workflow_event(
                    "author_started",
                    {
                        "provider": getattr(self.author_provider, "name", None),
                        "model": getattr(self.author_provider, "model", None),
                        "length_tier": tier,
                        "skill_hint": skill_hint,
                        "tool_count": len(tools),
                        "attachment_count": len(attachment_paths),
                        "user_language": language,
                    },
                )
                if self._run_store is not None:
                    self._run_store.set_phase("answer")
                final_text, audit_payload = yield from self._verified_synthesis(
                    system=system,
                    content=content,
                    turn=turn,
                    capture_usage=_capture_usage,
                    cancel_event=cancel_event,
                    stage="answer",
                    tools=tools,
                    verbosity=answer_contract.VERBOSITY.get(tier),
                )
            raise_if_cancelled()
            answered = True
            yield StreamEvent("done", _done_payload(final_text, audit_payload))
        except WorkflowCancelled:
            # Paused, not lost: finished specialist bundles are on disk and a
            # resumed run reuses them.
            if self._run_store is not None:
                self._run_store.finish("paused", pause_reason="cancelled")
            yield StreamEvent(
                "cancelled",
                {"message": "Cancelled by user.", "run_id": run_id, "resumable": True},
            )
        except Exception as exc:
            out_of_money = is_out_of_money_error(exc) or str(exc) == OUT_OF_MONEY_MESSAGE
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
        stage: str,
        tools: list[Any],
        verbosity: Optional[str] = None,
        language: str = "",
    ) -> Iterator[StreamEvent]:
        """One author turn. Yields provider events, returns the answer text.

        With ``reveal=False`` the prose is buffered rather than streamed, so a
        verification pass can run before the reader sees a single citation.
        Tool calls, sources and phase events still stream either way. The text
        passes through the answer lint (whole lines at a time) on its way out.
        """
        chunks: list[str] = []
        stripper = _ToolXmlStripper()
        linter = AnswerLinter(language=answer_contract.language_code(language))
        got_delta = False
        thought: list[str] = []

        def emit_text(clean: str) -> Iterator[StreamEvent]:
            if not clean:
                return
            chunks.append(clean)
            if reveal:
                yield StreamEvent("delta", {"text": clean})

        def close_thought() -> None:
            # Reasoning deltas are previews and never stored; one `reasoning`
            # event per segment, logged where the segment ends, is what a
            # reload replays and what the event log keeps.
            text = "".join(thought).strip()
            thought.clear()
            if text:
                log_workflow_event("reasoning", {"text": text})

        for ev in self.author_provider.stream(
            system=system,
            messages=[{"role": "user", "content": content}],
            tools=tools,
            max_iterations=self.settings.parent_max_iterations,
            # Only a pinned length tier sets a verbosity; "auto" leaves the
            # provider's default.
            **({"verbosity": verbosity} if verbosity else {}),
        ):
            if cancel_event is not None and cancel_event.is_set():
                raise WorkflowCancelled()
            if ev.kind == "reasoning_delta":
                thought.append(ev.data.get("text", ""))
                yield ev
                continue
            close_thought()
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
        close_thought()
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
        turn: TurnContext,
        final_text: str,
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
            turn=turn,
            final_text=final_text,
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
        turn: TurnContext,
        capture_usage: Callable[[StreamEvent], None],
        cancel_event: Optional[threading.Event],
        stage: str,
        tools: list[Any],
        verbosity: Optional[str] = None,
    ) -> Iterator[StreamEvent]:
        """Write the answer, audit it, repair what the audit flagged, then reveal.

        Returns ``(final_text, audit_payload)``. When verification is disabled
        the prose streams as it is generated and no repair runs — repairing an
        answer the reader has already seen would append a second copy rather
        than correct the first.
        """
        buffered = self._verify_before_reveal()
        rounds = max(0, int(self.settings.max_repair_rounds)) if buffered else 0
        draft = yield from self._synthesis_pass(
            system=system,
            content=content,
            reveal=not buffered,
            capture_usage=capture_usage,
            cancel_event=cancel_event,
            stage=stage,
            tools=tools,
            verbosity=verbosity,
            language=turn.user_language,
        )
        if self._run_store is not None:
            self._run_store.set_phase(stage)
            self._run_store.write_artifact(f"{stage}-draft", draft, title="Draft answer")
        audit: Optional[dict[str, Any]] = None
        for attempt in range(rounds + 1):
            last = attempt >= rounds
            audit = yield from self._audit_pass(
                turn=turn,
                final_text=draft,
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
                    self._repair_prompt(draft, audit, turn.user_language),
                    list(turn.attachments),
                    self.author_provider.name,
                ),
                reveal=False,
                capture_usage=capture_usage,
                cancel_event=cancel_event,
                stage="repair",
                tools=tools,
                verbosity=verbosity,
                language=turn.user_language,
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

    def _verify_before_reveal(self) -> bool:
        """True when answers are audited before the reader sees them.

        The author's answer is buffered whenever the audit is on; one that
        states no law skips the audit (``_should_run_cite_check``), so the hold
        costs only latency.
        """
        return bool(self.settings.verify_before_reveal and self.settings.enable_cite_check)

    def _repair_prompt(
        self,
        draft: str,
        audit: dict[str, Any],
        language: str = "",
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
        lines += ["", answer_contract.repair_rules(language or "Chinese")]
        return "\n".join(lines)

    def _run_cite_check_after_summary(
        self,
        *,
        turn: TurnContext,
        final_text: str,
        cancel_event: Optional[threading.Event],
    ) -> Iterator[StreamEvent]:
        """Run the /cite-check skill, as a SkillAgent sub-agent, on the answer.

        Skips an answer that states no law (chit-chat, a translation, a
        reasoning answer) — see `_should_run_cite_check`.

        Streams the cite-check sub-agent's events to the caller and yields a
        final ``citation_audit`` event (see `_parse_cite_check_payload`).
        """
        def raise_if_cancelled() -> None:
            if cancel_event is not None and cancel_event.is_set():
                raise WorkflowCancelled()

        if not self.settings.enable_cite_check:
            payload = _skipped_cite_check_payload(reason="cite-check disabled (enable_cite_check=false)")
            log_workflow_event("citation_audit_skipped", {"reason": payload["skip_reason"]})
            yield StreamEvent("citation_audit", payload)
            return

        if not _should_run_cite_check(final_text):
            payload = _skipped_cite_check_payload(
                reason="the answer states no law — no legal citations expected"
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

        # Explicit court-filing / regulatory signals → strict pass.
        stakes = _infer_cite_check_stakes(turn.methods_used, final_text)

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

        # The cite_check role runs on the fast tier — the work is tool-driven
        # (extract_citations_tool, validate_citations_tool,
        # cite_check_report_tool) and doesn't need the author's full model.
        agent = SkillAgent("cite-check", self.cite_check_provider, self.settings)
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
            for ev in agent.stream(task_text, attachments=list(turn.attachments)):
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


