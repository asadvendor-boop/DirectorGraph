#!/usr/bin/env bash
set -euo pipefail

BASE_URL="${1:-http://localhost:8000}"
if [[ "${DIRECTORGRAPH_ALLOW_LOCAL_ECS:-}" != "true" ]]; then
  cat >&2 <<'EOF'
This ECS/Compose verification script is local local-development scaffolding.
It is not valid deployment proof for the serverless submission.

Use scripts/verify-serverless-deployment.sh against the Function Compute web URL.

To run this local script intentionally, set:
  DIRECTORGRAPH_ALLOW_LOCAL_ECS=true
EOF
  exit 2
fi

echo "[1/4] API health"
curl -fsS "$BASE_URL/api/health" | python3 -m json.tool

echo "[2/4] Public model configuration"
curl -fsS "$BASE_URL/api/config" | python3 -m json.tool

echo "[3/4] Docker workload"
docker compose ps

echo "[4/4] Alibaba integration evidence in source"
grep -R --line-number -E 'oss2\.Bucket|dashscope.*aliyuncs\.com|DASHSCOPE_API_KEY' services/api/app infra/alibaba | head -n 20
