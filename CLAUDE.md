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

**Diagram layout is Graphviz, not a browser.** `dot` (≥2.43, `/usr/bin/dot`)
computes every diagram that reaches a slide, resolved through
`documents/graph_layout.find_engine()`. Mermaid survives as an *authoring
syntax* — parsed for node kinds, labels and edges — and as the raster renderer
behind `render_flowchart_image`, so `mmdc` and Chrome are optional and a
missing one degrades one tool instead of every deck.

Optional: `d2` (MPL-2.0, `conda install -n llm d2`) for structure charts with
nested containers, which is the one arrangement Graphviz clusters do not match
for readability. `graph_layout.unresolvable_render_binaries()` names whatever is
missing, and `serve` prints it at startup — a missing render binary is
otherwise silent until a diagram simply never appears.

```
conda run -n llm npm i @mermaid-js/mermaid-cli mcp-mermaid --save-dev --ignore-scripts
```

**Never invoke a Node CLI by its shebang.** `node_modules/.bin/mmdc` is a JS
file with `#!/usr/bin/env node`, so the *launching* process's PATH decides
whether it runs. A server started by an absolute interpreter path never gets
the env's `bin/` on PATH, and every Mermaid render in the running server failed
with exit 127 for that reason. `writers/_node.find_node()` exists for exactly
this; `diagrams.resolve_mermaid_cli()` now applies it, invoking
`node <mermaid-cli/src/cli.js>` with Node's directory prepended to the child's
PATH. A missing interpreter raises `MermaidNotInstalled`, not `RuntimeError`,
because callers fall back on the former and the latter made the degraded path
unreachable.

**`ppt-master` is an external skill, not a package.** It is driven from a
configured skill root — `~/.claude/skills/ppt-master` by default — and is
deliberately **not vendored** (124 MB / 13k files, and its
`attribution_guard.py` fails closed on any modification, so it must stay an
intact official distribution):

```
git clone https://github.com/hugohe3/ppt-master ~/.claude/skills/ppt-master
# optional: its visual-review stage wants a browser
conda run -n llm pip install playwright && conda run -n llm python -m playwright install chromium
```

`LEGAL_HELPER_SKILL_ROOTS` (`os.pathsep`-separated) replaces the external
roots only; this package is always searched first. See **External procedural
skills**.

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
- `legal_helper/documents/` — the deliverable layer. `office.py` (inspect / extract / guarded edit), `redline.py` (w:ins/w:del), `pdf.py`, `graph_layout.py` (Graphviz geometry), `diagrams.py` (Mermaid authoring + raster), `quality.py` (deterministic output checks), `contact_sheet.py`, and `writers/` — one module per output format plus `writers/scripts/` (the Node renderers and `deck.css`).
- `legal_helper/rag/` — local bge-m3 embeddings + Qdrant (embedded) + legal-aware hierarchical chunker.

## Authoring rules

- **SKILL.md ≤ 100 lines.** Use pointer paragraphs (`Read references/foo.md for ...`) instead of inlining deep content.
- **Frontmatter required**: `name`, `description`, `argument-hint` (optional), `allowed_connectors` (optional), `rag_collections` (optional).
- **No aviation strings outside `domains/aviation/`.** If you find yourself writing FAA/EASA/ICAO/IDERA/Cape Town/MRO/AD/SB inside `skills/` or `playbook/`, stop and put it in `domains/aviation/overlays/<skill>.md` instead.
- **PRC primary, comparative second.** Generic skills should reference 法律 → 行政法规 → 部门规章 → 规范性文件 → 司法解释 → 指导案例 first; US/EU when relevant.

## Document, deck and diagram production

**Every deliverable is written, measured, looked at, and repaired before it is
reported as done.** This is the Excel loop below, generalised: inspect →
guarded edit → diff → re-inspect became write → lint → render → repair →
re-lint. The deterministic layer comes first because it costs nothing.

- **`documents/quality.py` is a conformance floor, and only that.** Shapes off
  the canvas, pairwise overlap past 2% of the smaller shape, text that cannot
  fit its box (CJK-aware, reading the frame's real insets rather than assuming
  PowerPoint's defaults), runs under the 11pt body / 9pt label floor, WCAG
  contrast, empty placeholders big in both dimensions. Findings carry `slide`
  and `shape` indices, not prose, so a finding names a location the model can
  go and look at rather than a sentence it has to interpret.
- **A check belongs here only when a pass means "nothing is provably broken".**
  It loses its place the moment a pass could be read as "this is good".
  Coverage, overfill and dead-band were all deleted on that test: measured over
  89 slides of real reference work plus 9 generated ones, `canvas_underfilled`
  ranked the emptiest generated slide (three cards holding ~45 characters, 0.81)
  *above* the reference set's densest statutory slide (ten articles with a
  holding each, 0.66) — because a pale card is ink. Every page
  `canvas_overfilled` ever flagged was correct work. All three could be passed
  by padding, and padding produced the deck that got rejected. **Do not add a
  density metric either**: the best process map in the reference set measures
  13 em, because its labels live inside grouped graphics.
- **Style is not in the code.** Whether a title should assert or name, whether
  load should vary, whether a deck needs an ask — those differ between an
  internal 汇报, a training 宣贯, a seminar talk and a pitch, and none of it is
  derivable from a corpus. Guide the model and let the model decide; an
  earlier attempt encoded one genre's conventions as a schema of roles,
  exhibit floors and profile flags, and it was deleted. When the user supplies
  a reference deck or template, that governs — and when they supply a mature
  external skill, drive it rather than reimplementing its judgement (see
  **External procedural skills**).
- **The advisory prose is gone.** `_visual_result` used to ask the model to
  "check each for text overflowing its box, blank or near-empty pages,
  overlapping elements…". Every clause of that is now arithmetic, and nothing
  could tell whether the instruction had been followed.
- **Rendering an authored file measures it and shows it, in one call.**
  `tools/documents._visual_result` builds a contact sheet, runs
  `quality.lint_artifact` over the pages, and returns both as an image result,
  so the pages arrive as image blocks the model actually sees with the
  findings and a `next_step` beside them. `authored=False` for a file the
  model is *reading* (`render_pdf_pages`) — telling it to repair a source PDF
  it never wrote drove repeat single-page renders.
- **Looking is opt-in; measuring is not.** Nothing forces a `render_*` call,
  but once one happens the lint is attached whether the model asked for it or
  not. There is no enforced write→review→repair loop in the tree: an earlier
  `review.py` / `wireframe.py` pair implemented one and was reverted. Do not
  cite it as if it exists, and if it returns, wire it through
  `max_repair_rounds` so it shares the existing cost dial.
- **HTML is the default deck path.** `write_pptx_from_html` authors in real
  CSS, renders in Chromium, and maps the computed geometry onto native
  PowerPoint objects — text stays text, `<table>` stays a table. Measured
  Reach for `write_pptx` when the enum genuinely fits; reach for HTML whenever
  the arrangement matters. A `<div data-diagram="name">` in the HTML reserves a
  box that the diagram is drawn into as native shapes, which is how a dense CSS
  layout and an editable diagram share one slide.
- **One canvas: 13.333 × 7.5in.** `writers/pptx.py` wrote 10 × 5.625 for its
  fallback while every other path and every `SlideFlowchart` default assumed
  the wide canvas — a `w: 12.2` default does not fit on a 10in slide. Do not
  reintroduce a second canvas size.
- **`write_pptx` returns a bare absolute path on success** and an
  `ERROR:`-prefixed string on failure, including `ERROR: … DEGRADED …` naming
  every dropped feature when the fallback writer ran. Returning JSON there
  would break every caller that chains the result straight into
  `render_pptx_slides` / `inspect_pptx`. `write_pptx_from_html` returns JSON
  and the per-slide geometry it mapped.
- **Diagrams are geometry, not pictures.** `documents/graph_layout.py` asks
  Graphviz for node boxes, routed edge polylines, arrowhead tips and edge-label
  positions, in slide inches, and the emitter turns them into native shapes.
  Four kinds on a slide's `flowchart` block: `flow` (process map, from a Mermaid
  string), `mindmap` (思维导图) and `structure` (结构图, plain connectors
  because a reporting line is not a step). A `swimlane` kind existed and was
  reverted; its dispatch branch and its `kind` / `lane_order` schema entries
  are removed, because a schema that advertises a kind the code cannot build
  is worse than a missing feature — `write_pptx` returned a bare success path
  and a slide with no diagram on it at all. `direction: "auto"` tries both rank directions and keeps
  whichever fills the box. A node's `role` names its lane and drives a stable
  colour; `sub` hangs its deliverable underneath; an edge `label` carries the
  condition or the statutory period. `quality.diagram_encoding` reports which of
  those dimensions a drawing actually uses — reported, never graded.
- **The fit adjusts type, never geometry alone.** Scaling a drawing down
  shrinks its boxes and leaves the text at its original size; scaling it up
  leaves a legible diagram in a corner. `layout_graph` re-measures the boxes at
  a new font size instead, bounded by `MIN_DIAGRAM_FONT_PT` (9) and
  `MAX_DIAGRAM_FONT_PT` (22). The previous path scaled x and y independently,
  which is why fixed-size nodes collided on one axis.
- **Edges are routed polylines with one arrowhead on the last segment.** The
  previous emitter drew a single line between node *centres*, so every
  arrowhead was buried inside the shape it pointed at. A pptxgenjs line is a
  bounding box plus `flipH`/`flipV`, and flipping carries the arrowheads with
  it, so direction survives the flip.
- **Mermaid's inline edge-label form is supported.** `A -- yes --> B` as well as
  `A -->|yes| B`; only the piped form used to match, so every labelled decision
  branch was dropped in silence and process maps rendered with their yes/no
  arms missing.

### Excel / workbook operations

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
- Pricing table: `DEFAULT_PRICING` in `usage.py`, **verified 2026-09-22**
  against the official Anthropic and OpenAI pricing pages; override with the
  `LEGAL_HELPER_PRICING_JSON` env var (same shape) when list prices change.
  Re-verify at each monthly review — the whole gpt-5.6 family moved between
  July and September and the stale table was wrong in both directions.
- `scripts/monthly_harness_review.md` + `.sh` — recurring monthly harness
  review (external sweep + repo audit + cost-center check), designed for a
  crontab entry `23 9 1 * *` running the shell script.

## Context & memory management

- **Every input-side limit is carved out of one number: `context.turn_input_ceiling_for`.**
  It is the smaller of a tier share of the *input* window
  (`effective_context_window_for` = window − `max_tokens`, because the window is
  shared with the output) and the provider's pricing cliff
  (`cost_ceiling_for` = 272K − 13K buffer for OpenAI, None for Anthropic). The
  ceiling is then divided: ~21K measured fixed overhead (system prompt + tool
  schemas), 30% to the transcript digest (`DIGEST_SHARE_OF_TURN_CEILING`), the
  rest for tool results and the user's message — tool results get no fixed
  share, because in-turn clearing (below) keeps them under the ceiling. **If you
  change the digest share, re-check that the sum still fits** — the arithmetic
  is the whole point. A fraction of the window alone put the trigger
  at 750K on a 1M model while OpenAI started surcharging at 272K, and that gap
  was ~55% of the 2026-09 bill.
- **Compaction is tier-aware, not a constant.** `context.compaction_threshold_for`
  returns 0.75 for frontier models and 0.55 for the fast tier
  (`is_fast_tier`: haiku / luna / mini / nano). Rationale: strong agents do
  better carrying the transcript further and isolating work in sub-agents,
  weak ones do better summarizing early. `chat_compaction_threshold: 0.0` in
  `config.yaml` means "derive"; any positive value pins it for every model —
  but it tunes the window share only, never the pricing cliff.
- **The compaction decision uses the provider's token count, not the estimate.**
  `context.compaction_decision` takes `provider_usage_tokens` (the last turn's
  reported input, which the server already captures for the UI's context ring)
  and treats it as authoritative: it counts the system prompt, tool schemas,
  attachments and tool results, none of which a transcript estimate can see.
  The CJK-aware estimate stays as the fallback and is reported alongside.
- **Tool results are budgeted per tool** (`legal_helper/tool_budget.py`), in
  estimated *tokens* rather than bytes — the same 50 KB is ~12K tokens of
  English and ~50K of Chinese. Over-budget results either truncate with a
  stated loss or spill to `state/tool_results/` and hand the model a path to
  `read_document(path=…, offset=…, limit=…)`. Add a heavy new source to
  `_EXACT_BUDGETS` / `_PREFIX_BUDGETS`. Two exemptions are load-bearing:
  a result carrying inline images is never re-serialised (it would break the
  image protocol), and a result that *is* the deliverable — `run_skill`,
  `cite_check_report_tool`, the grounding tools — is never cut, because
  truncating it shortens the memo rather than the evidence. Both providers
  apply this at their tool-execution sites; keep them in step.
  **The image exemption is a hole, not an entry in `EXEMPT_TOOLS`.** Both
  providers skip `apply_result_budget` entirely for any image-carrying result
  (`openai_provider.py` and its Anthropic twin, at each tool-execution site),
  so text riding along with a render — the lint payload, for instance — is
  un-budgeted. Keep such payloads small, structured and self-capping;
  `quality.QualityReport.to_payload` enforces its own character ceiling and
  groups repeated checks rather than listing them per slide.
- **Task completion outranks budget: nothing caps a turn's work.** Every
  `*_max_iterations` defaults to 0 (= no round limit; a positive value is still
  honoured), and the cumulative `turn_result_budget` is deleted. Both used to
  stop work mid-task — the orchestrator's 8 rounds cut a workbook reshape at
  ~33 calls with no answer written. **Do not reintroduce a cap on rounds or on
  cumulative tool output**; solve growth with context management instead.
- **Long loops are kept under the ceiling by clearing, not stopping**
  (`legal_helper/turn_compaction.py`, both providers). Before each request the
  projected input (provider's count for the last request + results appended
  since) is checked against `turn_input_ceiling_for`; past it, the oldest tool
  results are saved to `state/tool_results/` and replaced by a stub naming the
  path for `read_document`, down to `CLEAR_TARGET_SHARE` (50%) of the ceiling
  so the cache-busting pass is rare. Lossless by construction: a result that
  cannot be saved is not cleared. The latest round is never touched;
  `EXEMPT_TOOLS` deliverables go last. Anthropic hooks the SDK runner's
  `_handle_request`; OpenAI keeps a local mirror of the conversation and, on a
  clearing pass only, re-sends it (output items as `item_reference`) instead of
  chaining `previous_response_id`, then resumes chaining.
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

## Answer verification and durable runs

- **A substantive legal answer is audited before the reader sees it.**
  `WorkflowExecutor._verified_synthesis` buffers the synthesis, runs the
  citation audit, repairs what the audit flagged, and only then streams the
  prose. Research progress, tool calls and phases stream live throughout, so
  the UI still shows work happening; the existing `citation_audit_started`
  event already drives an "auditing" phase. Gated by `verify_before_reveal`
  (default on) **and** `enable_cite_check` — note `config.yaml` currently ships
  `enable_cite_check: false`, which disables both the audit and the repair.
- **Repair is bounded and targeted.** `max_repair_rounds` (default 1) rounds,
  each one synthesis turn plus one re-audit. `_repair_prompt` passes the draft
  plus the critical/major findings and requires: fix only what was flagged,
  re-verify with tools, never invent a replacement citation, and write
  `pinpoint unavailable` / `无法获得具体条款` where that is the truth. An
  intermediate failing audit is not emitted as the answer's verdict; only the
  final one is.
- **`clarify` has two gates, both arithmetic** (`_resolve_clarify`):
  `intake_confidence >= CLARIFY_CONFIDENCE_THRESHOLD` (0.8) means the router is
  hedging, so the turn proceeds; more than `MAX_CLARIFY_ROUNDS` (2) consecutive
  clarifying turns also proceeds. Either way the question it wanted to ask
  becomes an assumption the answer must state. An **absent**
  `intake_confidence` is not a confident one — the heuristic planner omits it,
  and reading that as confidence would disable clarify entirely.
- **Every run is recorded under `state/runs/<run_id>/`** (`legal_helper/runs.py`):
  `artifacts/NN-<phase>.md` per phase (plan, each research bundle, the draft,
  each audit, each repair) plus `snapshot.json` and `events.jsonl`. A cancel or
  a provider outage finishes the run as `paused`, not lost; passing that
  `run_id` as `resume_run_id` reuses the completed bundles instead of paying
  for that research again. `GET /api/runs` lists them. Every write is
  best-effort: durability must never be able to fail a turn.
- **An auditing skill cannot rewrite what it audits.** `tool_policy.py` declares
  which tools mutate a document and drops them for `READ_ONLY_SKILLS`
  (`cite-check`). The distinction is authoring versus inspection, not the name:
  the page-render family stays, because it writes a PNG so the model can *look*
  at a page to confirm a pinpoint, while `render_flowchart_image` is mutating —
  it authors a diagram rather than viewing a document. Classification is by
  explicit name, never by prefix, for exactly that reason.

### Visual review

- **A deliverable is measured on every render, and the model is shown it.**
  There is no separate review module: `tools/documents._visual_result` is the
  whole mechanism — contact sheet plus `quality.lint_artifact` findings plus a
  `next_step`, returned as an image result so the pages reach the model as
  pixels. `authored=False` marks a file the model is reading rather than one it
  wrote. Whether it *looks* is still the model's choice; whether the file is
  *measured* is not.
- **`review_artifacts_before_reveal` does not exist.** Neither does
  `documents/review.py` or `documents/wireframe.py`. An enforced
  lint → wireframe → pixel repair loop was built and reverted; earlier
  revisions of this file described it as shipped, which is how a documented
  setting came to be read from a `config.yaml` that never defined it. If the
  loop returns, bound it with `max_repair_rounds` so it shares the audit's
  cost dial, and forbid deleting content to make a slide fit — cutting the
  substance to satisfy a layout check trades away the wrong thing.

## External procedural skills

- **Two kinds of skill, distinguished by where the directory is.** An in-tree
  skill under `legal_helper/skills/` is legal methodology, so its `SKILL.md`
  body is inlined into the sub-agent with the PRC playbook and the citation
  contract. A skill discovered under an external root is a *procedure*: it
  carries its own workflow, references, artifact contracts and gate scripts,
  and it is **driven**, not inlined. `ppt-master` is the motivating case.
- **Discovery is `skills.skill_roots()`, and this package is always first.**
  It cannot be configured away — the in-tree legal skills are the product, and
  a mistyped `LEGAL_HELPER_SKILL_ROOTS` must not be able to delete them. That
  variable replaces the *external* roots only (default `~/.claude/skills`), and
  an earlier root shadows a later one so a third party cannot silently replace
  a methodology. **Nothing hardcodes a skill name any more**: the previous two
  allowlists — a tuple in `skills/__init__.py` and a separate `Literal` in
  `tools/orchestrator.py` — had already drifted, leaving `flowchart` and
  `docx-redline` on disk and undispatchable by `run_skill`.
- **Three tools make driving possible** (`tools/skill_runtime.py`), and the
  containment is the point. `write_text_file` writes only under `outputs_dir`,
  **not** `project_root`, which holds the source tree. `run_skill_script` runs
  only a `.py` that resolves *inside* a discovered skill's directory, with
  `argv` as a list so there is no shell to inject into; a non-zero exit is
  returned as data, because that is how a skill's own gate reaches the model.
  `list_skill_dir` exists so reference paths are read rather than guessed.
  The trust boundary is the skill root: a script under a configured root runs
  with the harness's privileges, so never point a root at a directory the
  model can write to — the two tools compose into arbitrary code execution.
- **An external skill gets a host runtime contract, never the playbook.**
  `agent._external_skill_prompt` supplies the resolved absolute `SKILL_DIR`
  (such a skill is typically told never to guess it), the call mapping, and the
  two assumptions that fail here: there is no interactive channel, so a
  blocking user gate runs under the skill's own explicit-delegation provision
  and the decision is reported rather than fabricated; and there are no
  background processes, so a preview server is never started. Handing it the
  PRC playbook would be a conflicting instruction, not extra context.
- **A procedure must never be round-capped.** It spends rounds reading its own
  workflow, authoring one artifact per page, running its gate, repairing, then
  exporting; the old 8-round research cap could not reach its export step at
  all. `external_skill_max_iterations` and `sub_agent_max_iterations` both
  default to 0 (unlimited) — under-budgeting a loop does not make it cheaper,
  it makes it fail after paying for most of the work.
- **The in-tree authoring contract does not apply to external skills.** Under
  100 lines, a not-legal-advice line, a reference to the general playbook —
  those are contracts about methodology we maintain. Contract tests iterate
  `skills.internal_skill_names()`; `list_skills()` reports every discovered
  skill and flags which are external.
- **Never vendor a large external skill.** `ppt-master` is 124 MB / 13k files
  and its `attribution_guard.py` fails closed on any modification, so it must
  remain an intact official distribution at a configured root.

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

- Both `claude-opus-5-5` and `gpt-6-sol` (the primaries, and the only high-effort models offered) must work end-to-end on every change.
- Fast/lightweight tier: `claude-sonnet-5` and `gpt-6-luna`. There is no bare `gpt-6` alias — always name the variant.
- **Retired 2026-09-22:** `claude-opus-5`, `claude-opus-4-8`, `claude-opus-4-7`, `claude-haiku-4-5`, and the whole `gpt-5.6` family (terra / sol / luna). The reasons are in `config.RETIRED_MODELS`. They stay priced in `usage.DEFAULT_PRICING` and windowed in `context._WINDOWS` so historical ledger months still replay.
- **Anthropic thinking is adaptive, never `budget_tokens`.** Every offered Claude model rejects `thinking: {type: "enabled"}` with a 400. `anthropic_provider._thinking_kwargs` maps the shared effort dial onto `thinking: {type: "adaptive"}` + `output_config.effort`. `none` maps to `low` effort because Opus 5.5 cannot disable thinking at all. `claude-sonnet-5` is the fast *model* but not the fast *compaction tier*: it has a 1M window and frontier-class quality, so `is_fast_tier` deliberately excludes it.
- `gpt-5.5` is **retired from the high-effort list** (2026-09-11): at verified list prices it is $5.00/$30.00 against Terra's $2.00/$12.00 for the same work, and it was 5.7% of September requests but 55% of real spend. It stays in `usage.DEFAULT_PRICING` so historical ledger months still replay — do not put it back in `OPENAI_HIGH_EFFORT_MODELS`.
- **Retirement is enforced at load, not only at selection.** A chat freezes its
  model at creation (`ChatStore.default_settings`) and re-applies it on every
  later run, so editing the tuples above only ever reached *new* chats — 85 of
  236 chats were still pinned to `gpt-5.5` on 2026-09-21, and 18 requests billed
  at its rates after the retirement date. `Settings.resolve_model_for_provider`
  collapses anything outside `offered_models_for_provider` (high-effort tier +
  fast tier + the `config.yaml` pin) to the user's configured model, and it runs
  in three places: `ChatStore._normalized_settings` (write path),
  `ChatStore._migrate_unoffered_models` (one-time rewrite at open), and
  `server._settings_for_chat` (the run itself). Substitutions emit a
  `model_selection_migrated` event. **Retiring a model means removing it from
  the tuple and nothing else** — the allowlist does the rest. Never read a
  persisted model, jurisdiction or pack straight into a run without resolving it
  against live policy first.
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
python -m pytest -q                      # always `python -m`: bare `pytest` resolves
                                         # to base's interpreter and dies at collection
python -m pytest -q tests/test_pptx_flowchart.py   # native flowchart shape contract
dot -V && d2 --version                   # diagram engines; d2 is optional
python -c "from legal_helper.skills import list_skills; print([s['name'] for s in list_skills()])"
```

## Testing

- `python -m pytest -q tests/test_skill_anatomy.py` — anatomy contract: each SKILL.md <100 lines, frontmatter valid, referenced files exist.
- `python -m pytest -q tests/test_provider_parity.py` — Anthropic and OpenAI see identical tool surface and equivalent results.
- `python -m pytest -q tests/test_tool_budget.py tests/test_context.py tests/test_turn_compaction.py` — per-result budgets, the turn ceiling, and in-turn clearing (no caps).
- `python -m pytest -q tests/test_verified_synthesis.py tests/test_runs.py` — audit-then-reveal, the clarify gates, and the durable run record.
- `python -m pytest -q tests/test_graph_layout.py tests/test_document_quality.py tests/test_docx_layout.py` — diagram geometry, the output linter, and the DOCX table/indent contract.
- `python -m pytest -q tests/test_skill_runtime.py` — skill discovery and, more importantly, the containment of `write_text_file` / `run_skill_script`: escapes out of `outputs/` and out of a skill directory are asserted, not assumed.
- The baseline is **3 failed / 625 passed / 2 skipped**; the three failures are
  `tests/test_caac_local_connector.py` (staged CAAC corpus absent). Anything else is a regression.
- Live smoke once per provider per major change: `MODEL_PROVIDER=anthropic` then `MODEL_PROVIDER=openai`.
