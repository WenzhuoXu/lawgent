- **US aviation rules:** `faa_title14_search` for 14 CFR text; `drs_search`
  (FAA documents via the Federal Register) for ADs, ACs and SAFOs — follow a
  hit's `html_url` with `fetch_url_to_artifact`, since drs.faa.gov itself does
  not serve readable pages.
- **EU aviation rules:** `easa_ad_search` + `easa_ad_fetch` for EASA ADs,
  `easa_ear_index` for the Easy Access Rules.
- **PRC civil aviation:** PKULaw tools (when present) for 民航 statutes, CCAR
  规章 and 规范性文件; `caac_local_search` / `caac_local_fetch` over the staged
  CAAC corpus.
- **Treaties and ICAO documents:** the `aviation_treaties` and `icao_doc`
  retrieval collections (Chicago, Warsaw/Hague, MC99, Tokyo, Hague, Beijing,
  Cape Town and its Aircraft Protocol; the freely available ICAO Docs) — then
  confirm the state's participation and any filed differences.
- **Ethiopian civil aviation:** `ecaa_index` / `ecaa_fetch` (ECAA pages) are a
  curated starting point, not the record; the binding text is the proclamation
  or directive itself.
