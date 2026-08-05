# Citation Verification — Procedure

> Adapted from sboghossian/master-claude-for-legal `skills/citation-verifier.md`
> (MIT) and anthropics/claude-for-legal `litigation-legal/CLAUDE.md`. Both
> are publicly licensed; this file paraphrases and extends with PRC-aware
> equivalents.

## The two hallucination modes

This skill addresses the failure modes documented across the >2,000 court
filings catalogued in the Damien Charlotin AI Hallucination Cases
database (May 2026) and the Stanford RegLab 2024 legal-RAG study.

**Mode 1 — fabricated citation.** The model invents a reporter cite, a
statute number, a case name, a 法释 number, a 案号. Modern legal-research
connectors (CourtListener, Westlaw, PKULaw, EUR-Lex, flk.npc.gov.cn)
will *not* find these because they do not exist. Detection: round-trip
the cite against the connector; if no hit, the cite is fabricated.

**Mode 2 — misgrounded citation.** The cite exists. The passage exists.
But the passage does not say what the draft claims it says — the source
addresses a different fact pattern, the holding cuts the other way, the
quote was lightly edited, the pinpoint supports only part of the
proposition, or the cited text is dicta / a dissent / a rejected argument
the court quoted. This is harder to detect because it passes a "does the
case exist" check. It is the dominant failure mode in sanction cases.

## The 6-phase procedure

### Phase 1 — Extract

Read the full draft. Categorize every citation:

- **Case** (US: `Smith v. Jones, 123 F.3d 456 (9th Cir. 2018)` —
  PRC: `(2023)京01民终12345号`).
- **Statute / regulation / pinpoint** (US: `15 U.S.C. § 78u-4(b)(1)` —
  PRC: `《刑法》第233条`).
- **Judicial interpretation** (PRC: `法释〔2024〕5号`).
- **State Council** (PRC: `国函〔2023〕12号`, `国办发〔2023〕45号`).
- **Treaty / EU instrument** (`Regulation (EU) 2016/679 art. 6(1)`,
  `Chicago Convention art. 33`).
- **Internal documents / exhibits / transcript pages**.
- **Secondary** (Wright & Miller, Restatement, 张明楷《刑法学》).

Extract every quoted phrase ("anything inside quotation marks attributed
to a source"). Extract every assertion that attributes a holding to a
source without quoting ("the court held X", "法院认定 X", "Article 6 provides
that Y"). These all need Mode-2 verification.

### Phase 2 — Mode-1 (does the cite exist?)

For each case cite, search the available legal-research connector
(`courtlistener_search`, `pkulaw_case_search`, EU `eurlex_search`). If
not found, mark **FABRICATED**. Do not assume the search failed; modern
connectors do not lose real cases. (When the connector itself is offline,
mark **COULD NOT CHECK** — not "confirmed.")

For each statute / regulatory cite, confirm the cited provision exists
in the cited code at the cited pinpoint. Confirm the version cited is
current (or appropriate for the time period at issue) — repealed
authorities are CRITICAL.

For each PRC interpretation, cross-check 法释 number and year against
PKULaw; cross-check `国函`/`国办发`/`国发` against PKULaw or
flk.npc.gov.cn.

### Phase 3 — Mode-2 (does the source support the claim?)

For each quoted phrase: open the source, search for the verbatim phrase,
mark VERIFIED / VERIFIED (near-match) / **PARAPHRASE PASSED AS QUOTE** /
NOT FOUND. Tool: `quote_roundtrip_tool(quote, source_text)`.

For each "the court held X" assertion: read the cited pinpoint and
determine whether it supports the assertion *as stated*. The key check
is **partial support** — if the proposition is "X, Y, and Z" and the
pinpoint supports only Z, either split the cite per element or narrow
the proposition. This is the Stanford RegLab "misgrounded citation"
failure mode.

For each retrieved passage cited for a legal proposition: confirm it is
a holding — not dicta, not a dissent, not a quoted argument the court
rejected, not a different statute that happens to use similar words.
When you cannot confirm, tag `[retrieved but verify support]`.

### Phase 4 — Currency / good-law

For US: KeyCite / Shepardize equivalent — flag overruled, abrogated,
distinguished, vacated.

For PRC: pull `status` field from `flk_npc_search` (有效 / 已修改 /
已废止 / 尚未生效). A 法律 in the draft whose status is `已废止` is a
CRITICAL finding.

For EU: pull EUR-Lex "in force" / "no longer in force" metadata; many
directives in the EU corpus are superseded.

### Phase 5 — Provenance audit

Every citation must carry a provenance tag. See `provenance-tags.md`.
Default for untagged is `[model knowledge — verify]` — *not* "no tag".
Untagged cites are MODEL-ONLY items in the report.

### Phase 6 — Render

Call `cite_check_report_tool(text, document_name=…, stakes=…, sources_consulted=[…])`.
The report has three severity tiers:

- **CRITICAL** — fabricated, misgrounded, paraphrase-as-quote on a
  holding, out-of-date authority. Must address before filing.
- **NUANCED** — partial support, framing overstates, dicta cited as
  holding. Counsel review.
- **MODEL-ONLY** — uncited propositions, untagged cites, could-not-check
  items. Confirm or add citation.

The coverage line is the single-sentence audit summary:

> Checked N of M citations. J confirmed; K could not be retrieved;
> I flagged as potential miscitations; H flagged as misgrounded.

## Stakes — when to apply which standard

Per sboghossian/citation-verifier: verification rigor matches downstream
consequences.

| Stakes | Standard | Threshold |
|---|---|---|
| **Court filings** (motions, briefs, complaints, responses) | Strictest. Flag every issue including minor ones. | ≥5 fabricated/miscited/misgrounded OR any paraphrase-as-quote on a holding → **DO NOT FILE** banner |
| **Regulatory submissions** (SAMR / NMPA / CSRC / SEC / EMA / ESMA) | Strictest. Treat as a filing. | Same |
| **Client opinion letters** | Strict. | Same |
| **Internal memos / sample-checked drafts** | Lighter touch. Severity ladder still applies. | No do-not-file gate |

## The "could not check" rule

Anthropic claude-for-legal: *"When source text is unavailable, say
'could not check,' never 'confirmed.' A false positive ('this cite is
fine' when you couldn't read the source) is worse than 'couldn't check
this one.'"*

This is enforced in `validate_citations` (three-valued status: `verified` /
`flagged` / `could_not_check`) and in `cite_check_report_tool` (untagged
and unfetched items emit as `could_not_check` MODEL-ONLY items).

## Tool-vs-model conflict

When a retrieved result conflicts with model knowledge — the tool says a
case was not overruled but you believe it was; the tool says a statute
says X but you believe Y — surface **both** and flag:

> "The research tool says [X]. My training knowledge says [Y]. These
> conflict. Verify with the primary source before relying on either."

Do not silently prefer the tool OR training. The conflict is the signal.

## Composition with other skills

This skill is the verification layer. It composes with everything that
produces legal output: `/review-contract`, `/brief`, `/legal-response`,
`/compliance-check`. The pattern is: produce → verify → review flags →
fix → re-verify → file. For high-stakes outputs (court filings,
regulatory submissions, opinion letters) the verifier should run twice
— once after first draft, once after fixes.
