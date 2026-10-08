# Issue and element method (争点—请求/抗辩—要件—待证事实—证据)

How to break a matter into issues, close each issue element by element,
search case law per issue, and check the result for internal consistency.
Authority rank, weight labels and the date-before-text rule are in
`/playbook/general_playbook.md` §0; the reasoning steps are §3a. This file
is the working method behind them. Sources and licences are listed at the
end.

## 1. Issue tree

Build one tree per matter before researching:

`法律关系 → 请求 / 抗辩 → 要件 → 待证事实 → 证据 (pinpoint) / 来源`

- **Fix the requests first.** List each 诉讼请求 (or claim in arbitration)
  with its type — 给付 / 确认 / 形成 — and mark dependencies: a request that
  presupposes another (返还 after 确认无效, 违约金 after 解除) is decided with
  its premise and falls with it.
- **One request basis per branch.** A 案由 is an index, not a request basis;
  a branch is complete only when it can say who claims what from whom on
  which norm. Competing bases (违约 vs 侵权, 第186条 民法典) are separate
  branches; say which one the analysis follows and why.
- **List unproven assumptions** as leaves of the tree, not as facts.

## 2. Claim lifecycle — the row order of the element table

Test every request basis in four stages, in this order, even when a stage
is one line:

| Stage | Question | Typical content |
|---|---|---|
| 产生 (arises) | Did the right come into existence? | the norm's elements; formation and effectiveness of the legal act; defences that stop the right arising (无效, 免责, 不可抗力) |
| 未消灭 (not extinguished) | Has it lapsed since? | 履行, 抵销, 免除, 提存, 混同 (民法典 第557条), 解除, guarantee period lapse |
| 可行使 (exercisable now) | Can it be enforced today? | 诉讼时效 (抗辩权, 第192条), 同时履行 / 先履行 / 不安抗辩 (第525–527条), conditions, 先诉抗辩 (第687条) |
| 抗辩 / 反抗辩 (replies) | Is a defence itself defeated? | 时效中断 (第195条), notice given in time, waiver, the defence's own elements unmet |

The presets in `resources/element_tables/` tag each defence with its
`stage`; keep that order in the table so a reader sees whether the claim
fails at birth, has ended, or is merely blocked.

**否认 is not 抗辩.** A denial disputes an element of the claimant's case
and leaves the burden where it was; a defence asserts new facts (with its
own elements and burden on the party raising it). Never write the two into
one row.

## 3. Element → fact → evidence, and the burden

- One row per element: `element | burden | supporting facts | contrary facts
  | evidence (pinpoint) | authority (verified, weight label) | strength`
  (columns as in `methodology.md` §2).
- **Burden** follows the element: 谁主张，谁举证 (民事诉讼法 2023修正 第67条)
  unless a statute or 司法解释 shifts it; cite the shifting provision, never
  an invented "principle".
- **真伪不明.** When the evidence on a decisive fact is in equipoise, the
  party with the burden loses on that fact. State it in those terms; do not
  split the difference, and do not supply a fact the record lacks to make
  the conclusion fit.
- **Contradictions first.** Before applying any rule, list facts that
  conflict — two dates for one event, two versions of a meeting, a document
  that contradicts the narrative, an amount that does not reconcile. Carry
  each contradiction into the table as a `disputed` row and show how each
  version changes the outcome. Computing straight through a contradiction
  is the commonest silent error.

## 4. Three search rounds per issue

Run them per issue, not per matter:

1. **Exact basis.** The governing norm and its pinpoint in the version in
   force on the event date; 司法解释 on point; 类案 by 案由 + the norm.
2. **Gap sweep.** Synonyms, the concept one level up and one level down,
   related norms and the definitions they rely on, other 案由 that reach
   the same facts.
3. **Contrary.** Adverse cases, split lines, newer law or 司法解释 that cut
   the other way, and the opponent's strongest reading. A round that finds
   nothing is recorded as such.

Record each round: tool, date, query, result count, sources kept, and
sources considered but excluded with the reason. The excluded list is what
lets a reviewer see the contrary round actually ran.

## 5. 类案 search — SPC 2020 guidance as a checklist

《最高人民法院关于统一法律适用加强类案检索的指导意见（试行）》 (in force
2020-07-31) binds courts; follow it so the work product matches what the
court will do.

- **What a 类案 is (一).** Similar in 基本事实, 争议焦点 and 法律适用问题, and
  already 生效. Shared keywords are not similarity.
- **Scope, in order (四):** (1) SPC 指导性案例; (2) SPC 典型案例 and SPC
  生效裁判; (3) the province's 高级法院 参考性案例 and its 生效裁判; (4) the
  上一级法院 and the trial court's own 生效裁判. Except for 指导性案例, search
  the last three years first; once a tier yields 类案, lower tiers may be
  skipped.
- **Also search 人民法院案例库.** Courts must search it and refer to its
  入库案例 (《人民法院案例库建设运行工作规程》 第19、21条); a 入库案例 a party
  submits must be answered in the judgment's reasoning.
- **Methods (五):** keyword, statute-linked (法条关联), and case-linked
  (案例关联) search.
- **Compare (六):** check each hit for similarity before calling it a 类案.
- **Report contents (八):** who searched, when, on which platform, how, the
  results, each 类案's 裁判要点, the pending case's 争议焦点, and whether each
  类案 is followed (参照) or used for reference (参考) and why.
- **Effect (九, 十):** a 指导性案例 is followed unless it conflicts with newer
  law, 行政法规 or 司法解释 or has been superseded; other 类案 are reference.
  When a party cites a 指导性案例, the court must say in its reasoning
  whether it follows it and why.
- **Conflicting 类案 (十一):** weigh court level, decision date and whether
  the 审判委员会 discussed it; flag the split rather than pick silently.

## 6. 类案 matrix

One row per case the analysis relies on or distinguishes:

`case id (库编号 / 案号) | court, level, date | status (生效 / 再审 / 撤销) |
key facts and issue | rule applied | weight label (playbook §0) |
comparability: shared element facts / decisive differences / procedural
differences / norm-version differences | adverse passages | verification`

- Read the full text of every case cited; a case known only from a summary
  or a database abstract is a lead and stays out of the strength rationale.
- A case decided under a since-amended norm is compared on the version it
  applied, and says so.
- Keep the opposing cases in the matrix; the contrary round feeds it.

## 7. Consistency check before the bundle (八个一致)

Run all eight; each miss is fixed or reported, never left silent:

1. **Requests ↔ scope** — every request is analysed; nothing is decided
   that nobody asked for.
2. **Facts ↔ evidence** — every fact relied on has an evidence pinpoint or
   is marked assumed.
3. **Facts used ↔ facts found** — reasoning uses only facts established
   earlier in the analysis.
4. **Reasoning ↔ norm** — each step tracks the request / defence basis and
   its elements.
5. **Norm ↔ conclusion** — the legal consequence matches the outcome and
   the relief claimed.
6. **Each side's main argument ↔ answered** — the opponent's core defence
   and strongest authority are dealt with.
7. **Internal logic** — no conclusion contradicts another; dependencies
   between requests are honoured.
8. **Names, numbers, dates** — parties, abbreviations, amounts and dates
   are the same everywhere.

## Sources and licences

- Claim lifecycle (§2) and the defence-stage idea adapted, with changes,
  from `lilialla/request-right-skill-reference`
  (https://github.com/lilialla/request-right-skill-reference) — methodology
  text licensed CC BY 4.0 (https://creativecommons.org/licenses/by/4.0/),
  code MIT. No data files were copied.
- The eight-point consistency check (§7), the denial-vs-defence split and
  the 真伪不明 rule (§3) adapted from `happylawyer/jiubufa-skill` (要件审判
  九步法; https://github.com/happylawyer/jiubufa-skill), MIT, © 2025
  happylawyer.
- The issue tree (§1), three search rounds (§4) and 类案 matrix (§6)
  follow ideas in `CSlawyer1985/claude-for-legal-ZH` (`issue-memo`,
  `case-comparison`; https://github.com/CSlawyer1985/claude-for-legal-ZH),
  Apache-2.0; restated in our own words, no text copied.
- §5 restates the SPC guidance (court.gov.cn/fabu/xiangqing/243981.html)
  and the 案例库 rules (法〔2024〕92号, court.gov.cn/zixun/xiangqing/431662.html),
  verified 2026-10-07.
