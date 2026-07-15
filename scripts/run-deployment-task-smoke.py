#!/usr/bin/env python3
from __future__ import annotations

import argparse
import json
import os
import re
import subprocess
import sys
import time
from datetime import UTC, datetime
from pathlib import Path
from typing import Any
from urllib.error import HTTPError, URLError
from urllib.parse import urlparse
from urllib.request import Request, urlopen


SECRET_RE = re.compile(
    r"(AKID[A-Za-z0-9]{12,}|LTAI[A-Za-z0-9]{10,}|sk-[A-Za-z0-9_-]{12,}|"
    r"BEGIN (RSA |EC |OPENSSH )?PRIVATE KEY|Bearer\s+[A-Za-z0-9._~+/=-]{8,})",
    re.IGNORECASE,
)
SUCCESS_TASK_STATUSES = {"succeeded", "success", "completed", "passed"}
TERMINAL_TASK_STATUSES = SUCCESS_TASK_STATUSES | {"failed", "canceled", "cancelled"}


def parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(
        description="Run deployed DirectorGraph task smokes and write final submission JSON evidence."
    )
    parser.add_argument(
        "--mode",
        choices=("mock-task", "live-judge-test"),
        required=True,
        help="mock-task creates a no-paid project and runs it; live-judge-test calls /api/judge-test.",
    )
    parser.add_argument("--base-url", default=os.environ.get("DIRECTORGRAPH_BASE_URL", ""))
    parser.add_argument("--output", type=Path)
    parser.add_argument("--judge-code", default=os.environ.get("DIRECTORGRAPH_JUDGE_CODE", ""))
    parser.add_argument("--timeout-seconds", type=int, default=900)
    parser.add_argument("--poll-seconds", type=float, default=3.0)
    parser.add_argument(
        "--allow-local",
        action="store_true",
        help="Allow localhost/HTTP URLs for local dev smoke evidence only; never use for final artifacts.",
    )
    parser.add_argument(
        "--dry-run",
        action="store_true",
        help="Print the evidence contract without contacting the deployed app or writing final artifacts.",
    )
    return parser.parse_args()


def now() -> str:
    return datetime.now(UTC).isoformat()


def git_commit() -> str:
    try:
        return subprocess.check_output(["git", "rev-parse", "HEAD"], text=True).strip()
    except (OSError, subprocess.CalledProcessError):
        return "unknown"


def default_output(mode: str) -> Path:
    if mode == "mock-task":
        return Path("evidence/deployment/mock-task-smoke.json")
    return Path("evidence/deployment/live-judge-test-smoke.json")


def validate_base_url(base_url: str, *, allow_local: bool) -> str:
    if not base_url.strip():
        raise SystemExit("DIRECTORGRAPH_BASE_URL or --base-url is required.")
    normalized = base_url.rstrip("/")
    parsed = urlparse(normalized)
    host = parsed.hostname or ""
    local = host in {"localhost", "0.0.0.0", "::1"} or host.startswith("127.")
    if allow_local:
        if parsed.scheme not in {"http", "https"} or not parsed.netloc:
            raise SystemExit("--base-url must be an HTTP(S) URL.")
        return normalized
    if parsed.scheme != "https" or not parsed.netloc or local:
        raise SystemExit("Final deployment task smokes require a non-local https:// base URL.")
    return normalized


def request_json(
    method: str,
    base_url: str,
    path: str,
    *,
    payload: dict[str, Any] | None = None,
    headers: dict[str, str] | None = None,
    timeout: int = 60,
) -> dict[str, Any]:
    body = None
    request_headers = {"Accept": "application/json"}
    if payload is not None:
        body = json.dumps(payload).encode("utf-8")
        request_headers["Content-Type"] = "application/json"
    if headers:
        request_headers.update(headers)
    request = Request(f"{base_url}{path}", data=body, headers=request_headers, method=method)
    try:
        with urlopen(request, timeout=timeout) as response:
            raw = response.read().decode("utf-8")
    except HTTPError as exc:
        detail = exc.read().decode("utf-8", errors="replace")
        raise RuntimeError(f"{method} {path} returned HTTP {exc.code}: {detail[:500]}") from exc
    except URLError as exc:
        raise RuntimeError(f"{method} {path} failed: {exc.reason}") from exc
    try:
        parsed = json.loads(raw)
    except json.JSONDecodeError as exc:
        raise RuntimeError(f"{method} {path} returned non-JSON response") from exc
    if not isinstance(parsed, dict):
        raise RuntimeError(f"{method} {path} returned a non-object JSON payload")
    return parsed


def status_value(payload: dict[str, Any] | None) -> str:
    if not payload:
        return ""
    return str(payload.get("status") or "").lower()


def url_host(value: str | None) -> str | None:
    if not value:
        return None
    parsed = urlparse(value)
    return parsed.netloc or None


def run_submission(mode: str, base_url: str, judge_code: str) -> tuple[dict[str, Any], dict[str, Any]]:
    if mode == "live-judge-test":
        if not judge_code:
            raise SystemExit("DIRECTORGRAPH_JUDGE_CODE or --judge-code is required for live Judge Test smoke.")
        submission = request_json(
            "POST",
            base_url,
            "/api/judge-test",
            payload={},
            headers={"X-DirectorGraph-Judge-Code": judge_code},
        )
        return submission, {"judge_code_supplied": True, "route": "/api/judge-test"}

    brief = {
        "title": "Deployment Smoke: Contract Repair",
        "premise": (
            "A tiny serverless smoke production verifies that DirectorGraph can queue, "
            "execute, inspect, and complete a no-paid task."
        ),
        "genre": "science-fiction drama",
        "tone": "concise, cinematic, audit-focused",
        "target_audience": "hackathon deployment reviewers",
        "duration_seconds": 5,
        "aspect_ratio": "9:16",
        "language": "English",
        "visual_style": "grounded cinematic realism, controlled lighting",
        "budget_usd": 3,
        "repair_reserve_percent": 20,
        "seed": 20260710,
        "required_prop": "red paper crane",
        "max_shots": 2,
    }
    project = request_json("POST", base_url, "/api/projects", payload=brief)
    project_id = str(project.get("id") or "")
    if not project_id:
        raise RuntimeError("Project creation response did not include an id")
    submission = request_json("POST", base_url, f"/api/projects/{project_id}/run")
    return submission, {
        "created_project_id": project_id,
        "duration_seconds": brief["duration_seconds"],
        "max_shots": brief["max_shots"],
        "route": f"/api/projects/{project_id}/run",
    }


def poll_until_complete(
    base_url: str,
    task_id: str,
    project_id: str,
    *,
    timeout_seconds: int,
    poll_seconds: float,
) -> tuple[dict[str, Any], dict[str, Any], list[dict[str, Any]]]:
    deadline = time.monotonic() + timeout_seconds
    observations: list[dict[str, Any]] = []
    last_task: dict[str, Any] = {}
    last_project: dict[str, Any] = {}
    while time.monotonic() < deadline:
        last_task = request_json("GET", base_url, f"/api/tasks/{task_id}")
        last_project = request_json("GET", base_url, f"/api/projects/{project_id}")
        task_status = status_value(last_task)
        project_status = status_value(last_project)
        observations.append(
            {
                "observed_at": now(),
                "project_status": project_status,
                "task_status": task_status,
            }
        )
        if task_status in SUCCESS_TASK_STATUSES and project_status == "completed":
            return last_task, last_project, observations
        if task_status in TERMINAL_TASK_STATUSES and task_status not in SUCCESS_TASK_STATUSES:
            break
        if project_status == "failed":
            break
        time.sleep(poll_seconds)
    raise RuntimeError(
        "Task smoke did not complete successfully: "
        f"task_status={status_value(last_task)!r}, project_status={status_value(last_project)!r}"
    )


def safe_manifest_summary(manifest: dict[str, Any]) -> dict[str, Any]:
    project = manifest.get("project")
    audit_trail = manifest.get("audit_trail")
    return {
        "schema": manifest.get("schema"),
        "project_status": project.get("status") if isinstance(project, dict) else None,
        "audit_trail_count": len(audit_trail) if isinstance(audit_trail, list) else 0,
    }


def safe_storage_summary(storage_manifest: dict[str, Any] | None) -> dict[str, Any] | None:
    if not storage_manifest:
        return None
    signed_objects = storage_manifest.get("signed_objects")
    manifest = storage_manifest.get("manifest")
    manifest_ref = storage_manifest.get("manifest_ref")
    return {
        "schema": storage_manifest.get("schema"),
        "manifest_key": manifest_ref.get("key") if isinstance(manifest_ref, dict) else None,
        "object_key_count": len(manifest.get("object_keys", [])) if isinstance(manifest, dict) else 0,
        "signed_object_count": len(signed_objects) if isinstance(signed_objects, list) else 0,
    }


def assert_no_secret(path: Path, payload: dict[str, Any]) -> None:
    text = json.dumps(payload, sort_keys=True, ensure_ascii=False)
    if SECRET_RE.search(text):
        raise SystemExit(f"Refusing to write secret-like data to {path}")


def write_json(path: Path, payload: dict[str, Any]) -> None:
    assert_no_secret(path, payload)
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(payload, indent=2, sort_keys=True) + "\n", encoding="utf-8")
    print(f"wrote={path.as_posix()}")


def dry_run_payload(mode: str, output: Path) -> dict[str, Any]:
    return {
        "schema": "directorgraph.deployment-task-smoke-plan.v1",
        "mode": mode,
        "output": output.as_posix(),
        "base_url_env": "DIRECTORGRAPH_BASE_URL",
        "judge_code_env": "DIRECTORGRAPH_JUDGE_CODE" if mode == "live-judge-test" else None,
        "writes_signed_urls": False,
        "stores_judge_code": False,
    }


def main() -> None:
    args = parse_args()
    output = args.output or default_output(args.mode)
    if args.dry_run:
        print(json.dumps(dry_run_payload(args.mode, output), indent=2, sort_keys=True))
        return

    base_url = validate_base_url(args.base_url, allow_local=args.allow_local)
    started_at = now()
    health = request_json("GET", base_url, "/api/health")
    readiness = request_json("GET", base_url, "/api/readiness")
    config = request_json("GET", base_url, "/api/config")
    expected_provider = "mock" if args.mode == "mock-task" else "live"
    provider_mode = str(config.get("provider_mode") or health.get("provider_mode") or "")
    if provider_mode != expected_provider:
        raise RuntimeError(f"Expected provider_mode={expected_provider}, got {provider_mode!r}")

    submission, request_summary = run_submission(args.mode, base_url, args.judge_code)
    task_id = str(submission.get("task_id") or "")
    project_id = str(submission.get("project_id") or request_summary.get("created_project_id") or "")
    if not task_id or not project_id:
        raise RuntimeError("Submission response must include task_id and project_id")

    task, project, observations = poll_until_complete(
        base_url,
        task_id,
        project_id,
        timeout_seconds=args.timeout_seconds,
        poll_seconds=args.poll_seconds,
    )
    manifest = request_json("GET", base_url, f"/api/projects/{project_id}/manifest")
    try:
        storage_manifest = request_json("GET", base_url, f"/api/projects/{project_id}/storage-manifest")
    except RuntimeError:
        storage_manifest = None

    final_video_host = url_host(project.get("final_video_url"))
    checks = {
        "app_mode_web": health.get("mode") == "web",
        "expected_provider_mode": provider_mode == expected_provider,
        "readiness_ready_or_mock": readiness.get("status") == "ready" or expected_provider == "mock",
        "submission_returned_task_id": bool(task_id),
        "task_succeeded": status_value(task) in SUCCESS_TASK_STATUSES,
        "project_completed": status_value(project) == "completed",
        "final_video_available": bool(project.get("final_video_url")),
        "production_manifest_available": manifest.get("schema") == "directorgraph.production-manifest.v1",
        "storage_manifest_checked": storage_manifest is not None,
    }

    payload = {
        "schema": "directorgraph.deployment-task-smoke.v1",
        "smoke_type": args.mode.replace("-", "_"),
        "status": "pass" if all(checks.values()) else "fail",
        "repository_commit": git_commit(),
        "started_at": started_at,
        "completed_at": now(),
        "base_url": base_url,
        "provider_mode": provider_mode,
        "request": request_summary,
        "submission": {
            "project_id": project_id,
            "job_id": submission.get("job_id"),
            "task_id": task_id,
            "initial_status": submission.get("status"),
        },
        "task": {
            "id": task.get("id"),
            "task_id": task_id,
            "status": task.get("status"),
            "attempts": task.get("attempts"),
            "function_compute_request_id_present": bool(task.get("function_compute_request_id")),
            "durable_status_present": bool(task.get("durable_status")),
        },
        "project": {
            "id": project_id,
            "status": project.get("status"),
            "production_profile": (project.get("brief") or {}).get("production_profile"),
            "duration_seconds": (project.get("brief") or {}).get("duration_seconds"),
            "shot_count": len(project.get("shots") or []),
            "final_video_url_host": final_video_host,
            "accepted_shots": len([shot for shot in project.get("shots") or [] if shot.get("accepted")]),
        },
        "health": {
            "mode": health.get("mode"),
            "provider_mode": health.get("provider_mode"),
            "build_sha": (health.get("build") or {}).get("sha"),
            "live_ready": (health.get("deployment") or {}).get("live_ready"),
            "oss_ready": (health.get("deployment") or {}).get("oss_ready"),
            "function_compute_task_configured": (health.get("deployment") or {}).get(
                "function_compute_task_configured"
            ),
        },
        "readiness": {
            "status": readiness.get("status"),
            "checks": readiness.get("checks"),
        },
        "config": {
            "provider_mode": config.get("provider_mode"),
            "live_ready": config.get("live_ready"),
            "oss_ready": config.get("oss_ready"),
            "judge_access_code_configured": config.get("judge_access_code_configured"),
        },
        "manifest": safe_manifest_summary(manifest),
        "storage_manifest": safe_storage_summary(storage_manifest),
        "observations": observations,
        "checks": checks,
    }
    if payload["status"] != "pass":
        raise RuntimeError(f"Smoke checks did not all pass: {checks}")
    write_json(output, payload)


if __name__ == "__main__":
    try:
        main()
    except Exception as exc:
        print(str(exc), file=sys.stderr)
        raise SystemExit(1) from exc
