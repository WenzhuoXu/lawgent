# Verification Report — Format

> Output of `cite_check_report_tool`. Adapted from
> sboghossian/master-claude-for-legal `skills/citation-verifier.md` (MIT)
> with PRC-language support.

## File location

The report is rendered as `verification-report.md` adjacent to the input
document being verified. It is **not** appended to a legal-answer
bundle and is **not** subject to the playbook's ≤2000-char ceiling
(that ceiling governs legal answers; an audit trail is necessarily as
long as the audit).

## Skeleton (English)

```markdown
# Citation Verification Report

**Document:** <filename or "pasted-text">
**Stakes:** filed | internal
**Generated:** YYYY-MM-DD HH:MMZ
**Sources consulted:** PKULaw, CourtListener, EUR-Lex, ...

## Summary

Checked N of M citations. J confirmed; K could not be retrieved;
I flagged as potential miscitations; H flagged as misgrounded
(cite exists but doesn't support the proposition).

> **⚠️ DO NOT FILE in current form** — <reason>
> (only when stakes=filed AND escalation threshold breached)

## CRITICAL — must address before filing

Fabricated cites, misgrounded propositions (cite exists but source
doesn't support the claim), and paraphrases passed off as quotes.

| Location | Issue | Claimed | Source says | Fix |
|---|---|---|---|---|
| char 1245-1278 | fabricated | *Acme v. Beta, 999 F.3d 1234* | not found in any reporter | Strike citation; find supporting authority or remove proposition |
| char 2401-2480 | paraphrase_as_quote | "the parties shall" | source says "the parties must" | Remove quotation marks or correct quote |
| char 3120-3204 | misgrounded | 《劳动合同法》第40条 supports unilateral termination for cause | Article 40 covers no-fault termination with 30-day notice, not for-cause | Re-cite to art. 39 (单方解除-过错) or narrow the claim |

## NUANCED — review for accuracy

Cite supports part of the proposition but not the whole; framing may
overstate. The Stanford RegLab "partial-support" failure mode.

| Location | Issue | Claimed | Source says | Fix |
|---|---|---|---|---|
| char 4500-4612 | could_not_check | assertion: "the court held X under Y rule" | (assertion-to-source check not yet performed) | Confirm the cited pinpoint actually supports the whole proposition |

## MODEL-ONLY — confirm or add citation

Uncited propositions and cites missing a provenance tag. Default tag
for un-retrieved citations is `[model knowledge — verify]`.

| Location | Issue | Claimed | Source says | Fix |
|---|---|---|---|---|
| char 5012-5034 | untagged | 42 USC § 1983 | (no provenance tag within 160 chars) | Tag with `[model knowledge — verify]` or with the actual retrieval source |

---

*Not legal advice. This is an AI-assisted pre-review pass; counsel must
spot-check every flagged item against a primary source before filing or
external delivery.*
```

## Skeleton (中文)

The renderer accepts `lang="zh"` and produces the same tables under
中文 headers (`# 引证核验报告`, `## 汇总`, `## CRITICAL — 提交前必须修正`,
`## NUANCED — 请人工复核`, `## MODEL-ONLY — 请确认或补充引证`).

The coverage line reads:

> 已核 N 条 / 共 M 条引证：J 条确认；K 条无法核验；I 条疑似错引；
> H 条疑似 misgrounded（引用真实，但来源未支持论点）。

## Issue-type values (the `Issue` column)

- `confirmed` — never appears (confirmed items are not listed; they
  appear only in the coverage line).
- `flagged_miscited` — cite malformed or unknown reporter (Mode 1).
- `fabricated` — cite not found in any reporter / DB (Mode 1).
- `misgrounded` — cite exists, source doesn't support proposition (Mode 2).
- `paraphrase_as_quote` — quote not verbatim in source (Mode 2).
- `could_not_check` — connector / source unavailable.
- `untagged` — cite has no provenance tag.
- `out_of_date` — cite to a repealed / superseded / abrogated authority.

## Severity rules

| Severity | When |
|---|---|
| CRITICAL | `fabricated`, `flagged_miscited`, `misgrounded`, `paraphrase_as_quote`, `out_of_date` |
| NUANCED | partial-support / framing-overstates; assertion-to-source checks pending |
| MODEL-ONLY | `untagged`, `could_not_check` on uncited propositions, "should be cited but isn't" |

## Escalation threshold (the DO NOT FILE banner)

Only renders when `stakes=filed` **and**:

- ≥5 CRITICAL items of type `fabricated`, `flagged_miscited`, or
  `misgrounded`, OR
- any `paraphrase_as_quote` whose claimed text contains "hold" /
  "认定" / "判决" (i.e. on a holding).

Threshold is configurable in `legal_helper/citations/report.py`
(`DEFAULT_FAB_THRESHOLD`).
