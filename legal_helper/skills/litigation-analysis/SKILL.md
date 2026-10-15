---
name: litigation-analysis
description: Analyze a litigation or arbitration matter — map every claim and defense to its legal elements with supporting facts and verified authority (案情分析 / 争议焦点归纳), build an evidence chronology, and run the 诉讼时效 limitation check. Use when the user supplies case facts, pleadings, or a dispute file and wants element-by-element strength assessment, a timeline, an authority list, or a limitation-period answer. For drafting the resulting 起诉状 / 答辩状 use /draft-agreement; for replying to a counterparty or regulator use /legal-response.
argument-hint: "<case facts / pleadings / dispute file> [--side 原告|被告|claimant|respondent] [--focus elements|chronology|limitation|authorities]"
allowed_connectors: [pkulaw_case_search, pkulaw_case_list, pkulaw_anhao, "pkulaw_*", courtlistener_search, flk_npc_search]
rag_collections: [general]
---

# /litigation-analysis — Claims, Chronology, Limitation

Produce the analysis behind a matter assessment: claims/defenses element
table, evidence chronology, 诉讼时效 position, verified authorities.

**Not legal advice.** Strategy and filing decisions stay with counsel.

## Output Contract (binding)

As a specialist, emit the harness's bundle contract (Findings · Issues ·
Artefacts · Out of scope · Sources), supplied with your task.

## Artefacts

The analysis header (matter · side · elements mapped / unsupported · chronology path or inline count · limitation ok / at-risk / expired / n/a with operative dates · authorities cited / verified) and the element table.

## Authority-verification contract (binding)

- Every PRC case cited is verified live: `pkulaw_case_search` →
  `search_case` for fact-pattern / legal-issue lookups, `pkulaw_case_list`
  → `get_case_list` for keyword / title scans. Record 案号, court, date,
  weight label (general playbook §0: 应当参照 / 参考 / persuasive), and treatment
  (followed / distinguished / superseded).
- Every 案号 is normalized through `pkulaw_anhao` → `anhao_recognition`
  before it enters the bundle; `citations/prc.py` regex is the offline
  fallback only.
- Statutory elements and limitation articles are re-verified via
  `pkulaw_fatiao` / `pkulaw_citation_validator`; `flk_npc_search` is a
  status-only fallback. US comparative authority goes through
  `courtlistener_search` with the same treatment check.
- An unverifiable authority is dropped or flagged `unverified — do not
  cite`, never propped up. The automatic `/cite-check` gate runs on
  the final answer; never bypass it.

## Inputs

- **Matter record** — case facts, pleadings, contracts, correspondence;
  `read_document` handles attachments; record page / 条 pinpoints.
- **Side** analyzed for (原告 / 被告 / claimant / respondent) — burdens
  and strength calls flip with it.
- **Posture** — pre-filing, 一审, 二审 / appeal, arbitration,
  enforcement; forum if known (court / 仲裁机构).

## Workflow

1. Read the general playbook §0–§3a and the active pack playbook; frame
   each cause of action by its 请求权基础 and the live defenses; confirm
   side and posture.
2. Element table from the matching `resources/element_tables/` preset:
   map each element to supporting facts, contrary facts, evidence (with
   pinpoints), and verified authority; mark unsupported elements — never
   average over a missing element.
3. Chronology: every dated event with its source pinpoint; `write_xlsx`
   past ~10 events (layout in methodology), else inline; flag disputed dates.
4. Limitation check: accrual, 中止 / 中断 events, the outer cap, and any
   special-law period (特别法优先) — verify each article via
   `pkulaw_fatiao` before relying on it; compute the expiry with
   `compute_period` (rule `prc_civil_code`); state operative dates.
5. Authority pass per the contract above; then 争议焦点 synthesis: which
   elements the matter actually turns on.
6. Sanity check per general playbook §3.

## Pointers

- Methodology — element mapping, burdens, chronology layout, 诉讼时效
  framework (民法典 第188条, 中止 / 中断), treatment, QA:
  `references/methodology.md`.
- Issue tree, claim lifecycle, three search rounds, 类案检索 checklist, 类案
  matrix, 八个一致: `references/issue_element_method.md`.
- Cause-of-action presets + schema: `resources/element_tables/README.md`.
- Authority hierarchy + citation: `/playbook/general_playbook.md` §0 +
  §2; CREAC reasoning §3a.
- When the **aviation** pack is active, also apply
  `/domains/aviation/overlays/litigation-analysis.md`.

Analysis language follows the user and the matter documents; reasoning
and tool calls may use whichever language fits the material.
