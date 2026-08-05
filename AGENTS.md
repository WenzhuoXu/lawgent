# Operational notes

These are operator-level notes for running and re-running the agent in
this repository. Project-level conventions for Claude Code itself live in
`CLAUDE.md`.

## Environment

- Run **everything** inside the `llm` conda env. Python, Node, and all
  MCP installs live there.
- `conda run -n llm <command>` is the convenient form for one-shot
  invocations:
  - `conda run -n llm python -m pytest -q tests/`
  - `conda run -n llm python -m legal_helper serve --port 8010`
  - `conda run -n llm npm run build`

## Domain packs

- List installed packs: `GET /domains` from the running server, or
  `python -c "from legal_helper.domains import available_packs; print(available_packs())"`.
- Activate per run: `--domain-pack aviation` on `run`, `chat`, or `serve`.
- Activate persistently: set `active_domain_packs: [aviation]` in
  `config.yaml` or `LEGAL_HELPER_ACTIVE_DOMAIN_PACKS=aviation` in the
  environment.
- Add a new pack: create `legal_helper/domains/<name>/pack.yaml` +
  `playbook.md` + `overlays/<skill>.md` files. The pack is auto-discovered
  on next process start.

## MCP servers

`.mcp.json` is the registry. External MCPs are installed into the `llm`
env (see README); the in-tree `ccar_aviation` MCP starts from
`python -m legal_helper.mcp.servers.ccar_aviation`. The PKULaw MCP is
remote HTTP — set `PKULAW_API_TOKEN` in `.env`.

To smoke an MCP from the command line:

```bash
conda run -n llm python -m legal_helper.mcp.servers.ccar_aviation
# (drives stdin/stdout MCP protocol; usually used via the agent runtime)
```

## RAG ingestion

```bash
conda run -n llm python -m legal_helper.rag.ingest --collection general --seed
conda run -n llm python -m legal_helper.rag.ingest --collection aviation --seed
conda run -n llm python -m legal_helper.rag.ingest --collection user --path /path/to/corpus
```

First call downloads bge-m3 weights (~2 GB) into the HF cache; subsequent
calls are local. Qdrant data lives under `state/qdrant/`.

## Background server restart

`conda run -n llm ...` works for foreground commands, but detached
`nohup conda run ...` can exit silently in this project. To restart the
production web service in the background:

1. Stop old app processes on ports 8010 / 8011 / 5174.
2. Rebuild the frontend: `conda run -n llm npm run build`.
3. Start the backend with the env Python directly:

   ```bash
   setsid "$(conda info --base)/envs/llm/bin/python" -u -m legal_helper serve \
       --host 0.0.0.0 --port 8010 </dev/null > logs/server-8010.log 2>&1 &
   ```

4. Verify:
   - `ss -ltnp | rg ':8010'`
   - `curl -s -o /tmp/legal_helper_runtime_options.json -w '%{http_code}' http://127.0.0.1:8010/api/runtime-options`
   - `curl -s http://127.0.0.1:8010/domains | jq`
   - `curl -s http://127.0.0.1:8010/jurisdictions | jq`
