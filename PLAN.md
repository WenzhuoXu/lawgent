# PLAN — Legal AI Helper architecture

This document captures the working architecture as of the May 2026
restructure, with model IDs and counts refreshed 2026-09-11. Historical
aviation-only roots are preserved as a domain pack rather than as the core
identity. For current runtime behaviour (context management, memory,
grounding, cost) see `README.md`; this file is the architectural intent.

## Goal

Maintain a standalone Python agent that gives a **legal practitioner** a
usable interface to:

- Review contracts (commercial / M&A / services / employment / etc.)
- Triage NDAs against a configurable playbook.
- Run compliance checks under PRC-first defaults (US / EU comparative).
- Brief on legal topics, daily activity, or incidents.
- Score risk via a severity × likelihood matrix.
- Prepare meeting / negotiation packages.
- Structure responses to regulators and counterparties.
- Assemble closing / signature packages.
- Run third-party diligence.
- Extract and validate citations (eyecite + PRC regex).

## Principles

1. **Generic core, domain packs on top.** The 15 practice skills are generic. The
   `legal_helper/domains/<pack>/` folder layers per-skill overlays plus a
   pack-specific playbook. Aviation is the first populated pack; future
   packs (healthcare, fintech, employment) drop in without touching the
   core.
2. **PRC primary, comparative secondary.** PRC legislation (宪法 → 法律 →
   行政法规 → 地方性法规 / 规章) and judicial authority (司法解释; cases with a
   weight label: 指导性案例 应当参照, 入库案例 参考, others persuasive) are the
   default, per playbook §0. US / EU appear when relevant; never as default.
3. **Provider parity.** Anthropic Claude (`claude-opus-5-5`) and OpenAI
   GPT (`gpt-6.1-sol`) are first-class peers behind one `Provider` protocol.
   All tools — connectors, MCP-discovered, RAG, citations — surface as
   plain function tools to both SDKs; no provider-native MCP path.
4. **One author per answer, specialists only when the work splits.** A
   router plans each turn; simple and standard turns are answered by one
   tool-armed author holding the playbook and the routed skill's method;
   complex turns are decomposed into issues and 2-3 parallel specialists whose
   bundles one integrator turns into the answer.
5. **Grounded answers.** Three source-of-truth tiers:
   - **RAG** (local bge-m3 + Qdrant) for indexed corpora.
   - **Connectors / MCPs** for live primary sources (eCFR, Federal
     Register, GovInfo, CourtListener, EUR-Lex, flk.npc.gov.cn,
     caac.gov.cn, hosted PKULaw).
   - **Hosted web_search / web_fetch** as the last-resort fallback.
6. **Citations are first-class.** `legal_helper/citations` extracts +
   validates US (eyecite + reporters-db) and PRC (regex for 案号, 法释,
   国函, 国办发, 部令, statute pinpoints). The legacy
   `audit_citations` is preserved.

## Architecture

```
                 user request (+ chat digest, files, generated deliverables)
                              │
                              ▼
         router (fast tier) ── newest-first view of the chat
         mode · complexity · one task · must-cover points · fact conflicts
                              │
          ┌───────────────────┴────────────────────┐
     simple / standard                          complex
          │                                        │
          ▼                                        ▼
   single-pass author                     decomposer (fast tier)
   (playbook + routed skill method,       issues + 2-3 specialists
    research + document tools,                     │
    create_deck → ppt-master)                      ▼
          │                            SkillAgent × N (parallel, forked
          │                            context, chat digest as cache prefix)
          │                            Findings · Issues · Artefacts ·
          │                            Out of scope · Sources
          │                                        │
          │                                        ▼
          │                              integrator (author prompt,
          │                              conflict rule, open gaps)
          └───────────────────┬────────────────────┘
                              ▼
             answer contract + answer lint → (optional) citation audit
```

## Repository layout

```
legal_helper/
├── README.md            # user-facing intro
├── PLAN.md              # this file
├── AGENTS.md            # operator notes
├── CLAUDE.md            # Claude-Code project guidance
├── .mcp.json            # external MCP registry
├── config.yaml          # provider + jurisdiction + packs + citation style
├── requirements.txt     # source-of-truth deps (no pyproject.toml today)
├── legal_helper/
│   ├── cli.py / __main__.py
│   ├── server.py        # FastAPI; /domains, /jurisdictions, /chat/*
│   ├── workflow.py      # router, planner, single-pass author, specialists, integrator
│   ├── authoring.py     # the answer author's system prompt
│   ├── answer_contract.py  # the one output contract (author + specialist)
│   ├── answer_lint.py   # deterministic answer clean-up (stream filter)
│   ├── agent.py         # SkillAgent (pack-aware specialists, external skills)
│   ├── config.py        # Settings with jurisdiction + packs
│   ├── providers/       # Anthropic + OpenAI behind one Protocol
│   ├── connectors/      # in-process search functions (US/EU/PRC/aviation)
│   ├── mcp/             # external MCP boundary + in-tree ccar_aviation
│   ├── citations/       # audit + extract + validate + format + prc
│   ├── rag/             # bge-m3 + Qdrant + hierarchical chunker
│   ├── tools/           # function-tool registrations
│   ├── skills/          # 15 generic SKILL.md files (<100 lines each)
│   ├── playbook/        # general_playbook.md (PRC-first)
│   ├── domains/         # domain packs (aviation populated; others future)
│   ├── documents/       # read / edit / redline / write / render / lint
│   ├── webui/           # static fallback UI
│   └── webui_dist/      # vite build output
├── src/                 # React + Vite source
├── examples/
│   ├── general/         # generic samples (NDA, employment, compliance inquiry)
│   └── aviation/        # aviation samples (lease, MRO NDA, AD situation)
└── tests/               # unit + contract tests; 2 opt-in live-API smoke tests
```

## Domain packs

A pack is a directory under `legal_helper/domains/<name>/` with:

```
pack.yaml          # metadata (name, jurisdictions, mcp_servers, skill_overlays)
playbook.md        # pack-specific playbook (loaded after general_playbook.md)
references/        # long-form prose pulled on demand (optional)
overlays/<skill>.md # per-skill deltas
```

Activation: `active_domain_packs: [aviation]` in `config.yaml`, or
`--domain-pack aviation` on the CLI.

Currently populated: `aviation`. Empty / future: healthcare, fintech,
employment, IP, etc.

## Provider parity contract

Documented in `CLAUDE.md`. Summary:

- MCP servers are runtime infrastructure, not provider features.
- Every MCP tool is reflected as a function tool with identical JSON
  Schema for both providers.
- No provider-native MCP path (`mcp_servers` parameter / Responses-API
  `mcp` field). The runtime is *structured* for a future native fast
  path but does not use it.
- Hosted `web_search` / `web_fetch` remain at parity.
- RAG is fully local (bge-m3 + Qdrant); no provider API in the retrieval
  hot path.
- `tests/test_provider_parity.py`, `test_mcp_adapter.py`, and
  `test_domain_pack.py` enforce this.

## History (origin)

This project began life as an aviation-law-specific agent
(`Aviation Legal Helper`), patterned after Anthropic's published `legal`
plugin. The May 2026 restructure generalised the core to legal AI broadly
while preserving the aviation IP in a single opt-in domain pack. See
`domains/aviation/playbook.md` for the carried-forward aviation defaults
(insurance, Cape Town / IDERA, Section 1110, AD / SB allocation, return
conditions, MRO diligence, etc.).
