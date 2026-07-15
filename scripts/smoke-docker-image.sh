#!/usr/bin/env bash
set -euo pipefail

IMAGE="${1:-directorgraph:final-check}"
PLATFORM="${DIRECTORGRAPH_DOCKER_SMOKE_PLATFORM:-linux/amd64}"
EXPECTED_BUILD_SHA="${DIRECTORGRAPH_EXPECTED_BUILD_SHA:-}"

docker image inspect "$IMAGE" >/dev/null

echo "image=$IMAGE"
echo "platform=$PLATFORM"
echo "image_id=$(docker image inspect --format '{{.Id}}' "$IMAGE")"
echo "architecture=$(docker image inspect --format '{{.Architecture}}' "$IMAGE")"
echo "os=$(docker image inspect --format '{{.Os}}' "$IMAGE")"
echo "revision_label=$(docker image inspect --format '{{index .Config.Labels "org.opencontainers.image.revision"}}' "$IMAGE")"
echo "created_label=$(docker image inspect --format '{{index .Config.Labels "org.opencontainers.image.created"}}' "$IMAGE")"

if [[ -n "$EXPECTED_BUILD_SHA" ]]; then
  actual_revision="$(docker image inspect --format '{{index .Config.Labels "org.opencontainers.image.revision"}}' "$IMAGE")"
  if [[ "$actual_revision" != "$EXPECTED_BUILD_SHA" ]]; then
    echo "Expected revision label $EXPECTED_BUILD_SHA but found $actual_revision" >&2
    exit 1
  fi
fi
if [[ "$PLATFORM" == "linux/amd64" ]]; then
  actual_architecture="$(docker image inspect --format '{{.Architecture}}' "$IMAGE")"
  if [[ "$actual_architecture" != "amd64" ]]; then
    echo "Expected linux/amd64 image but found architecture=$actual_architecture" >&2
    exit 1
  fi
fi

echo "[web]"
docker run --rm -i --platform "$PLATFORM" \
  -e APP_MODE=web \
  -e PROVIDER_MODE=mock \
  -e SEED_DEMO=false \
  -e FRONTEND_DIST=/app/static \
  -e MEDIA_ROOT=/tmp/directorgraph/media \
  -e OSS_REPOSITORY_ROOT=/tmp/directorgraph/oss \
  -e DATABASE_URL=sqlite:////tmp/directorgraph/web-smoke.db \
  "$IMAGE" python - <<'PY'
import json
from pathlib import Path

from fastapi.testclient import TestClient

from app.config import get_settings
from app.main import app, frontend_index

settings = get_settings()
client = TestClient(app)
root_response = client.get("/")
health_response = client.get("/api/health")
readiness_response = client.get("/api/readiness")
payload = {
    "app": app.title,
    "mode": settings.app_mode,
    "provider_mode": settings.provider_mode,
    "frontend_index_exists": frontend_index.exists(),
    "frontend_index": str(frontend_index),
    "root_status_code": root_response.status_code,
    "health_status_code": health_response.status_code,
    "readiness_status_code": readiness_response.status_code,
    "readiness_status": readiness_response.json().get("status"),
}
assert settings.app_mode == "web", payload
assert Path("/app/static/index.html").is_file(), payload
assert payload["frontend_index_exists"], payload
assert root_response.status_code == 200, payload
assert health_response.status_code == 200, payload
assert readiness_response.status_code == 200, payload
print(json.dumps(payload, sort_keys=True))
PY

echo "[task]"
docker run --rm -i --platform "$PLATFORM" \
  -e APP_MODE=task \
  -e PROVIDER_MODE=mock \
  -e SEED_DEMO=false \
  -e INLINE_WORKER=false \
  -e FRONTEND_DIST=/app/static \
  -e MEDIA_ROOT=/tmp/directorgraph/media \
  -e OSS_REPOSITORY_ROOT=/tmp/directorgraph/oss \
  -e DATABASE_URL=sqlite:////tmp/directorgraph/task-smoke.db \
  "$IMAGE" python - <<'PY'
import json

from fastapi.testclient import TestClient

from app.api.routes import router
from app.config import get_settings
from app.db import SessionLocal
from app.main import app, task_mode_allows_path
from app.repository import create_project, get_job_by_task_id, get_project
from app.schemas import JobStatus, ProjectBrief, ProjectStatus
from app.task_runtime import parse_payload
from app.task_submitter import submit_project_task

settings = get_settings()
parsed = parse_payload(
    '{"project_id":"dg-container-project","operation":"run_project"}',
    None,
    "dg-container-smoke-task",
)
allowed = {
    "/": task_mode_allows_path("/"),
    "/api/health": task_mode_allows_path("/api/health"),
    "/api/readiness": task_mode_allows_path("/api/readiness"),
    "/api/function-compute/tasks": task_mode_allows_path("/api/function-compute/tasks"),
    "/api/projects": task_mode_allows_path("/api/projects"),
}
router_paths = {getattr(route, "path", "") for route in router.routes}

with TestClient(app) as client:
    root_response = client.get("/")
    health_response = client.get("/api/health")
    projects_response = client.get("/api/projects")
    with SessionLocal() as session:
        project = create_project(
            session,
            ProjectBrief(
                title="Container task smoke",
                premise="A compact mock production proves the task image can accept and process a Function Compute payload.",
                duration_seconds=10,
                budget_usd=3,
                max_shots=2,
                production_profile="judge_test",
            ),
            settings=settings,
        )
        task = submit_project_task(session, project, "run_project", settings)
        job = get_job_by_task_id(session, task.task_id)
        assert job is not None
        task_payload = dict(job.payload)
    task_response = client.post("/api/function-compute/tasks", json=task_payload)
    with SessionLocal() as session:
        job = get_job_by_task_id(session, task.task_id)
        project = get_project(session, task.project_id)

payload = {
    "app": app.title,
    "mode": settings.app_mode,
    "provider_mode": settings.provider_mode,
    "task_id": parsed["task_id"],
    "operation": parsed["operation"],
    "processed_task_id": task.task_id,
    "task_receiver_route_present": "/api/function-compute/tasks" in router_paths,
    "allowed_paths": allowed,
    "root_status_code": root_response.status_code,
    "health_status_code": health_response.status_code,
    "projects_status_code": projects_response.status_code,
    "task_status_code": task_response.status_code,
    "task_response": task_response.json(),
    "job_status": job.status if job else None,
    "project_status": project.status,
    "final_video_present": bool(project.final_video_url),
}
assert settings.app_mode == "task", payload
assert parsed["task_id"] == "dg-container-smoke-task", payload
assert parsed["operation"] == "run_project", payload
assert payload["task_receiver_route_present"], payload
assert root_response.status_code == 404, payload
assert health_response.status_code == 200, payload
assert projects_response.status_code == 404, payload
assert task_response.status_code == 202, payload
assert job and job.status == JobStatus.SUCCEEDED.value, payload
assert project.status == ProjectStatus.COMPLETED.value, payload
assert project.final_video_url, payload
assert allowed["/api/health"], payload
assert allowed["/api/readiness"], payload
assert allowed["/api/function-compute/tasks"], payload
assert not allowed["/"], payload
assert not allowed["/api/projects"], payload
print(json.dumps(payload, sort_keys=True))
PY
