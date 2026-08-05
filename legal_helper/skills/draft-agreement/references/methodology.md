# Drafting methodology

Deep reference for `/draft-agreement`. Read the section you need; do not
load the whole file when a single question is at issue.

## 1. Intake and framing

- **Deal terms first.** Extract from the term sheet: parties (full legal
  names + 统一社会信用代码 / registration numbers when available), the
  exchange (what for what), money terms, dates, and any positions the
  client has already conceded. Every term-sheet item must land in a
  clause or be listed under `Out of scope` with a reason.
- **Side discipline.** A draft is written *for* one side. Default
  positions in the clause library are marked by side (`side_default`
  notes); flip them consciously, never accidentally. A "mutual" ask still
  needs a side for tie-breaks (e.g. who holds the audit right).
- **Type resolution.** If the deal terms fit no library preset, compose
  from the nearest preset plus individual clauses; say so in a `drafting`
  Finding. Do not force a services deal into an NDA skeleton.
- **Missing terms.** A missing commercial term (price, term length,
  territory) becomes a bracketed placeholder `[【待商定】/ TBD]` in the
  draft AND a Finding — never silently invent a number.

## 2. Clause-plan discipline

- Plan before prose: list clause ids (review-contract bucket vocabulary)
  in order, mark each as `library` (adapted from a preset), `custom`
  (drafted for this deal), or `omitted` (with reason).
- **Ordering.** PRC commercial convention: 首部 (title, parties,
  recitals/鉴于条款) → definitions → operative business clauses → risk
  allocation (warranties, liability, indemnity) → confidentiality / IP /
  data → term + termination → general provisions (force majeure, notices,
  assignment, entire agreement) → governing law + dispute resolution →
  signature block (盖章 + 签字; PRC practice expects 公章 or 合同专用章).
- **One obligation per clause.** Compound clauses hide obligations from
  review; the draft → `/review-contract` round-trip depends on clause
  granularity matching the bucket vocabulary.
- **Cross-references** use clause numbers, checked after final numbering
  (a renumber pass at the end, before `write_docx`).

## 3. Language and controlling version

- Per general playbook §1, output language follows the user; the deal may
  override (e.g. Chinese-only counterparty).
- **Bilingual drafts**: clause-by-clause parallel text (zh first for
  PRC-law contracts), one controlling-language clause. Default:
  PRC-law-governed → Chinese controls; foreign-law-governed with a PRC
  party → state the parties' choice explicitly, never leave both
  versions "equally authentic" without a conflict rule.
- Quote statutory language in the original with a translation when a
  clause tracks a statute (e.g. force majeure tracking 民法典 第180条
  "不能预见、不能避免且不能克服的客观情况").
- Litigation filings (`resources/prc_filings/`) are Chinese-only
  documents; an English courtesy translation, if asked for, is a separate
  non-filing artifact and must say so on its face.

## 4. PRC legal-basis and mandatory-content checks

Verify every pinpoint through `pkulaw_fatiao` (`get_law_item_content`) or
`pkulaw_citation_validator` before it enters the draft; `flk_npc_search`
confirms existence/status only. Article numbers below are drafting
anchors — re-verify against the current text every run, since amendments
renumber articles.

- **General contract**: content checklist 民法典 第470条; formation 第490
  条; performance + good faith 第509条; agreed / statutory termination
  第562–563条; breach 第577条; damages foreseeability 第584条; 违约金 and
  judicial adjustment 第585条; force majeure 第180条 + 第590条.
- **Choice of law (涉外)**: party autonomy 涉外民事关系法律适用法 第3条 +
  第41条 — only available for foreign-related contracts; a purely
  domestic deal cannot choose foreign law. Flag as `local-law` when the
  deal is borderline (e.g. FIE subsidiaries on both sides).
- **Dispute resolution**: arbitration clause must name the commission and
  scope (仲裁法 第16条; indeterminate → invalid, 第18条). Court election
  must respect 民事诉讼法 第35条 (actual-connection points, no breach of
  级别/专属管辖). Never draft both litigation and arbitration as
  alternatives — pathological under PRC practice.
- **Confidentiality / NDA**: pre-contract duty 民法典 第501条; trade
  secret floor 反不正当竞争法 第9条. A regulator / judicial compelled-
  disclosure carve-out is mandatory-content per the library default.
- **Employment**: written contract 劳动合同法 第10条; mandatory terms 第
  17条 (a draft missing one is a `risk` Finding, not a style choice);
  probation caps 第19条; 违约金 only for training service period (第22条)
  and non-compete (第23条); non-compete scope/duration + compensation 第
  23–24条; termination grounds 第39–40条; severance 第47条. Labor
  disputes go to 劳动仲裁 first (劳动争议调解仲裁法 第5条) — do not draft
  a court-first clause.
- **License / IP**: patent assignment/license writing + registration
  专利法 第10条; trademark license recordal 商标法 第43条; copyright
  license/assignment 著作权法 第26–27条; cross-border technology
  transfer runs through 技术进出口管理条例 (catalogue check).
- **Data**: personal-information processing basis 个人信息保护法 第13条;
  entrusted-processing agreement 第21条; cross-border transfer gates 第
  38条. Any clause moving personal data out of the PRC triggers general
  playbook §6 escalation.

## 5. PRC filing formalities (起诉状 / 答辩状)

- 起诉状 content follows 民事诉讼法 第122条 (起诉条件), 第123条 (副本按
  被告人数) and 第124条 (记明事项: 当事人信息、诉讼请求和所根据的事实与
  理由、证据和证据来源、证人姓名和住所); 答辩状 timing 第128条 (十五日).
- Track the SPC / 司法部《民事起诉状、答辩状示范文本（试行）》(2024)
  structure where applicable; the skeletons in `resources/prc_filings/`
  follow it. Layout follows GB/T 9704-style 公文 conventions; use the
  dedicated docx template for filings once one ships in `documents/`.
- 诉讼请求 are numbered, self-contained, and executable (给付/确认/形成
  claims stated so a judgment can copy them); interest and costs claims
  are separate numbered items.
- 事实与理由 keeps facts before law; the legal basis paragraph cites
  verified pinpoints only, per §4 above.
- The court, 具状人 signature/盖章, and date block are never omitted;
  attachments list (副本份数, evidence list) closes the document.

## 6. QA pass

1. **Round-trip check.** Re-read the assembled draft with `read_document`
   after `write_docx`: clause numbering continuous, cross-references
   resolve, no orphan placeholders except intentional `[待商定]` ones.
2. **Coverage check.** Every term-sheet item → a clause or an
   `Out of scope` line; every mandatory-content rule for the type (§4)
   satisfied or flagged as a `risk` Finding.
3. **Basis check.** Count clauses vs verified bases; the bundle's
   `basis_verified` must be an honest count, and any `pinpoint
   unavailable` clause appears in Findings.
4. **Bundle.** Findings are drafting decisions and gaps, not clause
   restatements. `Sources` lists each statute/authority once in the
   general playbook §5 line format — this is what `audit_citations` and
   the downstream `/cite-check` gate see; never bypass either.
