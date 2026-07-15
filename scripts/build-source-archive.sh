#!/usr/bin/env bash
set -euo pipefail

ROOT="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
cd "$ROOT"

OUTPUT="submission/source-archive.tar.gz"
PREFIX="directorgraph/"
ALLOW_DIRTY="false"

usage() {
  cat <<'EOF'
Usage: bash scripts/build-source-archive.sh [--output PATH] [--prefix PREFIX/] [--allow-dirty]

Builds the DirectorGraph source archive from tracked repository files and writes
a SHA-256 checksum next to it. By default the working tree must be clean so the
archive can be traced to the committed source.
EOF
}

while [[ $# -gt 0 ]]; do
  case "$1" in
    --output)
      OUTPUT="${2:?missing output path}"
      shift 2
      ;;
    --prefix)
      PREFIX="${2:?missing archive prefix}"
      shift 2
      ;;
    --allow-dirty)
      ALLOW_DIRTY="true"
      shift
      ;;
    -h|--help)
      usage
      exit 0
      ;;
    *)
      echo "Unknown argument: $1" >&2
      usage >&2
      exit 2
      ;;
  esac
done

if [[ "$PREFIX" != */ ]]; then
  echo "Archive prefix must end with '/': $PREFIX" >&2
  exit 2
fi

WORKTREE_STATUS="$(git status --porcelain)"
WORKTREE_DIRTY="false"
if [[ -n "$WORKTREE_STATUS" ]]; then
  WORKTREE_DIRTY="true"
fi

if [[ "$ALLOW_DIRTY" != "true" && -n "$WORKTREE_STATUS" ]]; then
  echo "Refusing to build source archive from a dirty worktree." >&2
  echo "Commit or stash changes first, or use --allow-dirty for non-final smoke evidence." >&2
  exit 3
fi

COMMIT_SHA="$(git rev-parse HEAD)"
mkdir -p "$(dirname "$OUTPUT")"

tmp_archive="$(mktemp "${TMPDIR:-/tmp}/directorgraph-source.XXXXXX.tar.gz")"
trap 'rm -f "$tmp_archive"' EXIT

python3 - "$tmp_archive" "$PREFIX" "$COMMIT_SHA" "$WORKTREE_DIRTY" <<'PY'
import gzip
import io
import json
import os
import subprocess
import sys
import tarfile
from pathlib import Path

archive_path, prefix, commit_sha, worktree_dirty = sys.argv[1:5]
tracked = subprocess.check_output(["git", "ls-files", "-z"]).split(b"\0")
excluded = {
    b"submission/source-archive.tar.gz",
    b"submission/source-archive.tar.gz.sha256",
}
paths = [Path(raw.decode("utf-8")) for raw in tracked if raw and raw not in excluded]
with open(archive_path, "wb") as raw_archive:
    with gzip.GzipFile(fileobj=raw_archive, mode="wb", mtime=0) as gz_archive:
        with tarfile.open(fileobj=gz_archive, mode="w") as archive:
            for path in sorted(paths, key=lambda item: item.as_posix()):
                if not path.exists():
                    raise SystemExit(f"Tracked path is missing from working tree: {path}")
                if path.is_dir():
                    continue
                archive_name = f"{prefix}{path.as_posix()}"
                if path.is_symlink():
                    info = tarfile.TarInfo(archive_name)
                    info.type = tarfile.SYMTYPE
                    info.linkname = os.readlink(path)
                    info.mode = 0o755 if os.access(path, os.X_OK) else 0o644
                    info.mtime = 0
                    info.uid = 0
                    info.gid = 0
                    info.uname = ""
                    info.gname = ""
                    archive.addfile(info)
                    continue
                info = tarfile.TarInfo(archive_name)
                data = path.read_bytes()
                info.size = len(data)
                info.mode = 0o755 if os.access(path, os.X_OK) else 0o644
                info.mtime = 0
                info.uid = 0
                info.gid = 0
                info.uname = ""
                info.gname = ""
                archive.addfile(info, io.BytesIO(data))

forbidden_parts = {
    ".git",
    "__pycache__",
    ".pytest_cache",
    ".ruff_cache",
    ".venv",
    "node_modules",
    "dist",
    "coverage",
    "data",
    "media",
}
forbidden_suffixes = (".pyc", ".pyo", ".db", ".sqlite3", ".tsbuildinfo", ".DS_Store")
required_entries = {
    f"{prefix}README.md",
    f"{prefix}Dockerfile",
    f"{prefix}scripts/build-source-archive.sh",
    f"{prefix}scripts/check-architecture-assets.py",
    f"{prefix}scripts/check-private-oss-access.py",
    f"{prefix}scripts/check-submission-artifacts.py",
    f"{prefix}scripts/run-image-vulnerability-scan.sh",
    f"{prefix}scripts/smoke-docker-image.sh",
    f"{prefix}scripts/run-deployment-task-smoke.py",
    f"{prefix}scripts/run-live-api-smoke.py",
    f"{prefix}scripts/verify-source-archive.py",
    f"{prefix}scripts/write-function-min-instances.py",
    f"{prefix}scripts/write-image-digest.py",
    f"{prefix}scripts/write-live-evaluation.py",
    f"{prefix}scripts/write-public-links.py",
}
names: list[str] = []
violations: list[str] = []
with tarfile.open(archive_path, "r:gz") as archive:
    for member in archive.getmembers():
        name = member.name
        names.append(name)
        if name.startswith("/") or "/../" in f"/{name}/" or not name.startswith(prefix):
            violations.append(f"unsafe path: {name}")
            continue
        relative = name[len(prefix):]
        parts = [part for part in relative.split("/") if part]
        if any(part == ".env" or (part.startswith(".env.") and part != ".env.example") for part in parts):
            violations.append(f"secret env file: {name}")
        if any(part in forbidden_parts for part in parts):
            violations.append(f"generated/private path: {name}")
        if relative.endswith(forbidden_suffixes):
            violations.append(f"generated file suffix: {name}")

missing = sorted(required_entries - set(names))
if missing:
    violations.extend(f"missing required entry: {entry}" for entry in missing)
if violations:
    print("\n".join(violations), file=sys.stderr)
    raise SystemExit(1)

print(json.dumps(
    {
        "archive": archive_path,
        "commit": commit_sha,
        "entries": len(names),
        "prefix": prefix,
        "worktree_dirty": worktree_dirty == "true",
    },
    sort_keys=True,
))
PY

mv "$tmp_archive" "$OUTPUT"
python3 - "$OUTPUT" > "${OUTPUT}.sha256" <<'PY'
import hashlib
import sys
from pathlib import Path

path = Path(sys.argv[1])
digest = hashlib.sha256(path.read_bytes()).hexdigest()
print(f"{digest}  {path.as_posix()}")
PY

echo "source_archive=$OUTPUT"
cat "${OUTPUT}.sha256"
python3 scripts/verify-source-archive.py --archive "$OUTPUT" --checksum "${OUTPUT}.sha256" --prefix "$PREFIX"
