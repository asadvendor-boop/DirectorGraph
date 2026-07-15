from __future__ import annotations

import importlib.util
import json
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parents[3]
SPEC = importlib.util.spec_from_file_location(
    "check_submission_artifacts",
    ROOT / "scripts" / "check-submission-artifacts.py",
)
assert SPEC and SPEC.loader
checker = importlib.util.module_from_spec(SPEC)
SPEC.loader.exec_module(checker)


def task_smoke_payload(*, smoke_type: str = "mock_task", provider_mode: str = "mock") -> dict:
    return {
        "schema": "directorgraph.deployment-task-smoke.v1",
        "smoke_type": smoke_type,
        "status": "pass",
        "repository_commit": "41ff4c44db0b90739e517a0dbf6dd5e9ed659068",
        "base_url": "https://directorgraph.example.test",
        "provider_mode": provider_mode,
        "submission": {
            "project_id": "project-123",
            "task_id": "dg-run-project-123",
        },
        "task": {
            "status": "succeeded",
        },
        "project": {
            "status": "completed",
        },
        "checks": {
            "app_mode_web": True,
            "expected_provider_mode": True,
            "task_succeeded": True,
            "project_completed": True,
        },
    }


def test_validate_mock_task_smoke_requires_structured_success() -> None:
    checker.validate_task_smoke(
        Path("evidence/deployment/mock-task-smoke.json"),
        task_smoke_payload(),
    )


def test_validate_live_judge_task_smoke_requires_live_provider() -> None:
    checker.validate_task_smoke(
        Path("evidence/deployment/live-judge-test-smoke.json"),
        task_smoke_payload(smoke_type="live_judge_test", provider_mode="live"),
    )


def test_final_artifact_manifest_uses_live_judge_smoke_not_legacy_mock_task() -> None:
    artifact_paths = {path.as_posix() for path in checker.ARTIFACTS.values()}

    assert "evidence/deployment/live-judge-test-smoke.json" in artifact_paths
    assert "evidence/deployment/mock-task-smoke.json" not in artifact_paths


@pytest.mark.parametrize(
    ("field", "value", "message"),
    [
        ("schema", "directorgraph.old-task-smoke.v1", "schema must be"),
        ("base_url", "http://localhost:8000", "base_url must use https"),
        ("provider_mode", "live", "provider_mode must be mock"),
        ("checks", {"task_succeeded": True, "project_completed": False}, "all task smoke checks"),
    ],
)
def test_validate_task_smoke_rejects_weak_or_local_evidence(
    field: str,
    value: object,
    message: str,
) -> None:
    payload = task_smoke_payload()
    payload[field] = value
    with pytest.raises(ValueError, match=message):
        checker.validate_task_smoke(Path("evidence/deployment/mock-task-smoke.json"), payload)


def test_example_production_manifest_uses_storage_audit_trail() -> None:
    manifest = json.loads((ROOT / "examples/demo-output/production-manifest.json").read_text(encoding="utf-8"))
    raw = json.dumps(manifest)

    assert manifest["schema"] == "directorgraph.production-manifest.v1"
    assert manifest["storage"]["schema"] == "directorgraph.production-storage-audit-trail.v1"
    assert manifest["storage"]["object_keys"]
    assert "http://localhost" not in raw
    assert "signature=" not in raw
    assert "final_video_url" not in raw
    assert "storyboard_url" not in raw
    assert "audio_url" not in raw
    assert "video_url" not in raw
    assert all("storyboard" not in shot and "video" not in shot for shot in manifest["audit_trail"])
    assert all(shot["checkpoint_keys"] for shot in manifest["audit_trail"])


def test_readme_treats_verified_autumn_path_as_current_live_master() -> None:
    readme = (ROOT / "README.md").read_text(encoding="utf-8")

    assert "0bc17703-d506-4590-86b3-080792f4d239" in readme
    assert "live_qwen_character_bound" in readme
    assert "live cloud deployment and model-quality evidence must be generated" not in readme
    assert "The included mock evidence is local orchestration evidence" not in readme
