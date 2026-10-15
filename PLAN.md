# PLAN — Legal AI Helper architecture

This document captures the working architecture as of the May 2026
restructure, with model IDs and counts refreshed 2026-09-11 and the turn
architecture updated for the one author loop of 2026-10-14
(`docs/plans/2026-10-14-single-agent-loop.md`). Historical aviation-only
roots are preserved as a domain pack rather than as the core identity. For
current runtime behaviour (context management, memory, grounding, cost) see
`README.md`; this file is the architectural intent.

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
   plain function tools to both SDKs; no provider-native MCP path. Only the
   operational tools are loaded on every request; every functional tool is
   deferred behind each provider's native tool search.
4. **One author per answer, specialists only when the work splits.** The
   chat's own model answers every turn in one tool loop; nothing routes the
   turn first. It loads a method (`load_skill`) or a domain pack
   (`load_pack`) when the request needs one. When the work splits into
   issues that each need sustained research, or the user asks for a
   workflow, it calls `run_workflow`: up to 3 parallel specialists whose
   bundles come back to it as the tool result. Depth is one.
5. **Grounded answers.** Three source-of-truth tiers:
   - **RAG** (local bge-m3 + Qdrant) for indexed corpora.
   - **Connectors / MCPs** for live primary sources (eCFR, Federal
     Register, GovInfo, CourtListener, EUR-Lex, flk.npc.gov.cn,
     hosted PKULaw) and the staged local CAAC corpus.
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
   author loop — the chat's model and effort, one tool loop per turn
   (playbook · method list · pack list · tool-search groups · workflow guidance)
     always loaded: read_document · load_skill · read_skill_file · load_pack ·
                    run_workflow · project memory · hosted web search
     tool search:   connectors · legal_source_search · retrieve_legal ·
                    document tools · create_deck → ppt-master · pack sources
                              │
                              ├── run_workflow(objective, tasks ≤ 3)
                              │     SkillAgent × N (parallel, forked context,
                              │     chat digest as cache prefix, depth 1)
                              │     Findings · Issues · Artefacts ·
                              │     Out of scope · Sources
                              │     → bundles + open gaps as the tool result;
                              │       the author integrates them in the loop
                              ▼
             answer contract + answer lint → (optional) citation audit
                              │
                              ▼
          fast tier, after `done`: rolling summary · chat title
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
├── requirements.txt     # source-of-truth deps
├── pyproject.toml       # package metadata + the `legal-helper` script
├── legal_helper/
│   ├── cli.py / __main__.py
│   ├── server.py        # FastAPI; /domains, /jurisdictions, /api/chats/*
│   ├── workflow.py      # the author loop, audit-then-reveal, answer export
│   ├── multi_agent.py   # run_workflow: parallel specialists, bundles as the result
│   ├── tool_exposure.py # always-loaded tools vs. those behind tool search
│   ├── turn_context.py  # per-turn state the author's tools read
│   ├── authoring.py     # the author's system prompt
│   ├── answer_contract.py  # the one output contract (author + specialist)
│   ├── answer_lint.py   # deterministic answer clean-up (stream filter)
│   ├── agent.py         # SkillAgent (pack-aware specialists, external skills)
│   ├── config.py        # Settings with jurisdiction + packs
│   ├── providers/       # Anthropic + OpenAI behind one Protocol
│   ├── connectors/      # in-process search functions (US/EU/PRC/aviation)
│   ├── mcp/             # external MCP boundary + in-tree ccar_aviation
│   ├── citations/       # audit + extract + validate + format + prc
│   ├── rag/             # bge-m3 + Qdrant + hierarchical chunker
│   ├── tools/           # function tools (incl. load_skill, load_pack)
│   ├── skills/          # 15 generic SKILL.md files (<100 lines each)
│   ├── playbook/        # general_playbook.md (PRC-first)
│   ├── domains/         # domain packs (aviation populated; others future)
│   ├── documents/       # read / edit / redline / write / render / lint
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
pack.yaml          # name, jurisdictions, mcp_servers, allowed_connectors, rag_collections, skill_overlays
playbook.md        # pack-specific playbook (loaded after general_playbook.md)
references/        # long-form prose pulled on demand (optional)
overlays/<skill>.md # per-skill deltas
```

Activation: pinned by `active_domain_packs: [aviation]` in `config.yaml`, a
project, or `--domain-pack aviation` on the CLI, which puts the pack's
playbook in the author's prompt from the start. Otherwise the author sees the
pack's name and loads it with `load_pack` when it judges the request falls in
the domain; there is no keyword detection.

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
- Deferred tools are found by each provider's native tool search (OpenAI
  `tool_search` with namespaces, Anthropic `tool_search_tool_bm25_20251119`);
  there is no fallback path, so a new model must pass the same probe.
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
