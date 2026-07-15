#!/usr/bin/env bash
set -euo pipefail

ROOT="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
TEMPLATE="$ROOT/infra/alibaba/serverless/s.yaml.template"
ACTION="${1:-plan}"

BUILD_SHA="${BUILD_SHA:-$(cd "$ROOT" && git rev-parse --short HEAD 2>/dev/null || echo local)}"
BUILD_TIMESTAMP="${BUILD_TIMESTAMP:-$(date -u +%Y-%m-%dT%H:%M:%SZ)}"
ACR_IMAGE_URI="${ACR_IMAGE_URI:-}"
SERVERLESS_DEVS_ACCESS="${SERVERLESS_DEVS_ACCESS:-default}"

export BUILD_SHA BUILD_TIMESTAMP SERVERLESS_DEVS_ACCESS

usage() {
  cat <<'EOF'
Usage: scripts/deploy-serverless.sh [plan|build|push|render-template|deploy]

Actions:
  plan             Print required values and write a redacted deployment plan.
  build            Build the one-image linux/amd64 container locally.
  push             Push ACR_IMAGE_URI; requires DIRECTORGRAPH_APPROVE_CLOUD_APPLY=push-serverless-image.
  render-template  Render the Serverless Devs template to stdout or RENDERED_TEMPLATE_PATH.
  deploy           Render a temporary template, run `s deploy`, then release provisioned instances with
                   `s <resource> provision put --qualifier LATEST --target 0`; requires
                   DIRECTORGRAPH_APPROVE_CLOUD_APPLY=deploy-serverless.

Required cloud values for deploy/render-template:
  ALIBABA_REGION ALIBABA_ACCOUNT_ID ACR_IMAGE_URI FC_RUNTIME_ROLE_ARN
  FC_WEB_SERVICE_NAME FC_WEB_FUNCTION_NAME FC_TASK_SERVICE_NAME FC_TASK_FUNCTION_NAME
  PUBLIC_MEDIA_BASE_URL FUNCTION_COMPUTE_TASK_URL FUNCTION_COMPUTE_AUTH_HEADER
  DASHSCOPE_API_KEY DASHSCOPE_REGION OSS_ENDPOINT OSS_BUCKET OSS_ACCESS_KEY_ID OSS_ACCESS_KEY_SECRET
  MAX_TOTAL_LIVE_SPEND_USD MAX_PROJECT_SPEND_USD REPAIR_RESERVE_PERCENT
  MAX_RENDER_ATTEMPTS_PER_SHOT MAX_PARALLEL_RENDERS JUDGE_RUN_MAX_DURATION_SECONDS JUDGE_RUN_MAX_SHOTS

This script never deploys, pushes, or mutates cloud resources without the explicit approval environment variable.
EOF
}

required_for_template=(
  ALIBABA_REGION
  ALIBABA_ACCOUNT_ID
  ACR_IMAGE_URI
  FC_RUNTIME_ROLE_ARN
  FC_WEB_SERVICE_NAME
  FC_WEB_FUNCTION_NAME
  FC_TASK_SERVICE_NAME
  FC_TASK_FUNCTION_NAME
  PUBLIC_MEDIA_BASE_URL
  DASHSCOPE_API_KEY
  DASHSCOPE_REGION
  OSS_ENDPOINT
  OSS_BUCKET
  OSS_ACCESS_KEY_ID
  OSS_ACCESS_KEY_SECRET
  FUNCTION_COMPUTE_TASK_URL
  FUNCTION_COMPUTE_AUTH_HEADER
  FUNCTION_COMPUTE_INVOKE_TIMEOUT_SECONDS
  MAX_TOTAL_LIVE_SPEND_USD
  MAX_PROJECT_SPEND_USD
  REPAIR_RESERVE_PERCENT
  MAX_RENDER_ATTEMPTS_PER_SHOT
  MAX_PARALLEL_RENDERS
  JUDGE_RUN_MAX_DURATION_SECONDS
  JUDGE_RUN_MAX_SHOTS
  JUDGE_CREATE_ACCESS_CODE
  FC_WEB_TIMEOUT_SECONDS
  FC_WEB_MEMORY_MB
  FC_WEB_DISK_MB
  FC_WEB_INSTANCE_CONCURRENCY
  FC_TASK_TIMEOUT_SECONDS
  FC_TASK_MEMORY_MB
  FC_TASK_DISK_MB
  FC_TASK_INSTANCE_CONCURRENCY
)

set_defaults() {
  export DASHSCOPE_REGION="${DASHSCOPE_REGION:-singapore}"
  export OSS_PUBLIC_BASE_URL="${OSS_PUBLIC_BASE_URL:-}"
  export FUNCTION_COMPUTE_INVOKE_TIMEOUT_SECONDS="${FUNCTION_COMPUTE_INVOKE_TIMEOUT_SECONDS:-10}"
  export MAX_TOTAL_LIVE_SPEND_USD="${MAX_TOTAL_LIVE_SPEND_USD:-35}"
  export MAX_PROJECT_SPEND_USD="${MAX_PROJECT_SPEND_USD:-6}"
  export REPAIR_RESERVE_PERCENT="${REPAIR_RESERVE_PERCENT:-20}"
  export MAX_RENDER_ATTEMPTS_PER_SHOT="${MAX_RENDER_ATTEMPTS_PER_SHOT:-2}"
  export MAX_PARALLEL_RENDERS="${MAX_PARALLEL_RENDERS:-3}"
  export JUDGE_RUN_MAX_DURATION_SECONDS="${JUDGE_RUN_MAX_DURATION_SECONDS:-15}"
  export JUDGE_RUN_MAX_SHOTS="${JUDGE_RUN_MAX_SHOTS:-3}"
  export PUBLIC_DEMO_PROJECT_ID="${PUBLIC_DEMO_PROJECT_ID:-}"
  export FC_WEB_TIMEOUT_SECONDS="${FC_WEB_TIMEOUT_SECONDS:-60}"
  export FC_WEB_MEMORY_MB="${FC_WEB_MEMORY_MB:-2048}"
  export FC_WEB_DISK_MB="${FC_WEB_DISK_MB:-512}"
  export FC_WEB_INSTANCE_CONCURRENCY="${FC_WEB_INSTANCE_CONCURRENCY:-10}"
  export FC_TASK_TIMEOUT_SECONDS="${FC_TASK_TIMEOUT_SECONDS:-3600}"
  export FC_TASK_MEMORY_MB="${FC_TASK_MEMORY_MB:-4096}"
  export FC_TASK_DISK_MB="${FC_TASK_DISK_MB:-10240}"
  export FC_TASK_INSTANCE_CONCURRENCY="${FC_TASK_INSTANCE_CONCURRENCY:-1}"
}

missing_values() {
  local missing=()
  for name in "$@"; do
    if [[ -z "${!name:-}" ]]; then
      missing+=("$name")
    fi
  done
  if ((${#missing[@]})); then
    printf '%s\n' "${missing[@]}"
  fi
}

require_values() {
  local missing
  missing="$(missing_values "$@")"
  if [[ -n "$missing" ]]; then
    echo "Missing required environment values:" >&2
    echo "$missing" >&2
    exit 2
  fi
}

require_approval() {
  local expected="$1"
  if [[ "${DIRECTORGRAPH_APPROVE_CLOUD_APPLY:-}" != "$expected" ]]; then
    echo "Refusing cloud mutation. Set DIRECTORGRAPH_APPROVE_CLOUD_APPLY=$expected after human approval." >&2
    exit 3
  fi
}

write_plan() {
  mkdir -p "$ROOT/evidence/deployment"
  python3 - "$ROOT/evidence/deployment/serverless-deploy-plan.json" <<'PY'
import json
import os
import sys
from datetime import datetime, timezone

path = sys.argv[1]
fields = [
    "ALIBABA_REGION",
    "ALIBABA_ACCOUNT_ID",
    "ACR_IMAGE_URI",
    "FC_RUNTIME_ROLE_ARN",
    "FC_WEB_SERVICE_NAME",
    "FC_WEB_FUNCTION_NAME",
    "FC_TASK_SERVICE_NAME",
    "FC_TASK_FUNCTION_NAME",
    "OSS_ENDPOINT",
    "OSS_BUCKET",
    "PUBLIC_MEDIA_BASE_URL",
    "FUNCTION_COMPUTE_TASK_URL",
    "MAX_TOTAL_LIVE_SPEND_USD",
    "MAX_PROJECT_SPEND_USD",
    "JUDGE_RUN_MAX_DURATION_SECONDS",
    "JUDGE_RUN_MAX_SHOTS",
]
secret_fields = [
    "DASHSCOPE_API_KEY",
    "OSS_ACCESS_KEY_ID",
    "OSS_ACCESS_KEY_SECRET",
    "FUNCTION_COMPUTE_AUTH_HEADER",
    "JUDGE_CREATE_ACCESS_CODE",
]
payload = {
    "schema": "directorgraph.serverless-deploy-plan.v1",
    "generated_at": datetime.now(timezone.utc).isoformat(),
    "build_sha": os.environ.get("BUILD_SHA"),
    "build_timestamp": os.environ.get("BUILD_TIMESTAMP"),
    "values": {name: os.environ.get(name) or None for name in fields},
    "secrets_configured": {name: bool(os.environ.get(name)) for name in secret_fields},
    "required_actions": [
        "Build linux/amd64 one-image container",
        "Push immutable image to Alibaba Container Registry",
        "Deploy web and task Function Compute functions",
        "Release provisioned instances for both functions with s provision put --target 0",
        "Verify Function Compute minimum/on-demand instance settings from the live console or API",
        "Verify /api/health, /api/readiness, public demo, task endpoint, OSS read/write, and Judge Test",
    ],
}
with open(path, "w", encoding="utf-8") as handle:
    json.dump(payload, handle, indent=2, sort_keys=True)
print(path)
PY
}

render_template() {
  local output="${1:-/dev/stdout}"
  require_values "${required_for_template[@]}"
  python3 - "$TEMPLATE" "$output" <<'PY'
import os
import re
import stat
import sys
from pathlib import Path

template = Path(sys.argv[1])
output = sys.argv[2]
text = template.read_text(encoding="utf-8")
names = sorted(set(re.findall(r"\$\{([A-Z0-9_]+)\}", text)))
missing = [name for name in names if name not in os.environ]
if missing:
    raise SystemExit("Missing template values: " + ", ".join(missing))
for name in names:
    text = text.replace("${" + name + "}", os.environ[name])
if output == "/dev/stdout":
    sys.stdout.write(text)
else:
    path = Path(output)
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(text, encoding="utf-8")
    path.chmod(stat.S_IRUSR | stat.S_IWUSR)
    print(path)
PY
}

release_provisioned_instances() {
  local template_path="$1"
  local resource
  for resource in directorgraph-web directorgraph-task; do
    echo "Releasing provisioned instances for ${resource} (target=0)."
    s "$resource" provision put -t "$template_path" --qualifier LATEST --target 0
  done
}

set_defaults

case "$ACTION" in
  help|-h|--help)
    usage
    ;;
  plan)
    echo "Serverless deployment plan for DirectorGraph"
    echo "Build SHA: $BUILD_SHA"
    echo "ACR image: ${ACR_IMAGE_URI:-<missing>}"
    echo "Missing values for deploy:"
    missing_values "${required_for_template[@]}" || true
    echo "Wrote redacted plan: $(write_plan)"
    ;;
  build)
    require_values ACR_IMAGE_URI
    docker build --platform linux/amd64 \
      --build-arg BUILD_SHA="$BUILD_SHA" \
      --build-arg BUILD_TIMESTAMP="$BUILD_TIMESTAMP" \
      -t "$ACR_IMAGE_URI" "$ROOT"
    ;;
  push)
    require_values ACR_IMAGE_URI
    require_approval push-serverless-image
    docker push "$ACR_IMAGE_URI"
    ;;
  render-template)
    render_template "${RENDERED_TEMPLATE_PATH:-/dev/stdout}"
    ;;
  deploy)
    require_approval deploy-serverless
    command -v s >/dev/null 2>&1 || {
      echo "Serverless Devs CLI 's' is required for deploy." >&2
      exit 4
    }
    temp_file="$(mktemp "${TMPDIR:-/tmp}/directorgraph-s.yaml.XXXXXX")"
    trap 'rm -f "$temp_file"' EXIT
    render_template "$temp_file" >/dev/null
    s deploy -t "$temp_file" -y
    release_provisioned_instances "$temp_file"
    ;;
  *)
    usage >&2
    exit 2
    ;;
esac
