# Provenance Tags — Taxonomy & Usage

> Adapted (with attribution) from anthropics/claude-for-legal
> `litigation-legal/CLAUDE.md`. The exact tag wording matches that
> repo's discipline so cross-tool review notes are interoperable.

Every citation in a legal output carries a tag describing **where it
came from**. The reviewer can tell at a glance which cites to Shepardize
first. Provenance tags describe *provenance, not confidence* — a tag is
not a vote of trust, it is a record of what happened in this session.

## Retrieval tags — only when the cite literally appeared in that source's tool result this session

### PRC

- `[PKULaw]` — for any cite retrieved through one of the nine PKULaw
  sub-services (`pkulaw_law_search`, `pkulaw_fatiao`, `pkulaw_case_search`,
  `pkulaw_case_list`, `pkulaw_anhao`, `pkulaw_law_recognition`,
  `pkulaw_citation_validator`, `pkulaw_doc_link`, `pkulaw_nl_search`).
- `[flk.npc.gov.cn]` — for any cite retrieved via `flk_npc_search`
  against the 国家法律法规数据库.

### US

- `[CourtListener]` — federal opinions / dockets via the free
  CourtListener API (default US case-law connector in this repo).
- `[Westlaw]` / `[Lexis]` / `[PACER]` — when those connectors are
  available.
- `[eCFR]` — for current CFR text via `ecfr_search`.
- `[FederalRegister]` — for proposed/final rules via `federal_register_search`.
- `[GovInfo]` — for USC / public laws via `govinfo_search`.
- `[USPTO]` — for IP filings.

### EU

- `[EUR-Lex]` — for any cite retrieved via `eurlex_search`.

### Other

- `[Trellis]` / `[Descrybe]` / `[Harvey]` / `[CoCounsel]` —
  third-party legal-research connectors when available.
- `[statute / regulator site]` — for direct primary-source URLs
  (e.g. caac.gov.cn, mot.gov.cn, samr.gov.cn, ec.europa.eu).
- `[user provided]` — for cites the user/partner explicitly supplied.

## Verify tags — the default for everything else

- `[model knowledge — verify]` — **default tag.** If you didn't retrieve
  it, it's model knowledge, no matter how confident you are. Untagged
  cites in a draft are automatically treated as this by
  `provenance_audit_tool`.
- `[web search — verify]` — for cites surfaced via the hosted
  `web_search` tool. Always lower trust than a primary-source connector.
- `[retrieved but verify support]` — for cites that came from a
  connector but the *holding* hasn't been confirmed to actually support
  the proposition the draft asserts. Use after Mode-2 round-trip when
  the answer is ambiguous.

## Settled — for stable references checked against a primary source on a known date

- `[settled — last confirmed YYYY-MM-DD]` — stable statutory / regulatory
  references that have been checked against a primary source on the stated
  date. The date matters: *"stable"* references change. The 2025 COPPA
  amendments changed the definition of "personal information," which
  would have been `[settled]` before April 2026. Colorado AI Act's
  effective date has moved twice. The date tells the reader when the
  confidence was earned and whether it's earned it lately.

When you can't confirm the date of the last check, use `[model knowledge
— verify]` instead — **an unconfirmed "settled" is the confident
overclaim the whole attribution system exists to prevent.**

## Drafting markers — the expanded forms

These are the longer cousins of `[verify]`, used by drafting skills
(`/brief`, `/review-contract`, `/legal-response`) with the specific
claim spelled out. `cite-check` accepts them as resolved-to-verify tags.

- `[VERIFY: <specific factual assertion>]` — anything not confirmed
  against the record.
- `[UNCERTAIN: <specific legal proposition>]` — anything not confirmed
  against current authority.
- `[CITE NEEDED: <specific cite — fact/rule believed but cite not yet pinned>]`
  — the rule is believed; the cite is missing.
- `[verify exact quote — record cite pending]` — placeholder when you
  meant to quote but don't have the source open.
- `[premise flagged — verify]` — user stated a rule/date/threshold the
  analysis is building on; verify before propagating.
- `[statute unretrieved — verify]` — disagreeing with a cited statute
  without having pulled the text.

## Hard rules

1. **Provenance tags describe what happened, not what you'd like to
   claim.** Tag a citation with the MCP source (e.g. `[CourtListener]`)
   *only* when the citation literally appeared in that tool's result
   this session. Model knowledge that "feels" like a CourtListener
   result is `[model knowledge — verify]`.
2. **Do not promote a tag to a more trustworthy tier because the
   citation "seems right."** The tag describes provenance, not confidence.
3. **Never strip or collapse the tags.** They are the reviewing
   attorney's fastest signal about which citations to Shepardize first
   before filing.
4. **Untagged ≠ trusted.** An untagged cite is `[model knowledge —
   verify]` by default and must be flagged by `provenance_audit_tool`.

## Quick rendering rules

In the draft body, a tag immediately follows the cite, within ~140
characters:

> *Acme v. Beta*, 999 F.3d 1234 (9th Cir. 2024) `[CourtListener]`
>
> 《个人信息保护法》第38条 `[PKULaw]`
>
> Regulation (EU) 2016/679 art. 6(1) `[EUR-Lex]`
>
> 13 USC §1252 `[model knowledge — verify]`
