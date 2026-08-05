# Aviation Cite Formats (cite-check overlay)

Aviation legal answers cite a heterogeneous mix of statutes, ADs, treaties,
ICAO Annexes/Docs, and operator manuals. The `audit_citations` check
should accept any of the following pinpoint formats. When converting a
draft to a primary source, prefer the **bolded** canonical form.

## Statutes & regulations

| Jurisdiction | Canonical form | Examples |
|---|---|---|
| US 14 CFR | **`14 CFR § <part>.<section>`** | `14 CFR § 121.213`, `14 CFR § 250.5`, `14 CFR § 382.81` |
| US 49 CFR | **`49 CFR § <part>.<section>`** | `49 CFR § 1544.103` (TSA), `49 CFR § 175.10` (HazMat) |
| EU EASA Implementing Rules | **`Reg (EU) <num>/<year> Part-<X> <ITEM>`** | `Reg (EU) 965/2012 Part-CAT CAT.OP.MPA.105` |
| EU passenger rights | **`Reg (EC) <num>/<year> art. <N>`** | `Reg (EC) 261/2004 art. 5`, `Reg (EC) 1107/2006 art. 9` |
| PRC CCAR | **`CCAR-<num>(-R<rev>) 第<X>条`** | `CCAR-121-R7 第121.583条`, `CCAR-396 第15条` |
| PRC normative docs | **`民航发〔YYYY〕<num>号 第<X>条`** or `MD-FS-<num>` | `民航发〔2024〕12号 第3条` |
| ICAO Annexes | **`Annex <N> §<X>.<Y>`** | `Annex 6 Part I §4.4.1`, `Annex 9 §3.45`, `Annex 13 §5.12` |
| ICAO Docs | **`ICAO Doc <num> §<X>.<Y>`** | `ICAO Doc 9082 §I-3`, `ICAO Doc 9284 §1.1.1` |

## Airworthiness Directives & Safety Bulletins

| Issuer | Canonical form | Examples |
|---|---|---|
| FAA AD | **`FAA AD <YYYY-NN-NN>`** | `FAA AD 2025-12-09` |
| FAA AC | **`FAA AC <num>(-<rev>)`** | `FAA AC 120-92B` |
| FAA SAFO / InFO | **`FAA SAFO <NNNNN>`** / **`FAA InFO <NNNNN>`** | `FAA SAFO 23001` |
| EASA AD | **`EASA AD <YYYY-NNNN>`** | `EASA AD 2025-0123` |
| EASA EAD (Emergency) | **`EASA EAD <YYYY-NNNN-E>`** | `EASA EAD 2026-0095-E` |
| EASA SIB | **`EASA SIB <YYYY-NN>`** | `EASA SIB 2024-15` |
| TC AD (Canada) | **`TC AD CF-<YYYY-NN>`** | `TC AD CF-2024-12` |
| CAAC 适航指令 | **`CAAC AD <YYYY-NNNN>` or 民航局适航指令` | `CAAC AD 2024-0008` |

For every AD cite, pinpoint **also** to the underlying mandatory requirement
paragraph (e.g. "AD 2025-0123 §(g)(2)") whenever the AD has compliance
sub-paragraphs.

## Treaties (aviation_treaties RAG collection)

| Treaty | Canonical form | Examples |
|---|---|---|
| Chicago Convention | **`Chicago Convention art. <N>`** | `Chicago Convention art. 33` |
| Tokyo Convention 1963 | **`Tokyo Convention art. <N>`** | `Tokyo Convention art. 6` |
| Hague Convention 1970 | **`Hague Convention 1970 art. <N>`** | `Hague Convention 1970 art. 7` |
| Montreal Convention 1971 / 1988 Protocol | **`Montreal Convention 1971 art. <N>`** | `Montreal Convention 1971 art. 1` |
| Beijing Convention 2010 + Protocol | **`Beijing Convention 2010 art. <N>`** | `Beijing Convention 2010 art. 1` |
| Montreal Convention 1999 (MC99) | **`MC99 art. <N>(<sub>)`** | `MC99 art. 17(1)`, `MC99 art. 21` |
| Warsaw 1929 | **`Warsaw Convention art. <N>`** | `Warsaw Convention art. 17` |
| Cape Town 2001 + Aircraft Protocol | **`Cape Town Convention art. <N>`** / **`Aircraft Protocol art. <N>`** | `Cape Town Convention art. 13`, `Aircraft Protocol art. IX` |

When the user is operating from a state that has made an Article 13
advance-remedies declaration, note the declaration explicitly with the
cite (e.g. `Cape Town Convention art. 13 + PRC Article 13 declaration`).

## Retrieval order when validating a draft

When `cite-check` is invoked over a draft that contains treaty / AD /
passenger-rights cites, fan out as follows (parallel where independent):

1. **Treaty cites** → `retrieve_legal(collection="aviation_treaties")`
   filtered by `treaty_name`. The hit's `metadata.cite_prefix` tells you
   whether the draft's prefix is correct.
2. **ICAO Annex / Doc cites** → `retrieve_legal(collection="icao_doc")`.
3. **AD numbers** → `drs_search(query=<AD#>, doc_types=["AD"])` for FAA;
   `easa_ad_fetch(ad_number=<num>)` for EASA.
4. **CFR cites** → `faa_title14_search` or `ecfr_search`.
5. **EU regs** → `eurlex_search(document_type="REG")`.
6. **CCAR / CAAC 法律法规 cites** →
   `aviation_source_search(source="caac_local_fetch", identifier=<title or CCAR part>, article=<article>)`.
   Pin the draft's article number to the returned `article_text`. If
   `article_text` is empty, check the result's `alternates` for a revision
   that contains the article; if still missing or the entry is flagged
   `pdf_stub`, cross-check `pkulaw_fatiao__get_law_item_content` and treat
   that as authoritative. Flag any cite whose body could not be produced
   from either source as `needs human verification`.

If a draft cites a treaty/Annex that the RAG has no chunk for, mark the
cite **`needs human verification`** in the cite-check report — do not
fall through to web search and improvise.

## When a pinpoint is genuinely unavailable

Write `pinpoint unavailable` (English) or `无法获得具体条款` (Chinese)
**explicitly**. Do not invent a section number. `audit_citations` accepts
this literal string.
