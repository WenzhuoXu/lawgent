#!/usr/bin/env bash
# Fetch the aviation treaty + freely-available ICAO Doc corpus.
#
# Reads source URLs from corpus/{aviation_treaties,icao_doc}/manifest.yaml
# and downloads each into the matching file path. Idempotent: skips files
# already present unless --force is given. Tolerates 404 — logs and moves
# on. Run from anywhere; resolves paths relative to the repo root.

set -uo pipefail

REPO_ROOT="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
cd "$REPO_ROOT"

FORCE=0
if [[ "${1:-}" == "--force" ]]; then
    FORCE=1
fi

LOG_DIR="${REPO_ROOT}/logs"
mkdir -p "$LOG_DIR"
LOG="${LOG_DIR}/fetch_aviation_corpus_$(date +%Y%m%d_%H%M%S).log"
ok=0
fail=0
skip=0

log() {
    echo "[$(date -Iseconds)] $*" | tee -a "$LOG"
}

fetch_one() {
    local rel="$1" url="$2" base="$3"
    local out="${REPO_ROOT}/legal_helper/rag/corpus/${base}/${rel}"
    mkdir -p "$(dirname "$out")"
    if [[ $FORCE -eq 0 && -s "$out" ]]; then
        log "SKIP  $rel  (already present, $(stat -c%s "$out") bytes)"
        ((skip++))
        return 0
    fi
    log "FETCH $rel  <- $url"
    # -L follow redirects, -f fail on HTTP error, --connect-timeout, --max-time
    # Use --retry only on transient failures; 4xx like 404 is final.
    if curl -sSL -A "legal-helper-corpus/1.0" \
            --connect-timeout 15 --max-time 180 \
            --retry 2 --retry-delay 3 \
            -o "$out.partial" "$url"; then
        if [[ -s "$out.partial" ]]; then
            # Reject HTML error pages saved as .pdf.
            local magic
            magic=$(head -c 8 "$out.partial" 2>/dev/null || echo "")
            if [[ "$rel" == *.pdf && "$magic" != "%PDF-"* ]]; then
                rm -f "$out.partial"
                log "BADPDF $rel  — server returned non-PDF (likely 404 HTML)"
                ((fail++))
                return 1
            fi
            mv "$out.partial" "$out"
            local size
            size=$(stat -c%s "$out")
            log "OK    $rel  ($size bytes)"
            ((ok++))
        else
            rm -f "$out.partial"
            log "EMPTY $rel  — server returned empty body"
            ((fail++))
        fi
    else
        rm -f "$out.partial"
        log "FAIL  $rel  <- $url"
        ((fail++))
    fi
}

parse_manifest() {
    # $1 = path to manifest.yaml, $2 = corpus base subdir
    # Print "file\turl" lines.
    local manifest="$1"
    python3 - "$manifest" <<'PY'
import sys, yaml
m = yaml.safe_load(open(sys.argv[1])) or {}
for f in m.get("files") or []:
    rel = f.get("file") or ""
    url = f.get("source_url") or ""
    if rel and url:
        print(f"{rel}\t{url}")
PY
}

run_corpus() {
    local base="$1"
    local manifest="${REPO_ROOT}/legal_helper/rag/corpus/${base}/manifest.yaml"
    log "==> $base  (manifest: $manifest)"
    if [[ ! -f "$manifest" ]]; then
        log "WARN  $manifest not found, skipping"
        return
    fi
    while IFS=$'\t' read -r rel url; do
        [[ -z "$rel" ]] && continue
        fetch_one "$rel" "$url" "$base"
    done < <(parse_manifest "$manifest" "$base")
}

log "fetch_aviation_corpus.sh starting (force=$FORCE)"
run_corpus aviation_treaties
run_corpus icao_doc
log "done. ok=$ok skip=$skip fail=$fail"
log "log: $LOG"
