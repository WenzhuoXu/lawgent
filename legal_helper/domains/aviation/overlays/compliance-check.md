# Aviation overlay — /compliance-check

Layered when the aviation domain pack is active. Add the following to the
generic regulatory hierarchy and workflow.

## Authority hierarchy (additions)

- International: Chicago Convention; ICAO Annexes 1–19 + SARPs (esp.
  Annex 6 ops, Annex 8 airworthiness, Annex 13 accident investigation,
  Annex 16 environmental, Annex 17 security); Tokyo Convention + MP14;
  Warsaw / Montreal liability conventions; Cape Town Convention.
- US: 14 CFR Parts 21, 23, 25, 27, 29, 33, 35, 39, 43, 61, 63, 65, 67,
  91, 117, 119, 121, 125, 129, 133, 135, 136, 137, 141, 142, 145;
  49 CFR Parts 830 (NTSB reporting), 1540–1552 (TSA security); 22 CFR
  120–130 (ITAR); 15 CFR 730–774 (EAR); 31 CFR ch. V (OFAC).
- EU: Reg. (EU) 2018/1139 (EASA Basic Reg) + Part-CAT / NCC / NCO / SPA;
  Part-145 / CAMO / M / CAO / 66 / 147; Part-21 (initial + continuing
  airworthiness); Reg. 996/2010 (accident investigation); Reg. 376/2014
  (occurrence reporting just-culture); Reg. 785/2004 (insurance minimums).
- PRC: CCAR-21 / 23 / 25 / 39 / 43 / 91 / 121 / 135 / 145, CCAR-395 事故
  调查规定; 民航法; 公共航空运输旅客服务管理规定; 民用航空器适航审定
  程序.

## Approval / filing checklist additions

- Special operations LOAs / OpSpec: RVSM (B046), RNP-1 / RNP-AR / RNAV
  (B036), CPDLC / FANS-1A, ETOPS / EDTO, MNPS, polar (B055), special
  airports (C067), MEL (D095), international ops specs (A032–A036).
- Aircraft modification: TC vs STC vs minor change (Part 21); DER / ODA
  support (Form 8110-3); ICA update; W&B amendment; AFMS; MEL revision.
- Cross-border transfer / deregistration: DOR / DOR-EQ; IDERA in approved
  Cape Town form; International Registry filings (interest / sale /
  assignment / discharge); FAA AC Form 8050-1 (or destination registry
  equivalent); BAS export airworthiness approval; customs / VAT;
  insurance certificate amendment.

## Aviation-specific reporting matrix

| Authority | Trigger | Window |
|---|---|---|
| NTSB | 14 CFR § 830.5 accident/incident | Immediate |
| FAA SDR | 14 CFR § 121.703 / § 135.415 | 96 hours |
| State of registry | ICAO Annex 13 | As soon as practicable |
| State of operator | ICAO Annex 13 | ASAP |
| EU Reg. 376 | Occurrence | 72 hours |
| TSA | 49 CFR § 1544 SD breach | Per SD |
| CAAC | CCAR-395 / 民用航空器事件调查规定 | Per rule |
| EPA / FAA noise | 14 CFR Parts 34, 36; CORSIA | Annual / triggered |

## Data treatment (aviation-specific)

| Data type | Special treatment |
|---|---|
| FOQA / FDA | FAA Order 8000.82; 14 CFR § 13.401 — protection from enforcement use |
| ASAP | FAA AC 120-66B confidentiality; tri-party MOU (FAA + operator + employees/union) |
| SMS / occurrence reports | EU Reg. 376/2014 just-culture |
| PNR | EU PNR Directive 2016/681; US APIS / Secure Flight; PRC PIPL cross-border rules |
| CVR / FDR | 49 USC § 1114; ICAO Annex 13 §5.12 — restricted use |
| Crew medical | HIPAA / EASA Part-MED — controlled by AME / regulator |

## Common aviation scenarios

- **New route** — OpSpec country pair (A032/A033/A034), ETOPS/EDTO, MNPS /
  NAT HLA / NOPAC, foreign FAC permit, hostile-environment authorizations,
  insurance war risk, FAR 117 + foreign rest rules, customs / APIS / PNR,
  dangerous goods, diplomatic clearances.
- **Switching MRO** — Part-145 cert scope, BASA/TIP coverage, capability
  list match, export-control screening, MRO liability + product liability
  insurance, audit/qualification per CAMP, lessor consent.
- **Handling FOQA/ASAP** — FAA Order 8000.82 protection, de-identification
  before sharing, ASAP ERC governance, no enforcement use absent
  carveouts.
- **ITAR / EAR** — DDTC and BIS screening for US-origin parts / software /
  technical data; deemed-export risk for foreign-national personnel;
  denied-party screening for counterparties / sub-lessees / MRO vendors /
  financiers.
