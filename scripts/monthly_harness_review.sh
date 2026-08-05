#!/usr/bin/env bash
# Monthly legal-AI harness review — invoked from cron on the 1st of each month.
# Runs Claude Code headless with the report-only prompt in
# scripts/monthly_harness_review.md and appends output to logs/.
set -uo pipefail

# Point LEGAL_HELPER_REPO at your checkout, or run this script from inside it.
REPO="${LEGAL_HELPER_REPO:-$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)}"
LOG_DIR="$REPO/logs"
mkdir -p "$LOG_DIR" "$REPO/outputs/harness_reviews"

# Resolve the newest Claude Code native binary bundled with the VS Code
# extension so the job keeps working across extension upgrades.
CLAUDE_BIN=$(ls -1d "$HOME"/.vscode-server/extensions/anthropic.claude-code-*/resources/native-binary/claude 2>/dev/null | sort -V | tail -1)
if [[ -z "${CLAUDE_BIN:-}" ]]; then
  CLAUDE_BIN=$(command -v claude || true)
fi
if [[ -z "${CLAUDE_BIN:-}" ]]; then
  echo "monthly_harness_review: no Claude Code binary found" >> "$LOG_DIR/monthly_harness_review.log"
  exit 1
fi

cd "$REPO"
{
  echo "===== monthly harness review started $(date -Is) ====="
  "$CLAUDE_BIN" -p "$(cat "$REPO/scripts/monthly_harness_review.md")" \
    --permission-mode acceptEdits
  echo "===== finished $(date -Is) (exit $?) ====="
} >> "$LOG_DIR/monthly_harness_review.log" 2>&1
