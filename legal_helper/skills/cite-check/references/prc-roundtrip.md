# PRC Round-Trip — PKULaw + flk.npc.gov.cn

PRC has no `eyecite` analog. The regex extractor in
`legal_helper/citations/prc.py` is the structural-form layer; round-trip
against a primary source is the authority layer. Use this guide when the
cite-check workflow reaches **Phase 2 (Mode-1 verify)**, **Phase 3
(Mode-2 verify)**, or **Phase 4 (currency)**.

## Source-of-truth ladder

| Source | Auth header | Coverage | When |
|---|---|---|---|
| **PKULaw MCP (9 sub-services)** | `PKULAW_API_TOKEN` via WSO2 `apikey` | 法律 + 行政法规 + 部门规章 + 司法解释 + 案例 + 国函/国办发 + 律所文章 + 论文 | Default first stop. |
| **flk.npc.gov.cn** | none (public) | 法律 / 行政法规 / 监察法规 / 司法解释 / 部门规章 | Free fallback; cross-check; case law NOT covered. |
| **官方网站** (samr.gov.cn, mee.gov.cn, npc.gov.cn, court.gov.cn) | none | as published | Last-resort primary citation when the above miss. |

## Mode-1 patterns (does the cite exist?)

### 案号 (judgment case number, e.g. `(2023)京01民终12345号`)

1. `pkulaw_anhao__anhao_recognition(text)` — normalize the form, get
   canonical 案号 + 法院码 + 案件类型.
2. `pkulaw_case_list__get_case_list(anhao=…)` — fetch the case record.
3. If PKULaw returns nothing, fall back to the legal `citations/prc.py`
   regex match (structural validity only). There is no free public
   case-law DB, so a missing PKULaw hit is `could_not_check`, *not*
   "fabricated" — flag it MODEL-ONLY.

### 法律 / 行政法规 statute pinpoint (《X》第N条)

1. `pkulaw_fatiao__get_law_item_content(title="X", item="N")` — fetch
   exact 条号 + body text.
2. Fallback: `flk_npc_search(query="X")` confirms the statute exists and
   returns its current `status` (有效 / 已修改 / 已废止 / 尚未生效); it
   does not return body text — for 第N条 body call PKULaw.
3. The flk_npc `status` field is the **good-law** check for Phase 4.

### 法释〔YYYY〕N号 (SPC judicial interpretation)

1. `pkulaw_law_search__search_article(query="法释〔YYYY〕N号", level="judicial_interpretation")`.
2. Fallback: `flk_npc_search(query="法释〔YYYY〕N号", level="judicial_interpretation")`.

### 国函 / 国办发 / 国发 (State Council issuances)

1. `pkulaw_law_search__search_article(query="国函〔YYYY〕N号")`.
2. Fallback: `flk_npc_search(query="国函〔YYYY〕N号")`.

### 部令 / 部发 / 部公告 (Ministry orders)

1. `pkulaw_law_search__search_article(query=…)`.
2. Fallback: `flk_npc_search(query=…, level="rule")`.
3. Many ministry rules are issued by 国家市场监管总局 (SAMR) / 生态环境部 (MEE) /
   药监局 (NMPA) / 网信办 (CAC) etc. — the ministry's own website is often
   the best primary citation when PKULaw + NPC database both miss.

## Mode-2 patterns (does the source support the claim?)

After fetching the source text:

1. `quote_roundtrip_tool(quote=<draft quote>, source_text=<fetched text>)` —
   classifies as `exact` / `near` / `not_found` / `could_not_check`.
2. `pkulaw_citation_validator__adjust_provisions(text)` — PKULaw's own
   citation-chain validator. Pass a paragraph containing the cite; it
   returns whether the citation chain is internally consistent and
   whether each cited 条款 actually exists in the parent law.
3. For 司法解释 → 法律 mappings (e.g. 法释 references that point back into
   一部具体法律的某一条), `pkulaw_doc_link__get_linked_content(text)` returns
   the resolved linked content and exposes the dangling references.

A paraphrase-as-quote on a 法院认定 / 法院判决 sentence is CRITICAL — the
sentence-leveled holding gate behind the escalation threshold.

## Phase 4 — currency / 现行有效 check

- **法律 / 行政法规.** `flk_npc_search` returns a `status` field. Map
  `已废止` / `已失效` → CRITICAL with issue_type=`out_of_date`. Map
  `已修改` → NUANCED if the cited pinpoint is in the affected revision.
- **司法解释.** PKULaw `pkulaw_law_search` exposes `effective_date` and
  any superseding interpretation reference. If a later 法释 has expressly
  superseded the cited one, flag CRITICAL.
- **指导性案例.** SPC may withdraw guiding cases; PKULaw returns
  `withdrawn=true` on the case record. Withdrawn guiding cases cited as
  authority are CRITICAL.

## Language

Citations in the draft and source remain in the original language.
Quotes from PRC sources are rendered in Chinese with an inline English
translation when the proposition is in English:

> 《刑法》第233条 "过失致人死亡的，处三年以上七年以下有期徒刑"
> ("whoever negligently causes the death of another shall be sentenced
> to fixed-term imprisonment of not less than three years and not more
> than seven years")

The report renderer accepts `lang="zh"` so the audit speaks the user's
language; internal `cite-check` reasoning may run in whichever language fits
the sources — 中文 is encouraged when tracing PRC provisions (per the global
language policy in `CLAUDE.md`).
