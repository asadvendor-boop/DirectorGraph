#!/usr/bin/env python3
from __future__ import annotations

import argparse
import json
import os
import subprocess
from datetime import UTC, datetime
from pathlib import Path
from urllib.parse import urlparse

import httpx


DEFAULT_OUTPUT = Path("evidence/deployment/private-oss-access-check.json")
DENIED_STATUSES = {401, 403}


def parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(
        description="Verify that a private OSS object denies anonymous access."
    )
    parser.add_argument("--url", default=os.environ.get("OSS_ANONYMOUS_TEST_URL"))
    parser.add_argument("--output", type=Path, default=DEFAULT_OUTPUT)
    parser.add_argument("--timeout", type=float, default=20.0)
    parser.add_argument("--dry-run", action="store_true")
    return parser.parse_args()


def git_commit() -> str:
    try:
        return subprocess.check_output(["git", "rev-parse", "HEAD"], text=True).strip()
    except (OSError, subprocess.CalledProcessError):
        return "unknown"


def assert_public_https_url(value: str | None) -> str:
    if not value:
        raise ValueError("OSS_ANONYMOUS_TEST_URL or --url is required")
    parsed = urlparse(value)
    host = parsed.hostname or ""
    if parsed.scheme != "https" or not parsed.netloc:
        raise ValueError("OSS anonymous test URL must be an https:// URL")
    if host in {"localhost", "0.0.0.0"} or host.startswith("127.") or host == "::1":
        raise ValueError("OSS anonymous test URL must not point to a local host")
    lowered = value.lower()
    for marker in ("todo", "tbd", "placeholder", "example.com", "<missing>"):
        if marker in lowered:
            raise ValueError("OSS anonymous test URL contains placeholder text")
    return value


def redacted_url(value: str) -> str:
    parsed = urlparse(value)
    return f"{parsed.scheme}://{parsed.netloc}{parsed.path}"


def dry_run_payload() -> dict:
    return {
        "schema": "directorgraph.private-oss-access-plan.v1",
        "output": DEFAULT_OUTPUT.as_posix(),
        "required": ["OSS_ANONYMOUS_TEST_URL or --url for a private OSS object"],
        "policy": "anonymous HEAD/GET must return 401 or 403",
    }


def main() -> None:
    args = parse_args()
    if args.dry_run:
        print(json.dumps(dry_run_payload(), indent=2, sort_keys=True))
        return
    url = assert_public_https_url(args.url)
    attempts = []
    with httpx.Client(timeout=httpx.Timeout(args.timeout, connect=min(args.timeout, 10.0))) as client:
        for method in ("HEAD", "GET"):
            response = client.request(method, url, follow_redirects=False)
            attempts.append(
                {
                    "method": method,
                    "status_code": response.status_code,
                    "headers": {
                        key.lower(): value
                        for key, value in response.headers.items()
                        if key.lower() in {"server", "x-oss-request-id", "x-oss-server-time", "content-type"}
                    },
                }
            )
            if response.status_code in DENIED_STATUSES:
                break
    if not any(item["status_code"] in DENIED_STATUSES for item in attempts):
        raise SystemExit(f"Anonymous OSS access was not denied: {attempts}")
    payload = {
        "schema": "directorgraph.private-oss-access-check.v1",
        "generated_at": datetime.now(UTC).isoformat(),
        "repository_commit": git_commit(),
        "target_url": redacted_url(url),
        "anonymous_access": "denied",
        "expected_statuses": sorted(DENIED_STATUSES),
        "attempts": attempts,
    }
    args.output.parent.mkdir(parents=True, exist_ok=True)
    args.output.write_text(json.dumps(payload, indent=2, sort_keys=True) + "\n", encoding="utf-8")
    print(f"private_oss_access_check={args.output.as_posix()}")


if __name__ == "__main__":
    try:
        main()
    except ValueError as exc:
        raise SystemExit(str(exc)) from exc
