#!/usr/bin/env python3
from __future__ import annotations

import argparse
import hashlib
import json
import os
import stat
import subprocess
import tarfile
from dataclasses import dataclass
from pathlib import Path


DEFAULT_ARCHIVE = Path("submission/source-archive.tar.gz")
DEFAULT_PREFIX = "directorgraph/"
ARCHIVE_EXCLUDES = {
    "submission/source-archive.tar.gz",
    "submission/source-archive.tar.gz.sha256",
}
FORBIDDEN_PARTS = {
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
FORBIDDEN_SUFFIXES = (".pyc", ".pyo", ".db", ".sqlite3", ".tsbuildinfo", ".DS_Store")
REQUIRED_RELATIVE_PATHS = {
    "README.md",
    "Dockerfile",
    "scripts/build-source-archive.sh",
    "scripts/check-architecture-assets.py",
    "scripts/check-private-oss-access.py",
    "scripts/check-submission-artifacts.py",
    "scripts/run-image-vulnerability-scan.sh",
    "scripts/run-deployment-task-smoke.py",
    "scripts/run-live-api-smoke.py",
    "scripts/verify-source-archive.py",
    "scripts/write-function-min-instances.py",
    "scripts/write-image-digest.py",
    "scripts/write-live-evaluation.py",
    "scripts/write-public-links.py",
}


@dataclass(frozen=True)
class ExpectedEntry:
    archive_name: str
    relative_path: str
    kind: str
    mode: int
    sha256: str | None = None
    linkname: str | None = None


def parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(description="Verify DirectorGraph source archive integrity.")
    parser.add_argument("--archive", type=Path, default=DEFAULT_ARCHIVE)
    parser.add_argument("--checksum", type=Path)
    parser.add_argument("--prefix", default=DEFAULT_PREFIX)
    return parser.parse_args()


def fail(violations: list[str]) -> None:
    raise SystemExit("\n".join(violations))


def read_checksum(archive_path: Path, checksum_path: Path) -> str:
    line = checksum_path.read_text(encoding="utf-8").strip()
    parts = line.split()
    if len(parts) != 2:
        raise ValueError(f"checksum file must contain '<sha256>  <path>': {checksum_path}")
    digest, recorded_path = parts
    if recorded_path != archive_path.as_posix():
        raise ValueError(f"checksum path mismatch: {recorded_path!r}")
    if len(digest) != 64 or any(char not in "0123456789abcdef" for char in digest):
        raise ValueError(f"checksum digest is not lowercase SHA-256: {digest!r}")
    actual = hashlib.sha256(archive_path.read_bytes()).hexdigest()
    if actual != digest:
        raise ValueError("source archive checksum mismatch")
    return actual


def tracked_paths() -> list[Path]:
    raw = subprocess.check_output(["git", "ls-files", "-z"])
    paths = []
    for item in raw.split(b"\0"):
        if not item:
            continue
        relative = item.decode("utf-8")
        if relative in ARCHIVE_EXCLUDES:
            continue
        paths.append(Path(relative))
    return sorted(paths, key=lambda path: path.as_posix())


def expected_entries(prefix: str) -> dict[str, ExpectedEntry]:
    expected: dict[str, ExpectedEntry] = {}
    violations: list[str] = []
    for path in tracked_paths():
        relative = path.as_posix()
        archive_name = f"{prefix}{relative}"
        if not path.exists():
            violations.append(f"tracked path is missing from working tree: {relative}")
            continue
        if path.is_dir():
            continue
        if path.is_symlink():
            mode = 0o755 if os.access(path, os.X_OK) else 0o644
            expected[archive_name] = ExpectedEntry(
                archive_name=archive_name,
                relative_path=relative,
                kind="symlink",
                mode=mode,
                linkname=os.readlink(path),
            )
            continue
        if not path.is_file():
            violations.append(f"tracked path is not a regular file or symlink: {relative}")
            continue
        executable = bool(path.stat().st_mode & stat.S_IXUSR)
        mode = 0o755 if executable else 0o644
        expected[archive_name] = ExpectedEntry(
            archive_name=archive_name,
            relative_path=relative,
            kind="file",
            mode=mode,
            sha256=hashlib.sha256(path.read_bytes()).hexdigest(),
        )
    if violations:
        fail(violations)
    return expected


def path_violations(name: str, prefix: str) -> list[str]:
    violations: list[str] = []
    if name.startswith("/") or "/../" in f"/{name}/" or not name.startswith(prefix):
        return [f"unsafe path: {name}"]
    relative = name[len(prefix) :]
    parts = [part for part in relative.split("/") if part]
    if not relative or any(part in {"", ".", ".."} for part in relative.split("/")):
        violations.append(f"unsafe path: {name}")
    if any(part == ".env" or (part.startswith(".env.") and part != ".env.example") for part in parts):
        violations.append(f"secret env file: {name}")
    if any(part in FORBIDDEN_PARTS for part in parts):
        violations.append(f"generated/private path: {name}")
    if relative.endswith(FORBIDDEN_SUFFIXES):
        violations.append(f"generated file suffix: {name}")
    return violations


def verify_members(archive_path: Path, prefix: str, expected: dict[str, ExpectedEntry]) -> int:
    seen: set[str] = set()
    violations: list[str] = []
    with tarfile.open(archive_path, "r:gz") as archive:
        for member in archive.getmembers():
            name = member.name
            if name in seen:
                violations.append(f"duplicate archive entry: {name}")
                continue
            seen.add(name)
            violations.extend(path_violations(name, prefix))
            expected_entry = expected.get(name)
            if expected_entry is None:
                violations.append(f"unexpected archive entry: {name}")
                continue
            actual_mode = member.mode & 0o777
            if actual_mode != expected_entry.mode:
                violations.append(
                    f"mode mismatch for {name}: expected {expected_entry.mode:o}, got {actual_mode:o}"
                )
            if expected_entry.kind == "symlink":
                if not member.issym():
                    violations.append(f"type mismatch for {name}: expected symlink")
                elif member.linkname != expected_entry.linkname:
                    violations.append(f"symlink target mismatch for {name}")
                continue
            if not member.isfile():
                violations.append(f"type mismatch for {name}: expected file")
                continue
            extracted = archive.extractfile(member)
            if extracted is None:
                violations.append(f"cannot read archive member: {name}")
                continue
            actual_sha = hashlib.sha256(extracted.read()).hexdigest()
            if actual_sha != expected_entry.sha256:
                violations.append(f"content mismatch for {name}")

    expected_names = set(expected)
    missing = sorted(expected_names - seen)
    extra = sorted(seen - expected_names)
    violations.extend(f"missing tracked entry: {name}" for name in missing)
    violations.extend(f"unexpected archive entry: {name}" for name in extra if name not in expected)
    required = {f"{prefix}{relative}" for relative in REQUIRED_RELATIVE_PATHS}
    violations.extend(f"missing required entry: {name}" for name in sorted(required - seen))
    if violations:
        fail(violations)
    return len(seen)


def main() -> None:
    args = parse_args()
    archive_path = args.archive
    checksum_path = args.checksum or Path(f"{archive_path.as_posix()}.sha256")
    prefix = args.prefix
    if not prefix.endswith("/"):
        raise SystemExit(f"archive prefix must end with '/': {prefix}")
    if not archive_path.is_file():
        raise SystemExit(f"source archive is missing: {archive_path}")
    if not checksum_path.is_file():
        raise SystemExit(f"source archive checksum is missing: {checksum_path}")

    digest = read_checksum(archive_path, checksum_path)
    expected = expected_entries(prefix)
    entries = verify_members(archive_path, prefix, expected)
    print(
        json.dumps(
            {
                "archive": archive_path.as_posix(),
                "checksum": checksum_path.as_posix(),
                "entries": entries,
                "matched_tracked_files": len(expected),
                "prefix": prefix,
                "schema": "directorgraph.source-archive-verification.v1",
                "sha256": digest,
            },
            sort_keys=True,
        )
    )


if __name__ == "__main__":
    main()
