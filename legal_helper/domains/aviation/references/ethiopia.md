# Ethiopia (ET) — civil-aviation legal reference

Pulled on demand when an aviation task touches Ethiopia (e.g. opening an
Addis Ababa / Bole route, an Ethiopian-registered counterparty, ET-AAA
registration marks, an Ethiopian Airlines codeshare/wet-lease, or ECAA
oversight). Default jurisdictional focus elsewhere in the product stays PRC;
this file is the Ethiopia overlay for aviation work only.

> **Not legal advice.** Ethiopian gazette text below is mirrored from a
> third-party source (metaappz) and ECAA's site; verify article numbers
> against the official Federal Negarit Gazeta before reliance.

## 1. Regulator and instrument stack

| Layer | Instrument | Connector |
|---|---|---|
| Regulator | Ethiopian Civil Aviation Authority (ECAA), under the Ministry of Transport and Logistics | `ecaa_index`, `ecaa_fetch` |
| Primary statute | **Civil Aviation Proclamation No. 616/2008** — safety/security/economic oversight, ECAA powers, air operator certification, registration, licensing, airspace | `ethiopia_law_fetch('616/2008')` |
| Amendment | **Civil Aviation (Amendment) Proclamation No. 1179/2020** — definitions, sanctions, enforcement | `ethiopia_law_fetch('1179/2020')` |
| Institutional | **ECAA Re-establishment Proclamation No. 273/2002** — objectives, organs, powers | `ethiopia_law_fetch('273/2002')` |
| Implementing rules | Ethiopian Civil Aviation Regulations / Rules (ECAR) — personnel licensing, operations, airworthiness, air navigation, security | `ecaa_fetch('sectoral/regulation')` then follow PDF document_links |
| Treaty layer | ICAO Convention on International Civil Aviation (Chicago 1944) — Ethiopia is an original contracting state (1947); Annexes 1–19 implemented via ECAR | `retrieve_legal(collection='aviation_treaties' / 'icao_doc', ...)` |
| Finance/lease | Cape Town Convention + Aircraft Protocol (IDERA) — Ethiopia is a contracting state | `retrieve_legal(collection='aviation_treaties', query='Cape Town Aircraft Protocol IDERA')` |
| Market access | Bilateral Air Services Agreement (BASA) between Ethiopia and the partner state + ECAA economic licensing | `web_search('Ethiopia bilateral air services agreement <partner>')` |

## 2. Route-opening legal checklist (foreign carrier into Addis Ababa)

1. **Traffic rights** — confirm the operative BASA grants the route, frequency,
   and capacity; check designation/route-schedule annex. Not in the gazette
   mirror — verify via the partner CAA + ECAA air-transport directorate.
2. **Foreign air operator authorisation** — ECAA validation of the carrier's
   AOC and ops specs under the Civil Aviation Proclamation 616/2008 +
   Ethiopian Civil Aviation Regulations (operations part).
3. **Insurance** — minimum third-party / passenger liability per ECAR and
   MC99 (Ethiopia's third-party and passenger liability regime); align with
   Reg (EC) 785/2004 thresholds if the partner side is EU.
4. **Ground handling / station** — local establishment, investment permit
   (Investment Proclamation 1180/2020, EIC), and Commercial Code 1243/2021
   for the handling/GSA contracts.
5. **Security & facilitation** — ICAO Annex 17 (security) and Annex 9
   (facilitation) as implemented in ECAR; ECAA security programme acceptance.
6. **Slots / charges** — Addis Ababa Bole (HAAB/ADD) airport charges and slot
   coordination via the airport operator / ECAA economic regulation.

## 3. Citation format

- Statute: `Proclamation No. 616/2008, art. X, Federal Negarit Gazeta` (give
  the year/issue when known).
- Implementing rule: `ECAR Part-N, §N.N`.
- Treaty: `Chicago Convention Annex N, Standard N.N`; `MC99 art. N`;
  `Cape Town Convention / Aircraft Protocol art. N`.
- When the exact article cannot be confirmed against the gazette (the 616/2008
  and 273/2002 mirrors are bilingual scans), write `pinpoint unavailable` and
  soften the claim rather than guessing an article number.

## 4. Source caveats

- `ethiopia_law_fetch` returns a clean typed English transcript only for the
  subset of proclamations metaappz has re-typed (e.g. 3/1995). For 616/2008,
  1179/2020 and 273/2002 it falls back to the bilingual Amharic+English gazette
  **scan**, where English clause text is interleaved with Ge'ez — locate the
  English article heading for the pinpoint, and prefer `ecaa_fetch` /
  `web_fetch` on an official ECAA PDF when one is available.
- The ECAA site (`ecaa.gov.et`) is the authoritative regulator surface; treat
  the metaappz mirror as a convenience and flag any conflict for counsel.
