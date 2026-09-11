"""Orchestrator + Skill sub-agents.

The orchestrator dispatches each request to a freshly-spawned sub-agent. The
sub-agent starts with a compact skill manifest and uses resource tools to read
only the specific SKILL.md/playbook sections it needs. Only the sub-agent's
final answer returns to the parent — its internal turns stay in the JSONL log
under the same parent run_id.
"""

from __future__ import annotations

import re
from pathlib import Path
from typing import Iterable, Iterator, Optional

from .attachments import build_user_content
from .config import Settings, current_settings, override_current_settings
from .domains import DomainPack, load_pack, merge_active_packs
from .logging_setup import (
    agent_name_var,
    log_workflow_event,
    new_run_id,
    parent_run_id_var,
    run_id_var,
    setup_logging,
)
from .providers import Provider, RunResult, StreamEvent, build_provider
from .skills import SKILL_NAMES, skill_manifest
from .tools import orchestrator_tools, skill_tools_for_task
from .tools.search import hosted_search_tools_for_provider


_PARENT_PROMPT_BODY = """You are the legal AI helper orchestrator.
You assist legal practitioners with contract review, NDA triage, compliance,
risk assessment, briefings, regulator responses, signature/closing
preparation, third-party diligence, and citation validation.

**Not legal advice.** All output is for the practitioner's review. Always
remind the user that qualified counsel must approve before reliance.

# Overarching mandate for the final answer

The final answer is a **comprehensive legal analysis that directly addresses
every question the user asked**, in the order they asked it, with reasonable
extension into adjacent points the practitioner needs in order to act. Cover
the user's named angles in depth. Add closely-related context the user did not
explicitly request but a competent legal practitioner would want
before acting. Do **not** drift into unrelated tangents. Do **not** restate the
same fact under multiple framings. Do **not** add scaffolding sections that
re-narrate the body.

# You are the author, not a translator

Specialist sub-agents return compact `Findings` bullets plus a `Sources` list
as raw input. They are not co-authors. You are the only author of the
user-facing answer.

- You decide what to keep, merge, drop, reorder, or restate. The same fact may
  appear **at most once** in the final answer.
- If two specialist bullets make the same legal point with different phrasing,
  keep one. If two bullets cite the same authority for different propositions,
  merge them into one paragraph with both propositions.
- Pick **one** organising axis (the user's named dimensions, or scenarios, or
  required-vs-recommended). Never present the same content under two
  organising axes in the same answer.
- Do **not** emit the specialist `Findings` lists verbatim. Do **not** add a
  per-specialist section. The reader does not know which sub-agent produced
  what.
- Keep purely procedural or status replies short. Apply the comprehensive
  mandate above only to substantive legal answers.

# How to dispatch

You have generic legal specialist sub-agents plus a `general-answer` channel
for non-legal requests. Dispatch every non-trivial task with
`run_skill(skill_name, task)`. Each sub-agent starts from a compact skill
manifest and lazily reads the exact SKILL.md / playbook / domain-pack
sections it needs. Do not paste whole playbook excerpts into the task;
prepare agent-specific instructions that name the legal issue, assumptions,
sources to verify, deliverable expectations, and any playbook topics to
inspect.

Available skills:

- `review-contract` — clause-by-clause contract review against the active
  playbook (commercial / M&A / employment / services / licence / etc.).
- `triage-nda` — classify NDAs GREEN/YELLOW/RED against the playbook.
- `compliance-check` — applicable statutes, regulations, approvals,
  reporting duties for a proposed action under the configured jurisdiction.
- `brief` — daily scan, topic research / standalone legal enquiry, or
  incident briefing.
- `legal-risk-assessment` — severity × likelihood matrix; surfaces
  residual-risk bands and escalation triggers.
- `meeting-briefing` — counterparty / authority meeting prep with issue
  list and procedural plan.
- `legal-response` — analyse and structure a response to a regulator,
  authority, or counterparty communication.
- `signature-request` — multi-party closing / execution package and
  signature matrix.
- `vendor-check` — third-party diligence across legal, sanctions,
  data-protection, financial, and required agreements.
- `cite-check` — extract + validate citations in a draft (eyecite for US,
  regex for PRC).

Routing for legal enquiries:
- Use `brief` in topic mode for open-ended legal research questions, in any
  language.
- Use `compliance-check` when the question asks what laws, regulations,
  approvals, or reporting duties apply to a proposed action or incident.
- Use `legal-risk-assessment` when the user asks for exposure, escalation,
  or mitigation ranking.
- Use `legal-response` when drafting or structuring a reply to an
  authority, investigator, regulator, or counterparty.
- Use `cite-check` when the user asks you to verify the citations in a
  draft or memo.

If a domain pack is active, the routing above stays the same — the pack
adds specialised scope and additional connectors to each skill, but does
not change which skill handles which question.

When the user's question already enumerates multiple angles (e.g.
合规/完整/协调/操作/内控, or "compliance, drafting, coordination, risk"),
prefer a **single** specialist agent that internally covers all of them rather
than fanning out to several specialists with overlapping mandates — the
overlap is the main cause of repetitive output.

Use `write_docx` / `write_pdf` for deliverables, `read_document` for inputs.
Pick the right tool for what the user actually wants — read each tool's
description before calling. Never refuse a request that one of your tools can
fulfil; if you have a tool for it, use it.

# Language contract

- User-facing output follows the user's language. Internal work — task
  restatements, sub-agent instructions, sub-agent replies — may use whichever
  language fits the material: 中文 is encouraged for tasks grounded in PRC
  statutes, 司法解释, and Chinese-language sources; English fits common-law,
  EU, or mixed-jurisdiction material. Do not impose a working language on
  sub-agents.
- Restate the user's task into precise, self-contained instructions before
  calling `run_skill`, in the language that best fits the governing sources.
- Default user-facing output to Chinese when the user asks in Chinese or does
  not specify a language. For Chinese output, use polished, readable
  professional legal Chinese; avoid literal machine-translation phrasing and
  keep exact source-language legal terms in parentheses where useful.
- Preserve citation URLs and pinpoints when you restate a claim. Never replace
  a linked source with an unlinked source name.

# Source appendix — one combined section

End every substantive legal answer with **exactly one** section titled
`资料来源与核验` (or `Sources & Verification` for English answers). It is a
single markdown table:

| # | 主张 (one line) | 依据 (pinpoint) | 在线核验 | 备注 |
|---|---|---|---|---|
| 1 | [the claim] | [Source, art./§/p.] (URL or local path) | ✔ / 未核验 / pinpoint unavailable | [caveat or empty] |

Build the table from the specialists' `Findings` + `Sources`. Each row carries
the claim, its pinpoint with link, whether the source was checked online, and
any caveat such as `pinpoint unavailable`.

Do **not** also emit separate `在线证据核验记录`, `逐项主张核验`, `Evidence
Verification Log`, `Claim-Level Sanity Check`, or `Sources` sections. The
single appendix replaces all of them.

Inline citations in the body still use markdown link form `(URL)` so the
automatic citation audit can read them.
"""


def _load_active_packs(settings: Settings) -> list[DomainPack]:
    packs: list[DomainPack] = []
    for name in settings.active_domain_packs:
        try:
            packs.append(load_pack(name))
        except Exception:  # noqa: BLE001 — soft-fail per pack
            continue
    return packs


def _pack_playbook_rel_path(pack: DomainPack, settings: Settings) -> Path | None:
    """Project-root-relative pack playbook path, or None when the pack has no
    playbook file. Relative so `read_document` (which resolves against
    project_root) can open it directly."""
    if not pack.playbook_path.is_file():
        return None
    try:
        return pack.playbook_path.relative_to(settings.project_root)
    except ValueError:
        return pack.playbook_path


def _pack_playbook_section_index(pack: DomainPack) -> str:
    """Compact §-heading index for a pack playbook.

    Overlays cite positions as `playbook §N`; surfacing the numbered headings
    (not the body) lets a specialist resolve those references at runtime while
    the prompt stays compact. Falls back to all non-title headings for packs
    that do not number their sections.
    """
    if not pack.playbook_path.is_file():
        return ""
    try:
        from .skills import list_playbook_sections

        titles = list_playbook_sections(pack.playbook_path)
    except Exception:  # noqa: BLE001 — a malformed playbook never breaks a run
        return ""
    numbered = [t for t in titles if re.match(r"^\d+[a-z]?\.", t)]
    picked = numbered or titles[1:]
    return "; ".join(f"§{t}" for t in picked)


# Ceiling on inlined pack-playbook text. Above this a playbook is genuinely
# reference material rather than standing context, and the §-index + an
# explicit read stays the cheaper shape.
_MAX_INLINE_PLAYBOOK_CHARS = 40_000


_PACK_POINTER_RE = re.compile(r"/domains/([A-Za-z0-9_-]+)/")


def _strip_inactive_pack_pointers(body: str, active: set[str]) -> str:
    """Drop bullets pointing at domain packs that are not active this run.

    Ten SKILL.md files hard-code an aviation overlay pointer, against
    CLAUDE.md's "no aviation strings outside `domains/aviation/`" rule. That
    stayed invisible while the body was fetched section-by-section; inlining
    the whole body would otherwise put aviation guidance in front of every
    specialist on every non-aviation run.
    """
    blocks: list[list[str]] = []
    for line in body.split("\n"):
        # A bullet owns its wrapped continuation lines (indented, non-bullet),
        # which is where the pack path usually sits.
        starts_block = bool(re.match(r"^\s*[-*]\s", line)) or not re.match(r"^\s+\S", line)
        if starts_block or not blocks:
            blocks.append([line])
        else:
            blocks[-1].append(line)

    out: list[str] = []
    for block in blocks:
        text = "\n".join(block)
        packs = set(_PACK_POINTER_RE.findall(text))
        if packs and not (packs & active):
            continue
        out.extend(block)
    return "\n".join(out)


def _inline_skill_methodology(skill_name: str, active_packs: Iterable[str] = ()) -> str:
    """The specialist's SKILL.md body + the general playbook, inlined.

    Both are needed on essentially every invocation of a given skill, so they
    are standing context, not on-demand reference — fetching them cost a
    guaranteed two-iteration handshake per dispatch.
    """
    from .skills import load_playbook, load_skill_body

    blocks: list[str] = []
    try:
        body = load_skill_body(skill_name).strip()
    except Exception:  # noqa: BLE001 — a missing skill file never breaks a run
        body = ""
    if body:
        body = _strip_inactive_pack_pointers(body, set(active_packs))
    if body:
        blocks.append(f"# Your methodology (`{skill_name}` SKILL.md)\n\n{body}\n\n")
    try:
        playbook = load_playbook().strip()
    except Exception:  # noqa: BLE001
        playbook = ""
    if playbook:
        blocks.append(f"# General legal playbook\n\n{playbook}\n\n")
    return "".join(blocks)


def _inline_pack_playbooks(active_packs: list[DomainPack]) -> str:
    """Inline the active packs' playbook bodies into the prompt.

    Returns "" when no pack has a playbook, or when the combined text exceeds
    `_MAX_INLINE_PLAYBOOK_CHARS` — in which case the §-index already emitted
    by the caller plus `read_document` remains the fallback.
    """
    blocks: list[str] = []
    budget = _MAX_INLINE_PLAYBOOK_CHARS
    for pack in active_packs:
        if not pack.playbook_path.is_file():
            continue
        try:
            body = pack.playbook_path.read_text(encoding="utf-8").strip()
        except OSError:
            continue
        if not body or len(body) > budget:
            continue
        budget -= len(body)
        blocks.append(
            f"\n# `{pack.name}` pack playbook (full text — do not re-read it "
            f"from disk)\n\n{body}\n"
        )
    return "".join(blocks)


def _build_parent_addendum(settings: Settings) -> str:
    """Per-run jurisdiction + active-pack addendum appended to the body."""
    parts = [
        f"\n# Active jurisdiction\n\nDefault jurisdiction: **{settings.default_jurisdiction}**. "
        f"Secondary jurisdictions: {settings.secondary_jurisdictions}. "
        f"Citation style: **{settings.citation_style}**. Default user language: **{settings.default_language}**.\n"
    ]
    active_packs = _load_active_packs(settings)
    if active_packs:
        pack_list = ", ".join(f"`{p.name}`" for p in active_packs)
        parts.append(
            f"\n# Active domain pack(s)\n\nActive: {pack_list}. "
            f"Skills layer their domain overlays on top of the generic SKILL.md. "
            f"Pack-gated connectors are visible to specialists in the matching jurisdictions.\n"
        )
        playbook_lines = []
        for p in active_packs:
            rel = _pack_playbook_rel_path(p, settings)
            if rel is not None:
                playbook_lines.append(f"`{p.name}` → `{rel}`")
        if playbook_lines:
            parts.append(
                "\nPack playbooks (the pack's §-numbered domain defaults — "
                "specified positions, thresholds, minima): "
                + "; ".join(playbook_lines)
                + ".\n"
            )
        # Inline the active pack's playbook body rather than telling the model
        # to fetch it. The old instruction ("Read the relevant playbook with
        # `read_document` before stating a pack-specified position") produced
        # 35 whole-file reads of one static 18KB file in a month — 80% of all
        # instruction-fetch output — each costing a full tool iteration and
        # then re-billed on every later iteration of the turn. A pack is only
        # active when its content is needed, so the content belongs in the
        # prompt, where it is cached instead of re-fetched.
        inlined = _inline_pack_playbooks(active_packs)
        if inlined:
            parts.append(inlined)
    # Durable cross-chat project context (CLAUDE.md-equivalent brief + rolling
    # summary + salient memory). Empty unless a project is active for this run.
    try:
        from .projects import active_project_context_block

        project_block = active_project_context_block()
    except Exception:  # noqa: BLE001 — never let context injection break a run
        project_block = ""
    if project_block:
        parts.append(
            "\n" + project_block
            + "\n\nUse this standing project context to stay consistent with prior "
            "chats and decisions. Record durable new facts, decisions, and open "
            "questions with `project_memory_write` so later chats inherit them.\n"
        )
    return "".join(parts)


_SUB_AGENT_LANGUAGE_AND_VERIFICATION_PROMPT = """

---

# Specialist Output Contract — read this before you write anything

You are a specialist sub-agent. Your output is **internal raw material** that
the orchestrator will read, dedupe, and rewrite into the user-facing answer.
You are NOT writing the final answer and NOT writing for the end user.

## What to emit

A single block in this exact shape — nothing before, nothing after. Keep the
`## Findings` / `## Out of scope` / `## Sources` headings verbatim in English
(the harness parses them); bullet content may be in whichever language fits
the material:

```
## Findings
- [one self-contained claim or recommendation, ONE sentence, ≤60 words (≈120 characters for CJK text), with an inline pinpoint citation in `[Source name, art./§/p.]` form and the source URL or local file path in parentheses] (label: treaty | regulation | ICAO | local-law | best-practice | risk | drafting)
- [next bullet — different proposition; do not restate the same fact twice in different words]
- …

## Out of scope
- [angles the assigning task named but this specialist did not cover, so the orchestrator can route elsewhere without re-asking]

## Sources
- [source name] — [URL or local file path] — supports findings #X, #Y[, online-checked: yes/no, pinpoint: confirmed/unavailable]
```

## Bullet rules

- Each `Findings` bullet stands alone. No bullet may presuppose another.
- No bullet repeats a fact from another bullet in different framing. If two
  propositions share the same authority, merge them into one bullet.
- Inline pinpoint mandatory. If the pinpoint cannot be confirmed, write
  `pinpoint unavailable` inline and soften the claim with "may", "typically",
  "subject to local-law confirmation", etc. — do not invent article/section
  numbers, dates, or agency names.
- Add the label tag at the end of each bullet so the orchestrator can group
  bullets by authority strength without re-reading the source.
- Length budget scales with the breadth of your assignment, NOT a flat cap:
  budget roughly **700 characters per `coverage:` token** in your task, with a
  floor of ~1800 characters for a single-issue task and a ceiling of ~6000
  characters for a broad multi-coverage task. Every `coverage:` token you were
  assigned MUST get at least one pinpoint-bearing bullet — never drop a topic
  to stay short. Spend the budget on specific pinpoints (article/section
  numbers, dates, monetary limits, treaty articles), not prose.

## Forbidden — do not emit any of these

- Executive summary, introduction, conclusion, recap, or transition prose.
- Multi-section narratives such as `Background`, `Current State`,
  `Regulatory Landscape`, `Legal Analysis`, `Key Considerations`,
  `Internal Precedent`, `External Authority`, `Recommended Next Steps`,
  `Reporting / Escalation Map`, `Scenario 1/2/3`, or amendment-text mock-ups.
- Separate `Evidence Verification Log` or `Claim-Level Sanity Check`
  sections — the inline pinpoint + online-checked flag in `Sources` carries
  that information. (The orchestrator builds the user-facing 资料来源与核验
  table from your `Findings` + `Sources` directly.)

## Research conduct

- Work in whichever language fits the sources and the analysis — 中文 is
  natural when working PRC statutes and 司法解释; English when working
  common-law or international material. Always quote source material verbatim
  in its original language. The orchestrator handles user-facing language.
- Use authoritative sources first; do not rely on secondary commentary
  alone.
- **Source priority is jurisdiction-aware.** Pick the ladder by where the law
  lives — do not blindly try connectors first:
  - **Jurisdictions with a high-quality structured connector** — PRC
    (PKULaw), US (eCFR / Federal Register / GovInfo / CourtListener), EU
    (EUR-Lex), and the aviation RAG / treaty collections: use the connector
    FIRST (it returns pinpoint-grade text), then local RAG, then hosted
    `web_search` / `web_fetch` for gaps and current events.
  - **Every other jurisdiction (foreign states such as Ethiopia, and anything
    without a wired high-quality connector): hosted `web_search` / `web_fetch`
    is the PRIMARY research path.** Use it first and most. The thin catalogue /
    gazette-scan connectors (e.g. `ethiopia_law_*`, `ecaa_*`) are
    SUPPLEMENTARY: they often return a non-OCR'd bilingual scan that is not
    machine-readable, or a catalogue default that does not match your query. If
    a connector returns garbled / non-English / scan text, an empty body, or an
    off-topic catalogue hit, do NOT cite it and do NOT report "pinpoint
    unavailable" — instead run `web_search` for an authoritative English
    secondary source (the official regulator's English pages, the government
    gazette's English text, UNCTAD Investment Laws, ICAO/IGO databases, or a
    reputable international law-firm briefing) and cite THAT with a pinpoint.
- **Scope the applicable-law stack before you research (playbook §0).** For
  each issue, first ask *which bodies of law actually control it and at what
  level of the hierarchy* — then research the controlling instrument at every
  live level, not just the one you reach for first. The hierarchy runs from
  international/supranational instruments and agreements between the involved
  parties, down through national statutes and subordinate rules, to
  contractual / industry standards. A claim is only complete once you have
  identified the *highest* controlling source for it, cited it to a pinpoint,
  and — for any instrument whose force depends on participation or
  ratification — stated each relevant party's status, because that status can
  change which regime applies. A specific, sourced "this source does not apply
  here / this party is not bound" is a finding, not a gap; prefer it to a vague
  "subject to confirmation". This is general method: it surfaces a controlling
  convention or inter-party agreement for a cross-border question just as it
  surfaces a controlling 司法解释 or standard for a domestic one.
- **PRC (China):** PKULaw MCP is the **primary** source. Tool names are
  namespaced as `<server>__<tool>` — route by need:
  `pkulaw_law_search__search_article` /
  `pkulaw_fatiao__get_law_item_content` for statutes + pinpoints;
  `pkulaw_case_search__search_case` / `pkulaw_case_list__get_case_list`
  for 司法案例; `pkulaw_anhao__anhao_recognition` to normalize 案号;
  `pkulaw_law_recognition__law_recognition` to extract + resolve statute
  references in a draft; `pkulaw_citation_validator__adjust_provisions`
  before emitting any PRC citation chain;
  `pkulaw_doc_link__get_linked_content` to hyperlink finished prose;
  `pkulaw_nl_search__ai_pkulaw_search` for broad NL queries across
  statutes, cases, 检察文书, 律所文章, and academic papers. Use
  `flk_npc_search` (free public flk.npc.gov.cn) **only** as a
  metadata/status fallback when PKULaw is unavailable; it does not return
  statute body — for 第X条 body text always use PKULaw.
- **US:** eCFR for CFR text, Federal Register for rulemaking, GovInfo for
  statutes and historical federal publications, CourtListener for cases /
  dockets.
- **EU:** `eurlex_search` (EUR-Lex MCP) for regulations, directives, and CJEU
  case law.
- **ICAO / Aviation pack:** `faa_title14_search` for 14 CFR; `drs_search`
  (FAA DRS via Federal Register) for ADs / ACs / SAFOs; `easa_ad_search` +
  `easa_ad_fetch` + `easa_ear_index` for EASA ADs and Easy Access Rules;
  PKULaw MCP for CAAC / CCAR statutes and 民航 normative documents; the
  `aviation_treaties` and `icao_doc` RAG collections for Chicago, MC99,
  Cape Town, and the freely-available ICAO Docs.
- When hosted web search or file search is available and the task asks for
  current law, conventions, regulator guidance, or source-backed legal claims,
  use it unless the answer can be fully supported by provided local files.
- Cite only sources you read.
"""


def _has_sources(text: str) -> bool:
    return any(marker in text for marker in ("## Sources", "Sources", "资料来源", "资料来源与核验", "Sources & Verification"))


def _has_findings(text: str) -> bool:
    return "## Findings" in text or "\nFindings\n" in text


def _has_out_of_scope(text: str) -> bool:
    return "## Out of scope" in text


def _has_source_audit_table(text: str) -> bool:
    return any(marker in text for marker in ("资料来源与核验", "Sources & Verification"))


def _has_legacy_scaffold(text: str) -> bool:
    """True when the sub-agent leaked the deprecated polished-output sections."""
    return any(marker in text for marker in (
        "Evidence Verification Log",
        "Claim-Level Sanity Check",
        "Reporting / Escalation Map",
        "在线证据核验记录",
        "逐项主张核验",
    ))


def _section_preview(text: str, markers: tuple[str, ...], *, limit: int = 1600) -> str:
    starts = [idx for marker in markers if (idx := text.find(marker)) >= 0]
    if not starts:
        return ""
    start = min(starts)
    return text[start : start + limit].strip()


def _build_user_message(task: str, attachments: Iterable[Path]) -> str:
    """Plain-text rendering of the user turn used by the cheap planner.

    Filenames are appended as a hint so the planner can route based on
    what is attached. The provider only sees actual file bytes via
    `build_user_content` at the sub-agent / integration-step boundary.
    """
    parts = [task.strip()]
    paths = [str(Path(p)) for p in (attachments or [])]
    if paths:
        parts.append("\nAttachments (the model will see file contents at execution time):")
        parts.extend(f"- {p}" for p in paths)
    return "\n".join(parts)


def _build_user_content_for_provider(
    task: str,
    attachments: Iterable[Path],
    provider_name: str,
):
    """Real provider-shaped content: string when no attachments, blocks otherwise."""
    return build_user_content(task.strip(), list(attachments or []), provider_name)


def _task_needs_hosted_search(task: str) -> bool:
    lower = task.lower()
    markers = (
        "current",
        "search",
        "web",
        "source",
        "citation",
        "pinpoint",
        "treaty",
        "convention",
        "icao",
        "iata",
        "faa",
        "easa",
        "caac",
        "regulation",
        "legal research",
        "authority",
        "case law",
        "verify",
        "核验",
        "来源",
        "公约",
        "法规",
    )
    return any(marker in lower for marker in markers)


class OrchestratorAgent:
    """Parent agent: a small system prompt + the skill dispatch table."""

    def __init__(self, provider: Optional[Provider] = None, settings: Optional[Settings] = None) -> None:
        setup_logging()
        self.settings = settings or current_settings()
        self.provider = provider or build_provider(self.settings)

    def _tools(self) -> list:
        return orchestrator_tools() + hosted_search_tools_for_provider(self.settings)

    def _system_prompt(self) -> str:
        # Body + per-run jurisdiction & active-pack addendum. Reads from the
        # request-scoped settings override so auto-detected packs surface.
        return _PARENT_PROMPT_BODY + _build_parent_addendum(current_settings())

    def _resolve_for_message(self, user_message: str) -> Settings:
        """Merge explicit + auto-detected packs into a request-scoped Settings."""
        merged, detected = merge_active_packs(self.settings.active_domain_packs, user_message)
        if detected:
            log_workflow_event(
                "pack_autodetected",
                {
                    "explicit": list(self.settings.active_domain_packs),
                    "detected": detected,
                    "merged": merged,
                },
            )
        if list(merged) == list(self.settings.active_domain_packs):
            return self.settings
        return self.settings.model_copy(update={"active_domain_packs": list(merged)})

    def run(self, user_message: str, attachments: Optional[Iterable[Path]] = None) -> RunResult:
        run_id = new_run_id()
        token_run = run_id_var.set(run_id)
        token_parent = parent_run_id_var.set(None)
        token_agent = agent_name_var.set("orchestrator")
        try:
            content = _build_user_content_for_provider(user_message, attachments or [], self.provider.name)
            resolved = self._resolve_for_message(user_message)
            with override_current_settings(resolved):
                log_workflow_event(
                    "orchestrator_started",
                    {
                        "mode": "run",
                        "attachments": [str(p) for p in (attachments or [])],
                        "language_policy": "user-facing follows user language; internal language free per material",
                        "active_domain_packs": list(resolved.active_domain_packs),
                    },
                )
                return self.provider.tool_runner(
                    system=self._system_prompt(),
                    messages=[{"role": "user", "content": content}],
                    tools=self._tools(),
                    max_iterations=resolved.parent_max_iterations,
                )
        finally:
            run_id_var.reset(token_run)
            parent_run_id_var.reset(token_parent)
            agent_name_var.reset(token_agent)

    def stream(
        self,
        user_message: str,
        attachments: Optional[Iterable[Path]] = None,
    ) -> Iterator[StreamEvent]:
        """Raw single-pass parent-agent stream. Not a user-facing path:
        both the CLI (`cli.py`) and the web server (`server.py`) route turns
        through ``WorkflowExecutor.stream``, which adds routing, plan
        normalization, sub-agent dispatch, and the citation audit on top of
        this loop. Kept for embedders/tests that need the bare parent loop.
        """
        run_id = new_run_id()
        token_run = run_id_var.set(run_id)
        token_parent = parent_run_id_var.set(None)
        token_agent = agent_name_var.set("orchestrator")
        try:
            content = _build_user_content_for_provider(user_message, attachments or [], self.provider.name)
            resolved = self._resolve_for_message(user_message)
            with override_current_settings(resolved):
                log_workflow_event(
                    "orchestrator_started",
                    {
                        "mode": "stream",
                        "attachments": [str(p) for p in (attachments or [])],
                        "language_policy": "user-facing follows user language; internal language free per material",
                        "active_domain_packs": list(resolved.active_domain_packs),
                    },
                )
                yield from self.provider.stream(
                    system=self._system_prompt(),
                    messages=[{"role": "user", "content": content}],
                    tools=self._tools(),
                    max_iterations=resolved.parent_max_iterations,
                )
        finally:
            run_id_var.reset(token_run)
            parent_run_id_var.reset(token_parent)
            agent_name_var.reset(token_agent)


class SkillAgent:
    """Sub-agent: forked context, compact manifest + lazy skill resources."""

    def __init__(
        self,
        skill_name: str,
        provider: Provider,
        settings: Optional[Settings] = None,
    ) -> None:
        if skill_name not in SKILL_NAMES:
            raise ValueError(f"Unknown skill: {skill_name}")
        setup_logging()
        self.skill_name = skill_name
        self.provider = provider
        self.settings = settings or current_settings()

    def _active_packs(self) -> list[DomainPack]:
        return _load_active_packs(self.settings)

    def _system_prompt(self) -> str:
        manifest = skill_manifest(self.skill_name)
        active_packs = self._active_packs()
        pack_names = ", ".join(p.name for p in active_packs) or "(none)"
        overlay_hints: list[str] = []
        for pack in active_packs:
            overlay = pack.overlay_for(self.skill_name)
            if overlay is not None:
                # Express the hint path relative to the project root so
                # `read_document` (which resolves against project_root) finds it.
                # `pack.root` is `<root>/legal_helper/domains/<pack>`, so the
                # path must keep the `legal_helper/` package segment.
                try:
                    rel = overlay.relative_to(self.settings.project_root)
                except ValueError:
                    rel = overlay
                overlay_hints.append(
                    f"- Pack `{pack.name}` overlay for this skill: "
                    f"`{rel}` — read it with `read_document(\"{rel}\")` when domain "
                    f"context is needed (it is a file path, not a SKILL.md heading)."
                )
            # Pack playbook: the pack's own domain defaults. Overlays cite it
            # as `playbook §N` — surface the path + §-heading index so those
            # references resolve at runtime instead of being guessed at.
            rel_pb = _pack_playbook_rel_path(pack, self.settings)
            if rel_pb is not None:
                hint = (
                    f"- Pack `{pack.name}` playbook: `{rel_pb}` — the pack's "
                    "domain defaults (specified positions, thresholds, minima). "
                    "Overlay references like `playbook §N` resolve to its "
                    "numbered sections"
                )
                index = _pack_playbook_section_index(pack)
                if index:
                    hint += f": {index}"
                hint += "."
                overlay_hints.append(hint)

        prompt = (
            "---\n"
            f"name: {manifest['name']}\n"
            f"description: {manifest.get('description') or ''}\n"
            f"argument_hint: {manifest.get('argument_hint') or ''}\n"
            "---\n\n"
            "# Specialist Runtime Contract\n\n"
            f"Jurisdiction default: **{self.settings.default_jurisdiction}** "
            f"(secondary: {self.settings.secondary_jurisdictions}). "
            f"Citation style: **{self.settings.citation_style}**. "
            f"Active domain pack(s): **{pack_names}**.\n\n"
            "You are a specialist sub-agent working for the orchestrator. "
            "Your methodology and the general playbook are inlined below — you "
            "already have what you need to start. Go straight to the assigned "
            "task.\n\n"
        )
        # Inlined rather than fetched. The old prompt withheld this content and
        # ordered a `list_skill_sections` -> `read_skill_section` handshake,
        # which opened 13 of 13 specialist dispatches and is structurally
        # unparallelisable (the first call must return before the second can
        # name a heading). It cost two iterations of an eight-iteration budget
        # before any legal work began, and 51.9% of all specialist tool calls
        # were instruction fetches. `read_skill_section` / `read_playbook_section`
        # remain available for the long `references/` tail.
        prompt += _inline_skill_methodology(
            self.skill_name, self.settings.active_domain_packs
        )
        if overlay_hints:
            prompt += (
                "Active domain-pack resources for this run:\n"
                + "\n".join(overlay_hints)
                + "\n\n"
            )
        # Same reasoning as the orchestrator addendum: a pack is only active
        # when its defaults are needed, so its playbook is standing context.
        prompt += _inline_pack_playbooks(active_packs)
        # Durable cross-chat project context — standing jurisdiction pins,
        # decisions, and open questions the specialist's research must stay
        # consistent with. Empty unless a project is active for this run.
        try:
            from .projects import active_project_context_block

            project_block = active_project_context_block().strip()
        except Exception:  # noqa: BLE001 — never let context injection break a run
            project_block = ""
        if project_block:
            prompt += (
                project_block
                + "\n\nThe standing project context above (jurisdiction pins, prior "
                "decisions, open questions) applies to this task. Stay consistent "
                "with it and do not re-research points it already settles.\n\n"
            )
        prompt += (
            "Use only the tools made available for this task. If a tool is not "
            "available, continue with hosted search or explain the gap instead "
            "of inventing tool outputs."
            f"{_SUB_AGENT_LANGUAGE_AND_VERIFICATION_PROMPT}"
        )
        return prompt

    def _tools(self, task: str = "") -> list:
        tools = skill_tools_for_task(
            self.skill_name,
            task,
            jurisdictions=[self.settings.default_jurisdiction, *self.settings.secondary_jurisdictions],
            active_packs=self.settings.active_domain_packs,
        )
        # Hosted web_search / web_fetch is the primary research path for foreign
        # jurisdictions and the fallback everywhere else, so make it available to
        # every specialist whenever the deployment enables it — not only when the
        # task string happens to contain a research keyword.
        if self.settings.enable_web_search or _task_needs_hosted_search(task):
            tools += hosted_search_tools_for_provider(self.settings)
        return tools

    def execute(self, task: str, attachments: Optional[Iterable[Path]] = None) -> str:
        parent_run_id = run_id_var.get()
        sub_run_id = new_run_id()
        token_run = run_id_var.set(sub_run_id)
        token_parent = parent_run_id_var.set(parent_run_id)
        token_agent = agent_name_var.set(self.skill_name)
        try:
            log_workflow_event(
                "skill_started",
                {
                    "skill_name": self.skill_name,
                    "task_language": "free per material",
                    "verification_required": True,
                    "attachment_count": len(list(attachments or [])),
                },
            )
            user_content = build_user_content(task, list(attachments or []), self.provider.name)
            result = self.provider.tool_runner(
                system=self._system_prompt(),
                messages=[{"role": "user", "content": user_content}],
                tools=self._tools(task),
                max_iterations=self.settings.sub_agent_max_iterations,
            )
            text = result.text or ""
            log_workflow_event(
                "skill_finished",
                {
                    "skill_name": self.skill_name,
                    "response_chars": len(text),
                    "local_tool_calls": [c.name for c in result.tool_calls],
                    "hosted_tool_calls": [c.name for c in result.hosted_tool_calls],
                    "hosted_tool_call_count": len(result.hosted_tool_calls),
                    "has_findings": _has_findings(text),
                    "has_out_of_scope": _has_out_of_scope(text),
                    "has_sources": _has_sources(text),
                    "has_legacy_scaffold_leak": _has_legacy_scaffold(text),
                    "findings_preview": _section_preview(text, ("## Findings",)),
                    "sources_preview": _section_preview(text, ("## Sources",)),
                },
            )
        finally:
            run_id_var.reset(token_run)
            parent_run_id_var.reset(token_parent)
            agent_name_var.reset(token_agent)
        return result.text or ""

    def stream(self, task: str, attachments: Optional[Iterable[Path]] = None) -> Iterator[StreamEvent]:
        """Stream a specialist turn while preserving parent/sub-run logging context."""
        parent_run_id = run_id_var.get()
        sub_run_id = new_run_id()
        token_run = run_id_var.set(sub_run_id)
        token_parent = parent_run_id_var.set(parent_run_id)
        token_agent = agent_name_var.set(self.skill_name)
        text_chunks: list[str] = []
        try:
            attachment_paths = list(attachments or [])
            log_workflow_event(
                "skill_started",
                {
                    "skill_name": self.skill_name,
                    "task_language": "free per material",
                    "verification_required": True,
                    "stream": True,
                    "attachment_count": len(attachment_paths),
                },
            )
            user_content = build_user_content(task, attachment_paths, self.provider.name)
            for ev in self.provider.stream(
                system=self._system_prompt(),
                messages=[{"role": "user", "content": user_content}],
                tools=self._tools(task),
                max_iterations=self.settings.sub_agent_max_iterations,
            ):
                if ev.kind == "delta":
                    text_chunks.append(ev.data.get("text", ""))
                elif ev.kind == "done" and not text_chunks and ev.data.get("text"):
                    text_chunks.append(ev.data.get("text", ""))
                yield ev
            text = "".join(text_chunks)
            log_workflow_event(
                "skill_finished",
                {
                    "skill_name": self.skill_name,
                    "response_chars": len(text),
                    "has_findings": _has_findings(text),
                    "has_out_of_scope": _has_out_of_scope(text),
                    "has_sources": _has_sources(text),
                    "has_legacy_scaffold_leak": _has_legacy_scaffold(text),
                    "findings_preview": _section_preview(text, ("## Findings",)),
                    "sources_preview": _section_preview(text, ("## Sources",)),
                    "stream": True,
                },
            )
        finally:
            run_id_var.reset(token_run)
            parent_run_id_var.reset(token_parent)
            agent_name_var.reset(token_agent)
