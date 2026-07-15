#!/usr/bin/env python3
from __future__ import annotations

import argparse
import json
import os
import re
import subprocess
from datetime import UTC, datetime
from pathlib import Path
from urllib.parse import urlparse


DEFAULT_OUTPUT_JSON = Path("evals/live-results.json")
DEFAULT_OUTPUT_MD = Path("evals/live-results.md")
DIGEST_RE = re.compile(r"^sha256:[a-f0-9]{64}$")


def parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(
        description="Promote a verified live ablation report to final live-results artifacts."
    )
    parser.add_argument("--eval-report-json", type=Path, default=Path("evals/eval-report.json"))
    parser.add_argument("--eval-report-md", type=Path, default=Path("evals/eval-report.md"))
    parser.add_argument("--output-json", type=Path, default=DEFAULT_OUTPUT_JSON)
    parser.add_argument("--output-md", type=Path, default=DEFAULT_OUTPUT_MD)
    parser.add_argument("--web-url", default=os.environ.get("PUBLIC_APP_URL"))
    parser.add_argument("--image-digest", default=os.environ.get("IMAGE_DIGEST"))
    parser.add_argument("--task-id", default=os.environ.get("DIRECTORGRAPH_TASK_ID"))
    parser.add_argument("--run-kind", default=os.environ.get("EVALUATION_RUN_KIND", "flagship_live"))
    parser.add_argument(
        "--limitation",
        action="append",
        default=[],
        help="Known limitation to include. May be provided multiple times.",
    )
    parser.add_argument(
        "--dry-run",
        action="store_true",
        help="Print the required fields and exit without writing live-results artifacts.",
    )
    return parser.parse_args()


def git_commit() -> str:
    try:
        return subprocess.check_output(["git", "rev-parse", "HEAD"], text=True).strip()
    except (OSError, subprocess.CalledProcessError):
        return "unknown"


def assert_https_url(label: str, value: str | None) -> str:
    if not value:
        raise ValueError(f"{label} is required")
    parsed = urlparse(value)
    host = parsed.hostname or ""
    if parsed.scheme != "https" or not parsed.netloc:
        raise ValueError(f"{label} must be an https:// URL")
    if host in {"localhost", "0.0.0.0"} or host.startswith("127.") or host == "::1":
        raise ValueError(f"{label} must not point to a local host")
    lowered = value.lower()
    for marker in ("todo", "tbd", "placeholder", "example.com", "<missing>"):
        if marker in lowered:
            raise ValueError(f"{label} contains placeholder text")
    return value


def assert_digest(value: str | None) -> str:
    if not value or not DIGEST_RE.match(value):
        raise ValueError("image_digest must be sha256:<64 lowercase hex>")
    return value


def load_report(path: Path) -> dict:
    if not path.is_file():
        raise ValueError(f"Missing eval report JSON: {path}")
    data = json.loads(path.read_text(encoding="utf-8"))
    if data.get("schema") != "directorgraph.eval-report.v1":
        raise ValueError(f"{path}: expected directorgraph.eval-report.v1")
    if "baselines" not in data or "repair_efficiency" not in data:
        raise ValueError(f"{path}: missing required eval report metrics")
    return data


def build_payload(args: argparse.Namespace, report: dict) -> dict:
    limitations = list(args.limitation) or [
        "Live provider quality claims are limited to the referenced production run and provider/account state at generation time."
    ]
    return {
        "schema": "directorgraph.live-evaluation.v1",
        "generated_at": datetime.now(UTC).isoformat(),
        "repository_commit": git_commit(),
        "deployment": {
            "web_url": assert_https_url("web_url", args.web_url),
            "image_digest": assert_digest(args.image_digest),
        },
        "runs": [
            {
                "kind": args.run_kind,
                "project_id": report.get("project_id"),
                "project_title": report.get("project_title"),
                "task_id": args.task_id,
                "mode": report.get("mode"),
                "source_report_schema": report.get("schema"),
            }
        ],
        "metrics": {
            "baselines": report.get("baselines"),
            "delta": report.get("delta"),
            "repair_efficiency": report.get("repair_efficiency"),
            "resource_ledger": report.get("resource_ledger"),
            "rejected_attempts": report.get("rejected_attempts", []),
        },
        "limitations": limitations,
    }


def markdown(payload: dict, source_markdown: str) -> str:
    deployment = payload["deployment"]
    run = payload["runs"][0]
    return f"""# DirectorGraph Live Evaluation Results

Generated: {payload['generated_at']}

Repository commit: `{payload['repository_commit']}`

Deployment: {deployment['web_url']}

Image digest: `{deployment['image_digest']}`

Project: `{run.get('project_id')}` ({run.get('project_title')})

Task: `{run.get('task_id') or 'not recorded'}`

## Limitations

{chr(10).join(f'- {item}' for item in payload['limitations'])}

## Source Ablation Report

{source_markdown.strip()}
"""


def dry_run_payload() -> dict:
    return {
        "schema": "directorgraph.live-evaluation-plan.v1",
        "outputs": [DEFAULT_OUTPUT_JSON.as_posix(), DEFAULT_OUTPUT_MD.as_posix()],
        "required_inputs": [
            "evals/eval-report.json from a live production database or OSS-recovered read model",
            "evals/eval-report.md from the same live report",
            "PUBLIC_APP_URL as a public HTTPS URL",
            "IMAGE_DIGEST as sha256:<64 lowercase hex>",
        ],
    }


def main() -> None:
    args = parse_args()
    if args.dry_run:
        print(json.dumps(dry_run_payload(), indent=2, sort_keys=True))
        return
    report = load_report(args.eval_report_json)
    source_md = args.eval_report_md.read_text(encoding="utf-8")
    payload = build_payload(args, report)
    args.output_json.parent.mkdir(parents=True, exist_ok=True)
    args.output_json.write_text(json.dumps(payload, indent=2, sort_keys=True) + "\n", encoding="utf-8")
    args.output_md.write_text(markdown(payload, source_md), encoding="utf-8")
    print(f"live_results_json={args.output_json.as_posix()}")
    print(f"live_results_md={args.output_md.as_posix()}")


if __name__ == "__main__":
    try:
        main()
    except (ValueError, json.JSONDecodeError) as exc:
        raise SystemExit(str(exc)) from exc
