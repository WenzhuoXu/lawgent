# CLAUDE.md — project guidance for Claude Code

Project-level rules. Loaded into every Claude Code session in this repo.

## Identity & scope

`legal_helper` is a **Legal AI Helper**. Default jurisdictional focus is **PRC (China) law**; US and EU are supported as comparative jurisdictions. Aviation is **one of several domain packs** (currently the only populated one); never treat aviation as the whole product.

When the user does not specify a domain pack, behave as a general legal assistant. Surface aviation-only content only when the `aviation` pack is active: pinned in `active_domain_packs` (settings, a project, `--domain-pack`) or loaded by the author with `load_pack` because the request falls in the domain. There is no keyword auto-detection.

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

**Every deck is made by ppt-master.** See **External procedural skills** and
**Document, deck and diagram production**; there is no in-tree deck writer.

**Diagrams outside a deck are drawn by Graphviz, not a browser.** `dot`
(≥2.43, `/usr/bin/dot`, resolved through `documents/graph_layout.find_engine()`)
draws every Mermaid `flowchart` / `graph` / `mindmap` picture that
`render_flowchart_image` makes, in an installed CJK face
(`graph_layout.cjk_font_family()`). Mermaid is the authoring syntax, parsed in
`documents/diagrams.py`; mermaid-cli (`mmdc`, which needs Node and Chrome)
draws only the other Mermaid types, so a missing one degrades sequence or gantt
diagrams, never a process map. A diagram on a slide is not drawn here at all:
ppt-master draws it inside the deck.

Optional: `d2` (MPL-2.0, `conda install -n llm d2`) for structure charts with
nested containers, which is the one arrangement Graphviz clusters do not match
for readability. `graph_layout.unresolvable_render_binaries()` names whatever is
missing, and `serve` prints it at startup — a missing render binary is
otherwise silent until a diagram simply never appears.

```
conda run -n llm npm i @mermaid-js/mermaid-cli --save-dev --ignore-scripts
```

**Never invoke a Node CLI by its shebang.** `node_modules/.bin/mmdc` is a JS
file with `#!/usr/bin/env node`, so the *launching* process's PATH decides
whether it runs. A server started by an absolute interpreter path never gets
the env's `bin/` on PATH, and every Mermaid render in the running server failed
with exit 127 for that reason. `writers/_node.find_node()` exists for exactly
this; `diagrams.resolve_mermaid_cli()` applies it, invoking
`node <mermaid-cli/src/cli.js>` with Node's directory prepended to the child's
PATH. A missing interpreter raises `MermaidNotInstalled`, not `RuntimeError`,
because callers fall back on the former and the latter made the degraded path
unreachable.

**`ppt-master` is an external skill, not a package.** It is driven from a
configured skill root (`~/.claude/skills` by default) and is deliberately **not
vendored** (the skill folder is 83 MB / 13k files, ≈250 MB with history, and
its `attribution_guard.py` fails closed on any modification, so it must stay an
intact official distribution). The skill is the repository's
`skills/ppt-master/` folder, not the repository root — cloning straight into
`~/.claude/skills/ppt-master` puts the wrong directory at the discovery path.
Clone it elsewhere, keep `.git` for updates, and link the skill folder in;
discovery resolves the link:

```
git clone https://github.com/hugohe3/ppt-master ~/src/ppt-master
ln -s ~/src/ppt-master/skills/ppt-master ~/.claude/skills/ppt-master
conda run -n llm pip install --dry-run --report /tmp/r.json -r ~/src/ppt-master/requirements.txt  # install only what is additive
conda run -n llm python ~/.claude/skills/ppt-master/scripts/attribution_guard.py              # exit 0 = intact
# update: git -C ~/src/ppt-master pull, then re-run the guard
# optional: its visual-review stage wants a browser
conda run -n llm pip install playwright && conda run -n llm python -m playwright install chromium
```

`google-genai` (its Gemini image backend only) is left out of the `llm` env:
it would upgrade `pydantic`, `websockets` and `google-auth` under the harness.
Its narration stage needs `ffmpeg`/`ffprobe`, which the host does not have.

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

- `legal_helper/skills/<name>/` — generic, domain-agnostic legal methods. Each is a `SKILL.md` (<100 lines), plus `references/` (deep prose read on demand) and `resources/` (presets and output templates) where the method needs them.
- `legal_helper/domains/<pack>/` — domain packs. Each is `pack.yaml` + `playbook.md` + `research_sources.md` (the domain's source routing) + `references/` + `overlays/<skill>.md` (per-skill aviation deltas).
- `legal_helper/playbook/general_playbook.md` — PRC-first global defaults, inlined for every legal author and specialist.
- `legal_helper/workflow.py` — the author loop (`WorkflowExecutor.stream`), the author's tool surface and the audit-then-reveal citation check; `multi_agent.py` (the `run_workflow` tool and its specialists), `agent.py` (`SkillAgent`: the specialist and external-procedure runtime), `authoring.py` (the author's system prompt), `answer_contract.py` (the one output contract), `answer_lint.py` (deterministic answer clean-up), `tool_exposure.py` (which tools wait behind tool search), `turn_context.py` (the per-turn state a tool reads: digest, attachments, cancel signal, run record, the turn's settings).
- `legal_helper/connectors/` — in-process tool functions over httpx / WebFetch. The primary call path for both providers.
- `legal_helper/mcp/` — external MCP boundary. HTTP servers become plain function tools (`tools/mcp_tools.py`): the nine PKULaw sub-services in `.mcp.json`, and the first-party CourtListener and GovInfo MCPs (`first_party.py`, secondary to the direct connectors). The stdio entries — EUR-Lex and the in-tree `ccar_aviation` server — serve external MCP clients; the harness reaches those sources through its own connectors.
- `legal_helper/citations/` — eyecite-backed `extract` / `validate`, PRC and EU regex, grounding (`ground_answer`: fetch the cited source, round-trip the quotes), provenance tags, the verification report and log, format conversion, and the failure taxonomy (`trajectory.py`).
- `legal_helper/documents/` — the deliverable layer. `office.py` (inspect / extract / guarded edit), `redline.py` (w:ins/w:del), `pdf.py`, `graph_layout.py` (CJK text measurement + the Graphviz / d2 render binaries), `diagrams.py` (Mermaid parsed and drawn by Graphviz; mermaid-cli for other types), `quality.py` (deterministic output checks), `contact_sheet.py`, `soffice.py` (headless LibreOffice under a per-call profile), `lineage.py`, `review_fonts.py` + `review_fonts.conf` (GB-font stand-ins for review renders), and `writers/` — the DOCX, PDF and XLSX writers with their shared Markdown front end (`writers/_markdown.py`) and language rules (`writers/_text.py`). Decks are made by `tools/decks.create_deck` (ppt-master), never by an in-tree writer.
- `legal_helper/rag/` — local bge-m3 embeddings + Qdrant (embedded) + legal-aware hierarchical chunker. The embedder and reranker load per search and are released when it ends (`retrieve.models_held`): kept resident, they held ~6 GiB of GPU memory in an idle server. A reload costs ~5 s per search.
- `src/` — the React web UI. `npm run build` writes `legal_helper/webui_dist/`, which `serve` serves; without a build the server answers with a 503 page saying to run it.

## Authoring rules

- **SKILL.md ≤ 100 lines.** Use pointer paragraphs (`Read references/foo.md for ...`) instead of inlining deep content.
- **Frontmatter required**: `name`, `description`, `argument-hint` (optional), `allowed_connectors` (optional), `rag_collections` (optional).
- **No aviation strings outside `domains/aviation/`.** If you find yourself writing FAA/EASA/ICAO/IDERA/Cape Town/MRO/AD/SB inside `skills/` or `playbook/`, stop and put it in `domains/aviation/overlays/<skill>.md` instead.
- **PRC primary, comparative second.** Generic skills follow general playbook §0: legislation by 立法法 rank (宪法 → 法律 → 行政法规 → 地方性法规 / 规章), then 司法解释 (legal effect, cited after the statute it applies), then cases by weight label (指导性案例 应当参照 → 案例库参考案例 参考 → other 类案 persuasive); 规范性文件 and SPC 会议纪要 support reasoning only, never as 裁判依据. US/EU when relevant.

## Document, deck and diagram production

**Every deliverable can be written, measured, looked at, and repaired before
it is reported as done** — the measuring is automatic on every render; looking
and repairing are the model's call (**Looking is opt-in**). This is the Excel
loop below, generalised: inspect → guarded edit → diff → re-inspect became
write → lint → render → repair → re-lint. The deterministic layer comes first
because it costs nothing.

- **Every deck is made by ppt-master, through `create_deck`** (`tools/decks.py`).
  A presentation, a single slide, a diagram slide, 汇报材料, 课件 — on every
  authoring surface: the author and a general specialist
  (`workflow._document_io_tools`) and every in-tree skill specialist
  (`tools.skill_tools_for_task`). It runs ppt-master to completion in a forked
  `SkillAgent` on the `deck` role (by default the chat's own model and
  effort), never round-capped. `run_procedure` never offers ppt-master: a
  second route to the same skill would skip everything below.
- **The sub-agent sees the task, not the chat.** `create_deck` writes the
  `brief` into the workspace as the deck's closed corpus and states the
  language (default: the brief's), audience, purpose, page count, sources,
  template, the user's quick intent, and that `project_manager.py init` takes
  `--dir <workspace>` — its default is a `projects/` folder inside the skill's
  own installation. The tool description tells the caller the same thing:
  nothing absent from the brief reaches a slide.
- **The project never reaches the user's file list.** It lives under
  `<outputs_dir>/_decks/`, which the artifact scan skips (any path component
  starting with `_` or `.`); only the export is copied, to
  `<outputs_dir>/<safe title>.pptx`, never over an existing file, with lineage
  recorded from the user's inputs. The sub-agent's own `outputs_dir` is that
  workspace, so its writes, script runs and renders stay inside it.
- **A deck is verified by the tool, not by the sub-agent's word.** The export
  is found on disk (an `exports/` file wins; a copied source deck never
  counts), opened with python-pptx and counted, linted, and matched to
  ppt-master's own postflight report. The result is compact JSON:
  `status` (`ok` | `failed` | `unavailable`), `pptx_path`, `slides`,
  `gate_verdict`, `decisions_under_delegation`, `lint_summary`, `next_step`,
  plus `project_dir`, timing and tool-call counts. Without ppt-master the
  status is `unavailable` with the install instruction; there is no fallback
  writer, and `next_step` says not to substitute another format.
- **Progress reaches the stream while it runs.** `DeckProgress` sits over the
  active event sink for the run and turns each sub-agent tool call into one
  `deck_progress` event (`stage`: page_authored, quality_gate, export, …;
  `message`, `ok`/`exit_code`, `current`/`total` pages), forwarding the
  sub-agent's own tool events unchanged; `logging_setup.sub_agent_scope` tags
  them with the `create_deck` call, so the UI nests them under it (see
  **Specialist events name their parent call**); the deck card
  (`components/parts/ProcedureCard.tsx`) reads "running" until `create_deck`
  itself returns, with pages against the target and the latest step.
- **The deck sub-agent runs at low effort and patches instead of rewriting.**
  The `deck` role is `effort: low`, and the sub-agent holds `edit_text_file`
  (exact-string replace) beside `write_text_file`. Measured 2026-10-15 on
  gpt-6-luna, the same 3-page brief, deck stage only: 774 s → 292 s, 61 → 43
  requests, 79.9K → 19.9K output tokens. At the chat's effort (medium) three
  quarters of the output was reasoning, the spec lock was written four times
  and every page three times in full; now the lock is written once and every
  repair is a patch. The low-effort deck lost about a point of design polish
  (the timeline and per-point pinpoints of the earlier run) and gained a case
  caption and page numbers; the user chose the faster deck.
- **Decks the user already has** are read and changed with `inspect_pptx`,
  `edit_pptx_text`, `reshape_pptx` and `render_pptx_slides`. `inspect_pptx`
  and `reshape_pptx`'s `shape_name` reach shapes inside groups, where a
  ppt-master deck keeps all of its text; `edit_pptx_text` already walked every
  paragraph. A redesign of the user's deck is `create_deck` with
  `template_path`.
- **`documents/quality.py` is a conformance floor, and only that.** Shapes off
  the canvas, pairwise overlap past 2% of the smaller shape, text that cannot
  fit its box (CJK-aware, reading the frame's real insets rather than assuming
  PowerPoint's defaults), table rows that grow past the slide
  (`table_overflow`), runs under the 11pt body / 9pt label floor, WCAG
  contrast, empty placeholders big in both dimensions. Groups are opened and
  every child mapped through its group's `chOff`/`chExt` into slide inches —
  the top-level walk saw none of a ppt-master deck's 112 nested text frames.
  Overlap between two unfilled text boxes is judged on their glyphs, not their
  line boxes. Findings carry `slide` and `shape` indices (a dotted path such
  as `"4.2"` inside a group), and `where` adds the shape's name, so a finding
  names a location the model can go and look at.
  For Word, PDF and Excel the floor is: an ordered list that keeps counting
  across a heading (plain "1." lists only — "第%1条" clause numbering runs on
  by design), literal Markdown residue (`**`, `[x](http`, `####`, `|---|`,
  `[^1]`, `~~`, `<br>`) by paragraph / table cell / page, a table that runs
  onto another page without a repeating header row (needs the render's PDF),
  and XLSX error values and formulas with no cached value. **A file type with
  no checks reports `conformance: "no checks for this type"`, never "pass"**,
  and an unreadable file reports "not checked".
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
  13 em, because its labels live inside grouped graphics. A CJK language-tag
  check was measured and not added: all 215 Han runs in both real ppt-master
  exports are `zh-CN` in the theme's East Asian face. Both exports lint clean
  (303 shapes); a new check must keep it that way or name a real defect.
- **Word and PDF output is PRC-ready by default** (`writers/docx.py`,
  `writers/pdf.py`). A document is Chinese when most of its words are
  (`_text.han_share` ≥ 0.5 — one fullwidth comma no longer flips an English
  memo), and then it is A4 with "第 X 页 共 Y 页"; English is Letter with
  "Page X of Y"; `page_size` overrides. Both writers parse Markdown with one
  markdown-it-py front end (CJK-friendly emphasis, so `**结论：**正文` is bold; a
  single newline is a new paragraph; raw HTML other than `<br>`/`<sup>`/… is
  printed as text). Every ordered list gets its own `w:num` with a
  `w:startOverride`, nesting is `w:ilvl`, links are `w:hyperlink`, `[^1]` are
  real footnotes, table header rows repeat and rows never split.
  `layout="gbt9704"` is verified against GB/T 9704-2012 and the
  gongwen-gbt9704-skill verifier (A4, 37/26/35/28 mm, 22 × 28 grid, 一、（一）
  1.（1） in 黑体/楷体/仿宋/仿宋 unbolded, 小标宋 title, "— n —" outside).
  `reference_docx` writes into a firm or court template.
- **The not-legal-advice notice is never part of the instrument.** It goes into
  document properties (Word `description`, PDF `Subject`) in the document's
  language; `disclaimer="none"` plus a `disclaimer_reason` omits it for
  filings and signed letters. The export path titles an answer by its own first
  heading (then the chat title, then 法律分析 / Legal Analysis by language).
- **Fonts: name the GB fonts, review with stand-ins.** A Chinese DOCX names
  仿宋 / 黑体 / 楷体 / 宋体 / 方正小标宋简体 — what the reader's Word/WPS has. A review
  render (`render_docx_pages` / `render_xlsx_pages`) passes
  `documents/review_fonts.conf` to soffice as `FONTCONFIG_FILE`, so each role
  gets a distinct open face, and tells the model which faces are stand-ins.
  Never edit the user's fontconfig; never bundle a GB font (方正 faces are
  commercial, and the tree is MIT).
- **Style is not in the code.** Whether a title should assert or name, whether
  load should vary, whether a deck needs an ask — those differ between an
  internal 汇报, a training 宣贯, a seminar talk and a pitch, and none of it is
  derivable from a corpus. Guide the model and let the model decide; an
  earlier attempt encoded one genre's conventions as a schema of roles,
  exhibit floors and profile flags, and it was deleted. When the user supplies
  a reference deck or template, that governs — and when they supply a mature
  external skill, drive it rather than reimplementing its judgement (see
  **External procedural skills**).
- **Rendering an authored file measures it and shows it, in one call.**
  `tools/documents._visual_result` builds a contact sheet, runs
  `quality.lint_artifact` over the pages, and returns both as an image result,
  so the pages arrive as image blocks the model actually sees with the
  findings and a `next_step` beside them. The findings stand in for advisory
  prose ("check each page for overflowing text…"): every such clause is
  arithmetic, and nothing could tell whether an instruction had been followed.
  `authored=False` for a file the model is *reading* (`render_pdf_pages` on a
  PDF that `write_pdf` did not produce) — telling it to repair a source PDF
  it never wrote drove repeat single-page renders. `render_pptx_slides` runs
  LibreOffice under a per-call profile (`documents/soffice.convert`): on the
  shared default profile two concurrent renders failed one of them.
- **Looking is opt-in; measuring is not.** Nothing forces a `render_*` call,
  but once one happens the lint is attached whether the model asked for it or
  not. There is no enforced write→review→repair loop (see **Visual review**).
- **A diagram on a slide is ppt-master's; a diagram as a picture is
  Graphviz's.** `render_flowchart_image` is for a Word document, an email or
  the chat. Mermaid `flowchart` / `graph` sources become DOT (subgraphs become
  clusters, decisions diamonds, labels wrapped into balanced lines) and
  `mindmap` sources a left-to-right tree with plain ties; Graphviz measures
  every label in the font it draws, so boxes fit their text. A source the
  parser cannot read, and every other Mermaid type, goes to mermaid-cli; a
  structure of nested containers is D2.
- **The Mermaid parser keeps what the author wrote.** `A -- yes --> B` as well
  as `A -->|yes| B` (only the piped form used to match, so every labelled
  decision branch was dropped in silence), chains `A --> B --> C` (a plain
  `finditer` consumed `B` and lost `B → C`), open links `---` and thick `==>`,
  quoted labels and `<br>` breaks.

### Document intake and editing

- **One reader for every format** (`documents/intake.py`, used by
  `read_document` and attachments). Word (.docx, and .doc/.rtf/.odt/.wps via a
  cached LibreOffice → .docx conversion) goes through `documents/docx_reader.py`:
  tracked changes as `{++…++}` / `{--…--}` under a one-line summary (they read
  as accepted text before — a counterparty's markup looked agreed), headers and
  footers as `[Header]` / `[Footer]`, and numbering labels computed from
  `numbering.xml` as Word shows them (第一条, not anydoc's `- 第1条`). PDFs get
  `[page N]` markers, `pages=`, and RapidOCR for image-only pages (cached by
  sha256 + page under `state/ocr_cache`, labelled `· OCR`); a read under 200
  characters always carries a warning. OFD is read in-tree; a .zip lists its
  members (`path="a.zip::member"`). An unknown binary is an error naming what
  is supported — never `read_text(errors="replace")` garbage.
- **One clause index** (`documents/clauses.py`) serves the attachment outline,
  `read_document(section="第三条")`, `extract_clauses` and `redline_docx`
  anchors; 第三条 = 第3条 = "3" resolve to the same clause.
- **DOCX edits never blank a run.** `edit_docx_text` / `reshape_docx` edit the
  text elements (`w:t`, `w:tab`, `w:br`), so footnote/comment references,
  fields and drawings in the same run survive; paragraphs carrying tracked
  revisions are refused with a pointer to `redline_docx`. `inspect_docx` lists
  every body paragraph under the 0-based index `reshape_docx` ops take.
- **Every DOCX edit returns `integrity`** (`documents/integrity.py`):
  ooxml-integrity (MIT) `compare` + new `check` findings, plus in-tree counts
  of bookmarks, fields, section breaks and endnote references. Losses only.
- **Tracked changes out:** `redline_docx` (anchored by text, clause label or
  paragraph index; tabs survive run splits; `w:trackRevisions` set; enforced
  protection other than tracked-changes refused; every miss returns a
  `reason` + `hint`) and `compare_docx` (LibreOffice compare over UNO in the
  system Python, `_lo_uno.py`). Both are gated by the round-trip contract:
  accept-all = revised text, reject-all = original.
- **No edit overwrites its source.** An output name equal to the input goes to
  `<stem>_vN` and the result says so (7 of 121 edit calls had overwritten
  theirs); every derived file is recorded in `documents/lineage.py`.
- **`reshape_xlsx` row/column inserts and deletes behave as in Excel**
  (`documents/xlsx_shift.py`): merges, validations, conditional formats,
  formula references on every sheet, defined names and row heights move with
  the cells, and each op reports its `side_effects` (#REF! included).
  `set_styles` starts from a copy of the cell's font, so theme colours work.

### Excel / workbook operations

Excel work is a general document-tool capability, not a legal method. The
author does it with the spreadsheet tools, which tool search finds in the
`spreadsheet` group; it needs no method and no specialist.

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

`write_xlsx` computes every formula and stores the result as the cell's cached
value (`writers/_xlsx_recalc.py`): LibreOffice recalculates a *copy*, the
values are written into the openpyxl file's `<v>` elements, and LibreOffice's
re-export is discarded, so styles, charts and validations stay byte-identical.
The `formulas` engine is the fallback without soffice and the second opinion
for a LibreOffice 7.3 `#NAME?` (XLOOKUP, LET). ISO date strings become date
cells, `number_formats` sets formats per column/range, and widths count a Han
character double. An openpyxl round-trip (the edit tools) drops cached values;
the lint reports them as `formula_without_cached_value`.

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
  **The surcharge is per request, the ledger row is per turn.** The OpenAI
  provider counts a turn's requests and keeps the share that came from
  requests over 272K (`requests`, `long_context` on the row), and only that
  share is surcharged. Rows written before 2026-10-12 carry only the turn's
  sum, so they are surcharged whole when the sum crosses 272K — an upper
  bound: 2026-08 is $133–191, 2026-09 $79–119. The earlier "under-reported
  by 21% / 31%" figures came from that turn-sum rule.
- OpenAI cache writes (`input_tokens_details.cache_write_tokens`, reported as
  `cache_write_input_tokens`) are a subset of `input_tokens` billed at the
  model's `cache_write` rate (1.25× input); each input token is priced once —
  ordinary, cached or written. Ledger rows before 2026-10-12 recorded 0 writes.
- `GET /api/usage` serves the month summary (`?month=YYYY-MM` for history);
  the web UI's settings dialog renders it (`CostCenter` in
  `src/components/controls/SettingsDialog.tsx`).
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
  ceiling is then divided: the fixed overhead (system prompt + loaded tool
  schemas — ~21K when measured on 2026-09-21 with every tool loaded; the
  schema share has since dropped to ~3K behind tool search, below), 30% to the
  transcript digest (`DIGEST_SHARE_OF_TURN_CEILING`), the rest for tool
  results and the user's message — tool results get no fixed
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
  `context.compaction_decision` takes `provider_usage_tokens` (the input of the
  turn's *last request*, `last_request_input_tokens` on the provider's `done`
  usage, read via `context.context_tokens_from_usage`; the UI's context ring
  shows the same number) and treats it as authoritative: it counts the system
  prompt, tool schemas, attachments and tool results, none of which a
  transcript estimate can see. Never feed it the turn-summed `input_tokens` —
  that is billing, and summed over a 30-round loop it read 3.64M on a
  1.05M-window model. The CJK-aware estimate stays as the fallback and is
  reported alongside.
- **Tool results are budgeted per tool** (`legal_helper/tool_budget.py`), in
  estimated *tokens* rather than bytes — the same 50 KB is ~12K tokens of
  English and ~50K of Chinese. Over-budget results either truncate with a
  stated loss or spill to `state/tool_results/` and hand the model a path to
  `read_document(path=…, offset=…, limit=…)`. Add a heavy new source to
  `_EXACT_BUDGETS` / `_PREFIX_BUDGETS`. Two exemptions are load-bearing:
  a result carrying inline images is never re-serialised (it would break the
  image protocol), and a result that *is* the deliverable — `run_workflow`'s
  bundles, `cite_check_report_tool`, the grounding tools (`EXEMPT_TOOLS`) — is
  never cut, because truncating it shortens the answer rather than the
  evidence. Both providers apply this at their tool-execution sites; keep them
  in step.
  **The image exemption is a hole, not an entry in `EXEMPT_TOOLS`.** Both
  providers skip `apply_result_budget` entirely for any image-carrying result
  (`openai_provider.py` and its Anthropic twin, at each tool-execution site),
  so text riding along with a render — the lint payload, for instance — is
  un-budgeted. Keep such payloads small, structured and self-capping;
  `quality.QualityReport.to_payload` enforces its own character ceiling and
  groups repeated checks rather than listing them per slide.
- **Task completion outranks budget: nothing caps a turn's work.** Every
  `*_max_iterations` defaults to 0 (= no round limit; a positive value is still
  honoured), and there is no cumulative tool-output budget. Both used to stop
  work mid-task — an 8-round cap on the main loop cut a workbook reshape at
  ~33 calls with no answer written. **Do not reintroduce a cap on rounds or on
  cumulative tool output**; solve growth with context management instead.
- **Long loops are kept under the ceiling by clearing, not stopping**
  (`legal_helper/turn_compaction.py`, both providers). On **OpenAI**, before
  each request the projected input (provider's count for the last request +
  results appended since) is checked against `turn_input_ceiling_for`; past
  it, the oldest tool results are saved to `state/tool_results/` and replaced
  by a stub naming the path for `read_document`, down to `CLEAR_TARGET_SHARE`
  (50%) of the ceiling so the cache-busting pass is rare. Lossless by
  construction: a result that cannot be saved is not cleared. The latest round
  is never touched; `EXEMPT_TOOLS` deliverables go last. OpenAI keeps a local
  mirror of the conversation and, on a clearing pass only, re-sends it (output
  items as `item_reference`) instead of chaining `previous_response_id`, then
  resumes chaining.
  **On Anthropic the transcript is never edited client-side.** Opus 5.5 binds
  each thinking block to the exact history before it: editing an earlier
  `tool_result` invalidates every later thinking block (silently dropped on
  older accounts — this one, measured 2026-10-07 — and a 400 on accounts
  created on or after 2026-08-31). The server clears instead
  (`clear_tool_uses_20250919`, `turn_compaction.anthropic_context_management`):
  trigger = the turn ceiling, `clear_at_least` = (1 − `CLEAR_TARGET_SHARE`) ×
  ceiling — measured, the server clears in sticky chunks of (input − trigger)
  + `clear_at_least`, and later requests read the cleared prefix from cache —
  `keep` = 10 newest tool uses, `EXEMPT_TOOLS` excluded. Losslessness is kept
  append-only: once a turn's projected input reaches half the trigger,
  `ClearingNotes` saves each sizeable result and names it in a text block
  after the round's `tool_result` blocks, which clearing does not touch. If
  the clearable results total less than `clear_at_least`, nothing is cleared.
- **Model windows live in `context._WINDOWS`.** A model missing from that dict
  silently gets `_DEFAULT_WINDOW` (200K) — which is how a 1M-window model ends
  up compacting at ~120K. **Add every new model ID here, to
  `usage.DEFAULT_PRICING`, and to the `*_HIGH_EFFORT_MODELS` tuple in
  `config.py` at the same time** — for OpenAI, its rows in the
  `openai_provider` capability tables too (**Provider parity contract**), and
  no model is offered before it passes the tool-search probe.
- **The SSE stream closes at `done`; summary and title run afterwards** on a
  per-chat serial worker (`server._submit_chat_housekeeping`: one job at a
  time per chat, in order, each reading the summary the previous one wrote)
  on the fast tier at the `title` (none) and `summariser` (low) rows of
  `role_profiles`. They used to hold the stream — and the disabled composer — open for p50 8 s,
  p90 63 s. Their `memory_updated` / `chat_updated` events normally reach no
  one; what they persist is what a reload, or a re-read of the chat list,
  shows.
- **Rolling summaries roll forward by delta, never by rewrite.** Both
  `memory.summarize_with_fast_model` (per chat) and `ProjectStore.compact`
  (cross-chat) ask the fast model only for `+ Section | …` / `- Section | …`
  lines and merge them deterministically via `memory.apply_delta` over the
  sections in `memory.SUMMARY_SECTIONS`. Re-summarizing a summary erodes
  detail every pass (context collapse); do not reintroduce a "rewrite this
  summary" prompt. An unparseable delta must leave the prior playbook intact.
- **Attachments: inline what fits, outline the rest — decided in tokens.**
  Text attachments are inlined in order while their *extracted* tokens
  (`context.estimate_tokens`, CJK-aware) fit `_INLINE_SHARE_OF_TURN_CEILING`
  (0.4) of `context.turn_input_ceiling_for`; the rest — a single huge one
  included — degrade to an outline (clause labels from `documents/clauses.py`,
  headings) plus the `read_document(…, section=…)` call that opens them. Bytes
  were the wrong unit: one 353 KB workbook extracted to ~367K tokens and was
  inlined into every sub-agent. `LEGAL_HELPER_INLINE_ATTACHMENT_TOKENS` sets the
  budget in tokens (`0` = never outline). Images and provider-side PDFs are
  unaffected; HEIC/BMP/TIFF are converted to JPEG first. An inlined attachment
  says it is the file's
  complete extracted text (and names embedded images, and for a workbook
  that cell addresses need `inspect_xlsx`): without that the model re-read
  every inlined Office file with `read_document` — two of five rounds of a
  27-file turn; replayed, 0 re-reads in 6 of 6 runs.
- **Anthropic server-side clearing is on by default.**
  `LEGAL_HELPER_ANTHROPIC_CLEAR_TOOLS` set to `0`/`false`/`off` turns it off; a
  number pins the trigger in tokens. It was opt-in while the trigger sat at a
  fixed 120K and every pass busted the cache; derived from the turn ceiling it
  fires only where the turn would otherwise run on to the window, and it is
  the only form of clearing preserved thinking allows.
- **Anthropic caching: 1h on the stable prefix, an automatic breakpoint on
  the loop.** The tool list and system prompt carry `ttl: "1h"` breakpoints
  (the next turn usually starts 5–60 minutes later); inside a local-tool loop
  a top-level `cache_control` adds a 5-minute breakpoint that follows the
  conversation, so each round reads the last. It is left off one-shot calls,
  where it would only bill the write premium, and whenever the explicit
  breakpoints already fill the four slots (a fifth is a 400). Measured: four
  breakpoints plus hosted web search is accepted.

## One author loop, the workflow as a tool, and the output contract

- **There is no router.** The chat's own model (the `author` role: the user's
  model and effort) reads every turn and answers it in one tool loop —
  greetings, exam questions and memos alike. Until 2026-10-14 a fast-tier
  router (luna, effort low, no tools) chose the path per turn and, for 48% of
  turns over 2026-10-08..14, wrote the answer itself inside a JSON field; one
  of those was a wrong LSAT necessary-assumption answer, shipped under the
  chat model's name because the direct path reported no usage. The router
  also never decomposed a turn in that window. Plan and measurements:
  `docs/plans/2026-10-14-single-agent-loop.md`. Do not put a classifier or a
  cheap model in front of the author; the fast tier does side work only
  (rolling summary, title).
- **The author decides what it needs** (`workflow.WorkflowExecutor.stream`):
  which method to load, whether to research, whether to delegate, whether to
  ask one clarifying question. Its tools are one surface
  (`workflow.author_tool_surface`): hosted search, document tools, legal
  connectors, `retrieve_legal`, `legal_source_search`, `load_skill` /
  `read_skill_file`, `load_pack` and the loadable packs' source tools,
  `run_workflow`, project tools. It carries no MCP tool: PKULaw stays off it
  until its gateway answers again (capability review 2026-10-07, R-6). A skill
  specialist and the citation audit reach the HTTP MCP tools their
  `allowed_connectors` admit (`"pkulaw_*"`) through
  `tools.skill_tools_for_task`.
- **Methods load on demand** (`tools/skill_resources.load_skill`). The system
  prompt lists each in-tree method by name and one line
  (`authoring.methods_block`); `load_skill` returns the SKILL.md body without
  its bundle-contract sections, the active pack's overlay, and the file list
  `read_skill_file` opens. A method the user picked in the UI (`skill_hint`)
  is inlined in the request tail. A specialist's own `list_skill_references` /
  `read_skill_reference` resolve the skill from `agent_name_var`, so they work
  only inside a specialist.
- **The multi-agent workflow is a tool** (`multi_agent.run_workflow_tool`).
  The author calls `run_workflow(objective, tasks)` with up to
  `max_workflow_tasks` (3) specialists — title, method, self-contained task,
  optional `depends_on`; each runs as a `SkillAgent` (or the general
  specialist) on the `specialist` role, in parallel under
  `effective_max_concurrent_agents`, with one retry after a transient failure,
  lowered concurrency for the batch on a rate limit, and fail-soft gaps; the
  bundles and open gaps return as the tool result and the author integrates
  them in the same loop (integration rules in `authoring.workflow_guidance`).
  Depth is one: specialists never hold the tool. The result is exempt from the
  result budget. A resumed run reuses a bundle with the same method under the
  same task id, or under the same title (`RunStore.resumable_tasks`).
- **Whether the user asked for a workflow is the model's call — never a
  keyword match.** The tool description and the system prompt say a request
  for a workflow or several agents, in any language or phrasing, makes the
  tool mandatory, and that a business workflow (审批工作流程) or an agent in
  agency law is not such a request (ZCode's `CreateWorkflow` rule; Codex also
  leaves it to the model). A regex detector was built and rejected: legal
  vocabulary collides with it.
- **Specialist events name their parent call.** Every event a sub-agent
  emits — a `run_workflow` specialist, `create_deck`'s ppt-master,
  `run_procedure`'s procedure — carries `task_id` and `parent_call_id`
  (`logging_setup.task_id_var`, `parent_call_id_var`; the providers set
  `tool_call_id_var` while a tool runs and put `call_id` on tool events;
  `sub_agent_scope` tags a deck or procedure run) — the Agent SDK's
  `parent_tool_use_id` convention. Previews (`agent_delta`, a specialist's
  `tool_progress`) go through `emit_transient` and are never persisted.
  The UI nests them under the spawning tool's part (`src/api/sse.ts`,
  `components/parts/WorkflowCard.tsx`, `ProcedureCard.tsx`).
- **The turn renders as one chain of thought** (`components/parts/ActivityChain.tsx`).
  Each reasoning section is a step; the tool calls that followed it hang under
  it as one-line rows; a sub-agent call is a step of its own. The translator
  ends a reasoning segment at every top-level tool event, so thoughts and the
  calls they led to alternate in stream order — one reasoning part kept open
  across a whole turn used to sit between tool cards and grow there.
  Reasoning deltas stay transient; `_synthesis_pass` logs one `reasoning`
  event per segment where it ends, which closes the streamed segment live and
  recreates it on reload. `GET /api/chats/{id}` returns a chat's newest 300
  events (`ChatStore.list_events`), so a turn longer than that — a deck turn
  with ~90 sub-agent calls — reloads without its first steps.
- **`done` names the model that wrote the answer**; the server stamps the
  message with it.
- **The answer language comes from the request's script**
  (`answer_contract.detect_language`, `workflow._turn_language`).
- **One author prompt** (`authoring.author_system_prompt`): role, conduct
  (work a disputed answer again before changing it; ask only for a fact that
  changes the answer), research conduct, the author contract, deliverable
  rules, the whole general playbook, the method list, the loadable-pack list,
  the "Tools on demand" groups, the workflow section and the jurisdiction /
  pinned-pack addendum. It is static per pinned-pack set, so it caches; a pack
  loaded mid-chat arrives as a tool result instead. Date, language and length
  tier go in the request tail (`answer_contract.author_tail`), and a method the
  user picked is appended to it — never in the system prompt
  (`tests/test_provider_cache_control.py`).
- **One output contract** (`answer_contract.py`). The author's sources list is
  one numbered line per source with a status word in the answer's language
  (已在线核验 / 未核验 / 用户提供 / 无法获得具体条款, or the English set) — no
  claim column, no disclaimer line (the UI and exports carry it once); a reply
  that states no law has no sources section. The author keeps the citation
  web search attaches and is never asked to wrap one in a markdown link; only a
  URL it holds as text (a fetched page, a connector result) may become
  `[Source, pinpoint](URL)`, and a link never has an empty target or a
  citation inside it. Asked to link every source, luna wrote empty `]()`
  targets and once put a search citation inside a link, where OpenAI's
  citation rendering swallowed the text between — 0 such answers in 868
  before that rule, 1 in 20 with it. Next steps are part of the answer
  whenever the facts call for action. The specialist bundle contract is
  Findings · Issues · Artefacts · Out of scope ·
  Sources with one label set and one length rule; a SKILL.md names only its
  artefacts. `tests/test_answer_contract.py` fails when a prompt grows its own
  copy of either.
- **A file is written for its readers; notes for the requester go in the
  reply** (`answer_contract.deliverable_rules`, rendered into the author
  prompt and the general specialist's prompt). The length tier and the author
  contract govern the chat answer only; before this, files written through
  `write_docx` had no contract, and the bare "give the path, do not paste the
  body" rule left the author no place for its caveats, so
  they went into the instrument — a department-facing 评审意见 carried the
  reviewer's own score-sheet corrections and ~15–20 evidence hedges at
  3.5–4.5× the length of the model opinion the user supplied. Replayed on
  gpt-6.1-sol (2026-10-12, 3 runs per arm): requester-input mentions in the
  file 4–11 → 0, hedges 14–23 → 1–10, length 3.5–4.2× → 2.6–3.2×, the
  same defects caught, the notes in the reply. It is the same separation
  as the not-legal-advice notice in document properties.
- **Length is a tier, not a cap.** `answer_contract.length_tier` returns the
  chat's pinned `answer_length` (brief / focused / full, each with an OpenAI
  `text.verbosity`) or `auto` — one sentence telling the author to fit the
  request, and the provider's default verbosity. It never touches max tokens,
  tool rounds or tool budgets, and an explicit length request in the message
  wins.
- **The answer lint is a stream filter** (`answer_lint.py`): it strips `utm_*`,
  collapses repeated search chips within a paragraph, rewrites upload and
  artifact paths to served routes (or to the bare file name), localises
  `pinpoint unavailable` in a Chinese answer, rewrites each sources line to
  `N. [name](url) — status` (the chip or bare URL moves onto the name, "——"
  counts as a separator), counts sources sections and logs an `answer_lint`
  event. It works on whole lines, so a URL is never split, and it never grades
  style.
- **Specialists see the chat.** Each gets the digest as its cache prefix, the
  rolling summary, the workflow's objective and its own task (plus the
  bundles of the tasks it `depends_on`), and only the attachments its task
  names (all of them when no task names any), and reports a typed outcome —
  completed, empty, truncated, refused, failed, cancelled, or reused from a
  paused run. A context overflow is not retried.
- **Roles are a table** (`config.DEFAULT_ROLE_PROFILES`, overridable as
  `role_profiles:` in config.yaml): model tier and effort for `author`,
  `specialist`, `cite_check`, `summariser`, `title` and `deck`. By default the
  author runs on the chat's own model and effort, the deck on the chat's model
  at low effort, specialists on the main tier capped at medium, and the other
  three on the fast tier.
  Effort caps include `max`, and the dial applies to both providers.
- **Capability tools wait behind native tool search** (`tool_exposure.py`).
  Only the harness's operational tools are loaded on every request, as in
  Codex: `read_document`, `load_skill`, `read_skill_file`, `load_pack`,
  `run_workflow`, project memory, hosted web search (and a specialist's own
  skill and audit tools). Every functional tool — legal search and the local
  corpus, deadline arithmetic, Word / PDF / spreadsheet / slide tools,
  diagrams, procedures, per-jurisdiction connectors, pack sources — is marked
  `defer_loading` and found by the provider's own tool search, however often
  it is used: OpenAI `{"type": "tool_search"}` with the
  deferred functions grouped into described `namespace` tools; Anthropic
  `tool_search_tool_bm25_20251119` (deferred tools last, the cache breakpoint
  on the last loaded tool). The `tools` array never changes, so the cached
  prefix survives; the author prompt lists the groups ("Tools on demand").
  Measured 2026-10-14 on the real surface with a one-word prompt: OpenAI
  17,865 → 3,007 input tokens, Anthropic (count_tokens) 37,423 → 4,971.
  Probed on every offered model; there is no fallback path — a new model must
  pass the same probe before it is offered. A new tool joins a group in
  `tool_exposure._GROUP_OF` (connectors group by jurisdiction, and a pack's
  source tools by pack, automatically); `DIRECT_TOOLS` is for operational
  tools only, never for a functional one because it is popular. Keep
  descriptions to what the model needs to choose and call the tool — no
  rationale or history.
- **Packs are pinned or loaded by the author — never keyword-detected.**
  A pinned pack (settings, project, `--domain-pack`) is inlined in the author
  prompt from the start. Every other installed pack is listed by
  name only (`tools/pack_tools.pack_catalog_block` — "`aviation` — Aviation
  Law", no regulator names), and the author calls `load_pack(name)` when it
  judges the request falls in the domain: the result carries the playbook,
  research sources, the methods it overlays and its source tools; the turn's
  settings gain the pack, so later `load_skill` overlays and `run_workflow`
  specialists have it. The citation audit runs on the executor's settings, so
  it does not see a pack loaded mid-turn. The pack's source tools sit on the
  surface from the start, deferred like every other source tool and found
  through the provider's tool search; there is no fallback. Keyword pack
  detection was deleted on 2026-10-14: it classified intent by keyword, and
  each toggle rewrote the cached system prompt. Generic prompts name no
  domain's regulators or tools
  (`test_no_aviation_tokens_in_prompts_when_the_pack_is_inactive`).

## Answer verification and durable runs

- **A substantive legal answer is audited before the reader sees it.**
  `WorkflowExecutor._verified_synthesis` buffers the author's answer, runs the
  citation audit when the answer states law (`_should_run_cite_check`,
  decided from the answer itself), repairs what the audit flagged, and
  only then streams the prose; with the audit on, every answer is held until
  written. Research progress, tool calls and phases stream live throughout, so
  the UI still shows work happening; the `citation_audit_started` event
  drives an "auditing" phase. Gated by `verify_before_reveal`
  (default on) **and** `enable_cite_check` — note `config.yaml` currently ships
  `enable_cite_check: false`, which disables both the audit and the repair.
- **Repair is bounded and targeted.** `max_repair_rounds` (default 1) rounds,
  each one synthesis turn plus one re-audit. `_repair_prompt` passes the draft
  plus the critical/major findings and requires: fix only what was flagged,
  re-verify with tools, never invent a replacement citation, and write
  `pinpoint unavailable` / `无法获得具体条款` where that is the truth. An
  intermediate failing audit is not emitted as the answer's verdict; only the
  final one is.
- **Every run is recorded under `state/runs/<run_id>/`** (`legal_helper/runs.py`):
  `artifacts/NN-<phase>.md` per phase (a workflow's task list, each research
  bundle, the draft, each audit, each repair) plus `snapshot.json` and
  `events.jsonl`. A cancel, a provider outage or a client that disconnects
  mid-turn finishes the run as `paused`, not lost; passing that `run_id` as
  `resume_run_id` reuses the completed bundles instead of paying for that
  research again. `GET /api/runs` lists them. Every write is
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
- **No loop forces a repair.** An enforced lint → wireframe → pixel repair
  loop was built and reverted; there is no setting for one, so do not cite
  one. If such a loop returns, bound it with `max_repair_rounds` so it shares
  the audit's cost dial, and forbid deleting content to make a slide fit —
  cutting the substance to satisfy a layout check trades away the wrong thing.

## External procedural skills

- **Two kinds of skill, distinguished by where the directory is.** An in-tree
  skill under `legal_helper/skills/` is legal methodology: the author loads its
  `SKILL.md` body with `load_skill` (bundle-contract sections stripped), and a
  specialist holding it gets the body inlined with the general playbook and
  the specialist bundle contract. A skill discovered under an external root is
  a *procedure*: it carries its own workflow, references, artifact contracts and gate scripts,
  and it is **driven**, not inlined. `ppt-master` is the motivating case, and
  `create_deck` is how it is driven (see **Document, deck and diagram
  production**). Any other installed procedure — a GB/T 9704 公文 generator
  with its red head, say — is driven by `tools/procedures.run_procedure` the
  same way: a sub-agent under the host contract in `<outputs>/_procedures/`,
  only the files it names under `## Deliverables` copied out. That tool is on
  the author's surface only while such a procedure is installed.
- **Discovery is `skills.skill_roots()`, and this package is always first.**
  It cannot be configured away — the in-tree legal skills are the product, and
  a mistyped `LEGAL_HELPER_SKILL_ROOTS` must not be able to delete them. That
  variable replaces the *external* roots only (default `~/.claude/skills`), and
  an earlier root shadows a later one so a third party cannot silently replace
  a methodology. **Nothing hardcodes a skill name**: two hand-kept allowlists
  once drifted apart and left `flowchart` and `docx-redline` on disk and
  unreachable. The author's method list and `run_workflow`'s method enum are
  the discovered in-tree methods (cite-check excluded; the enum adds
  `general-answer`); external procedures are never workflow tasks.
- **Discovered directories are resolved.** The install that keeps `.git` is a
  symlink into the clone's `skills/<name>/`, and every containment check
  compares resolved paths: an unresolved link made `run_skill_script` refuse
  each of the skill's own scripts.
- **Four tools make driving possible** (`tools/skill_runtime.py`), and the
  containment is the point. `write_text_file` and `edit_text_file` write only
  under `outputs_dir`, **not** `project_root`, which holds the source tree. `run_skill_script` runs
  only a `.py` (this Python) or `.mjs`/`.js`/`.cjs` (Node via
  `writers._node.find_node`) that resolves *inside* a discovered skill's
  directory — the skill directory itself is resolved first, so a staged skill
  that is a symlink into a checkout works and a script linked out of it does
  not — with `argv` as a list so there is no shell to inject into; a non-zero
  exit is returned as data, because that is how a skill's own gate reaches the
  model. mizzlelover/gongwen-gbt9704-skill (MIT, `.mjs` generator and
  verifier) is the Node case; it is staged as an intact clone, not vendored.
  `list_skill_dir` exists so reference paths are read rather than guessed.
  The trust boundary is the skill root: a script under a configured root runs
  with the harness's privileges, so never point a root at a directory the
  model can write to — the two tools compose into arbitrary code execution.
- **An external skill gets a host runtime contract, never the playbook.**
  `agent._external_skill_prompt` supplies the resolved absolute `SKILL_DIR`
  (such a skill is typically told never to guess it), the call mapping, and
  what does not hold here: `SKILL_DIR` is read-only, so a command whose default
  output lies inside the installation is pointed at the work directory;
  `run_skill_script` sets no environment variables and waits at most 900 s;
  `read_document` returns ~12K tokens per call, so a long reference is paged to
  its end; there is no interactive channel, so a blocking user gate runs under
  the skill's own explicit-delegation provision and the decision is reported
  under `## Decisions under delegation` rather than fabricated. ppt-master
  skips its confirmation only on the *user's* explicit delegation, so the deck
  request itself says the user delegates every confirmation stage: stated only
  in the host contract, luna at low effort stopped at the gate and asked for a
  reply that cannot come; and there are
  no background processes, so a preview server is never started. Handing it
  the PRC playbook would be a conflicting instruction, not extra context.
- **A procedure must never be round-capped.** It spends rounds reading its own
  workflow, authoring one artifact per page, running its gate, repairing, then
  exporting; an 8-round research cap could not reach its export step at
  all. `external_skill_max_iterations` and `sub_agent_max_iterations` both
  default to 0 (unlimited) — under-budgeting a loop does not make it cheaper,
  it makes it fail after paying for most of the work.
- **The in-tree authoring contract does not apply to external skills.** Under
  100 lines, a not-legal-advice line, a reference to the general playbook —
  those are contracts about methodology we maintain. Contract tests iterate
  `skills.internal_skill_names()`; `list_skills()` reports every discovered
  skill and flags which are external.
- **Never vendor a large external skill.** `ppt-master`'s skill folder is
  83 MB / 13k files and its `attribution_guard.py` fails closed on any
  modification, so it must remain an intact official distribution at a
  configured root.

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

- Both `claude-opus-5-5` and `gpt-6.1-sol` (the primaries, and the only high-effort models offered) must work end-to-end on every change.
- Fast/lightweight tier: `claude-sonnet-5` and `gpt-6-luna` (there is no `gpt-6.1-luna`). There is no bare `gpt-6` or `gpt-6.1` alias — always name the variant.
- **Per-model OpenAI request capabilities are tables, from live probes** (`openai_provider.OPENAI_REASONING_EFFORTS`, `OPENAI_VERBOSITY_MODELS`, `OPENAI_EXPLICIT_CACHE_MODELS`, 2026-10-07). `gpt-6.1-sol` takes low / medium / high / xhigh / max and rejects `none` / `minimal`; `gpt-6-luna` takes `none` too but not `minimal`. Omitting `reasoning` runs **both** at medium — so omitting it for `none` made every `none` luna call (chat titles among them) reason at medium. `reasoning_effort_for` sends the nearest level the model accepts (`none` → `low` on sol, `none` as-is on luna); for a model missing from the table it omits `none` / `minimal` and sends anything else as given. **Add a new model's row when you add the model.** `text.verbosity` is sent only to models in the verbosity table.
- **Retired 2026-09-22:** `claude-opus-5`, `claude-opus-4-8`, `claude-opus-4-7`, `claude-haiku-4-5`, and the whole `gpt-5.6` family (terra / sol / luna). **Retired 2026-10-07:** `gpt-6-sol`, superseded by `gpt-6.1-sol` (same $2.00/$10.00; cached input $0.10 against $0.20). The reasons are in `config.RETIRED_MODELS`. They stay priced in `usage.DEFAULT_PRICING` and windowed in `context._WINDOWS` so historical ledger months still replay.
- **Anthropic thinking is adaptive, never `budget_tokens`.** Every offered Claude model rejects `thinking: {type: "enabled"}` with a 400. `anthropic_provider._thinking_kwargs` maps the shared effort dial onto `thinking: {type: "adaptive", display: "summarized"}` + `output_config.effort`, on `tool_runner` as well as `stream`. `none` maps to `low` effort because Opus 5.5 cannot disable thinking at all. `display` must be set: Opus 5.5 and Sonnet 5 default to `omitted`, which streams empty thinking text.
- **A response's independent read-only tool calls run side by side**, on both providers (`providers/tool_exec.py`; the Anthropic runner's `_generate_tool_call_response` is replaced to get there). What may overlap is declared in `tool_policy` — `CONCURRENT_SAFE_TOOLS`, every connector in `connectors.REGISTRY`, tools of the `READ_ONLY_MCP_SERVERS` — never inferred from `is_mutating`: renders mutate nothing yet share PDFium, which is not thread-safe, so they sit in a `CONCURRENCY_GROUPS` group whose members never overlap (process-wide, across sub-agents too). Anything undeclared is a barrier that runs alone, in order. Started/finished events still go out in call order — the UI pairs them first-in, first-out per tool — and worker threads get a copy of the caller's contextvars, without which their events and usage attribution vanish.
- **Progress before results:** both providers emit `tool_progress` (`{tool_name, call_id, chars_so_far, agent}`, throttled to one per 250 ms per call, silent for calls that finish streaming inside that) while a call's arguments are still being written, and `hosted_tool_progress` (`status` searching / completed, plus `query` / `url` when known) when the provider reports a hosted search. Both are transient previews like `reasoning_delta`: emit them, never persist them per tick. `claude-sonnet-5` is the fast *model* but not the fast *compaction tier*: it has a 1M window and frontier-class quality, so `is_fast_tier` deliberately excludes it.
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

Every answer that states law ends with exactly one `## Sources` (or `## 资料来源`) section. Each entry must include a pinpoint marker (条款, article, section, §, paragraph, page). The author contract (`answer_contract.author_rules`) requires it and the citation audit checks it when `enable_cite_check` is on; do not bypass either. When a pinpoint genuinely cannot be obtained, write `pinpoint unavailable` / `无法获得具体条款` explicitly.

## PRC citation lookup — PKULaw MCP is the primary source

PKULaw exposes nine MCP sub-services (all share `PKULAW_API_TOKEN` via the
WSO2 `apikey` header). Use them as the **first** stop for PRC legal lookups
wherever they are on the tool list (skill specialists and the citation audit;
the author's surface leaves them off until the gateway answers again, see
**The author decides what it needs**); `flk_npc_search` / `flk_npc_fetch` are
the free public fallback (search, status and version history only — no
statute body text).

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

User-facing input/output language follows the user's language. **Internal reasoning, sub-agent tool calls, and author-to-specialist messages may use whichever language best fits the material** — pick freely as the work demands (e.g., 中文 when reasoning over PRC statutes and 案例, English for US/EU sources); do not force mid-pipeline translation. Quote primary sources in their original language; add a translation when the user-facing language differs.

## Common commands

```
conda activate llm
python -m legal_helper run --task "Review this NDA" --input examples/general/sample_commercial_nda.md   # --task is the user's message
python -m legal_helper run --domain-pack aviation --task "Review this aircraft lease" --input examples/aviation/sample_aircraft_lease.md
python -m legal_helper chat
python -m legal_helper serve --port 8010
npm run build                            # the web UI, into legal_helper/webui_dist/ (what serve serves)
python -m legal_helper.rag.ingest --collection aviation --source caac_ccar
python -m pytest -q                      # always `python -m`: bare `pytest` resolves
                                         # to base's interpreter and dies at collection
python -m pytest -q tests/test_decks.py tests/test_diagrams.py  # the deck path and diagram pictures
dot -V && d2 --version                   # diagram engines; d2 is optional
python -c "from legal_helper.skills import list_skills; print([s['name'] for s in list_skills()])"
```

## Testing

- `python -m pytest -q tests/test_skill_anatomy.py` — anatomy contract: each SKILL.md <100 lines, frontmatter valid, referenced files exist.
- `python -m pytest -q tests/test_provider_parity.py` — Anthropic and OpenAI see identical tool surface and equivalent results.
- `python -m pytest -q tests/test_tool_budget.py tests/test_context.py tests/test_turn_compaction.py` — per-result budgets, the turn ceiling, and in-turn clearing (no caps; server-side on Anthropic).
- `python -m pytest -q tests/test_tool_exec.py tests/test_stream_release.py` — concurrent tool calls (order, barriers, groups, contextvars) and the stream closing at `done` with housekeeping behind it.
- `python -m pytest -q tests/test_author_loop.py tests/test_verified_synthesis.py tests/test_runs.py` — one author stream per turn, methods on demand, audit-then-reveal, and the durable run record.
- `python -m pytest -q tests/test_decks.py tests/test_external_skill_install.py` — `create_deck` on a fake ppt-master run: the self-contained task, the `_decks/` workspace, export verification, progress events, and that it is on every authoring surface; and a symlinked skill install driving its own scripts.
- `python -m pytest -q tests/test_procedures.py` — `run_procedure` on a fake procedure run: only named deliverables reach the chat, never over a delivered file, and the tool is offered only while a non-deck procedure is installed.
- `python -m pytest -q tests/test_diagrams.py tests/test_document_quality.py tests/test_docx_layout.py` — Mermaid parsed and drawn by Graphviz, the output linter (groups, table overflow), and the DOCX table/indent contract.
- `python -m pytest -q tests/test_skill_runtime.py` — skill discovery and, more importantly, the containment of `write_text_file` / `run_skill_script`: escapes out of `outputs/` and out of a skill directory are asserted, not assumed.
- `python -m pytest -q tests/test_docx_prc_output.py tests/test_pdf_prc_output.py tests/test_xlsx_writer.py tests/test_document_lint_prc.py tests/test_document_tools_prc.py tests/test_skill_resources_and_gongwen.py` — PRC page/footer, list restarts, disclaimer-in-properties, the Markdown front end, templates, GB/T 9704 (checked by the staged gongwen verifier when present), stored formula values, and the Word/PDF/XLSX lint.
- `npm run typecheck` and `npm test` (vitest) — the UI's types and the SSE translator.
- The suites were not re-run after the 2026-10-14 one-author-loop rewrite: re-measure the pass/fail baseline before calling any failure a regression.
- Live smoke once per provider per major change: `MODEL_PROVIDER=anthropic` then `MODEL_PROVIDER=openai`.
