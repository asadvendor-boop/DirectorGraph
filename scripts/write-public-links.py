#!/usr/bin/env python3
from __future__ import annotations

import argparse
import json
import os
import subprocess
from datetime import UTC, datetime
from pathlib import Path
from urllib.parse import urlparse


DEFAULT_OUTPUT = Path("submission/public-links.json")
URL_FIELDS = (
    "repository_url",
    "public_app_url",
    "demo_video_url",
    "proof_url",
    "devpost_url",
    "deployment_proof_url",
)


def parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(description="Write the final public-links submission artifact.")
    parser.add_argument("--output", type=Path, default=DEFAULT_OUTPUT)
    parser.add_argument("--repository-url", default=os.environ.get("PUBLIC_REPOSITORY_URL"))
    parser.add_argument("--public-app-url", default=os.environ.get("PUBLIC_APP_URL"))
    parser.add_argument("--demo-video-url", default=os.environ.get("DEMO_VIDEO_URL"))
    parser.add_argument("--proof-url", default=os.environ.get("PROOF_URL"))
    parser.add_argument("--devpost-url", default=os.environ.get("DEVPOST_URL"))
    parser.add_argument("--deployment-proof-url", default=os.environ.get("DEPLOYMENT_PROOF_URL"))
    parser.add_argument(
        "--dry-run",
        action="store_true",
        help="Print the required fields and exit without writing submission/public-links.json.",
    )
    return parser.parse_args()


def git_commit() -> str:
    try:
        return subprocess.check_output(["git", "rev-parse", "HEAD"], text=True).strip()
    except (OSError, subprocess.CalledProcessError):
        return "unknown"


def assert_https_url(field: str, value: str | None, *, required: bool = True) -> str | None:
    if not value:
        if required:
            raise ValueError(f"{field} is required")
        return None
    parsed = urlparse(value)
    host = parsed.hostname or ""
    if parsed.scheme != "https" or not parsed.netloc:
        raise ValueError(f"{field} must be an https:// URL")
    if host in {"localhost", "0.0.0.0"} or host.startswith("127.") or host == "::1":
        raise ValueError(f"{field} must not point to a local host")
    lowered = value.lower()
    for marker in ("todo", "tbd", "placeholder", "example.com", "<missing>"):
        if marker in lowered:
            raise ValueError(f"{field} contains placeholder text")
    return value


def build_payload(args: argparse.Namespace) -> dict:
    proof_url = assert_https_url("proof_url", args.proof_url, required=False)
    devpost_url = assert_https_url("devpost_url", args.devpost_url, required=False)
    deployment_proof_url = assert_https_url(
        "deployment_proof_url",
        args.deployment_proof_url,
        required=False,
    )
    if not any((proof_url, devpost_url, deployment_proof_url)):
        raise ValueError("At least one of proof_url, devpost_url, or deployment_proof_url is required")
    return {
        "schema": "directorgraph.public-links.v1",
        "generated_at": datetime.now(UTC).isoformat(),
        "repository_commit": git_commit(),
        "repository_url": assert_https_url("repository_url", args.repository_url),
        "public_app_url": assert_https_url("public_app_url", args.public_app_url),
        "demo_video_url": assert_https_url("demo_video_url", args.demo_video_url),
        "proof_url": proof_url,
        "devpost_url": devpost_url,
        "deployment_proof_url": deployment_proof_url,
    }


def dry_run_payload() -> dict:
    return {
        "schema": "directorgraph.public-links-plan.v1",
        "output": DEFAULT_OUTPUT.as_posix(),
        "required": ["PUBLIC_REPOSITORY_URL", "PUBLIC_APP_URL", "DEMO_VIDEO_URL"],
        "one_of": ["PROOF_URL", "DEVPOST_URL", "DEPLOYMENT_PROOF_URL"],
        "url_policy": "https only; no localhost, loopback, example.com, TODO, TBD, or placeholders",
    }


def main() -> None:
    args = parse_args()
    if args.dry_run:
        print(json.dumps(dry_run_payload(), indent=2, sort_keys=True))
        return
    payload = build_payload(args)
    args.output.parent.mkdir(parents=True, exist_ok=True)
    args.output.write_text(json.dumps(payload, indent=2, sort_keys=True) + "\n", encoding="utf-8")
    print(f"public_links={args.output.as_posix()}")


if __name__ == "__main__":
    try:
        main()
    except ValueError as exc:
        raise SystemExit(str(exc)) from exc
