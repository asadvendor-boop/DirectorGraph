#!/usr/bin/env bash
set -euo pipefail

ROOT="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
cd "$ROOT"

echo "[1/9] Python syntax"
python -m compileall -q \
  services/api/app \
  services/api/tests \
  studio_mcp \
  evals \
  scripts/check-private-oss-access.py \
  scripts/check-submission-artifacts.py \
  scripts/check-architecture-assets.py \
  scripts/check-media-contract.py \
  scripts/run-deployment-task-smoke.py \
  scripts/run-live-api-smoke.py \
  scripts/verify-source-archive.py \
  scripts/write-function-min-instances.py \
  scripts/write-image-digest.py \
  scripts/write-live-evaluation.py \
  scripts/write-public-links.py

echo "[2/9] Backend tests"
(
  cd services/api
  python -m pytest -q -m 'not integration'
)

echo "[3/9] Frontend build"
(
  cd apps/web
  if [[ ! -d node_modules ]]; then npm ci; fi
  npm run build
)

echo "[4/9] Frontend Playwright e2e"
(
  cd apps/web
  npm run test:e2e
)

echo "[5/9] Shell syntax"
for script in \
  infra/alibaba/deploy-ecs.sh \
  infra/alibaba/verify-deployment.sh \
  scripts/build-source-archive.sh \
  scripts/deploy-serverless.sh \
  scripts/scan-docker-secrets.sh \
  scripts/run-image-vulnerability-scan.sh \
  scripts/smoke-docker-image.sh \
  scripts/run-dependency-audit.sh \
  scripts/verify-serverless-deployment.sh \
  scripts/validate.sh; do
  bash -n "$script"
done

echo "[6/9] JSON integrity"
python - <<'PY'
import json
from pathlib import Path
for path in Path('.').rglob('*.json'):
    if not {'node_modules', 'dist', 'test-results', 'playwright-report'} & set(path.parts):
        json.loads(path.read_text(encoding='utf-8'))
print('JSON documents valid')
PY

echo "[7/9] Architecture assets and bundled master"
python scripts/check-architecture-assets.py >/tmp/directorgraph-architecture-assets.json
python scripts/check-media-contract.py

echo "[8/9] Credential-pattern scan"
if grep -RInE \
  --exclude-dir=.git --exclude-dir=.venv --exclude-dir=node_modules --exclude-dir=dist \
  --exclude-dir=test-results --exclude-dir=playwright-report \
  --exclude='*.lock' --exclude='validate.sh' \
  '(AKID[A-Za-z0-9]{12,}|sk-[A-Za-z0-9]{20,}|BEGIN (RSA |EC |OPENSSH )?PRIVATE KEY)' .; then
  echo "Potential credential material found" >&2
  exit 1
fi

echo "[9/9] Cost estimate smoke"
python scripts/estimate-cost.py \
  --profile judge_test \
  --duration-seconds 15 \
  --shots 3 \
  --project-cap-usd 3 \
  --fail-over-cap >/tmp/directorgraph-cost-estimate.json
cat /tmp/directorgraph-cost-estimate.json

echo "DirectorGraph validation passed."
