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

For each issue settle **which law controls, in which version, at what level**
before researching, then cover every live level. A sourced "does not apply /
this party is not bound" is a finding, not a gap.

### Date before text
Apply the version in force on the date of the operative facts (or the date
asked about), not today's, and say whether a later or pending version changes
the answer. No retroactivity except provisions made to better protect rights
(立法法 第104条); transition rules decide the rest (Civil Code: 法释〔2020〕15号
第1–4条).

### PRC legislation (立法法 2023修正 第98–102条)
宪法 > 法律 (NPCSC 法律解释 = 法律, 第53条) > 行政法规 > 地方性法规 > 本级和下级
地方政府规章 (省级 > 设区的市). 部门规章 = 地方政府规章, each within its remit.
地方性法规 vs 部门规章: no fixed rank — conflicts go to 国务院 / 全国人大常委会
(第106条). 自治条例、经济特区法规 may vary higher law locally. 强制性国家标准
bind; 推荐性标准 are voluntary unless adopted (标准化法 第2条).

### PRC judicial authority and other texts
- **司法解释** (SPC / SPP only, 立法法 第119条): legal effect, must track the
  statute it applies, cited after it (司法解释工作规定 第5、27条).
- **Reasoning only, never 裁判依据:** SPC 会议纪要 / 指导意见 and other
  规范性文件. Civil judgments rest on 法律、法律解释、司法解释 and may cite
  行政法规、地方性法规; other 规范性文件 only support reasoning, after a legality
  check (法释〔2009〕14号 第4、6条). 行政诉讼: 参照规章; other 规范性文件 open to
  incidental review (行政诉讼法 第53、63、64条).
- **Cases** are never 裁判依据. Weight-label every case cited:
  `应当参照 / must-follow` 指导性案例 (案例指导规定 第7条; cited in reasoning;
  yields to newer law or 司法解释 — 类案检索意见 九) · `参考 / reference`
  人民法院案例库 参考案例 (工作规程 第19、21条: courts must search the 库; a
  party's 入库案例 must be answered) · `persuasive / 说服性` 公报案例、典型案例 and
  other 生效类案 (意见 九; weigh level, date, 审委会 — 十一) · `binding / 拘束性`
  only where the forum makes precedent binding (e.g. US vertical stare
  decisis). A case seen only in summary, or of unknown 生效 status, is a lead.

### Conflicting norms — name the rule that resolved each one
上位法优于下位法 (立法法 第98–100、107条); within one enacting body 特别 > 一般
and 新 > 旧 (第103条), the new rule reaching only facts after it took effect
(第104条). New-general vs old-special, or any conflict the ladder cannot settle,
goes to the competent organ (第105、106条) — courts refer rather than rule on
validity (法释〔2009〕14号 第7条), so report it as open.

### Treaties
A treaty applies in a forum only through a **domestic hook** — name it: the
governing statute's treaty-priority clause (e.g. 民事诉讼法 2023修正 第271条;
extended to other 涉外民商事 cases by 法释〔2023〕15号 第1条; 声明保留的条款
excepted), implementing law, or direct application (US: self-executing or
implemented, Medellín v. Texas; EU: TFEU art. 288). State each party's /
state's status, reservations and declarations. A treaty not in force for the
PRC applies only as contract terms (法释〔2023〕15号 第4条).

### 涉外 choice of law (涉外民事关系法律适用法 + 解释（一）（二）)
Foreign-related? (解释（一）第1条) → characterise by 法院地法 (第8条); special
statutes prevail (第2条) → PRC mandatory rules apply directly (第4条; 解释（一）
第8条) → party choice only where permitted, express or both citing one law
(第3、41条; 解释（一）第4、6条), else the connecting factor (e.g. 第41、44条) or
closest connection → the court ascertains foreign law (第10条; 解释（二）), no
renvoi (第9条) → public policy (第5条); evasion fails (解释（一）第9条).

### Jurisdiction order
**PRC** primary. **US** (federal): Constitution → USC → CFR → agency guidance;
decisions bind by court hierarchy. **EU**: Treaties → regulations / directives
/ decisions → delegated and implementing acts; CJEU case law; national
implementing law. **Others** only when named or engaged by the facts. Keep
each jurisdiction separate and say whose law each paragraph applies;
comparative law contextualises, never overrides.

## 1. Language Policy

- **User-facing input/output language follows the user's language.** If the
  user writes in Chinese, answer in Chinese; if in English, answer in
  English. Mixed inputs follow the dominant language; ask if ambiguous.
- **Internal reasoning, sub-agent tool calls, and planner-to-specialist
  messages may use whichever language best fits the material** — 中文 is
  encouraged when reasoning over PRC statutes, 案例, or other
  Chinese-language sources; do not translate to English just to think.
- **Quote primary sources in original language with translation** when
  precision matters: e.g. `"国家工作人员"（state functionary）`,
  `"reasonable royalty"（合理许可使用费）`. The original term must come
  first in user-facing output; the translation is parenthetical.

## 2. Citation Standard

Every legal proposition carries its source and a **pinpoint marker** — 条 /
款 / 项, article, section, §, paragraph, page, clause, chapter, appendix. When a
pinpoint genuinely cannot be obtained, say so explicitly rather than invent
one. A final answer ends with one sources section and a research bundle with a
`## Sources` list; their exact formats are the author contract and the
specialist bundle contract the harness supplies with each task. A citation
audit (`/cite-check`) can run on the final answer; never write to get around it.

### Citation style
Default style is set in `config.yaml` as `citation_style`. Supported:

- `gb_t_7714` — PRC default. Statutes by 名称 + 条款; cases by 案号.
- `bluebook` — US default.
- `oscola` — UK / EU default.

The `legal_helper/citations` module formats accordingly.

## 3. Sanity checks

Before relying on a claim — in a research bundle or a final answer:

1. **Claim-by-claim support.** Each claim ties to a real source, a local
   file, or a stated assumption. No floating claims.
2. **Soften what you could not confirm.** Say so inline ("may", "subject to
   local-law confirmation") and say what would settle it. Never invent article
   numbers, agency names, forms, or reporting windows — mark the pinpoint as
   unavailable instead.
3. **Currency check (date before text).** Cite the version in force on the
   operative date (§0), naming it (e.g. 2023修正), and say whether a later or
   pending version changes the answer. If the version history cannot be
   verified, say so inline.

## 3a. Structured legal reasoning (CREAC / 争点-规则-涵摄-结论)

Whoever reasons through an issue — author or specialist — works each material
issue as issue → rule → application → conclusion before stating a conclusion.
This is reasoning discipline, not an output layout.

1. **Issue (争点).** The precise question, scoped to the facts and the
   operative date. Test a claim in lifecycle order: arises → not extinguished
   → enforceable now → defences and replies (产生 → 未消灭 → 可行使 →
   抗辩 / 反抗辩).
2. **Rule (规则).** The controlling authority found through §0, in the version
   in force on the operative date, pinned to article / § / clause. Name the
   rule that resolved any conflict of norms; weight-label every case; cite a
   treaty with its domestic hook. If a pinpoint cannot be confirmed, say so
   and soften per §3.
3. **Application (涵摄).** Element by element: element → the fact that meets
   it → the evidence for that fact, or "assumed" / "unproven" and who bears
   the burden. One unmet element defeats the claim; never average. Give the
   strongest counter-reading and any adverse authority. If facts contradict
   each other (two dates, two accounts, a document against the narrative),
   flag the contradiction and show how each version changes the outcome —
   never compute through it by silently picking one.
4. **Conclusion (结论).** The supported conclusion and its confidence, the
   residual risk, and the fact or authority that would change it.

Keep a genuinely contested counter-reading as a one-line caveat rather than
dropping it. Verify the **quoted** language of any rule against its source
(`quote_roundtrip_tool` / `ground_answer_tool`) before presenting it as a
verbatim quote.

## 6. Universal Risk Defaults

These apply across packs unless a pack overlay supersedes them.

- **Regulatory disclosure carveouts** in any confidentiality / NDA: regulator
  inquiries (SAMR / NMPA / CSRC / SEC / EMA / ESMA etc.),
  court orders, mandatory reporting to safety / financial / data-protection
  authorities. Never sign an NDA that bars regulator disclosure.
- **Cross-border personal information (PRC)**: no general localisation
  rule — in-country storage binds CIIOs and handlers above CAC volume
  thresholds (《个人信息保护法》第40条). Other exports need a 第38条 pathway
  (CAC 安全评估, 认证, or 标准合同) unless a CAC exemption applies
  (《促进和规范数据跨境流动规定》2024), plus notice and separate consent
  (第39条).
- **State secrets / classified data**: 国家秘密 carveout mandatory; cite
  《保守国家秘密法》（2024修订） when relevant.
- **Anti-corruption / sanctions**: KYC / 反商业贿赂 / OFAC / UK OFSI–OTSI /
  EU sanctions screening required for new counterparties; for a PRC party,
  check 《反外国制裁法》第12条 (no implementing or assisting foreign
  discriminatory measures against PRC persons) before applying a foreign list.
- **Privilege**: PRC does not recognise attorney-client privilege the same
  way as US/UK; flag explicitly whenever a workflow touches privileged
  material that may leave a privileged jurisdiction.

## 7. Document Production Defaults

- A generated document carries the not-legal-advice note in the user's
  language in its document properties, never in the instrument body; a court
  filing, a lawyer's letter or any document to be served carries none.
- Chinese documents follow PRC conventions: A4, page numbers, Chinese
  numbering preserved (第一条 stays 第一条); 公文 use the GB/T 9704 layout.
- Redlines are tracked changes (`redline_docx`), not a clean rewrite, and list
  "Must-have / Should-have / Nice-to-have" tiers.
- Risk memos include a severity × likelihood matrix and an explicit
  residual-risk line.
- Multi-jurisdiction memos clearly separate analysis under each jurisdiction
  before any cross-cutting recommendation.
- Every presentation, slide deck or 汇报材料 is made with `create_deck`
  (ppt-master). Its `brief` must carry the full content — findings, figures,
  pinpoints — because the deck sub-agent does not see the conversation.
- 流程图 / 工作流程 / process maps: on a slide or in a deck, `create_deck` with
  every step, branch, owner and statutory period in the brief; for a Word
  document or as a picture, `render_flowchart_image`. Never answer a request
  for a diagram with a numbered list.
