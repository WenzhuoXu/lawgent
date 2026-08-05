# Monthly legal-AI harness review (recurring task)

You are running as a scheduled monthly job inside the `legal_helper` repo.
Read `CLAUDE.md` first and respect all of its contracts. This is a
**report-only** task: do not modify code; the only files you write live under
`outputs/harness_reviews/`.

## 1. External sweep — what moved this month

Use WebSearch/WebFetch. Prefer material from the last ~6 weeks. Cover:

1. **Open-source legal AI harnesses/frameworks** — new projects or major
   releases for agentic legal research, contract review/drafting; harness
   architecture ideas worth adopting.
2. **Anthropic ecosystem** — Agent Skills (especially legal skills), Claude
   Agent SDK features (memory, compaction, context editing, subagents), new
   API capabilities relevant to this harness (new tool versions, caching,
   batch pricing), MCP registry entries for law.
3. **Legal MCP servers & data connectors** — US (CourtListener/RECAP, GovInfo,
   eCFR, Federal Register), EU (EUR-Lex), PRC (PKULaw, 国家法律法规数据库,
   人民法院案例库), aviation sources.
4. **Accuracy & evals** — LegalBench/LegalAgentBench/LawBench/LexEval updates,
   citation-verification and hallucination research, eyecite releases.
5. **Legal RAG/retrieval** — embedding models beating bge-m3 on zh+en legal
   text, rerankers, structure-aware chunking, Qdrant releases.
6. **PRC legal AI ecosystem** — PKULaw AI/API changes, 通义法睿/法信,
   regulation of AI legal services.

Only report findings whose source URL you actually opened. Drop anything you
cannot verify.

## 2. Internal audit — does the framework have room to use it

Cross-check findings against the repo (`legal_helper/`): agent.py,
workflow.py, context.py, projects.py, connectors/, mcp/, citations/, rag/,
skills/, domains/, providers/, server.py. Identify concrete gaps (with
file:line evidence) in accuracy, capability, and efficiency. Also re-run
`pytest -q` (inside `conda run -n llm`) and note any newly failing guardrails.

## 3. Cost-center check

Read `state/usage/usage-<current-month>.jsonl` (and the prior month's file)
and summarize month-over-month token usage, cached-token share, and estimated
cost by model (the aggregation logic lives in `legal_helper/usage.py` —
`python -c "from legal_helper.usage import month_summary; print(month_summary())"`).
Flag anomalies (cache hit-rate collapse, cost spikes).

## 4. Report

Read the most recent prior report in `outputs/harness_reviews/` (if any) and
carry forward its open proposals with status (adopted / still open / obsolete).
Then write `outputs/harness_reviews/<YYYY-MM>-harness-review.md` containing:

- Executive summary (5-10 sentences).
- Verified external findings with source URLs.
- Internal gaps with file:line evidence.
- 8-15 ranked upgrade proposals: category (accuracy/capability/efficiency),
  priority (P0-P2), effort (S/M/L), implementation sketch naming real repo
  paths.
- Cost-center month summary and anomalies.
- Status of last month's proposals.
