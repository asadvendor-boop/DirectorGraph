#!/usr/bin/env bash
set -euo pipefail

ROOT="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
cd "$ROOT"

OUTPUT="${1:-evidence/deployment/dependency-audit.txt}"
mkdir -p "$(dirname "$OUTPUT")"

tmp_output="$(mktemp "${TMPDIR:-/tmp}/directorgraph-dependency-audit.XXXXXX.txt")"
trap 'rm -f "$tmp_output"' EXIT

{
  echo "schema=directorgraph.dependency-audit.v1"
  echo "generated_at=$(date -u +%Y-%m-%dT%H:%M:%SZ)"
  echo "commit=$(git rev-parse HEAD 2>/dev/null || echo unknown)"
  echo
  echo "[python:pip-audit]"
  python -m pip_audit services/api --progress-spinner off --format columns 2>&1
  echo
  echo "[node:npm-audit-high]"
  (
    cd apps/web
    npm audit --audit-level=high --omit=optional 2>&1
  )
  echo
  echo "overall_status=pass"
} | tee "$tmp_output"

mv "$tmp_output" "$OUTPUT"
echo "dependency_audit=$OUTPUT"
