---
name: cite-check
description: Round-trip every citation and quoted phrase in a draft against its primary source. Flags fabricated cites (Mode 1) AND cites that exist but don't support the claimed proposition (Mode 2 — the "misgrounded citation" failure mode behind ~all reported AI-citation sanctions). Produces a severity-tiered verification-report.md.
argument-hint: "<draft file or pasted text> [--stakes filed|internal]"
allowed_connectors: ["pkulaw_*"]
rag_collections: [general]
---

# /cite-check — Citation Audit

**Not legal advice.** Counsel spot-checks every flagged item.
Catches two hallucination modes (Anthropic + sboghossian + Stanford RegLab):
**Mode 1** = fabricated cite (not in any reporter / DB); **Mode 2** =
misgrounded cite (cite exists, passage exists, but doesn't support the
proposition). Mode 2 is the dominant failure mode in the >2,000 documented
court-filing sanction cases.

Read [references/verification.md](references/verification.md) for the
6-phase procedure and [references/provenance-tags.md](references/provenance-tags.md)
for the tag taxonomy, [references/prc-roundtrip.md](references/prc-roundtrip.md)
for PKULaw / `flk.npc.gov.cn` patterns.

## Output Contract (binding)

The deliverable is a tabular `verification-report.md` rendered by
`cite_check_report_tool` — *not* a `## Findings` bullet list. The bundle
contract's length budget governs research bundles, not audit output.

Shape (see [references/report-format.md](references/report-format.md)):

```
# Citation Verification Report
**Document:** …   **Stakes:** filed|internal   **Generated:** …
**Sources consulted:** [PKULaw, CourtListener, EUR-Lex, …]

## Summary
Checked N of M citations. J confirmed; K could not be retrieved;
I flagged as potential miscitations; H flagged as misgrounded.

> **⚠️ DO NOT FILE** — (only when stakes=filed AND threshold breached)

## CRITICAL  | Location | Issue | Claimed | Source says | Fix |
## NUANCED   …
## MODEL-ONLY …
```

The renderer adds the not-legal-advice line; pinpoint rules: `/playbook/general_playbook.md` §2.

## Workflow

1. Read the draft via `read_document` (if attached) or directly from the
   message. Set `stakes`: `filed` (court / regulatory submission / opinion
   letter) → strictest pass; `internal` otherwise.
2. **Extract** — `extract_citations_tool(text)` (US eyecite + PRC regex + EU regex).
3. **Validate structurally** — `validate_citations_tool(text)`. Status is
   three-valued: `verified` / `flagged` / `could_not_check`. *Never*
   return "confirmed" when no primary source was consulted.
4. **Fetch primary sources** — per the lookup table below.
5. **Round-trip quotes (Mode 2)** — for every quoted phrase, call
   `quote_roundtrip_tool(quote, source_text)`. `exact` / `near` /
   `not_found` (= paraphrase-as-quote) / `could_not_check`.
6. **Assertion-to-source support** — for "the court held X" / "法院认定 X"
   propositions, confirm the cited pinpoint actually supports the *whole*
   proposition (not just one element). Partial support = NUANCED.
7. **Provenance audit** — `provenance_audit_tool(text)` flags cites
   lacking a tag. Default for untagged is `[model knowledge — verify]`.
8. **Render** — `cite_check_report_tool(text, stakes=…, sources_consulted=[…])`.
9. **Verification log** — for every `verified` item, call
   `verification_log_append_tool(cite, source, "confirmed")` so the next
   matter doesn't re-Shepardize.

**Escalation gate.** If `stakes=filed` and the report carries ≥5
fabricated/miscited/misgrounded items OR any paraphrase-as-quote on a
holding citation, the report renders **DO NOT FILE**.

**Could-not-check rule.** "Could not check" is *never* "confirmed."
A false positive lets a bad cite through; an honest gap routes to the
attorney.

## Lookup priority by citation type

Official texts of every jurisdiction come from `legal_search` / `legal_fetch`
(`legal_sources` lists them); PKULaw MCP tools surface as `<server>__<tool>`.

| Citation type | Primary | Backup |
|---|---|---|
| 案号 (judgment case number) | `pkulaw_anhao__recognize_case_numbers` → `pkulaw_case_list__search_cases_by_keyword` | `legal_search` CN `kind="case"`; `citations/prc.py` regex |
| 法律 / 行政法规 / 司法解释 pinpoint (《X》第N条) | `pkulaw_fatiao__get_law_article` (title + 条号) | `legal_fetch` of the 国家法律法规数据库 text (`section="第N条"`): official text, status, versions |
| 部门规章 / 通告 / 国函 / 国办发 | `pkulaw_law_search__search_law_articles` | `legal_search` CN (source `cn.pkulaw`) + hosted web |
| 法条 batch normalize in a draft | `pkulaw_law_recognition__recognize_law_citations` | `citations/prc.py` regex |
| Citation chain validation | `pkulaw_citation_validator__verify_law_citations` | manual review |
| Broad NL question (PRC) | `pkulaw_nl_search__ai_pkulaw_search` | `legal_search` CN `mode="semantic"` + hosted web |
| Any other jurisdiction: statute, regulation, case | `legal_search` in that jurisdiction → `legal_fetch(ref, section=…)` | hosted `web_search` of the official site |

Domain packs layer extra sources via `domains/<pack>/overlays/cite-check.md`.
