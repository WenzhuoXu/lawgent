# PLAN — Legal AI Helper architecture

This document captures the working architecture as of the May 2026
restructure. Historical aviation-only roots are preserved as a domain
pack rather than as the core identity.

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

1. **Generic core, domain packs on top.** The 10 skills are generic. The
   `legal_helper/domains/<pack>/` folder layers per-skill overlays plus a
   pack-specific playbook. Aviation is the first populated pack; future
   packs (healthcare, fintech, employment) drop in without touching the
   core.
2. **PRC primary, comparative secondary.** PRC source hierarchy
   (法律 → 行政法规 → 部门规章 → 规范性文件 → 司法解释 → 指导案例) is
   the default. US / EU appear when relevant; never as default.
3. **Provider parity.** Anthropic Claude (`claude-opus-4-7`) and OpenAI
   GPT (`gpt-5.5`) are first-class peers behind one `Provider` protocol.
   All tools — connectors, MCP-discovered, RAG, citations — surface as
   plain function tools to both SDKs; no provider-native MCP path.
4. **Sub-agent runtime.** Orchestrator fans out via `run_skill`; each
   sub-agent gets a forked context with only its compact manifest.
   Progressive-disclosure resource tools (`list_skill_sections`,
   `read_skill_section`, `read_playbook_section`) pull deep content on
   demand.
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
                user request
                     │
                     ▼
    Orchestrator agent ── pack-aware system prompt
                     │
   run_skill(name, task)  +  write_docx / write_pdf / read_document
                     ▼
        SkillAgent (forked context)
        ├── compact manifest from SKILL.md frontmatter
        ├── jurisdiction + active-pack notice in the system prompt
        ├── pointers to load on demand:
        │       /playbook/general_playbook.md
        │       /domains/<pack>/playbook.md     (when pack is active)
        │       /domains/<pack>/overlays/<this-skill>.md
        ├── jurisdiction- and pack-filtered tools:
        │       connectors/{us_federal,us_courts,eu,prc,aviation}
        │       MCP-discovered tools (function-tool adapter)
        │       retrieve_legal (RAG)
        │       extract_citations_tool / validate_citations_tool
        │       read_document / write_docx / write_pdf / extract_clauses
        ▼
       Findings + Sources block
                     │
                     ▼
         Orchestrator authors user-facing answer
            (resolves dedup, picks one organising axis,
             builds the 资料来源与核验 table)
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
│   ├── agent.py         # OrchestratorAgent + SkillAgent (pack-aware)
│   ├── workflow.py      # planner-driven chat executor
│   ├── config.py        # Settings with jurisdiction + packs
│   ├── providers/       # Anthropic + OpenAI behind one Protocol
│   ├── connectors/      # in-process search functions (US/EU/PRC/aviation)
│   ├── mcp/             # external MCP boundary + in-tree ccar_aviation
│   ├── citations/       # audit + extract + validate + format + prc
│   ├── rag/             # bge-m3 + Qdrant + hierarchical chunker
│   ├── tools/           # function-tool registrations
│   ├── skills/          # 10 generic SKILL.md files (<100 lines each)
│   ├── playbook/        # general_playbook.md (PRC-first)
│   ├── domains/         # domain packs (aviation populated; others future)
│   ├── document_writers/
│   ├── webui/           # static fallback UI
│   └── webui_dist/      # vite build output
├── src/                 # React + Vite source
├── examples/
│   ├── general/         # generic samples (NDA, employment, compliance inquiry)
│   └── aviation/        # aviation samples (lease, MRO NDA, AD situation)
└── tests/               # 136 unit tests + 2 live-API smoke tests
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
