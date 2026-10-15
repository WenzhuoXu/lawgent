/**
 * Human-readable tool labels (bilingual). The UI never shows raw snake_case
 * tool names; an unlisted tool falls back to its name in title case.
 */

type LabelEntry = { verb: string; running: string };

export const TOOL_LABELS: Record<string, LabelEntry> = {
  fetch_url_to_artifact: { verb: "Downloaded file", running: "Downloading file…" },
  read_document: { verb: "Read document", running: "Reading document…" },
  write_pdf: { verb: "Generated PDF", running: "Generating PDF…" },
  write_docx: { verb: "Generated DOCX", running: "Generating DOCX…" },
  create_deck: { verb: "Built presentation (ppt-master)", running: "Building presentation (ppt-master)…" },
  run_workflow: { verb: "多智能体工作流 / Multi-agent workflow", running: "多智能体工作流运行中… / Running workflow…" },
  load_skill: { verb: "Loaded method", running: "Loading method…" },
  read_skill_file: { verb: "Read method reference", running: "Reading method reference…" },
  load_pack: { verb: "Loaded domain pack", running: "Loading domain pack…" },
  tool_search_call: { verb: "Searched tools", running: "Searching tools…" },
  tool_search_tool_bm25: { verb: "Searched tools", running: "Searching tools…" },
  tool_search_tool_result: { verb: "Tool search results", running: "Reading tool search results…" },
  run_procedure: { verb: "Ran document procedure", running: "Running document procedure…" },
  redline_docx: { verb: "Wrote tracked changes", running: "Writing tracked changes…" },
  compare_docx: { verb: "Compared documents", running: "Comparing documents…" },
  compute_period: { verb: "Computed deadline", running: "Computing deadline…" },
  render_flowchart_image: { verb: "Drew diagram", running: "Drawing diagram…" },
  web_search: { verb: "Searched the web", running: "Searching the web…" },
  web_fetch: { verb: "Fetched web page", running: "Fetching web page…" },
  web_search_call: { verb: "Searched the web", running: "Searching the web…" },
  file_search_call: { verb: "Searched vector files", running: "Searching vector files…" },
  web_search_tool_result: { verb: "Search results", running: "Reading search results…" },
  web_fetch_tool_result: { verb: "Fetched page", running: "Reading page…" },
  extract_clauses: { verb: "Extracted clauses", running: "Extracting clauses…" },
  extract_citations: { verb: "Extracted citations", running: "Extracting citations…" },
  validate_citations: { verb: "Validated citations", running: "Validating citations…" },
  retrieve_legal: { verb: "Retrieved from corpus", running: "Retrieving from corpus…" },
  drs_search: { verb: "Searched FAA DRS", running: "Searching FAA DRS…" },
  drs_fetch: { verb: "Fetched DRS document", running: "Fetching DRS document…" },
  easa_ad_search: { verb: "Searched EASA ADs", running: "Searching EASA ADs…" },
  easa_ad_fetch: { verb: "Fetched EASA AD", running: "Fetching EASA AD…" },
  easa_ear_index: { verb: "Indexed EASA rules", running: "Indexing EASA rules…" },
  aviation_source_search: { verb: "Aviation source lookup", running: "Aviation source lookup…" },
  faa_title14_search: { verb: "Searched 14 CFR", running: "Searching 14 CFR…" },
  ecfr_search: { verb: "Searched eCFR", running: "Searching eCFR…" },
  federal_register_search: { verb: "Searched Federal Register", running: "Searching Federal Register…" },
  govinfo_search: { verb: "Searched GovInfo", running: "Searching GovInfo…" },
  courtlistener_search: { verb: "Searched case law", running: "Searching case law…" },
  eurlex_search: { verb: "Searched EUR-Lex", running: "Searching EUR-Lex…" },
  flk_npc_search: { verb: "Searched 国家法律法规库", running: "Searching 国家法律法规库…" },
  flk_npc_fetch: { verb: "Fetched NPC statute", running: "Fetching NPC statute…" },
  ccar_search: { verb: "Searched CAAC", running: "Searching CAAC…" },
  ccar_fetch: { verb: "Fetched CAAC document", running: "Fetching CAAC document…" },
  caac_local_search: { verb: "Searched local CAAC corpus", running: "Searching local CAAC corpus…" },
  caac_local_fetch: { verb: "Fetched local CAAC document", running: "Fetching local CAAC document…" },
  legal_source_search: { verb: "Legal source lookup", running: "Legal source lookup…" },
  courtlistener_fetch: { verb: "Read court opinion", running: "Reading court opinion…" },
  ecfr_fetch: { verb: "Read eCFR section", running: "Reading eCFR section…" },
  federal_register_fetch: { verb: "Read Federal Register document", running: "Reading Federal Register document…" },
  govinfo_fetch: { verb: "Read GovInfo document", running: "Reading GovInfo document…" },
  eurlex_fetch: { verb: "Read EUR-Lex document", running: "Reading EUR-Lex document…" },
  ecaa_fetch: { verb: "Read ECAA document", running: "Reading ECAA document…" },
  ecaa_index: { verb: "Indexed ECAA documents", running: "Indexing ECAA documents…" },
  ethiopia_law_search: { verb: "Searched Ethiopian law", running: "Searching Ethiopian law…" },
  ethiopia_law_fetch: { verb: "Read Ethiopian law", running: "Reading Ethiopian law…" },
  run_skill_script: { verb: "Ran procedure script", running: "Running procedure script…" },
  write_text_file: { verb: "Wrote file", running: "Writing file…" },
  list_skill_dir: { verb: "Listed procedure files", running: "Listing procedure files…" },
  pkulaw_law_search__search_article: { verb: "北大法宝-法律法规检索（语义）", running: "北大法宝-法律法规检索（语义）…" },
  pkulaw_law_search__get_article: { verb: "北大法宝-法律法规取条文", running: "北大法宝-法律法规取条文…" },
  pkulaw_fatiao__get_law_item_content: { verb: "北大法宝-法条精读", running: "北大法宝-法条精读…" },
  pkulaw_case_search__search_case: { verb: "北大法宝-司法案例检索（语义）", running: "北大法宝-司法案例检索（语义）…" },
  pkulaw_case_list__get_case_list: { verb: "北大法宝-司法案例检索（关键词）", running: "北大法宝-司法案例检索（关键词）…" },
  pkulaw_anhao__anhao_recognition: { verb: "北大法宝-案号识别", running: "北大法宝-案号识别…" },
  pkulaw_law_recognition__law_recognition: { verb: "北大法宝-法条识别", running: "北大法宝-法条识别…" },
  pkulaw_citation_validator__adjust_provisions: { verb: "北大法宝-引证核验", running: "北大法宝-引证核验…" },
  pkulaw_doc_link__get_linked_content: { verb: "北大法宝-文档关联", running: "北大法宝-文档关联…" },
  pkulaw_nl_search__ai_pkulaw_search: { verb: "北大法宝-自然语言检索", running: "北大法宝-自然语言检索…" },
};

export const SOURCE_TOOL_NAMES = new Set<string>([
  "web_search", "web_fetch", "web_search_call", "file_search_call",
  "web_search_tool_result", "web_fetch_tool_result", "retrieve_legal",
  "legal_source_search", "aviation_source_search", "drs_search", "drs_fetch",
  "easa_ad_search", "easa_ad_fetch", "easa_ear_index", "faa_title14_search",
  "ecfr_search", "federal_register_search", "govinfo_search", "courtlistener_search",
  "eurlex_search", "flk_npc_search", "flk_npc_fetch", "ccar_search", "ccar_fetch",
  "caac_local_search", "caac_local_fetch", "courtlistener_fetch", "ecfr_fetch",
  "federal_register_fetch", "govinfo_fetch", "eurlex_fetch", "ecaa_fetch", "ecaa_index",
  "ethiopia_law_search", "ethiopia_law_fetch",
  "pkulaw_law_search__search_article", "pkulaw_law_search__get_article",
  "pkulaw_fatiao__get_law_item_content", "pkulaw_case_search__search_case",
  "pkulaw_case_list__get_case_list", "pkulaw_doc_link__get_linked_content",
  "pkulaw_nl_search__ai_pkulaw_search",
]);

export function isSourceRetrievalTool(name = ""): boolean {
  if (SOURCE_TOOL_NAMES.has(name)) return true;
  const bare = String(name).split("__", 1)[0];
  return SOURCE_TOOL_NAMES.has(bare);
}

export function displayToolName(name = "", status: "running" | "complete" = "complete"): string {
  const entry = TOOL_LABELS[name];
  if (entry) return status === "running" ? entry.running : entry.verb;
  return String(name || "tool")
    .replaceAll("_", " ")
    .replace(/\b\w/g, (c) => c.toUpperCase());
}
