# Single agent loop: the chat's model answers every turn; the workflow is a tool

Status: implemented, 2026-10-14. Replaced the router → (decomposer) →
author/integrator pipeline in `legal_helper/workflow.py`. The sections below
describe what was built; where the build departed from the first draft of this
plan, the text says so.

## 1. Why

On 2026-10-14 the harness answered an LSAT necessary-assumption question
(Barnes, PT answer C) with B. The chat was set to gpt-6.1-sol / medium; sol never
saw the question. The router — gpt-6-luna, effort low, no tools, writing inside a
JSON routing field — chose `direct_answer` and wrote the reply itself. The
message was then stamped with the chat's model, because the direct path emits no
usage, so the UI showed "gpt-6.1-sol" on luna's answer.

Measured from the run records (2026-10-08 → 2026-10-14, 52 routed turns):

| | turns |
|---|---|
| answered by the router (luna/low) itself | 25 (48%) |
| one sol author | 26 |
| multi-agent decomposition | **0** |

- Every turn waited p50 6.0 s / p90 9.3 s for the router before any work began.
- All chats: 152 router-written replies, 78 over 400 characters (email drafts,
  outlines, LSAT explanations, a 2.7K-character case outline).
- Replay of the Barnes turn, 6 runs per arm: sol/medium C 6/6; luna/low C 4/6;
  luna/high C 5/6. The router's only guard on a direct answer is a statute-cite
  regex, so a reasoning answer always passes.
- The router saves ~$0.0005 per turn. It buys almost nothing and costs latency,
  a wrong-answer path, and mis-attribution.

## 2. What the reference harnesses do

Studied from source: openai/codex (Apache-2.0), zai-org/ZCode (Apache-2.0),
anthropics/claude-agent-sdk-python (MIT) and the public anthropics/claude-code
repo (CHANGELOG, plugins — its core is not open source, so conventions only).

| | Claude Code | Codex | ZCode |
|---|---|---|---|
| Who answers a turn | the user's model, one loop | the user's model, one loop | the user's model, one loop |
| Cheap model used for | titles, summaries, fetched-page digests, gating checks | titles (luna), memory extraction | titles, web-fetch digests (same model, lowest effort) |
| Router / classifier before the turn | none | none | none |
| Sub-agents | `Agent` tool, model decides; events carry `parent_tool_use_id` | `spawn_agent` / `wait_agent` / `close_agent`; depth 1, ≤3 children (v2) | `Agent` tool; depth 1 by construction; result + usage footer |
| Multi-agent workflow | `Workflow` tool, explicit opt-in | `ExplicitRequestOnly` mode by default | `CreateWorkflow`: mandatory when the user asks for a workflow, otherwise not started |
| Detecting the opt-in | moved from the bare word "workflow" to explicit phrases after misfires | the model judges | the model judges, from the tool description |
| Skills | name + description listed; `Skill` tool loads the body | catalog in a developer message; model reads SKILL.md | reminder-injected list; `Skill` tool returns `<skill_content>` |

What we took: no pre-turn routing; the workflow as a tool with depth 1 and a
3-task cap; "mandatory when the user asks, any language" in the tool
description and the prompt (ZCode's wording), with the decision left to the
model; "depth or thoroughness alone is not a reason to delegate" (Codex);
`parent_tool_use_id`-style event tagging (Agent SDK); a method list plus a
`load_skill` tool returning `<skill_content>`; and, following Codex, a small
always-loaded core with every functional tool behind the provider's tool
search.

## 3. Architecture

```
user turn
  └─ author loop — the chat's model, the user's effort, one tool loop
       system  : one static prompt (role · conduct · research conduct ·
                 writing rules · deliverable rules · playbook · method list ·
                 pack list · tool-search groups · workflow guidance ·
                 pinned-pack addendum)                     ← cached
       request : chat digest (cached prefix) · summary · dated tail ·
                 [user-picked method] · the message
       tools   : loaded — hosted search/fetch · read_document · load_skill ·
                 read_skill_file · load_pack · run_workflow · project tools
                 deferred, found by native tool search — document tools ·
                 legal connectors · retrieve_legal · legal_source_search ·
                 compute_period · pack source tools
         └─ run_workflow(objective, tasks[1..3])           ← a tool, not a stage
              specialists in parallel (deps, concurrency cap, retry,
              rate-limit back-off, resume, run record), each a SkillAgent
              or the general specialist on the specialist role; bundles +
              gaps return as the tool result; the author integrates in the
              same loop
  └─ citation audit (when enabled) → repair → reveal       ← unchanged
  └─ housekeeping on the fast tier: rolling summary, title ← unchanged
```

Principles taken from the references:

1. **No pre-routing.** The user-selected model handles every turn, greetings
   included. The fast tier keeps only side work: title, rolling summary.
2. **The model chooses when to delegate.** `run_workflow` is offered on every
   turn with guidance on when it pays (several distinct issues, each needing
   sustained research in a different body of law or evidence base) and when it
   does not (anything answerable directly or with a few lookups).
3. **The model decides when the user has asked for the workflow.** No keyword
   or regex detection. The `run_workflow` description and the system prompt
   say that a request for a workflow or for several agents — in any language
   and any phrasing (多智能体, 工作流, sub-agents, …) — makes the tool
   mandatory, sized to the task (ZCode's `CreateWorkflow` rule). A business
   "workflow" (审批工作流程) or an agent in agency law is not such a request;
   the model tells them apart.
4. **Skills load on demand.** The system prompt lists each in-tree method by
   name and one line; `load_skill(name)` returns the method (bundle-contract
   sections stripped, active-pack overlay appended, file list), and
   `read_skill_file(name, path)` opens its references/resources. A method the
   user picked in the UI is inlined in the tail, as before.
5. **Depth 1, at most three children.** Specialists never get `run_workflow`;
   one call starts at most `max_workflow_tasks` (3) of them.
6. **Sub-agent events name their parent.** Every event a specialist emits —
   lifecycle, its own tool calls — carries `task_id` and `parent_call_id` (the
   `run_workflow` call), the `parent_tool_use_id` convention of the Agent SDK.
7. **Packs are pinned or loaded by the author, never detected.** A pinned pack
   is inlined in the system prompt; every other pack is listed by name, and
   the author calls `load_pack` when it judges the request falls in the
   domain.
8. **Functional tools wait behind native tool search, with no fallback.** Only
   the operational tools are loaded on every request; the rest are found by
   each provider's own tool search and called by name.

## 4. Backend changes

### `legal_helper/workflow.py`
- `WorkflowExecutor.stream`: no `plan()`. It builds the context parts, the turn
  language and one author request; opens a `TurnContext` the tools read; runs
  `_verified_synthesis` with the one author prompt and tool surface
  (`author_tool_surface`); finishes. `clarify_rounds_used` is gone.
- Deleted: `plan`, `_routing_prompt`, `_run_planner`, `_agent_workflow_plan`,
  `_parse_plan_*`, `_resolve_clarify`, `_normalize_plan`, `_single_task_plan`,
  `heuristic_plan`, `_simple_direct_response`, `_routing_view`, the router and
  decomposer prompts, `_DIRECT_LEGAL_SUBSTANCE_RE`, the clarify gates, the
  coverage-merge helpers, the attachment override, `_integration_prompt`, and
  the general-only system prompt path.
- `_stream_tasks` moved to `legal_helper/multi_agent.py` as `run_specialists`,
  emitting through the event sink (and a transient channel for previews)
  instead of yielding; its behaviour (deps, concurrency cap, retry, rate-limit
  back-off, typed outcomes, fail-soft, run record) is kept.
- The plan argument threaded through the audit is gone. The audit gate reads
  the answer (`_should_run_cite_check`); the audit's stakes read the answer and
  the methods the turn used (`TurnContext.methods_used`, noted by `load_skill`
  and `run_workflow`); `_verify_before_reveal` reads settings only. (The draft
  proposed a separate `TurnInfo`; `TurnContext` carries it.)
- `done` carries the author's provider and model; the server stamps those.

### `legal_helper/multi_agent.py`
- `run_workflow(objective, tasks)`: tasks are `{id?, title, method, task,
  depends_on?}`, 1..`max_workflow_tasks` (default 3, Codex's child cap). An
  unknown method, too many tasks, a dependency on a task that is not there, or
  a dependency cycle is an error that says how to fix it. `run_workflow_tool`
  builds the tool with the cap and the method enum (in-tree methods without
  `cite-check`, plus `general-answer`) filled in, as an explicit inline JSON
  schema (no `$ref`) so both providers read it the same way. The draft's
  separate `tools/workflow_tool.py` was not needed.
- Result: per-task outcome line, the bundles, the open gaps (failed / empty /
  truncated / refused tasks, each bundle's `Out of scope` items). Exempt from
  the result budget (it is the integration input, as the integrator's prompt was).
- Events (persisted unless marked): `workflow_started` {parent_call_id,
  objective, tasks}, `agent_task_started`, `agent_delta` (transient),
  `agent_task_finished` {outcome, tool_count, total_tokens, duration_ms,
  sources}, `agent_task_failed` / `_retried` / `_rate_limited`,
  `workflow_agent_concurrency_reduced` / `_limited`,
  `workflow_agent_rate_limit_degraded`, `workflow_degraded`,
  `workflow_tasks_reused`, `workflow_task_rejected`, `workflow_finished`
  {completed, failed, duration_ms, cancelled}; all carry `parent_call_id`.
- Resume: a paused run's bundles are reused for tasks with the same id and
  method; the run record keeps the task list.

### Per-turn state — `legal_helper/turn_context.py`
- `TurnContext` holds what a tool needs from the turn rather than from its
  arguments: the settings (which `load_pack` widens), the answer language, the
  digest, the attachments, the cancel event, the run record, the resumable
  bundles and the methods used. The executor opens it for the loop; worker
  threads see it because both providers run tools under a copy of the
  caller's context.

### Skill tools — `legal_helper/tools/skill_resources.py`
- `load_skill(name)`, `read_skill_file(name, path)`. The old
  `list_skill_references` / `read_skill_reference` stay for specialists, whose
  agent name is the skill. (The author was given them before, but they resolve
  the skill from the agent name, which is `orchestrator` there — they could not
  have worked.)

### Domain packs — `legal_helper/tools/pack_tools.py`
- Not in the first draft; built in the same change. Packs pinned in settings,
  a project or `--domain-pack` are inlined in the system prompt as before.
  Every other installed pack is listed by name only (`pack_catalog_block`), so
  no domain's regulators or tools reach a turn that never loads it.
- `load_pack(name)` returns the pack's playbook, research sources, the methods
  it overlays and its source tools, and adds the pack to the turn's settings,
  so later `load_skill` overlays and `run_workflow` specialists have it.
- The pack's source tools sit on the tool list from the start, deferred like
  every other source tool: the list is fixed for the whole loop. There is no
  proxy tool; an interim `call_pack_tool` proxy was removed.
- `domains/detect.py` (marker matching on the message and the last three
  turns) and its tests are deleted: it was the same keyword classification as
  the router's, and each toggle rewrote the cached system prompt.

### Tool exposure — `legal_helper/tool_exposure.py`
- Not in the first draft; built in the same change. `DIRECT_TOOLS` names the
  operational tools loaded on every request (`read_document`, `load_skill`,
  `read_skill_file`, `load_pack`, `run_workflow`, project memory, and a
  specialist's own skill and audit tools); hosted web search stays loaded too.
  Every other function tool is marked `defer_loading` and grouped (word, pdf,
  spreadsheet, slides, files, legal_research, deadlines, prc_law, us_law,
  eu_law, other_law, one group per pack).
- OpenAI: a `{"type": "tool_search"}` tool, the deferred functions grouped
  into described `namespace` tools. Anthropic: `tool_search_tool_bm25_20251119`,
  deferred tools last. The `tools` array never changes, so the cached prefix
  survives; the author prompt lists the groups ("Tools on demand").
- Probed live on every offered model (gpt-6.1-sol, gpt-6-luna,
  claude-opus-5-5, claude-sonnet-5): each searched, found and called a
  deferred tool in one response. There is no fallback: the interim per-model
  switch and its fallback path (`TOOL_SEARCH_MODELS`, `tool_search_supported`)
  were removed, and a new model must pass the same probe before it is offered.
- Measured on a one-word request: OpenAI 17,865 → 3,007 tool input tokens,
  Anthropic (count_tokens) 37,423 → 4,971.

### Prompts — `authoring.py`, `answer_contract.py`
- One author role for every turn: legal work by the playbook; ordinary requests
  (documents, spreadsheets, translation, reasoning questions, general
  questions) answered directly, without legal framing nobody asked for.
- Sources section: required in an answer that states law, not in every reply.
- Length: a pinned `answer_length` keeps its tier and verbosity; `auto` is
  one sentence ("fit the request …") and the provider's default verbosity.
- Clarifying: the model asks one question only when a missing fact changes the
  answer and neither a stated assumption nor a short answer per reading works.
- Disputed answers: re-derive from the source material before replying; change
  the answer only when the re-derivation supports it and say what changed.
  (General conduct, not a fix for one question.)
- Methods section (name + one line each), the pack list, the tool-search
  groups, and the workflow section (when to delegate, how to write tasks, how
  to integrate bundles — `_INTEGRATION_RULES`).

### Smaller
- `logging_setup`: `task_id_var`, `parent_call_id_var`, `tool_call_id_var`, a
  transient emitter (`transient_event_sink_var` / `emit_transient`), and the
  record fields.
- Both providers: `call_id` on `local_tool_call_started` / `_finished`, and
  `tool_call_id_var` set while a tool runs.
- `config.py`: roles `router`, `decomposer`, `integrator` removed;
  `max_workflow_tasks`. `config.yaml` comment updated.
- `chat_store.consecutive_clarify_rounds` and its server call removed;
  `chat_models.WorkflowPlan` / `PlanIssue` removed.
- `cli.py`: renders workflow lifecycle from the event sink.
- `tool_policy`: `run_workflow` is not concurrent-safe, so it runs alone as a
  barrier; no specialist surface holds it, the read-only `cite-check`
  included. `tool_budget.EXEMPT_TOOLS` += `run_workflow`.
- `citations/trajectory.py`: `workflow_task_rejected`.

## 5. Frontend changes

- `src/api/sse.ts`: router phases (`routing` / `planning`) removed; `thinking`
  on the author's first request, `workflow` while `run_workflow` runs. Tool
  parts are paired with provider `call_id`s; any event carrying `task_id`
  becomes part of that specialist's `data-agentTask` row (its nested tool
  calls, latest action, previews) under the `run_workflow` part named by
  `parent_call_id`. An event with no parent call is not drawn as a workflow
  (the interim legacy card for pre-change runs was removed).
- `src/components/parts/WorkflowCard.tsx`: header "多智能体工作流 · N 个子智能体
  · done/total · status"; the objective; one row per specialist (status icon
  and word, "子智能体 i · title", live latest action with a shimmer, receipt
  "method · N 次工具 · tokens · duration" once done), expanding to its task
  text, nested tool calls and source chips (ZCode's single-line sub-agent row;
  the CoT steps of the design target).
- `renderMessageParts.tsx`: a `run_workflow` tool part renders as the card;
  the old plan checklist / CoT widget is gone. `toolLabels.ts`: labels for
  `run_workflow`, `load_skill`, `read_skill_file`, `load_pack`.

## 6. Tests

- Router/decomposer/clarify tests deleted (`test_workflow_planning.py`,
  `test_pack_detection.py`); the stream tests are rewritten around one author
  loop with a fake provider.
- `tests/test_author_loop.py`: no planner call on any turn (a greeting is one
  provider stream on the chat's model); `done` names the author's model; the
  request carries the dated tail, digest and message; the author prompt is
  byte-identical across turns; a picked method is inlined; a pinned length
  sets verbosity and `auto` sends none; a specialist surface never holds
  `run_workflow`; `load_skill` strips bundle sections and appends the overlay.
- `run_workflow`: task validation (unknown methods, too many tasks, missing
  dependencies, cycles), bundles returned with every event naming its call,
  the concurrency cap, rate-limit back-off, and failed specialists degrading
  to open gaps; `tests/test_runs.py` covers bundle reuse on resume.
- Open: the frontend fixture tests (`tests/fixtures/frontend/*.sse`) were
  recorded from router-era streams and need re-recording from a stream with a
  `run_workflow` call before they can assert the nesting and live ≡ rehydrated.
- Run live on gpt-6-luna through the real server: greeting; a PRC legal
  question with tools; a Word memo from that answer; a spreadsheet; an
  aviation question (`load_pack`, then the CAAC tools through search); an
  explicit workflow request (two specialists in parallel); a deadline
  (`compute_period` through search). Not in that run: the Barnes question
  with a pushback, and a request that mentions 工作流程 without opting in.
- Anthropic's tool-search path is verified by the live probe and
  `count_tokens`, not yet through a full harness turn.

## 7. Docs

CLAUDE.md sections on planning/roles/clarify/integration rewritten;
memories updated (routing, complexity dial). README, PLAN.md and AGENTS.md
describe the one author loop, `run_workflow`, `load_skill`, `load_pack` and
native tool search.
