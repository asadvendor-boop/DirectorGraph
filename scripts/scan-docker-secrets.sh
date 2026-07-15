#!/usr/bin/env bash
set -euo pipefail

IMAGE="${1:-directorgraph:final-check}"
PLATFORM="${DIRECTORGRAPH_DOCKER_SMOKE_PLATFORM:-linux/amd64}"
TMP_DIR="$(mktemp -d "${TMPDIR:-/tmp}/directorgraph-image-secrets.XXXXXX")"
trap 'rm -rf "$TMP_DIR"' EXIT

docker image inspect "$IMAGE" >/dev/null

echo "image=$IMAGE"
echo "platform=$PLATFORM"
docker history --no-trunc "$IMAGE" >"$TMP_DIR/history.txt"
docker image inspect "$IMAGE" >"$TMP_DIR/inspect.json"
docker run --rm --platform "$PLATFORM" "$IMAGE" sh -lc '
python - <<'"'"'PY'"'"'
from pathlib import Path

root = Path("/app/static")
if not root.exists():
    print("frontend_static_missing")
    raise SystemExit(0)
for path in sorted(root.rglob("*")):
    if path.is_file():
        try:
            text = path.read_text(encoding="utf-8", errors="ignore")
        except OSError:
            continue
        print(f"---FILE {path}")
        print(text)
PY
' >"$TMP_DIR/frontend.txt"

python3 - "$TMP_DIR" <<'PY'
from __future__ import annotations

import re
import sys
from pathlib import Path

tmp_dir = Path(sys.argv[1])
patterns = [
    ("alibaba_access_key_id", re.compile(r"\bAKID[A-Za-z0-9]{12,}\b")),
    ("openai_style_secret", re.compile(r"\bsk-[A-Za-z0-9_-]{20,}\b")),
    ("private_key", re.compile(r"BEGIN (RSA |DSA |EC |OPENSSH )?PRIVATE KEY")),
    (
        "dashscope_api_key_assignment",
        re.compile(r"\bDASHSCOPE_API_KEY\s*[:=]\s*[\"']?[A-Za-z0-9_./+=-]{16,}"),
    ),
    (
        "oss_access_key_secret_assignment",
        re.compile(r"\bOSS_ACCESS_KEY_SECRET\s*[:=]\s*[\"']?[A-Za-z0-9_./+=-]{16,}"),
    ),
    (
        "function_compute_auth_header_assignment",
        re.compile(r"\bFUNCTION_COMPUTE_AUTH_HEADER\s*[:=]\s*[\"']?(Bearer|Basic)\s+[A-Za-z0-9._~+/=-]{16,}"),
    ),
]
scopes = {
    "history": tmp_dir / "history.txt",
    "image_inspect": tmp_dir / "inspect.json",
    "frontend_assets": tmp_dir / "frontend.txt",
}
findings = []
for scope, path in scopes.items():
    print(f"scan_scope={scope}")
    for line_number, line in enumerate(path.read_text(encoding="utf-8", errors="ignore").splitlines(), start=1):
        for name, pattern in patterns:
            if pattern.search(line):
                findings.append((scope, name, line_number))

if findings:
    print(f"secret_scan_status=fail findings={len(findings)}")
    for scope, name, line_number in findings:
        print(f"finding scope={scope} pattern={name} line={line_number}")
    raise SystemExit(1)

print("secret_scan_status=pass")
PY
