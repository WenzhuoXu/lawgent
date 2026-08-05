# Aviation Playbook — Domain Defaults

Loaded **after** `playbook/general_playbook.md` when the `aviation` domain pack
is active. Generic legal policy lives in the general playbook; this file
carries aviation-specific standard positions, acceptable ranges, and
escalation triggers.

> **Not legal advice.** Defaults below are commercially common but must be
> validated against the organization's actual risk appetite, fleet, route
> network, certificates, and counterparty mix.

## 0a. Regulatory sources by jurisdiction (connector cheatsheet)

Skills that have the aviation pack active see the following connectors
in their tool surface (gated by jurisdiction). The dispatcher
``aviation_source_search`` wraps them with a single ``source``
discriminator.

| Jurisdiction | Connector | What it returns | Primary use |
|---|---|---|---|
| US | `faa_title14_search` | 14 CFR sections via eCFR | Airline operating rules (Part 121), passenger rights (Part 250, 259, 382), drone (Part 107), etc. |
| US | `drs_search` / `drs_fetch` | FAA DRS — ADs, ACs, policy, SAFOs (Federal Register-backed) | Airworthiness Directive lookup, AC pinpoints. Search returns FR document + DRS deep-link. |
| US | `federal_register_search` | All FAA rulemaking notices | NPRMs, final rules, enforcement orders. |
| US | `govinfo_search` | US Code, historic CFR, Public Laws | Statutory backstop (49 USC §§ 40101-46507, §§ 80101-80504). |
| US | `courtlistener_search` | US case law | Aviation cases, MC99/Warsaw preemption, Cape Town § 1110 priority. |
| EU | `easa_ad_search` / `easa_ad_fetch` | EASA SP Tool — AD/EAD/SIB/SD | EU airworthiness directives. |
| EU | `easa_ear_index` | Curated index of Easy Access Rules XML | Part-CAT, Part-M/CAMO/145, Part-FCL, Part-21, SERA. |
| EU | `eurlex_search` | EU regulations / directives / CJEU | Reg (EC) 261/2004 (denied boarding), Reg 1107/2006 (PRM), 2027/97 (MC99), Reg 376/2014 (occurrence reporting). |
| ET | `aviation_source_search(source="ecaa_index")` | Curated Ethiopian civil-aviation legal-framework index | First stop for the Addis Ababa route: regulator, Civil Aviation Proclamation 616/2008 + 1179/2020, ECAA re-establishment 273/2002, ECAR, ICAO/Cape Town/BASA layer. |
| ET | `aviation_source_search(source="ethiopia_law_fetch", identifier="616/2008")` | Federal Negarit Gazeta proclamation body (typed transcript or bilingual PDF scan) | Civil Aviation Proclamation 616/2008, amendment 1179/2020, ECAA 273/2002, or any proclamation by number. |
| ET | `aviation_source_search(source="ecaa_fetch", identifier="sectoral/regulation")` | Readable text + document links from `ecaa.gov.et` | ECAA directorate / ECAR / directive pages; follow document_links via `fetch_url_to_artifact`. See `references/ethiopia.md`. |
| PRC | `aviation_source_search(source="caac_local" / "caac_local_fetch" / "caac_hybrid")` | Local CAAC 法律法规 + 民航规章 / CCAR corpus staged from caac.gov.cn | First stop for CAAC docs: exact title, CCAR part, validity, article, source URL. Use `caac_hybrid` when semantic expansion is useful. |
| PRC | PKULaw MCP sub-services | CCAR statutes, 司法案例, 案号 normalisation | Primary for PRC statutes; flk_npc as fallback. |
| All | `retrieve_legal` against ``aviation_treaties`` / ``caac_ccar`` RAG | Treaties; CAAC/CCAR semantic retrieval | Treaty pinpoints and broad CAAC issue search. |
| All | `retrieve_legal` against ``icao_doc`` RAG | ICAO Annexes 1–19, Doc 9082/9284/9303/9957 | SARPs and implementation guidance. |

For deferred sources (ICAO Data Services USOAP, FAA Registry CSV,
Transport Canada CARs, DOT Aviation Consumer, ANAC/CASA/JCAB), see the
"Deferred" section of the implementation plan.

## 0b. Treaty corpus (aviation_treaties RAG collection)

The aviation legal system has a layered structure: **treaty → ICAO
Annex/SARP → national implementing law → operator manual → contract**.
The treaty layer is rarely accessed via a live API; we ingest the
authoritative texts once into the ``aviation_treaties`` RAG collection
and access them via ``retrieve_legal``. Cite formats are documented in
``overlays/cite-check.md``.

Core treaties seeded:

- **Chicago Convention 1944** (Doc 7300) — constitutes ICAO; sovereignty;
  validation of certificates (art. 33); aircraft nationality (art. 17–21).
- **Tokyo Convention 1963** — captain authority; in-flight offences
  jurisdiction. Cited heavily in any unruly-passenger incident.
- **Hague Convention 1970** — unlawful seizure of aircraft.
- **Montreal Convention 1971** + **1988 Airport-Violence Protocol** —
  unlawful acts against safety of civil aviation.
- **Beijing Convention 2010 + Beijing Protocol 2010** — modernised
  unlawful-acts regime; aircraft as weapon, BCN/CBR threats.
- **Montreal Convention 1999 (MC99)** — passenger / baggage / cargo
  liability. Implemented in EU via Reg (EC) 2027/97 (as amended by
  889/2002); in the US via 49 USC § 40105 + § 41310. Default global
  forum-choice + damage-cap framework.
- **Warsaw System** (Warsaw 1929 + Hague 1955 + Guadalajara 1961 +
  Montreal Protocols 1, 2, 3, 4) — pre-MC99 liability regime. Still
  applicable to flights between a Warsaw-only state and any other state.
- **Cape Town Convention 2001 + Aircraft Equipment Protocol** — secured
  international interests in aircraft objects; IDERA; Article 13 advance
  remedies. Maintain a working table of state declarations (esp.
  Article 13 advance-remedies opt-in).
- **Rome Convention 1952** + **Montreal Convention 2009 (General Risks
  + Unlawful Interference)** — third-party surface-damage liability.
  Relevance varies; only Reg (EC) 785/2004 minima are universally
  binding in the EU. Most states have not ratified MC2009.

## 0d. Research order (treaty → SARP → national → operator)

For any aviation legal question, work the authority hierarchy in order
and call tools in this sequence. **Do not skip to a live connector
without first checking the RAG corpus** — the treaty layer is canon and
the connectors are noisier.

1. **`retrieve_legal(collection="aviation_treaties", filters_json='{"language": "<en|zh>"}')`**
   — first stop for any question that could touch carrier/passenger
   liability, captain authority, unlawful acts, hijacking, sabotage,
   leasing security interests, or jurisdiction over in-flight conduct.
2. **`retrieve_legal(collection="icao_doc", ...)`** — for SARPs /
   facilitation / charges / MRTD questions; supplements step 1 when the
   answer requires implementation-level detail.
3. **National implementing law via connectors**:
   - PRC: `aviation_source_search(source="caac_local")` for exact CAAC /
     CCAR lookup; `source="caac_local_fetch"` for one document/article;
     `source="caac_hybrid"` when the exact local hit should be supplemented
     with `caac_ccar` RAG. Skip results whose `flags` contain
     `explicit_expired` or `superseded` unless the question is about
     historical/superseded law. When the primary match is `pdf_stub` and
     no full-text alternate is offered, fall through to
     `pkulaw_fatiao__get_law_item_content` or fetch the source-page PDF —
     do not cite a CCAR article whose body you have not seen. PKULaw
     remains primary for non-aviation PRC statutes and cases; flk_npc is
     the public fallback.
   - US: `faa_title14_search`, `drs_search` (ADs/ACs), `federal_register_search`.
   - EU: `easa_ear_index` + `easa_ad_search` + `eurlex_search` for EU regs.
4. **Operator-manual / contract layer**: only via `read_document` on
   user-supplied attachments — never invent a manual provision.
5. **Web search** as last resort, scoped to authoritative domains
   (`*.icao.int`, `*.faa.gov`, `*.easa.europa.eu`, `caac.gov.cn`).

Cite every step. If steps 1–2 return nothing relevant, say so explicitly
before falling through to step 3 — that absence is itself a finding.

## 0c. ICAO Annexes & Docs (icao_doc RAG collection)

ICAO SARPs are non-self-executing — they bind contracting states, who
implement them through national law. They are still the **interpretive
canon** when a national rule is ambiguous. Cite from this RAG when:
(i) reasoning about a state that has not yet fully implemented the
relevant SARP, (ii) drafting an operational requirement that should be
ICAO-aligned, or (iii) explaining the upstream rationale for an
implementing rule.

Annexes most relevant to a commercial airline:

- **Annex 1** — Personnel Licensing.
- **Annex 2** — Rules of the Air (EU implementation: SERA).
- **Annex 6 Part I** — Operation of Aircraft, Commercial Air Transport
  — Aeroplanes. The most-cited Annex for a CAT operator.
- **Annex 8** — Airworthiness of Aircraft.
- **Annex 9** — Facilitation. Passenger/cargo border procedures, API/PNR,
  PRM accessibility. Acutely relevant to passenger-rights work.
- **Annex 13** — Aircraft Accident and Incident Investigation.
- **Annex 14** — Aerodromes.
- **Annex 17** — Aviation Security.
- **Annex 18** — Safe Transport of Dangerous Goods by Air (paired with
  Doc 9284 Technical Instructions).
- **Annex 19** — Safety Management (SMS framework).

Docs to seed: **9082** (charges policy), **9284** (DG TI), **9303**
(MRTD), **9957** (Facilitation Manual), **9734** (Safety Oversight
Manual).

## 0. Aviation-specific enquiry, research, and citation overlay

The generic enquiry and citation standard in `playbook/general_playbook.md`
applies. The aviation-specific additions are:

- **Authority hierarchy** for aviation legal analysis: international convention
  / ICAO Annex → national implementing law (e.g. CCAR for PRC, 14 CFR for US,
  EASA Implementing Rules + AMC/GM for EU) → operator manuals (OM-A/B/C/D,
  MEL, FCOM, MCM, CAME) → carrier conditions of carriage → airport / border
  procedures.
- **Aircraft context** every enquiry must restate: aircraft location,
  nationality/registry, state of operator, flight route (domestic / international),
  phase (in-flight / on-ground), AOC / AOC-equivalent holder.
- **Reporting channels** to consider as applicable: pilot-in-command / OCC,
  destination + departure civil aviation authorities, state of registry, state
  of operator, airport authority, border / immigration, police, TSA / 公安 /
  security, accident-investigation authority (NTSB / AAIB / BEA / BFU / 民航局
  事故调查), insurer, lessor.
- **Onboard passenger incident bundles** must surface bullets covering: factual
  trigger; crew/captain immediate-action authority (de-escalate, notify PIC,
  preserve evidence, record names/seats/witnesses/timeline); notification
  (OCC, destination authorities, security handoff); preservation (CCTV / crew
  reports / PNR / API / manifest, post-landing handover); limits (no crew
  immigration adjudication, no punishment, limits on physical force / search /
  seizure / restraint / isolation / photography / data sharing absent safety
  + captain authority + local law). Each bullet ends with one of: `treaty`,
  `regulation`, `ICAO`, `local-law`, `best-practice`, `risk`, `drafting`.

## 1. Insurance

### Hull All-Risks
- **Agreed value**: market replacement, not less than 110% of outstanding financing balance
- **Hull war / allied perils**: required for all international ops; war writeback (confiscation, hijacking, sabotage, political violence)
- **AVN52E**: required for lessor / mortgagee protections
- **AVN67B / LSW555D**: standard lessor / contract party endorsement
- **Severability of interest**, **waiver of subrogation** in favor of lessor / mortgagee
- **30-day notice of cancellation** (7 days for war perils)

### Liability
- **Combined single limit (CSL)**: minimum $1B for wide-body international; $500-750M for narrow-body domestic; $300M for regional turbo-prop; $50-150M for business jet
- **EU Reg. 785/2004** minimum SDR-based limits must be met for EU ops
- **Primary and non-contributory**; lessor / financier additional insured
- **No aggregate** (per-occurrence basis)

### Cyber
- Required for any IT vendor / SaaS handling FOQA / ASAP / PNR or crew personal data; minimum $10M; with regulatory-investigation coverage

## 2. Cape Town Convention / IDERA

- IDERA must be in approved Cape Town form, signed by registered owner **and** operator, irrevocable
- DOR / DOR-EQ executed at the time of IDERA
- International Registry filings made; priority searches before and after
- State of registry must have made Article 13 advance-remedies declarations for IDERA to operate
- IDERA reissuance required on any novation or change of operator

## 3. Section 1110 (US Leases / Financings)

- Section 1110 protections must be preserved (no waiver, no language that converts a true lease into a disguised loan that loses § 1110 protection)
- Owner trustee structures preferred for non-US beneficial owners
- US-citizenship test verified for non-trust US registrations

## 4. AD / SB Compliance & Cost Allocation

- **Pre-existing ADs** (issued before delivery): **lessor bears**
- **Post-delivery ADs**: **lessee bears**, subject to threshold (default: **$1M per AD**)
- **Terminating action**: lessee may elect within 12 months; cost split per threshold
- **Mandatory SBs**: lessee bears
- **Alert SBs**: lessee bears unless tied to a pre-delivery latent defect
- **Desirable SBs**: lessee discretion
- All AD compliance tracked in CAMP; audit rights for lessor / mortgagee with 5 business days' notice

## 5. Return Conditions (Operating Lease)

- **Hours / cycles to next major check**: minimum 50% of MPD interval
- **LLP minimum remaining cycles**: greater of (a) 25% of original LLP life, (b) 6,000 cycles, or (c) interval to next major event
- **Engine performance margin**: EGT margin minimum 20°C
- **Records**: continuous from manufacture, dirty fingerprint, last shop visit reports, AD compliance summary, SB compliance summary, structural inspection records, paint & interior survey
- **EOL compensation**: agreed $/hour or $/cycle pro-rated against next event; engine cost-per-hour as defined in lease
- **Acceptance tests**: ground engine run + functional check flight

## 6. Limitation of Liability / Indemnification

### Lease Default
- **Hell-or-high-water** rent obligation: required
- **Lessee indemnity**: broad, including tax, environmental, regulatory, IP, products liability for ops period; survives termination
- **Lessor liability**: limited to gross negligence and willful misconduct; insurance carveouts for additional insureds
- **Cap on lessee general liability**: no cap on safety / regulatory / IP / tax indemnities

### MRO Default
- **MRO indemnity**: workmanship + gross negligence + willful misconduct
- **Customer indemnity**: for customer-furnished parts, customer data, customer ops
- **Limitation of liability**: cap at 2× the value of the work order; uncapped for IP, confidentiality, gross negligence

## 7. Confidentiality / NDAs

- Mutual by default
- Term 2–3 years, survival 5 years (longer for trade secrets / type-certificate data)
- Standard commercial carveouts + **regulator / safety-disclosure carveouts** (CAAC, FAA, EASA, NTSB, AAIB, BEA, BFU, ICAO state authority)
- **No residuals clause** (or narrowly scoped to unaided memory; never applied to trade secrets, type-certificate / STC data)
- **ITAR / EAR acknowledgment** when technical data may be controlled

## 8. Governing Law / Dispute Resolution

- **Preferred**: New York law, NY state and federal courts; English law for international transactions involving European parties
- **Acceptable**: Irish law (very common for aircraft leasing), Singapore law (Asia-Pac transactions), Hong Kong law (HK-based parties)
- **Avoid**: arbitration as primary dispute mechanism for leases; mandatory arbitration in unfavorable jurisdictions
- **Sovereign immunity waiver**: required when contracting with state-owned operators

## 9. Assignment & Novation

- **Permitted assignees**: rated lessors (A- / A3 or better), recognized financial institutions, top-25 aircraft lessors
- **Lessee consent**: not unreasonably withheld
- **Novation procedure**: IDERA reissuance, IR re-filing, insurance certificate amendment, regulator non-objection

## 10. Sub-Lease / Wet-Lease

- Sub-lease permitted with lessor consent (not unreasonably withheld) and approval of operator AOC
- Wet lease / ACMI permitted subject to insurance and ITAR clearances
- Change of state of registry requires lessor consent

## 11. Export Control (ITAR / EAR)

- All transactions involving US-origin parts, software, or technical data require ITAR / EAR posture review
- Deemed-export risk for foreign-national personnel
- Denied-party screening for all counterparties, sub-lessees, MRO vendors, financiers

## 12. Charter / ACMI

- Insurance: ACMI operator carries hull + liability; lessor / customer additional insureds
- Aircraft substitution: permitted subject to equivalent or better type
- Cancellation fees: tier by lead time
- Ground stops / hostile-environment routing: insurance writebacks confirmed

## 13. Vendor Diligence Defaults

- New MRO: Part-145 certificate + capability match + BASA/TIP + insurance + sanctions screening before SOW execution
- New parts supplier: PMA / TC holder verified; traceability process confirmed; ASA-100 / AS9120 quality cert
- IT / SaaS handling aviation data: SOC 2 Type II + ISO 27001 + DPA + safety-data MOU compatibility

## 14. Escalation Triggers

Escalate to senior aviation counsel / head of legal / accountable manager when:
- IDERA cannot be obtained in approved form
- Section 1110 protections waived
- Hull or liability insurance below playbook minimums
- AD / SB threshold uncapped
- Operator AOC change without consent right
- Mandatory disclosure to safety regulator restricted by an NDA
- ITAR / EAR flow-down absent for controlled data
- Any clause that could affect airworthiness, certificate status, or repossession remedies

## 15. Document Production Defaults

- All generated documents include the aviation-not-legal-advice disclaimer
- All redline packages list "Must-have / Should-have / Nice-to-have" tier
- All risk memos include the severity × likelihood matrix and a residual-risk line
- All closing packages include the IR / FAA / CAAC / state-of-registry filings sequence
