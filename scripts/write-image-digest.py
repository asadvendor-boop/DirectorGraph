#!/usr/bin/env python3
from __future__ import annotations

import argparse
import json
import os
import re
import subprocess
from datetime import UTC, datetime
from pathlib import Path


DEFAULT_OUTPUT = Path("evidence/deployment/image-digest.json")
DIGEST_RE = re.compile(r"^sha256:[a-f0-9]{64}$")


def parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(description="Write shared immutable image digest evidence.")
    parser.add_argument("--output", type=Path, default=DEFAULT_OUTPUT)
    parser.add_argument("--image-uri", default=os.environ.get("ACR_IMAGE_URI"))
    parser.add_argument("--image-digest", default=os.environ.get("IMAGE_DIGEST"))
    parser.add_argument("--web-image-digest", default=os.environ.get("WEB_IMAGE_DIGEST"))
    parser.add_argument("--task-image-digest", default=os.environ.get("TASK_IMAGE_DIGEST"))
    parser.add_argument("--web-function", default=os.environ.get("FC_WEB_FUNCTION_NAME"))
    parser.add_argument("--task-function", default=os.environ.get("FC_TASK_FUNCTION_NAME"))
    parser.add_argument("--dry-run", action="store_true")
    return parser.parse_args()


def git_commit() -> str:
    try:
        return subprocess.check_output(["git", "rev-parse", "HEAD"], text=True).strip()
    except (OSError, subprocess.CalledProcessError):
        return "unknown"


def require_digest(label: str, value: str | None) -> str:
    if not value or not DIGEST_RE.match(value):
        raise ValueError(f"{label} must be sha256:<64 lowercase hex>")
    return value


def optional_non_placeholder(label: str, value: str | None) -> str | None:
    if not value:
        return None
    lowered = value.lower()
    for marker in ("todo", "tbd", "placeholder", "example.com", "<missing>"):
        if marker in lowered:
            raise ValueError(f"{label} contains placeholder text")
    return value


def build_payload(args: argparse.Namespace) -> dict:
    shared = args.image_digest
    web_digest = args.web_image_digest or shared
    task_digest = args.task_image_digest or shared
    web_digest = require_digest("web_image_digest", web_digest)
    task_digest = require_digest("task_image_digest", task_digest)
    if web_digest != task_digest:
        raise ValueError("web and task functions must use the same immutable image digest")
    return {
        "schema": "directorgraph.image-digest.v1",
        "generated_at": datetime.now(UTC).isoformat(),
        "repository_commit": git_commit(),
        "image_uri": optional_non_placeholder("image_uri", args.image_uri),
        "shared_image_digest": web_digest,
        "functions": [
            {
                "role": "web",
                "function_name": optional_non_placeholder("web_function", args.web_function),
                "image_digest": web_digest,
            },
            {
                "role": "task",
                "function_name": optional_non_placeholder("task_function", args.task_function),
                "image_digest": task_digest,
            },
        ],
    }


def dry_run_payload() -> dict:
    return {
        "schema": "directorgraph.image-digest-plan.v1",
        "output": DEFAULT_OUTPUT.as_posix(),
        "required": ["IMAGE_DIGEST or both WEB_IMAGE_DIGEST and TASK_IMAGE_DIGEST"],
        "policy": "web and task Function Compute functions must use the same sha256 image digest",
    }


def main() -> None:
    args = parse_args()
    if args.dry_run:
        print(json.dumps(dry_run_payload(), indent=2, sort_keys=True))
        return
    payload = build_payload(args)
    args.output.parent.mkdir(parents=True, exist_ok=True)
    args.output.write_text(json.dumps(payload, indent=2, sort_keys=True) + "\n", encoding="utf-8")
    print(f"image_digest={args.output.as_posix()}")


if __name__ == "__main__":
    try:
        main()
    except ValueError as exc:
        raise SystemExit(str(exc)) from exc
