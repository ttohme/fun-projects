#!/usr/bin/env bash
# Nightly continuous-improvement pipeline (assistant-improve.timer, 02:30 —
# before the 03:30 backup so the night's mined state lands in the snapshot).
#
#   1. Mine approval decisions into promptfoo regression cases
#   2. If candidate prompts exist, eval current vs candidates on all cases
#   3. Generate the win-rate report; a winning candidate becomes an
#      approval-gated prompt_revision job (never auto-deploys)
#   4. Append OpenClaw routing metrics to the report
#
#   scripts/improvement_run.sh
set -euo pipefail

REPO_ROOT="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
cd "$REPO_ROOT"
PY="${PY:-$([ -x .venv/bin/python3 ] && echo .venv/bin/python3 || echo python3)}"
export PYTHONPATH="$REPO_ROOT/apps/orchestrator"

stamp="$(date -u +%Y-%m-%d)"
mkdir -p var/evals/reports

echo "==> Mining approval decisions"
"$PY" apps/orchestrator/approval_miner.py mine

echo "==> Rule proposals"
"$PY" apps/orchestrator/approval_miner.py propose-rules \
  > "var/evals/reports/${stamp}-rules.json" || true

candidates=(apps/orchestrator/prompts/candidates/*.md)
if [ -e "${candidates[0]}" ]; then
  echo "==> Evaluating current vs candidate prompts"
  npx --yes promptfoo@latest eval \
    --config evals/improvementconfig.yaml \
    --output var/evals/latest.json || {
      echo "improvement: eval failed (no model reachable?) — skipping report" >&2
      exit 0
    }
  echo "==> Building report + gated promotion proposal"
  "$PY" apps/orchestrator/improvement_report.py report \
    --input var/evals/latest.json --stamp "$stamp"
else
  echo "==> No candidate prompts in apps/orchestrator/prompts/candidates/ — skipping eval"
fi

echo "==> OpenClaw routing metrics"
"$PY" apps/orchestrator/openclaw_log_miner.py \
  >> "var/evals/reports/${stamp}.md" 2>/dev/null || true

echo "improvement: done"
