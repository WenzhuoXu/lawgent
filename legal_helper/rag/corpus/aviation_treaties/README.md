# aviation_treaties corpus

Source documents for the `aviation_treaties` RAG collection. The actual
PDFs are **gitignored** (binaries, sourced from third parties); only
`manifest.yaml` is tracked.

## Refresh

```bash
conda activate llm
bash scripts/fetch_aviation_corpus.sh
python -m legal_helper.rag.ingest --collection aviation_treaties --seed
```

The fetch script is idempotent (skips files already present); ingest
upserts so re-running is cheap.

## Sources

See `manifest.yaml` for the canonical URL per file. Primary depositaries:
- **ICAO Treaty Collection** — Chicago, Tokyo, Hague, Montreal 1971/1988,
  Beijing 2010 + Protocol, MC99.
- **UN Treaty Series** — Warsaw 1929 (Vol 137), Montreal 1971 (Vol 974).
- **IATA-hosted** — MC99 canonical free PDF.
- **UNIDROIT** — Cape Town Convention + Aircraft Protocol.

Chinese versions: ICAO publishes 中文 editions for most post-1947
treaties. The fetch script tries the conventional URL pattern
(`<doc>_zh.pdf`) and silently skips 404s — refresh manually as needed.

## Citation pinpoint

Every chunk carries `treaty_name`, `language`, `cite_prefix`,
`authentic_text` metadata. Filter at retrieval time:

```python
retrieve_legal(
    query="captain authority unruly passenger",
    collection="aviation_treaties",
    filters_json='{"treaty_name": "Tokyo Convention"}',
)
```
