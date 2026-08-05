# Litigation-analysis methodology

Deep guidance for `/litigation-analysis`. The binding output and
authority-verification contracts live in `SKILL.md`; this file explains
how to do the work well.

## 1. Intake and posture

- Fix the four frame variables before analyzing: **parties**, **side**
  analyzed for, **cause(s) of action**, **posture** (pre-filing / 一审 /
  二审 / 再审 / arbitration / enforcement). A strength call made for the
  wrong side or posture is worse than none.
- Screen three procedural gates up front — each is a Finding when it
  bites, not a footnote:
  - **管辖 (jurisdiction / forum)** — 级别管辖, 地域管辖, any 协议管辖
    clause; for arbitration, whether a valid arbitration agreement ousts
    the court (raise it before 首次开庭, or the objection is waived).
  - **诉讼时效 (limitation)** — §4 below; run it even when nobody asked.
  - **当事人适格 / standing** — right plaintiff, right defendant
    (esp. 分公司 vs 法人, assignment chains, subrogation).
- Inventory the record: every document gets an id, date, and reliability
  note (original / copy / unsigned draft / translation). Missing
  documents the analysis depends on go under `Out of scope`.

## 2. Claims/defenses element mapping

- Work by 请求权基础: for each claim, find the statutory basis that
  grants the remedy, then decompose it into elements. Do the same for
  each defense (they have elements too). Presets in
  `resources/element_tables/` carry decompositions for common PRC causes
  of action; re-verify every preset pinpoint via `pkulaw_fatiao` before
  relying on it.
- Element-table columns (one row per element):
  `element | burden | supporting facts | contrary facts | evidence (pinpoint) | authority (verified) | strength`.
- **Burden discipline** — default 谁主张、谁举证 (民事诉讼法 第67条,
  2023 revision; re-verify the 条号, the 2021/2023 revisions renumbered
  the law) with the reversal / presumption cases from
  《最高人民法院关于民事诉讼证据的若干规定》. Record burden per element,
  not per claim: a claimant can carry four elements and the respondent
  the fifth.
- **Strength scale** — `strong` (evidence + authority both support),
  `arguable` (evidence thin or authority split), `weak` (contrary
  evidence dominates), `unsupported` (no evidence at all). Never average
  across elements: one `unsupported` element defeats the claim however
  strong the rest are — that is the point of element mapping.
- 争议焦点 synthesis comes last: the 2–4 elements the matter actually
  turns on, stated as questions the tribunal must answer.

## 3. Evidence chronology

- One row per dated event: `# | date | event | source (doc + pinpoint) |
  characterization (helps / hurts / neutral for the analyzed side) |
  disputed? | related element id(s)`.
- Dates come from the document, never from memory; an event whose date
  two documents state differently is one row marked `disputed` citing
  both pinpoints.
- Chronologies past ~10 events go to `write_xlsx`: sheet `Chronology`
  (columns above, freeze panes `B2`), sheet `Notes` (document inventory
  with reliability notes, legend). Re-read via `inspect_xlsx` and
  spot-check 3 rows against their sources before reporting the path.
- The chronology is also the limitation evidence: accrual, 中断 and 中止
  events must each appear as rows with pinpoints.

## 4. 诉讼时效 limitation framework (PRC)

Verify every article below via `pkulaw_fatiao` before relying on it.

- **General period** — 3 years from when the right-holder 知道或者应当知道
  the harm **and** the obligor (民法典 第188条), capped at 20 years from
  the harm itself (extension only by court decision in special
  circumstances).
- **Not sua sponte** — the court may not apply limitation on its own
  initiative (民法典 第193条); as respondent, plead it or lose it; as
  claimant, expect it.
- **中止 (suspension)** — obstacle in the **last six months** of the
  period (不可抗力, incapacity without guardian, post-succession
  uncertainty, obligor control, etc.); the period completes six months
  after the obstacle ends (民法典 第194条).
- **中断 (interruption)** — demand for performance, obligor's
  acknowledgment / agreement to perform, filing suit or arbitration, or
  equivalent acts; the period **restarts** from the interruption or the
  end of the proceedings (民法典 第195条). Log each candidate event in
  the chronology with its pinpoint — a dunning letter is worth pleading.
- **Excluded claims** — 停止侵害 / 排除妨碍 / 消除危险, return of
  registered-property, 抚养费 / 赡养费 claims and the like are not
  subject to limitation (民法典 第196条).
- **除斥期间 distinction** — fixed exercise periods (e.g. 撤销权) admit
  no 中止 / 中断 / extension (民法典 第199条); classify the period
  correctly before applying interruption arguments.
- **Special periods (特别法优先)** — special laws displace 第188条:
  labor-arbitration one year (劳动争议调解仲裁法 第27条), one-year
  carriage-of-goods-by-sea claims (海商法), insurance-claim periods
  (保险法), the two-year 申请执行 period in the 民事诉讼法 (renumbered
  across recent revisions — resolve the current 条号 via
  `pkulaw_fatiao`). Active domain packs may add their own; check the
  pack overlay before concluding the general period applies.
- State the limitation position as operative dates: accrual date, any
  中止 / 中断 events with dates, computed expiry, and today's status
  (`ok / at-risk / expired / n/a`), each date pinned to a chronology row.

## 5. Authority weight and treatment

- PRC weight order: 指导案例 (courts 应当参照 per
  《最高人民法院关于案例指导工作的规定》第7条) > 公报案例 / SPC 典型案例
  (persuasive) > higher-court judgments in the same province > other
  judgments. 类案检索 discipline follows
  《关于统一法律适用加强类案检索的指导意见（试行）》.
- Record treatment per authority: `followed / distinguished /
  superseded (law amended or 司法解释 replaced) / conflicting-line`.
  A case decided under a repealed provision is a trap — check the
  statute's revision history when the judgment predates the current
  text.
- Search discipline: `search_case` (semantic) for fact-pattern matches;
  `get_case_list` (keyword) for title / party / 案由 scans; normalize
  every 案号 via `anhao_recognition`. US comparative authority through
  `courtlistener_search`; note it as comparative, never controlling.

## 6. QA pass (procedural rubric)

Before emitting the bundle, self-score against this checklist — a miss
is a Finding or an `Out of scope` line, never silence:

1. Jurisdiction / arbitration screen done and stated.
2. Limitation position stated with operative dates and article pinpoints.
3. Every element has a burden owner and a strength call; no claim scored
   by averaging.
4. Every chronology row carries a source pinpoint; disputed dates marked.
5. Every cited authority verified per the SKILL.md contract; treatment
   recorded; no `unverified` authority inside a strength rationale.
6. Relief matches posture (damages vs specific performance vs 撤销;
   appeal grounds vs first-instance claims).
7. Bundle ≤ 2000 characters; detail lives in the workbook / table.
