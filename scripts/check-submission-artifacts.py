#!/usr/bin/env python3
from __future__ import annotations

import json
import re
from collections.abc import Iterable
from pathlib import Path
from urllib.parse import urlparse


ARTIFACTS = {
    "live_results": Path("evals/live-results.json"),
    "model_smoke": Path("evidence/live-api/model-smoke.json"),
    "redacted_fixtures": Path("evidence/live-api/redacted-response-fixtures.json"),
    "serverless_verification": Path("evidence/deployment/serverless-live-verification.json"),
    "private_oss": Path("evidence/deployment/private-oss-access-check.json"),
    "image_digest": Path("evidence/deployment/image-digest.json"),
    "live_judge_smoke": Path("evidence/deployment/live-judge-test-smoke.json"),
    "public_links": Path("submission/public-links.json"),
}
PLACEHOLDER_RE = re.compile(
    r"(\bTODO\b|\bTBD\b|\bFIXME\b|\bPLACEHOLDER\b|<missing>|example\.com|localhost|127\.0\.0\.1|0\.0\.0\.0)",
    re.IGNORECASE,
)
SECRET_RE = re.compile(
    r"(AKID[A-Za-z0-9]{12,}|LTAI[A-Za-z0-9]{12,}|sk-[A-Za-z0-9]{20,}|"
    r"OSSAccessKeyId=|[?&]Signature=|BEGIN (RSA |EC |OPENSSH )?PRIVATE KEY)"
)
DIGEST_RE = re.compile(r"^sha256:[a-f0-9]{64}$")
MIN_INSTANCE_KEYS = ("minimum_instances", "min_instances", "minInstanceCount", "minimumInstanceCount")
PROVISION_KEYS = ("provisioned_instances", "provisioned_target", "provisionedInstanceCount")


def load_json(path: Path) -> object:
    try:
        return json.loads(path.read_text(encoding="utf-8"))
    except json.JSONDecodeError as exc:
        raise ValueError(f"{path}: invalid JSON: {exc}") from exc


def walk_strings(value: object) -> Iterable[str]:
    if isinstance(value, str):
        yield value
    elif isinstance(value, dict):
        for item in value.values():
            yield from walk_strings(item)
    elif isinstance(value, list):
        for item in value:
            yield from walk_strings(item)


def walk_dicts(value: object) -> Iterable[dict]:
    if isinstance(value, dict):
        yield value
        for item in value.values():
            yield from walk_dicts(item)
    elif isinstance(value, list):
        for item in value:
            yield from walk_dicts(item)


def assert_no_placeholders(path: Path, value: object) -> None:
    for text in walk_strings(value):
        if PLACEHOLDER_RE.search(text):
            raise ValueError(f"{path}: placeholder or local URL string found: {text!r}")
        if SECRET_RE.search(text):
            raise ValueError(f"{path}: secret-like value found")


def assert_object(path: Path, value: object) -> dict:
    if not isinstance(value, dict):
        raise ValueError(f"{path}: expected a JSON object")
    return value


def assert_https_url(path: Path, label: str, value: object) -> str:
    if not isinstance(value, str) or not value.strip():
        raise ValueError(f"{path}: {label} must be a non-empty HTTPS URL")
    parsed = urlparse(value)
    host = parsed.hostname or ""
    if parsed.scheme != "https" or not parsed.netloc:
        raise ValueError(f"{path}: {label} must use https://")
    if host in {"localhost", "0.0.0.0"} or host.startswith("127.") or host == "::1":
        raise ValueError(f"{path}: {label} must not point to a local host")
    return value


def validate_public_links(path: Path, value: object) -> None:
    data = assert_object(path, value)
    if data.get("schema") != "directorgraph.public-links.v1":
        raise ValueError(f"{path}: schema must be directorgraph.public-links.v1")
    for key in ("repository_url", "public_app_url", "demo_video_url"):
        assert_https_url(path, key, data.get(key))
    proof_keys = ("proof_url", "devpost_url", "deployment_proof_url")
    if not any(data.get(key) for key in proof_keys):
        raise ValueError(f"{path}: one of {', '.join(proof_keys)} is required")
    for key in proof_keys:
        if data.get(key):
            assert_https_url(path, key, data[key])


def validate_live_results(path: Path, value: object) -> None:
    data = assert_object(path, value)
    if data.get("schema") != "directorgraph.live-evaluation.v1":
        raise ValueError(f"{path}: schema must be directorgraph.live-evaluation.v1")
    if not isinstance(data.get("repository_commit"), str) or not data["repository_commit"].strip():
        raise ValueError(f"{path}: repository_commit is required")
    deployment = data.get("deployment")
    if not isinstance(deployment, dict):
        raise ValueError(f"{path}: deployment object is required")
    assert_https_url(path, "deployment.web_url", deployment.get("web_url"))
    digest = deployment.get("image_digest")
    if not isinstance(digest, str) or not DIGEST_RE.match(digest):
        raise ValueError(f"{path}: deployment.image_digest must be sha256:<64 lowercase hex>")
    if not isinstance(data.get("runs"), list) or not data["runs"]:
        raise ValueError(f"{path}: runs must be a non-empty list")
    if not isinstance(data.get("metrics"), dict) or not data["metrics"]:
        raise ValueError(f"{path}: metrics must be a non-empty object")
    if not isinstance(data.get("limitations"), list):
        raise ValueError(f"{path}: limitations must be a list")


def validate_model_smoke(path: Path, value: object) -> None:
    data = assert_object(path, value)
    if data.get("schema") != "directorgraph.live-api-smoke.v1":
        raise ValueError(f"{path}: schema must be directorgraph.live-api-smoke.v1")
    if data.get("status") != "pass":
        raise ValueError(f"{path}: status must be pass")
    for key in ("repository_commit", "provider", "dashscope_region", "base_url_host", "model"):
        if not isinstance(data.get(key), str) or not data[key].strip():
            raise ValueError(f"{path}: {key} is required")
    usage = data.get("usage")
    if not isinstance(usage, dict) or not any(isinstance(value, int) for value in usage.values()):
        raise ValueError(f"{path}: usage must contain provider token counts")
    response = data.get("response")
    if not isinstance(response, dict) or response.get("ok") is not True:
        raise ValueError(f"{path}: response.ok must be true")


def validate_redacted_fixtures(path: Path, value: object) -> None:
    data = assert_object(path, value)
    if data.get("schema") != "directorgraph.redacted-provider-fixtures.v1":
        raise ValueError(f"{path}: schema must be directorgraph.redacted-provider-fixtures.v1")
    fixtures = data.get("fixtures")
    if not isinstance(fixtures, list) or not fixtures:
        raise ValueError(f"{path}: fixtures must be a non-empty list")
    for fixture in fixtures:
        if not isinstance(fixture, dict):
            raise ValueError(f"{path}: each fixture must be an object")
        for key in ("provider", "endpoint_host", "request", "response"):
            if key not in fixture:
                raise ValueError(f"{path}: fixture missing {key}")


def validate_serverless_verification(path: Path, value: object) -> None:
    data = assert_object(path, value)
    if data.get("schema") not in {
        "directorgraph.serverless-live-verification.v1",
        "directorgraph.hybrid-live-verification.v1",
    }:
        raise ValueError(f"{path}: schema must be a DirectorGraph live deployment verification schema")
    assert_https_url(path, "base_url", data.get("base_url"))
    health = data.get("health")
    readiness = data.get("readiness")
    config = data.get("config")
    checks = data.get("checks")
    if not isinstance(health, dict) or health.get("provider_mode") != "live":
        raise ValueError(f"{path}: health.provider_mode must be live")
    if not isinstance(config, dict) or config.get("provider_mode") != "live":
        raise ValueError(f"{path}: config.provider_mode must be live")
    if not isinstance(readiness, dict) or readiness.get("status") != "ready":
        raise ValueError(f"{path}: readiness.status must be ready")
    if not isinstance(checks, dict) or not all(checks.values()):
        raise ValueError(f"{path}: all serverless verification checks must be true")
    evidence_files = data.get("evidence_files")
    required = {"health.json", "readiness.json", "config.json"}
    if not isinstance(evidence_files, list) or not required.issubset(set(evidence_files)):
        raise ValueError(f"{path}: evidence_files must include health/readiness/config JSON")


def validate_image_digest(path: Path, value: object) -> None:
    data = assert_object(path, value)
    if data.get("schema") != "directorgraph.image-digest.v1":
        raise ValueError(f"{path}: schema must be directorgraph.image-digest.v1")
    shared = data.get("shared_image_digest")
    if not isinstance(shared, str) or not DIGEST_RE.match(shared):
        raise ValueError(f"{path}: shared_image_digest must be sha256:<64 lowercase hex>")
    runtimes = data.get("containers") or data.get("functions")
    if not isinstance(runtimes, list) or len(runtimes) < 2:
        raise ValueError(f"{path}: expected web/API and task runtime digest records")
    for record in runtimes:
        if not isinstance(record, dict) or record.get("image_digest") != shared:
            raise ValueError(f"{path}: every runtime record must use the shared image digest")


def numeric_zero(value: object) -> bool:
    if isinstance(value, bool):
        return False
    if isinstance(value, int | float):
        return value == 0
    if isinstance(value, str):
        return value.strip() in {"0", "0.0"}
    return False


def validate_function_min_instances(path: Path, value: object) -> None:
    data = assert_object(path, value)
    if data.get("schema") != "directorgraph.function-min-instances.v1":
        raise ValueError(f"{path}: schema must be directorgraph.function-min-instances.v1")
    records = []
    for item in walk_dicts(data):
        if any(key in item for key in MIN_INSTANCE_KEYS):
            records.append(item)
    if len(records) < 2:
        raise ValueError(f"{path}: expected at least two function records with minimum instance counts")
    for record in records:
        for key in MIN_INSTANCE_KEYS:
            if key in record and not numeric_zero(record[key]):
                raise ValueError(f"{path}: {key} must be zero for {record}")
        for key in PROVISION_KEYS:
            if key in record and not numeric_zero(record[key]):
                raise ValueError(f"{path}: {key} must be zero for {record}")


def validate_private_oss(path: Path, value: object) -> None:
    data = assert_object(path, value)
    if data.get("schema") != "directorgraph.private-oss-access-check.v1":
        raise ValueError(f"{path}: schema must be directorgraph.private-oss-access-check.v1")
    if data.get("anonymous_access") != "denied":
        raise ValueError(f"{path}: anonymous_access must be denied")
    strings = " ".join(text.lower() for text in walk_strings(value))
    statuses = [
        item
        for record in walk_dicts(value)
        for item in record.values()
        if isinstance(item, int)
    ]
    if not any(status in {401, 403} for status in statuses) and not any(
        word in strings for word in ("denied", "forbidden", "blocked", "unauthorized")
    ):
        raise ValueError(f"{path}: expected anonymous/private OSS access denial evidence")


def validate_task_smoke(path: Path, value: object) -> None:
    data = assert_object(path, value)
    if data.get("schema") != "directorgraph.deployment-task-smoke.v1":
        raise ValueError(f"{path}: schema must be directorgraph.deployment-task-smoke.v1")
    expected_type = "mock_task" if path.name == "mock-task-smoke.json" else "live_judge_test"
    if data.get("smoke_type") != expected_type:
        raise ValueError(f"{path}: smoke_type must be {expected_type}")
    if data.get("status") != "pass":
        raise ValueError(f"{path}: status must be pass")
    if not isinstance(data.get("repository_commit"), str) or not data["repository_commit"].strip():
        raise ValueError(f"{path}: repository_commit is required")
    assert_https_url(path, "base_url", data.get("base_url"))
    expected_provider = "mock" if expected_type == "mock_task" else "live"
    if data.get("provider_mode") != expected_provider:
        raise ValueError(f"{path}: provider_mode must be {expected_provider}")
    submission = data.get("submission")
    if not isinstance(submission, dict) or not submission.get("task_id") or not submission.get("project_id"):
        raise ValueError(f"{path}: submission.project_id and submission.task_id are required")
    task = data.get("task")
    if not isinstance(task, dict) or str(task.get("status", "")).lower() not in {"succeeded", "success", "completed", "passed"}:
        raise ValueError(f"{path}: task.status must be successful")
    project = data.get("project")
    if not isinstance(project, dict) or project.get("status") != "completed":
        raise ValueError(f"{path}: project.status must be completed")
    checks = data.get("checks")
    if not isinstance(checks, dict) or not checks or not all(checks.values()):
        raise ValueError(f"{path}: all task smoke checks must be true")


def validate_generic_json(path: Path, value: object) -> None:
    assert_object(path, value)


VALIDATORS = {
    ARTIFACTS["live_results"]: validate_live_results,
    ARTIFACTS["model_smoke"]: validate_model_smoke,
    ARTIFACTS["redacted_fixtures"]: validate_redacted_fixtures,
    ARTIFACTS["serverless_verification"]: validate_serverless_verification,
    ARTIFACTS["public_links"]: validate_public_links,
    ARTIFACTS["image_digest"]: validate_image_digest,
    ARTIFACTS["private_oss"]: validate_private_oss,
    ARTIFACTS["live_judge_smoke"]: validate_task_smoke,
}


def main() -> None:
    violations: list[str] = []
    for path in ARTIFACTS.values():
        if not path.is_file() or path.stat().st_size == 0:
            violations.append(f"{path}: missing or empty")
            continue
        try:
            value = load_json(path)
            assert_no_placeholders(path, value)
            VALIDATORS.get(path, validate_generic_json)(path, value)
        except ValueError as exc:
            violations.append(str(exc))
    if violations:
        raise SystemExit("\n".join(violations))
    print(
        json.dumps(
            {
                "artifacts": [path.as_posix() for path in ARTIFACTS.values()],
                "schema": "directorgraph.submission-artifact-check.v1",
                "status": "pass",
            },
            sort_keys=True,
        )
    )


if __name__ == "__main__":
    main()
