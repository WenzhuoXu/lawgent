# icao_doc corpus

Freely-available subset of ICAO Docs. The 19 ICAO Annexes and Doc 9284
(Technical Instructions for the Safe Transport of Dangerous Goods by Air)
are **paid** and not redistributed here. If your organisation has
licensed copies, drop them into `en/` or `zh/` and add a matching entry
to `manifest.yaml` — ingest picks them up on next `--seed` run.

## Refresh

```bash
conda activate llm
bash scripts/fetch_aviation_corpus.sh
python -m legal_helper.rag.ingest --collection icao_doc --seed
```
