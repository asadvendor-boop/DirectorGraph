#!/usr/bin/env bash
set -euo pipefail

BASE_URL="${1:-${DIRECTORGRAPH_BASE_URL:-}}"
TASK_ID="${DIRECTORGRAPH_TASK_ID:-}"
EXPECTED_BUILD_SHA="${EXPECTED_BUILD_SHA:-}"
EXPECT_LIVE="${EXPECT_LIVE:-true}"
EXPECT_PUBLIC_DEMO="${EXPECT_PUBLIC_DEMO:-false}"
OUTPUT_DIR="${OUTPUT_DIR:-evidence/deployment}"

if [[ -z "$BASE_URL" ]]; then
  echo "Usage: DIRECTORGRAPH_BASE_URL=https://... scripts/verify-serverless-deployment.sh" >&2
  echo "   or: scripts/verify-serverless-deployment.sh https://..." >&2
  exit 2
fi

mkdir -p "$OUTPUT_DIR"

sanitize_json() {
  local input="$1"
  local output="$2"
  python3 - "$input" "$output" <<'PY'
import json
import sys
from pathlib import Path

input_path = Path(sys.argv[1])
output_path = Path(sys.argv[2])

def sanitize(value):
    if isinstance(value, dict):
        sanitized = {}
        redacted_url = False
        for key, item in value.items():
            if (
                key in {"url", "signed_url"}
                and isinstance(item, str)
                and ("OSSAccessKeyId=" in item or "Signature=" in item)
            ):
                sanitized[key] = "redacted-signed-oss-url"
                redacted_url = True
            else:
                sanitized[key] = sanitize(item)
        if redacted_url:
            sanitized["url_redacted"] = True
            sanitized["redaction_reason"] = "signed OSS URL omitted from committed evidence"
        return sanitized
    if isinstance(value, list):
        return [sanitize(item) for item in value]
    return value

payload = json.loads(input_path.read_text(encoding="utf-8"))
output_path.write_text(json.dumps(sanitize(payload), indent=2, sort_keys=True), encoding="utf-8")
PY
}

fetch_json() {
  local name="$1"
  local path="$2"
  local output="$OUTPUT_DIR/${name}.json"
  local raw
  raw="$(mktemp)"
  curl -fsS "${BASE_URL%/}${path}" > "$raw"
  sanitize_json "$raw" "$output"
  rm -f "$raw"
  cat "$output"
}

echo "[1/7] Health"
fetch_json health /api/health

echo "[2/7] Readiness"
fetch_json readiness /api/readiness

echo "[3/7] Public config"
fetch_json config /api/config

echo "[4/7] Verify build/provider/readiness expectations"
python3 - "$OUTPUT_DIR" "$EXPECTED_BUILD_SHA" "$EXPECT_LIVE" <<'PY'
import json
import sys
from pathlib import Path

root = Path(sys.argv[1])
expected_sha = sys.argv[2]
expect_live = sys.argv[3].lower() == "true"
health = json.loads((root / "health.json").read_text(encoding="utf-8"))
readiness = json.loads((root / "readiness.json").read_text(encoding="utf-8"))
config = json.loads((root / "config.json").read_text(encoding="utf-8"))
if expected_sha and health.get("build", {}).get("sha") != expected_sha:
    raise SystemExit(f"Build SHA mismatch: expected {expected_sha}, got {health.get('build', {}).get('sha')}")
if expect_live and health.get("provider_mode") != "live":
    raise SystemExit(f"Expected live provider mode, got {health.get('provider_mode')}")
if expect_live and config.get("provider_mode") != "live":
    raise SystemExit(f"Expected live public config, got {config.get('provider_mode')}")
if readiness.get("status") != "ready":
    raise SystemExit(f"Readiness is not ready: {readiness.get('status')}")
checks = readiness.get("checks", {})
for key in ("live_credentials_ready", "media_publication_ready", "function_compute_task_configured"):
    if expect_live and not checks.get(key):
        raise SystemExit(f"Readiness check failed: {key}")
print("health/readiness/config expectations passed")
PY

echo "[5/7] Public demo readback"
if [[ "$EXPECT_PUBLIC_DEMO" == "true" ]]; then
  fetch_json public-demo /api/public/demo
  fetch_json public-demo-storage-manifest /api/public/demo/storage-manifest
else
  echo "Skipped; set EXPECT_PUBLIC_DEMO=true after PUBLIC_DEMO_PROJECT_ID is configured."
fi

echo "[6/7] Task status readback"
if [[ -n "$TASK_ID" ]]; then
  fetch_json "task-${TASK_ID}" "/api/tasks/${TASK_ID}"
else
  echo "Skipped; set DIRECTORGRAPH_TASK_ID after a run or Judge Test submission."
fi

echo "[7/7] Serverless proof metadata"
python3 - "$OUTPUT_DIR/serverless-verification-summary.json" "$OUTPUT_DIR/serverless-live-verification.json" "$BASE_URL" "$TASK_ID" "$EXPECTED_BUILD_SHA" "$EXPECT_LIVE" "$EXPECT_PUBLIC_DEMO" <<'PY'
import json
import sys
from datetime import datetime, timezone
from pathlib import Path

summary_output = Path(sys.argv[1])
live_output = Path(sys.argv[2])

def read_json(name: str):
    path = live_output.parent / name
    if not path.exists():
        return None
    return json.loads(path.read_text(encoding="utf-8"))

health = read_json("health.json")
readiness = read_json("readiness.json")
config = read_json("config.json")
public_demo = read_json("public-demo.json")
public_demo_storage = read_json("public-demo-storage-manifest.json")
task_id = sys.argv[4] or None
task_status = read_json(f"task-{task_id}.json") if task_id else None
expected_build_sha = sys.argv[5] or None
expect_live = sys.argv[6].lower() == "true"
expect_public_demo = sys.argv[7].lower() == "true"
evidence_files = sorted(path.name for path in live_output.parent.glob("*.json"))

payload = {
    "schema": "directorgraph.serverless-verification-summary.v1",
    "verified_at": datetime.now(timezone.utc).isoformat(),
    "base_url": sys.argv[3],
    "task_id": task_id,
    "evidence_files": evidence_files,
}
summary_output.write_text(json.dumps(payload, indent=2, sort_keys=True), encoding="utf-8")
print(json.dumps(payload, indent=2, sort_keys=True))

checks = {
    "build_sha_matches": (
        not expected_build_sha
        or bool(health and health.get("build", {}).get("sha") == expected_build_sha)
    ),
    "config_provider_live": bool(not expect_live or (config and config.get("provider_mode") == "live")),
    "health_provider_live": bool(not expect_live or (health and health.get("provider_mode") == "live")),
    "public_demo_checked": bool(not expect_public_demo or public_demo),
    "readiness_ready": bool(readiness and readiness.get("status") == "ready"),
    "task_status_checked": bool(not task_id or task_status),
}
live_payload = {
    "schema": "directorgraph.serverless-live-verification.v1",
    "verified_at": payload["verified_at"],
    "base_url": sys.argv[3],
    "expected_build_sha": expected_build_sha,
    "expect_live": expect_live,
    "expect_public_demo": expect_public_demo,
    "task_id": task_id,
    "checks": checks,
    "health": health,
    "readiness": readiness,
    "config": config,
    "public_demo": public_demo,
    "public_demo_storage_manifest": public_demo_storage,
    "task_status": task_status,
    "evidence_files": evidence_files,
}
live_output.write_text(json.dumps(live_payload, indent=2, sort_keys=True), encoding="utf-8")
print(json.dumps({"wrote": live_output.as_posix(), "schema": live_payload["schema"]}, sort_keys=True))
PY

echo "Serverless deployment verification completed."
