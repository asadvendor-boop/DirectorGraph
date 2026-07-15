from __future__ import annotations

import asyncio
import hmac
import json
import logging
from datetime import UTC, datetime
from typing import Any

from fastapi import APIRouter, BackgroundTasks, Depends, Header, HTTPException, Query, status
from fastapi.responses import StreamingResponse
from sqlalchemy import select
from sqlalchemy.orm import Session

from app.config import Settings, get_settings
from app.db import SessionLocal, get_db
from app.models import Event, Job, Project
from app.oss_repository import OssNotFoundError, create_oss_repository
from app.repository import (
    add_event,
    create_project,
    get_job_by_task_id,
    get_project,
    list_projects,
    project_to_read,
)
from app.schemas import (
    JobStatus,
    JudgeTestRequest,
    PatchRequest,
    ProjectBrief,
    ProjectRead,
    ProjectStatus,
    PublicConfig,
    RunResponse,
)
from app.task_checkpoints import (
    build_production_manifest_payload,
    checkpoint_project_read_model,
    checkpoint_task_status,
    list_project_read_models,
    load_project_events,
    load_project_read_model,
    load_task_status_by_task_id,
    load_task_status_object,
)
from app.task_runtime import run_task
from app.task_submitter import deterministic_task_id, submit_project_task

router = APIRouter(prefix="/api")
logger = logging.getLogger(__name__)
process_function_compute_task = run_task


def _not_found(project_id: str) -> HTTPException:
    return HTTPException(status_code=404, detail=f"Project {project_id} was not found")


def _enforce_live_approval(settings: Settings, judge_code: str | None) -> None:
    if settings.provider_mode != "live":
        return
    if not settings.judge_create_access_code:
        raise HTTPException(status_code=503, detail="Live production requires JUDGE_CREATE_ACCESS_CODE")
    if settings.judge_create_access_code:
        supplied = judge_code if isinstance(judge_code, str) else None
        if not supplied or not hmac.compare_digest(supplied, settings.judge_create_access_code):
            raise HTTPException(status_code=403, detail="Live production requires the judge access code")


def _enforce_judge_test_approval(settings: Settings, judge_code: str | None) -> None:
    supplied = judge_code if isinstance(judge_code, str) else None
    if settings.provider_mode == "live" and not settings.judge_create_access_code:
        raise HTTPException(status_code=503, detail="Judge Test requires JUDGE_CREATE_ACCESS_CODE")
    if settings.judge_create_access_code and (
        not supplied or not hmac.compare_digest(supplied, settings.judge_create_access_code)
    ):
        raise HTTPException(status_code=403, detail="Judge Test requires the judge access code")


def _enforce_live_deployment_ready(settings: Settings) -> None:
    if settings.provider_mode != "live":
        return
    missing = []
    if not settings.oss_ready:
        missing.append("OSS credentials")
    if not settings.function_compute_task_url:
        missing.append("FUNCTION_COMPUTE_TASK_URL")
    if "localhost" in settings.public_media_base_url or "127.0.0.1" in settings.public_media_base_url:
        missing.append("PUBLIC_MEDIA_BASE_URL")
    if missing:
        raise HTTPException(
            status_code=503,
            detail=f"Live production requires serverless configuration: {', '.join(missing)}",
        )


def _enforce_function_compute_task_auth(settings: Settings, authorization: str | None) -> None:
    expected = settings.function_compute_auth_header
    if not expected:
        return
    supplied = authorization if isinstance(authorization, str) else None
    if not supplied or not hmac.compare_digest(supplied, expected):
        raise HTTPException(status_code=403, detail="Function Compute task authorization failed")


async def _process_function_compute_task_background(payload: dict[str, Any]) -> None:
    try:
        await process_function_compute_task(payload)
    except Exception:
        logger.exception("DirectorGraph async task processing failed", extra={"task_id": payload.get("task_id")})


@router.get("/health")
def health(settings: Settings = Depends(get_settings)) -> dict:
    return {
        "status": "ok",
        "service": settings.app_name,
        "version": settings.app_version,
        "mode": settings.app_mode,
        "provider_mode": settings.provider_mode,
        "state_backend": settings.state_backend,
        "environment": settings.environment,
        "build": {
            "sha": settings.build_sha,
            "timestamp": settings.build_timestamp,
        },
        "deployment": {
            "dashscope_region": settings.dashscope_region,
            "live_ready": settings.live_ready,
            "oss_ready": settings.oss_ready,
            "function_compute_task_configured": bool(settings.function_compute_task_url),
        },
        "limits": {
            "max_total_live_spend_usd": settings.max_total_live_spend_usd,
            "max_project_spend_usd": settings.max_project_spend_usd,
            "judge_run_max_duration_seconds": settings.judge_run_max_duration_seconds,
            "judge_run_max_shots": settings.judge_run_max_shots,
        },
        "time": datetime.now(UTC).isoformat(),
    }


def readiness_payload(settings: Settings) -> dict:
    live_credentials_ready = settings.provider_mode == "mock" or settings.live_ready
    media_publication_ready = (
        settings.provider_mode == "mock"
        or settings.oss_ready
        or "localhost" not in settings.public_media_base_url
    )
    database_ready = settings.state_backend == "oss" or bool(settings.database_url)
    checks = {
        "app_mode_valid": settings.app_mode in {"web", "task"},
        "state_backend_valid": settings.state_backend in {"local", "oss"},
        "database_configured": bool(settings.database_url),
        "sql_startup_required": settings.state_backend != "oss",
        "media_root_configured": bool(settings.media_root),
        "provider_configured": settings.provider_mode in {"mock", "live"},
        "live_credentials_ready": live_credentials_ready,
        "media_publication_ready": media_publication_ready,
        "oss_ready": settings.oss_ready,
        "function_compute_task_configured": bool(settings.function_compute_task_url),
    }
    required = [
        checks["app_mode_valid"],
        checks["state_backend_valid"],
        database_ready,
        checks["media_root_configured"],
        checks["provider_configured"],
        checks["live_credentials_ready"],
        checks["media_publication_ready"],
    ]
    return {
        "status": "ready" if all(required) else "degraded",
        "service": settings.app_name,
        "version": settings.app_version,
        "mode": settings.app_mode,
        "provider_mode": settings.provider_mode,
        "state_backend": settings.state_backend,
        "build": {
            "sha": settings.build_sha,
            "timestamp": settings.build_timestamp,
        },
        "checks": checks,
        "limits": {
            "max_total_live_spend_usd": settings.max_total_live_spend_usd,
            "max_project_spend_usd": settings.max_project_spend_usd,
            "repair_reserve_percent": settings.repair_reserve_percent,
            "max_render_attempts_per_shot": settings.max_render_attempts_per_shot,
            "judge_run_max_duration_seconds": settings.judge_run_max_duration_seconds,
            "judge_run_max_shots": settings.judge_run_max_shots,
            "public_demo_project_configured": bool(settings.public_demo_project_id),
            "judge_access_code_configured": bool(settings.judge_create_access_code),
        },
    }


@router.get("/readiness")
def readiness(settings: Settings = Depends(get_settings)) -> dict:
    return readiness_payload(settings)


@router.get("/config", response_model=PublicConfig)
def public_config(settings: Settings = Depends(get_settings)) -> PublicConfig:
    return PublicConfig(
        provider_mode=settings.provider_mode,
        live_ready=settings.live_ready,
        oss_ready=settings.oss_ready,
        public_demo_project_id=settings.public_demo_project_id,
        judge_access_code_configured=bool(settings.judge_create_access_code),
        models={
            "story": settings.qwen_story_model,
            "vision": settings.qwen_vision_model,
            "image": settings.wan_image_model,
            "video": settings.wan_video_model,
            "reference_video": settings.wan_reference_model,
            "edit": settings.happyhorse_edit_model,
            "speech": settings.qwen_tts_model,
        },
    )


@router.post("/function-compute/tasks", status_code=status.HTTP_202_ACCEPTED)
async def function_compute_task_route(
    payload: dict[str, Any],
    background_tasks: BackgroundTasks = BackgroundTasks(),
    settings: Settings = Depends(get_settings),
    authorization: str | None = Header(default=None, alias="Authorization"),
    invocation_type: str | None = Header(default=None, alias="X-Fc-Invocation-Type"),
) -> dict[str, Any]:
    if settings.app_mode != "task":
        raise HTTPException(status_code=404, detail="Task endpoint is only available in APP_MODE=task")
    _enforce_function_compute_task_auth(settings, authorization)
    if isinstance(invocation_type, str) and invocation_type.lower() == "async":
        background_tasks.add_task(_process_function_compute_task_background, dict(payload))
        return {
            "accepted": True,
            "task_id": str(payload.get("task_id") or ""),
            "status": "queued",
        }
    try:
        result = await process_function_compute_task(payload)
    except ValueError as exc:
        raise HTTPException(status_code=400, detail=str(exc)) from exc
    return {
        "accepted": True,
        "task_id": str(payload.get("task_id") or ""),
        **result,
    }


def _public_demo_id(settings: Settings) -> str:
    if not settings.public_demo_project_id:
        raise HTTPException(status_code=404, detail="Public demo project is not configured")
    return settings.public_demo_project_id


def _as_utc(value: datetime) -> datetime:
    if value.tzinfo is None:
        return value.replace(tzinfo=UTC)
    return value.astimezone(UTC)


def _prefer_durable_read(local: ProjectRead | None, durable: ProjectRead | None) -> ProjectRead | None:
    if durable is None:
        return local
    if local is None:
        return durable
    if _as_utc(durable.updated_at) >= _as_utc(local.updated_at):
        return durable
    return local


def _project_read(db: Session, settings: Settings, project_id: str) -> ProjectRead | None:
    try:
        local = project_to_read(get_project(db, project_id))
    except KeyError:
        local = None
    durable = load_project_read_model(settings, project_id)
    return _prefer_durable_read(local, durable)


def _public_demo_project_read(db: Session, settings: Settings) -> ProjectRead:
    project_id = _public_demo_id(settings)
    read = _project_read(db, settings, project_id)
    if read is None:
        raise _not_found(project_id)
    if read.status != ProjectStatus.COMPLETED:
        raise HTTPException(status_code=409, detail="Public demo project is not completed")
    return read


def _event_payload_from_sql(event: Event) -> dict[str, Any]:
    return {
        "id": event.id,
        "kind": event.kind,
        "agent": event.agent,
        "message": event.message,
        "payload": event.payload,
        "created_at": event.created_at.isoformat(),
    }


def _event_payload_key(payload: dict[str, Any]) -> tuple[str, str, str, str]:
    return (
        str(payload.get("created_at") or ""),
        str(payload.get("kind") or ""),
        str(payload.get("agent") or ""),
        str(payload.get("message") or ""),
    )


def _event_stream_payloads(project_id: str, settings: Settings) -> tuple[bool, list[dict[str, Any]]]:
    found = False
    payloads: list[dict[str, Any]] = []
    with SessionLocal() as session:
        exists = session.get(Project, project_id)
        if exists:
            found = True
            payloads.extend(
                _event_payload_from_sql(event)
                for event in session.scalars(
                    select(Event)
                    .where(Event.project_id == project_id)
                    .order_by(Event.created_at, Event.id)
                )
            )

    durable_events = load_project_events(settings, project_id, limit=500)
    durable_read = load_project_read_model(settings, project_id)
    if durable_read or durable_events:
        found = True
    for event in durable_events:
        payloads.append(
            {
                "id": event.id,
                "kind": event.kind,
                "agent": event.agent,
                "message": event.message,
                "payload": event.payload,
                "created_at": event.created_at.isoformat(),
            }
        )

    deduped: dict[tuple[str, str, str, str], dict[str, Any]] = {}
    for payload in sorted(payloads, key=_event_payload_key):
        deduped[_event_payload_key(payload)] = payload
    return found, list(deduped.values())


@router.post("/projects", response_model=ProjectRead, status_code=status.HTTP_201_CREATED)
def create_project_route(
    brief: ProjectBrief,
    db: Session = Depends(get_db),
    settings: Settings = Depends(get_settings),
) -> ProjectRead:
    project = create_project(db, brief, settings=settings)
    checkpoint_project_read_model(db, project.id, settings)
    return project_to_read(project)


@router.get("/projects", response_model=list[ProjectRead])
def list_projects_route(
    db: Session = Depends(get_db),
    settings: Settings = Depends(get_settings),
) -> list[ProjectRead]:
    reads = {project.id: project_to_read(project, event_limit=20) for project in list_projects(db)}
    for read in list_project_read_models(settings):
        reads[read.id] = _prefer_durable_read(reads.get(read.id), read) or read
    return sorted(reads.values(), key=lambda project: project.created_at, reverse=True)


@router.get("/projects/{project_id}", response_model=ProjectRead)
def get_project_route(
    project_id: str,
    db: Session = Depends(get_db),
    settings: Settings = Depends(get_settings),
) -> ProjectRead:
    read = _project_read(db, settings, project_id)
    if read is None:
        raise _not_found(project_id)
    return read


@router.post("/projects/{project_id}/run", response_model=RunResponse, status_code=status.HTTP_202_ACCEPTED)
def run_project_route(
    project_id: str,
    db: Session = Depends(get_db),
    settings: Settings = Depends(get_settings),
    judge_code: str | None = Header(default=None, alias="X-DirectorGraph-Judge-Code"),
) -> RunResponse:
    _enforce_live_approval(settings, judge_code)
    _enforce_live_deployment_ready(settings)
    try:
        project = get_project(db, project_id)
    except KeyError as exc:
        raise _not_found(project_id) from exc
    if project.status not in {ProjectStatus.DRAFT.value, ProjectStatus.FAILED.value, ProjectStatus.COMPLETED.value}:
        task_id = deterministic_task_id(project.id, "run_project")
        existing = get_job_by_task_id(db, task_id)
        if existing:
            return RunResponse(
                project_id=project_id,
                job_id=existing.id,
                task_id=task_id,
                status=JobStatus(existing.status),
            )
        raise HTTPException(status_code=409, detail=f"Project is already {project.status}")
    task = submit_project_task(db, project, "run_project", settings)
    return RunResponse(
        project_id=project_id,
        job_id=task.job_id,
        task_id=task.task_id,
        status=JobStatus(task.status),
    )


@router.post("/judge-test", response_model=RunResponse, status_code=status.HTTP_202_ACCEPTED)
def judge_test_route(
    request: JudgeTestRequest | None = None,
    db: Session = Depends(get_db),
    settings: Settings = Depends(get_settings),
    judge_code: str | None = Header(default=None, alias="X-DirectorGraph-Judge-Code"),
) -> RunResponse:
    _enforce_judge_test_approval(settings, judge_code)
    _enforce_live_deployment_ready(settings)
    duration = max(5, min(settings.judge_run_max_duration_seconds, 15))
    max_shots = max(2, min(settings.judge_run_max_shots, 3))
    # $5 funds the ~$1.9 base run plus two full repair cycles without touching the
    # protected repair reserve; $3 left a single legitimate repair at the edge.
    budget = max(1.0, min(settings.max_project_spend_usd, 5.0))
    brief = ProjectBrief(
        title="Judge Test: The Last Delivery",
        premise=request.premise if request and request.premise else (
            "In a capped judge test, Courier-7 makes one final delivery to Mira and must preserve "
            "the red paper crane through the reveal."
        ),
        genre="science-fiction drama",
        tone="cinematic, intimate, emotionally resonant",
        target_audience="hackathon judges running a capped live smoke test",
        duration_seconds=duration,
        aspect_ratio="9:16",
        language="English",
        visual_style="grounded cinematic realism, controlled lighting, shallow depth of field",
        budget_usd=budget,
        repair_reserve_percent=settings.repair_reserve_percent,
        seed=20260710,
        required_prop="red paper crane",
        max_shots=max_shots,
        production_profile="judge_test",
    )
    project = create_project(db, brief, settings=settings)
    task = submit_project_task(db, project, "run_project", settings)
    return RunResponse(
        project_id=project.id,
        job_id=task.job_id,
        task_id=task.task_id,
        status=JobStatus(task.status),
    )


@router.post("/projects/{project_id}/patch", response_model=RunResponse, status_code=status.HTTP_202_ACCEPTED)
def patch_project_route(
    project_id: str,
    patch: PatchRequest,
    db: Session = Depends(get_db),
    settings: Settings = Depends(get_settings),
    judge_code: str | None = Header(default=None, alias="X-DirectorGraph-Judge-Code"),
) -> RunResponse:
    _enforce_live_approval(settings, judge_code)
    _enforce_live_deployment_ready(settings)
    try:
        project = get_project(db, project_id)
    except KeyError as exc:
        raise _not_found(project_id) from exc
    payload = {"instruction": patch.instruction, "affected_shot_ids": patch.affected_shot_ids}
    if project.status != ProjectStatus.COMPLETED.value:
        task_id = deterministic_task_id(project.id, "patch_project", payload)
        existing = get_job_by_task_id(db, task_id)
        if existing:
            return RunResponse(
                project_id=project_id,
                job_id=existing.id,
                task_id=task_id,
                status=JobStatus(existing.status),
            )
        raise HTTPException(status_code=409, detail="Semantic patching requires a completed production")
    task = submit_project_task(
        db,
        project,
        "patch_project",
        settings,
        payload=payload,
    )
    return RunResponse(
        project_id=project_id,
        job_id=task.job_id,
        task_id=task.task_id,
        status=JobStatus(task.status),
    )


def _production_manifest_from_project_read(project: ProjectRead, settings: Settings) -> dict:
    repo = create_oss_repository(settings)
    try:
        storage_manifest = repo.get_project_manifest(project.id).payload
    except OssNotFoundError:
        storage_manifest = None
    return build_production_manifest_payload(project, storage_manifest)


@router.get("/projects/{project_id}/manifest")
def production_manifest(
    project_id: str,
    db: Session = Depends(get_db),
    settings: Settings = Depends(get_settings),
) -> dict:
    project = _project_read(db, settings, project_id)
    if project is None:
        raise _not_found(project_id)
    return _production_manifest_from_project_read(project, settings)


def _storage_manifest_payload(project_id: str, expires_seconds: int, settings: Settings) -> dict:
    repo = create_oss_repository(settings)
    try:
        manifest = repo.get_project_manifest(project_id)
    except OssNotFoundError as exc:
        raise HTTPException(status_code=404, detail="Storage manifest was not found") from exc

    signed_objects = []
    for key in manifest.payload.get("object_keys", []):
        try:
            signed_objects.append(repo.presign_get(key, expires_seconds=expires_seconds).model_dump(mode="json"))
        except OssNotFoundError as exc:
            raise HTTPException(
                status_code=409,
                detail=f"Storage manifest references missing object: {key}",
            ) from exc
    return {
        "schema": "directorgraph.storage-manifest-read.v1",
        "generated_at": datetime.now(UTC).isoformat(),
        "project_id": project_id,
        "manifest_ref": manifest.ref.model_dump(mode="json"),
        "manifest": manifest.payload,
        "signed_objects": signed_objects,
    }


@router.get("/projects/{project_id}/storage-manifest")
def storage_manifest_route(
    project_id: str,
    expires_seconds: int = Query(default=900, ge=1, le=3600),
    db: Session = Depends(get_db),
    settings: Settings = Depends(get_settings),
) -> dict:
    if _project_read(db, settings, project_id) is None:
        raise _not_found(project_id)

    return _storage_manifest_payload(project_id, expires_seconds, settings)


@router.get("/public/demo", response_model=ProjectRead)
def public_demo_route(
    db: Session = Depends(get_db),
    settings: Settings = Depends(get_settings),
) -> ProjectRead:
    return _public_demo_project_read(db, settings)


@router.get("/public/demo/storage-manifest")
def public_demo_storage_manifest_route(
    expires_seconds: int = Query(default=900, ge=1, le=3600),
    db: Session = Depends(get_db),
    settings: Settings = Depends(get_settings),
) -> dict:
    project = _public_demo_project_read(db, settings)
    return _storage_manifest_payload(project.id, expires_seconds, settings)


@router.get("/projects/{project_id}/events/stream")
async def event_stream(
    project_id: str,
    after: int = Query(default=0, ge=0),
    settings: Settings = Depends(get_settings),
) -> StreamingResponse:
    async def generate():
        cursor = after
        while True:
            found, events = _event_stream_payloads(project_id, settings)
            if not found:
                yield f"event: error\ndata: {json.dumps({'detail': 'project not found'})}\n\n"
                return
            for stream_id, payload in enumerate(events, 1):
                if stream_id <= cursor:
                    continue
                cursor = stream_id
                payload = {**payload, "id": stream_id}
                yield f"id: {stream_id}\nevent: production\ndata: {json.dumps(payload)}\n\n"
            yield ": heartbeat\n\n"
            await asyncio.sleep(1.2)

    return StreamingResponse(generate(), media_type="text/event-stream")


@router.get("/jobs/{job_id}")
def get_job(job_id: str, db: Session = Depends(get_db)) -> dict:
    job = db.get(Job, job_id)
    if not job:
        raise HTTPException(status_code=404, detail="Job not found")
    return {
        "id": job.id,
        "project_id": job.project_id,
        "type": job.job_type,
        "status": job.status,
        "attempts": job.attempts,
        "result": job.result,
        "error": job.error,
    }


@router.get("/tasks/{task_id}")
def get_task_by_id(
    task_id: str,
    db: Session = Depends(get_db),
    settings: Settings = Depends(get_settings),
) -> dict:
    job = get_job_by_task_id(db, task_id)
    if not job:
        durable_status = load_task_status_by_task_id(settings, task_id)
        if not durable_status:
            raise HTTPException(status_code=404, detail="Task not found")
        return {
            "id": durable_status.get("job_id"),
            "task_id": task_id,
            "project_id": durable_status.get("project_id"),
            "type": durable_status.get("operation"),
            "status": durable_status.get("status"),
            "attempts": durable_status.get("attempts", 0),
            "result": durable_status.get("result"),
            "error": durable_status.get("error"),
            "function_compute_request_id": durable_status.get("function_compute_request_id"),
            "durable_status": durable_status,
        }
    durable_status = load_task_status_object(settings, job.project_id, task_id)
    return {
        "id": job.id,
        "task_id": task_id,
        "project_id": job.project_id,
        "type": job.job_type,
        "status": job.status,
        "attempts": job.attempts,
        "result": job.result,
        "error": job.error,
        "function_compute_request_id": job.payload.get("function_compute_request_id"),
        "durable_status": durable_status,
    }


@router.post("/tasks/{task_id}/stop")
def stop_task_by_id(
    task_id: str,
    db: Session = Depends(get_db),
    settings: Settings = Depends(get_settings),
    judge_code: str | None = Header(default=None, alias="X-DirectorGraph-Judge-Code"),
) -> dict:
    _enforce_judge_test_approval(settings, judge_code)
    job = get_job_by_task_id(db, task_id)
    if not job:
        durable_status = load_task_status_by_task_id(settings, task_id)
        if durable_status and durable_status.get("status") == JobStatus.CANCELED.value:
            return {
                "id": durable_status.get("job_id"),
                "task_id": task_id,
                "project_id": durable_status.get("project_id"),
                "type": durable_status.get("operation"),
                "status": durable_status.get("status"),
                "attempts": durable_status.get("attempts", 0),
                "result": durable_status.get("result"),
                "error": durable_status.get("error"),
                "function_compute_request_id": durable_status.get("function_compute_request_id"),
                "durable_status": durable_status,
            }
        raise HTTPException(
            status_code=409,
            detail="Task stop is only safe for a pending local task; live Function Compute stop is not verified",
        )
    if job.status == JobStatus.CANCELED.value:
        return get_task_by_id(task_id, db=db, settings=settings)
    if job.status != JobStatus.PENDING.value:
        raise HTTPException(
            status_code=409,
            detail=f"Task is {job.status}; only pending tasks can be safely stopped locally",
        )

    payload = dict(job.payload or {})
    previous_status = str(payload.get("pre_task_project_status") or ProjectStatus.DRAFT.value)
    if previous_status not in {status.value for status in ProjectStatus}:
        previous_status = ProjectStatus.DRAFT.value
    project = db.get(Project, job.project_id)
    job.status = JobStatus.CANCELED.value
    job.error = "Canceled before execution by operator request"
    job.result = {"canceled": True, "safe_stop": "pending-local-task"}
    job.locked_at = None
    if project is not None:
        project.status = previous_status
        project.updated_at = datetime.now(UTC)
        add_event(
            db,
            project.id,
            "task.canceled",
            f"Task {task_id} was canceled before execution",
            {
                "task_id": task_id,
                "operation": job.job_type,
                "previous_project_status": previous_status,
                "safe_stop": "pending-local-task",
            },
            agent="Production Manager",
            settings=settings,
        )
        checkpoint_project_read_model(db, project.id, settings)
    checkpoint_task_status(db, job.id, settings, dispatch_mode=payload.get("dispatch_mode"))
    db.commit()
    return get_task_by_id(task_id, db=db, settings=settings)
