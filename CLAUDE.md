# CLAUDE.md — project guidance for Claude Code

Project-level rules. Loaded into every Claude Code session in this repo.

## Identity & scope

`legal_helper` is a **Legal AI Helper**. Default jurisdictional focus is **PRC (China) law**; US and EU are supported as comparative jurisdictions. Aviation is **one of several domain packs** (currently the only populated one); never treat aviation as the whole product.

When the user does not specify a domain pack, behave as a general legal assistant. Surface aviation-only content only when `active_domain_packs` includes `aviation` or the user explicitly invokes the pack.

## Environment

Always run repo commands inside the `llm` conda env:

```
conda activate llm
```

Both Python and Node toolchains live in this env. External MCP servers and `pip` / `npx` installs must target the same env:

```
conda run -n llm pip install -r requirements.txt
conda run -n llm npx -y eurlex-mcp-server   # node MCPs
```

Flowchart rendering depends on `@mermaid-js/mermaid-cli` (the `mmdc`
binary) — installed locally under `node_modules/.bin/mmdc`. If it goes
missing rerun:

```
conda run -n llm npm i @mermaid-js/mermaid-cli mcp-mermaid --save-dev --ignore-scripts
```

`mmdc` needs Chrome; we auto-detect `/usr/bin/google-chrome`. The
`render_flowchart_image` tool and the `Slide.flowchart` block in
`write_pptx` both depend on this binary.

US sources (CourtListener, eCFR, Federal Register, GovInfo) are served by **direct-API connectors** in `legal_helper/connectors/`, not MCP servers — no install needed beyond `requirements.txt`.

The PKULaw MCP is **remote HTTP**, no install needed — set `PKULAW_API_TOKEN` in `legal_helper/api_key` (or `.env`).

**stdio MCP commands stay bare in `.mcp.json`.** `registry.resolve_command()`
resolves them at launch: `python` / `python3` → `sys.executable` (so an in-tree
server always runs under the same interpreter, and therefore the same deps, as
the harness), everything else → `PATH`, then the conda bin dirs. Do **not**
hardcode absolute paths in `.mcp.json` — that was the previous bug in reverse.
`LEGAL_HELPER_MCP_ENV` (default `llm`) names the env to search; an
unresolvable command produces a named error from `unresolvable_stdio_specs()`
instead of a spawned-and-dead child reporting `CONNECTION_CLOSED`.

## Repo anatomy

- `legal_helper/skills/<name>/` — generic, domain-agnostic skills. Each is `SKILL.md` (<100 lines) + `references/` (deep prose pulled on demand) + `resources/` (output templates) + `scripts/` (deterministic helpers).
- `legal_helper/domains/<pack>/` — domain packs. Each is `pack.yaml` + `playbook.md` + `references/` + `overlays/<skill>.md` (per-skill aviation deltas).
- `legal_helper/playbook/general_playbook.md` — PRC-first global defaults.
- `legal_helper/connectors/` — in-process tool functions over httpx / WebFetch. The primary call path for both providers.
- `legal_helper/mcp/` — external MCP boundary. Only used for MCPs we don't maintain in-tree (CourtListener, GovInfo, EDGAR, EUR-Lex, PKULaw) and the one in-tree MCP we ship (`ccar_aviation`).
- `legal_helper/citations/` — `audit_citations` (legacy, preserved), eyecite-backed `extract` / `validate`, PRC regex, format conversion.
- `legal_helper/rag/` — local bge-m3 embeddings + Qdrant (embedded) + legal-aware hierarchical chunker.

## Authoring rules

- **SKILL.md ≤ 100 lines.** Use pointer paragraphs (`Read references/foo.md for ...`) instead of inlining deep content.
- **Frontmatter required**: `name`, `description`, `argument-hint` (optional), `allowed_connectors` (optional), `rag_collections` (optional).
- **No aviation strings outside `domains/aviation/`.** If you find yourself writing FAA/EASA/ICAO/IDERA/Cape Town/MRO/AD/SB inside `skills/` or `playbook/`, stop and put it in `domains/aviation/overlays/<skill>.md` instead.
- **PRC primary, comparative second.** Generic skills should reference 法律 → 行政法规 → 部门规章 → 规范性文件 → 司法解释 → 指导案例 first; US/EU when relevant.

## Excel / workbook operations

Excel work is a general document-tool capability, not a legal specialist
skill. Route ordinary spreadsheet edits through the `general-answer` path with
document tools.

For existing `.xlsx` files:

1. Use `inspect_xlsx` to identify sheets/dimensions.
2. Use `inspect_xlsx_range` for the actual range to be edited. Do not use
   markdown-export row numbers as edit coordinates; blank rows and merged
   headers can shift them.
3. Use `edit_xlsx_cells_checked` for guarded scalar edits with
   `check_expected=true` where possible and `protected_ranges` for columns
   that must not change.
4. Use `diff_xlsx` after editing to prove intended cells changed and protected
   ranges have `diff_count=0`.
5. Re-inspect the output before reporting success.

Use `write_xlsx` for new workbooks. Preserve formulas, styles, merged cells,
sheet names, file/manual names, and protected columns unless the user
explicitly asks for restructuring.

## Cost center / usage ledger

- Every provider turn (both providers, run/tool_runner **and** stream paths)
  appends normalized token usage to `state/usage/usage-YYYY-MM.jsonl` via
  `legal_helper/usage.py` (`record_usage`). Month-keyed files make the
  summary reset each calendar month while keeping history.
- Billing semantics differ per provider and `usage.py` prices accordingly:
  Anthropic cache read/write are separate buckets; OpenAI cached tokens are a
  discounted subset of `input_tokens`.
- **OpenAI long-context surcharge**: a request whose `input_tokens` exceeds
  `OPENAI_LONG_CONTEXT_THRESHOLD` (272K) bills at 2x input and 1.5x output for
  the whole request. This is modelled (`is_long_context`), and
  `month_summary` reports `long_context_requests` per model and in totals.
  Leaving it out under-reported 2026-08 by 21% and 2026-09 by 31% — if you
  touch the pricing math, keep this term.
- `GET /api/usage` serves the month summary (`?month=YYYY-MM` for history);
  the webui settings bar renders it (`CostCenter` in `src/main.jsx`).
- Pricing table: `DEFAULT_PRICING` in `usage.py`, **verified 2026-09-11**
  against the official Anthropic and OpenAI pricing pages; override with the
  `LEGAL_HELPER_PRICING_JSON` env var (same shape) when list prices change.
  Re-verify at each monthly review — the whole gpt-5.6 family moved between
  July and September and the stale table was wrong in both directions.
- `scripts/monthly_harness_review.md` + `.sh` — recurring monthly harness
  review (external sweep + repo audit + cost-center check), designed for a
  crontab entry `23 9 1 * *` running the shell script.

## Context & memory management

- **Compaction is tier-aware, not a constant.** `context.compaction_threshold_for`
  returns 0.75 for frontier models and 0.55 for the fast tier
  (`is_fast_tier`: haiku / luna / mini / nano). Rationale: strong agents do
  better carrying the transcript further and isolating work in sub-agents,
  weak ones do better summarizing early. `chat_compaction_threshold: 0.0` in
  `config.yaml` means "derive"; any positive value pins it for every model.
- **Model windows live in `context._WINDOWS`.** A model missing from that dict
  silently gets `_DEFAULT_WINDOW` (200K) — which is how a 1M-window model ends
  up compacting at ~120K. **Add every new model ID here, to
  `usage.DEFAULT_PRICING`, and to the `*_HIGH_EFFORT_MODELS` tuple in
  `config.py` at the same time.**
- **Rolling summaries roll forward by delta, never by rewrite.** Both
  `memory.summarize_with_fast_model` (per chat) and `ProjectStore.compact`
  (cross-chat) ask the fast model only for `+ Section | …` / `- Section | …`
  lines and merge them deterministically via `memory.apply_delta` over the
  sections in `memory.SUMMARY_SECTIONS`. Re-summarizing a summary erodes
  detail every pass (context collapse); do not reintroduce a "rewrite this
  summary" prompt. An unparseable delta must leave the prior playbook intact.
- **Attachments: inject one document, navigate a corpus.** A single text
  attachment is inlined. Past `LEGAL_HELPER_INLINE_CORPUS_BYTES` (400 KB of
  extracted text, `0` disables) each text attachment degrades to an outline
  built from `rag.chunker._ARTICLE_RES` plus the `read_document` call that
  opens it. Images and provider-side PDFs are unaffected.
- **Anthropic server-side context management is opt-in.**
  `LEGAL_HELPER_ANTHROPIC_CLEAR_TOOLS` enables `clear_tool_uses_20250919`.
  It is off by default because it invalidates the cached prompt prefix every
  time it fires, and this provider relies on `cache_control` breakpoints for
  the system block and tool list. Do not default it on without measuring.

## Workflow failure taxonomy

`legal_helper/citations/trajectory.py` gives every workflow failure event a
subclass across two layers — `substantive` (the legal content is wrong) and
`procedural` (the agent misbehaved). `log_workflow_event` attaches the tag
automatically, so **add new failure events to `EVENT_SUBCLASSES` rather than
tagging at the call site**; progress events (`*_started`, `*_finished`)
deliberately have no entry. `profile()` aggregates a run and reports the
layers separately — a run with procedural failures and zero substantive ones
is the right-answer-wrong-reason case, and summing them would hide it.

## Provider parity contract

- Both `claude-opus-5` (primary; `claude-opus-4-8` / `claude-opus-4-7` still supported) and `gpt-5.6-terra` (primary; `gpt-5.6-sol` still supported) paths must work end-to-end on every change.
- Fast/lightweight tier: `claude-haiku-4-5` and `gpt-5.6-luna`. Note: the bare `gpt-5.6` alias routes to **Sol**, not Terra — always use the full `gpt-5.6-terra` ID.
- `gpt-5.5` is **retired from the high-effort list** (2026-09-11): at verified list prices it is $5.00/$30.00 against Terra's $2.00/$12.00 for the same work, and it was 5.7% of September requests but 55% of real spend. It stays in `usage.DEFAULT_PRICING` so historical ledger months still replay — do not put it back in `OPENAI_HIGH_EFFORT_MODELS`.
- MCP tools are surfaced as **plain function tools** to both SDKs; never use Anthropic's `mcp_servers` parameter or OpenAI's Responses-API `mcp` field as the only path.
- Hosted `web_search` / `web_fetch` already at parity; do not touch.
- RAG is local (bge-m3 + Qdrant); no provider API in the retrieval hot path.

## Citation rule

Every legal answer ends with a `## Sources` (or `## 资料来源`) section. Each entry must include a pinpoint marker (条款, article, section, §, paragraph, page). The `audit_citations` check enforces this; do not bypass it. When a pinpoint genuinely cannot be obtained, write `pinpoint unavailable` / `无法获得具体条款` explicitly.

## PRC citation lookup — PKULaw MCP is the primary source

PKULaw exposes nine MCP sub-services (all share `PKULAW_API_TOKEN` via the
WSO2 `apikey` header). Use them as the **first** stop for PRC legal lookups;
`flk_npc_search` is the free public fallback (search/status only — it does
not return statute body text).

| Need | PKULaw sub-service → tool | Fallback |
|---|---|---|
| Statute by title + 条号 | `pkulaw_fatiao` → `get_law_item_content` | none (flk_npc has no body fetch) |
| Statute semantic / keyword | `pkulaw_law_search` → `search_article` / `get_article` | `flk_npc_search` |
| 司法案例 (semantic) | `pkulaw_case_search` → `search_case` | none public |
| 司法案例 (keyword list) | `pkulaw_case_list` → `get_case_list` | none public |
| 案号 extract + normalize | `pkulaw_anhao` → `anhao_recognition` | `citations/prc.py` regex |
| 法条 extract + traceback in a draft | `pkulaw_law_recognition` → `law_recognition` | `citations/prc.py` regex |
| Citation chain anti-hallucination | `pkulaw_citation_validator` → `adjust_provisions` | manual eyeball |
| Hyperlink finished prose | `pkulaw_doc_link` → `get_linked_content` | manual |
| Broad NL legal Q (statutes + cases + 检察文书 + 律所文章 + papers) | `pkulaw_nl_search` → `ai_pkulaw_search` | `flk_npc_search` + hosted web |

`legal_helper/citations/prc.py` handles 案号, 法释, 国函, 国办发, 部令, and
statute pinpoints by regex (still useful for fast, offline extraction).
There is no PRC analog to `eyecite`; this regex extractor remains the working
state of the art.

## Language rule

User-facing input/output language follows the user's language. **Internal reasoning, sub-agent tool calls, and orchestrator-to-skill messages may use whichever language best fits the material** — pick freely as the work demands (e.g., 中文 when reasoning over PRC statutes and 案例, English for US/EU sources); do not force mid-pipeline translation. Quote primary sources in their original language; add a translation when the user-facing language differs.

## Common commands

```
conda activate llm
python -m legal_helper run --task review-contract --input examples/general/sample_commercial_nda.md
python -m legal_helper run --domain-pack aviation --task review-contract --input examples/aviation/sample_aircraft_lease.md
python -m legal_helper chat
python -m legal_helper serve --port 8010
python -m legal_helper.rag.ingest --collection aviation --source caac_ccar
pytest -q
pytest -q tests/test_pptx_flowchart.py   # native flowchart shape contract
```

## Testing

- `pytest -q tests/test_skill_anatomy.py` — anatomy contract: each SKILL.md <100 lines, frontmatter valid, referenced files exist.
- `pytest -q tests/test_provider_parity.py` — Anthropic and OpenAI see identical tool surface and equivalent results.
- Live smoke once per provider per major change: `MODEL_PROVIDER=anthropic` then `MODEL_PROVIDER=openai`.
