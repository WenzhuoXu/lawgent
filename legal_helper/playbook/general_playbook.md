# General Legal Playbook — Global Defaults

Loaded by every skill before any domain pack overlay. This file owns
jurisdiction policy, language policy, source-hierarchy defaults, and the
universal sanity-check rules. Domain-specific positions live under
`legal_helper/domains/<pack>/playbook.md` and are concatenated **after** this
file when the corresponding pack is active.

> **Not legal advice.** Defaults below are general professional practice
> orientation; they do not substitute for licensed counsel in the relevant
> jurisdiction.

## 0. Jurisdiction & Source Hierarchy

### Scope the applicable-law stack first
Before researching any issue, determine **which bodies of law control it and at
what level** — then cover every live level, not just the most familiar one. The
binding stack runs:

1. **International / supranational instruments and agreements between the
   involved parties** — multilateral conventions and treaties, and any
   bilateral or regional agreement between the specific states/parties at issue.
   For any such instrument, state each relevant party's participation /
   ratification status, because that status can decide whether the instrument
   applies at all.
2. **National law** — the per-jurisdiction hierarchy below.
3. **Subordinate / sectoral rules and standards**, then **contractual and
   industry standards**.

Identify the **highest controlling source** for each issue and pin it. A
sourced "this instrument does not bind this party / does not apply here" is a
finding, not a gap. This is general method, not a cross-border special case: it
surfaces a controlling convention or inter-party agreement for an international
question exactly as it surfaces a controlling 司法解释 or national standard for a
domestic one. Domestic statutes are frequently **not** the whole answer.

### Default jurisdiction order
1. **PRC (China)** — primary. Hierarchy:
   - 宪法 / 法律 (Constitution / National laws by NPC + NPCSC)
   - 行政法规 (State Council administrative regulations)
   - 部门规章 / 地方性法规 / 自治条例 (ministerial / local rules)
   - 规范性文件 / 部门通知公告 (normative documents)
   - 司法解释 / 指导性案例 (SPC interpretations + guiding cases)
   - 国家标准 (mandatory GB; recommended GB/T)
2. **United States** — secondary (federal). USC → CFR → agency guidance → case law.
3. **European Union** — secondary. TFEU/TEU → regulations → directives →
   implementing acts → CJEU case law → Member-State implementation.
4. **Other jurisdictions** — only when the user identifies them or the
   facts make them relevant (HK, UK, Singapore, etc.).

If a question concerns multiple jurisdictions, name each one explicitly and
keep the analysis under each clearly separated; never mix authority types.

### Comparative analysis
Use comparative jurisdictions to *contextualise*, not to override. Always
flag which jurisdiction's law is being applied in any given paragraph.

## 1. Language Policy

- **User-facing input/output language follows the user's language.** If the
  user writes in Chinese, answer in Chinese; if in English, answer in
  English. Mixed inputs follow the dominant language; ask if ambiguous.
- **Internal reasoning, sub-agent tool calls, and orchestrator-to-skill
  messages may use whichever language best fits the material** — 中文 is
  encouraged when reasoning over PRC statutes, 案例, or other
  Chinese-language sources; do not translate to English just to think.
- **Quote primary sources in original language with translation** when
  precision matters: e.g. `"国家工作人员"（state functionary）`,
  `"reasonable royalty"（合理许可使用费）`. The original term must come
  first in user-facing output; the translation is parenthetical.

## 2. Citation Standard

Every legal answer ends with a `## Sources` (English) or `## 资料来源`
(Chinese) section. One line per source. Each entry must include:

- Source name (statute, regulation, judgment, treaty, agency guidance)
- A **pinpoint marker**: 条款 / article / section / § / paragraph / page /
  clause / chapter / appendix. If the pinpoint genuinely cannot be obtained,
  write `pinpoint unavailable` or `无法获得具体条款` explicitly.
- The URL or local file path, when available.
- Optional: `online-checked: yes/no` and a short note tying the source to
  the bullet number(s) it supports.

The orchestrator fires the `/cite-check` skill on the final answer after
synthesis (skipped only for generic non-legal enquiries — see
`workflow._should_run_cite_check`). The skill runs on the fast model and
produces a tabular `verification-report.md` with severity tiers and a
coverage line. Do not bypass it.

### Citation style
Default style is set in `config.yaml` as `citation_style`. Supported:

- `gb_t_7714` — PRC default. Statutes by 名称 + 条款; cases by 案号.
- `bluebook` — US default.
- `oscola` — UK / EU default.

The `legal_helper/citations` module formats accordingly.

## 3. Sanity-Check Contract

Before emitting a bundle, every skill agent must:

1. **Claim-by-claim support.** Each Findings bullet ties to a real source, a
   local file, or an inline assumption. No floating claims.
2. **Soften unsupported claims.** Use "may", "typically", or "subject to
   local-law confirmation" inline. Do not invent article numbers, agency
   names, forms, or reporting windows — use `pinpoint unavailable` instead.
3. **Currency check.** State the effective version / date of regulations
   relied upon. If currency cannot be verified, say so inline.
4. **Authority labels.** Every Findings bullet ends with one of:
   `treaty`, `regulation`, `case`, `guidance`, `local-law`, `best-practice`,
   `risk`, `drafting`. The orchestrator uses these to separate legal
   requirements from operational best practice and from organisational
   playbook preferences at authoring time.

## 3a. Structured legal reasoning (CREAC / 争点-规则-涵摄-结论)

Reason through each distinct legal issue with an explicit issue → rule →
application → conclusion structure before writing the Findings bullet. This is
internal reasoning, not output scaffolding — it keeps each claim grounded and
prevents conclusory leaps, but the deprecated narrative sections in §4 still
must not appear in the bundle.

For every material issue:

1. **Issue (争点).** State the precise legal question, scoped to the facts.
2. **Rule (规则).** Identify the controlling authority by climbing the **full**
   §0 stack — start at the international / inter-party layer where it is live
   (controlling convention or agreement between the parties, with each party's
   status), then the national hierarchy (PRC: 法律 → 行政法规 → 部门规章 →
   规范性文件 → 司法解释 → 指导案例; US/EU when comparative), then subordinate
   rules and standards. Pin the article / § / clause of the highest controlling
   source. If the rule cannot be confirmed to a pinpoint, say so and soften per
   §3.
3. **Application (涵摄).** Apply the rule to the actual facts — both the
   support and the strongest counter-reading. Note where the outcome turns on
   an unverified fact or an open question.
4. **Conclusion (结论).** State the supported conclusion and its confidence;
   flag residual risk and what would change the answer.

Each Findings bullet is the distilled **conclusion** of one such chain, with
the rule's pinpoint inline. When the application is genuinely contested, keep
the counter-reading as a one-line caveat rather than dropping it. Verify the
**quoted** language of any rule against its source (`quote_roundtrip_tool` /
`ground_answer_tool`) before presenting it as a verbatim quote.

## 4. Deprecated Output Patterns

The following sections **must not** appear in a skill's bundle (the
orchestrator authors the user-facing presentation):

- `Evidence Verification Log` / `在线证据核验记录`
- `Claim-Level Sanity Check` / `逐项主张核验`
- Scenario walkthroughs / action matrices / limits matrices

Provide the underlying claims as Findings bullets with pinpoints; the
orchestrator decides how to render them.

## 5. Sources Line Format

```
- [source name] — [URL or local file path] — supports findings #X, #Y[, online-checked: yes/no, pinpoint: confirmed/unavailable]
```

If a source cannot be linked because it is proprietary or behind a wall,
name it inline as an unavailable source and do not rely on it as the only
support for a legal claim.

## 6. Universal Risk Defaults

These apply across packs unless a pack overlay supersedes them.

- **Regulatory disclosure carveouts** in any confidentiality / NDA: regulator
  inquiries (SAMR / NMPA / CSRC / SEC / EMA / ESMA etc.),
  court orders, mandatory reporting to safety / financial / data-protection
  authorities. Never sign an NDA that bars regulator disclosure.
- **Data localisation**: PRC default is that personal information of PRC
  data subjects is processed within the PRC unless a permitted cross-border
  pathway is in place (CAC SCC, 安全评估, or 认证).
- **State secrets / classified data**: 国家秘密 carveout mandatory; cite
  《保守国家秘密法》 when relevant.
- **Anti-corruption / sanctions**: KYC / 反贪污 / OFAC / UK-MoD / EU
  sanctions screening required for new counterparties.
- **Privilege**: PRC does not recognise attorney-client privilege the same
  way as US/UK; flag explicitly whenever a workflow touches privileged
  material that may leave a privileged jurisdiction.

## 7. Document Production Defaults

- All generated documents include a not-legal-advice disclaimer in the
  user's language.
- Redlines list "Must-have / Should-have / Nice-to-have" tiers.
- Risk memos include a severity × likelihood matrix and an explicit
  residual-risk line.
- Multi-jurisdiction memos clearly separate analysis under each jurisdiction
  before any cross-cutting recommendation.
- For 流程图 / 工作流程 / process maps, prefer ``write_pptx`` with a
  ``flowchart`` slide block (real shapes + arrows). Never reply with a
  numbered text-bullet list when the user asked for a diagram. See
  ``legal_helper/skills/flowchart`` for shape kinds and authoring rules.
