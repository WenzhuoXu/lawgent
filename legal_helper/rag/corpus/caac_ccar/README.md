# caac_ccar corpus

CAAC public aviation laws/regulations and CCAR department rules staged from
`caac.gov.cn` for the `caac_ccar` RAG collection.

## Refresh

```bash
conda run -n llm python scripts/fetch_caac_corpus.py
conda run -n llm python -m legal_helper.rag.ingest --collection caac_ccar --seed
```

The fetcher writes cleaned Markdown source pages under `documents/`, downloads
official PDF attachments under `attachments/`, and records per-file metadata in
`manifest.yaml`. Re-running is idempotent; add `--force` to re-download PDFs.

## Sources

- CAAC 信息公开 / 法律法规: `https://www.caac.gov.cn/XXGK/XXGK/FLFG/`
- CAAC 信息公开 / 民航规章: `https://www.caac.gov.cn/XXGK/XXGK/MHGZ/`

## Citation Pinpoint

Chunks carry `title`, `legal_category`, `document_number`, `ccar_part`,
`validity`, `publish_date`, and `source_url` metadata where CAAC provides it.
For CCAR questions, filter retrieval with `{"corpus_category": "ccar"}`.
