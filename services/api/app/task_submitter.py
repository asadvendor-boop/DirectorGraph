from __future__ import annotations

import asyncio
import hashlib
import json
from dataclasses import dataclass
from datetime import UTC, datetime
from typing import Any, Literal

from sqlalchemy.orm import Session

from app.config import Settings
from app.function_compute import invoke_function_compute_task
from app.models import Project
from app.repository import enqueue_job, get_job_by_task_id, set_project_status
from app.schemas import ProjectStatus
from app.task_checkpoints import (
    checkpoint_project_read_model,
    checkpoint_task_status,
    checkpoint_task_submission,
)
from app.worker import dispatch_inline

TaskOperation = Literal["run_project", "patch_project"]


@dataclass(frozen=True, slots=True)
class TaskSubmission:
    project_id: str
    job_id: str
    task_id: str
    status: str
    duplicate: bool
    dispatch_mode: str


def canonical_payload(payload: dict[str, Any] | None) -> str:
    return json.dumps(payload or {}, sort_keys=True, separators=(",", ":"), ensure_ascii=True)


def deterministic_task_id(project_id: str, operation: TaskOperation, payload: dict[str, Any] | None = None) -> str:
    digest = hashlib.sha256(
        f"{project_id}:{operation}:{canonical_payload(payload)}".encode()
    ).hexdigest()[:24]
    return f"dg-{operation.replace('_', '-')}-{digest}"


def submit_project_task(
    session: Session,
    project: Project,
    operation: TaskOperation,
    settings: Settings,
    *,
    payload: dict[str, Any] | None = None,
) -> TaskSubmission:
    task_id = deterministic_task_id(project.id, operation, payload)
    existing = get_job_by_task_id(session, task_id)
    job = enqueue_job(
        session,
        project.id,
        operation,
        task_id,
        {
            **(payload or {}),
            "project_id": project.id,
            "task_id": task_id,
            "operation": operation,
            "pre_task_project_status": project.status,
        },
        settings=settings,
    )
    job.payload = {
        **(job.payload or {}),
        "project_id": project.id,
        "job_id": job.id,
        "task_id": task_id,
        "operation": operation,
        "pre_task_project_status": (job.payload or {}).get("pre_task_project_status", project.status),
    }
    session.commit()
    duplicate = existing is not None
    message = (
        f"Task {task_id} reused existing submission"
        if duplicate
        else f"Task {task_id} registered for asynchronous production"
    )
    set_project_status(
        session,
        project.id,
        ProjectStatus.QUEUED,
        message,
        agent="Production Manager",
        settings=settings,
    )
    checkpoint_task_submission(
        session,
        job.id,
        settings,
        operation=operation,
        payload=payload,
        duplicate=duplicate,
    )
    checkpoint_project_read_model(session, project.id, settings)
    dispatch_mode = str((job.payload or {}).get("dispatch_mode") or "external-task")
    checkpoint_task_status(
        session,
        job.id,
        settings,
        duplicate=duplicate,
        dispatch_mode=dispatch_mode,
    )
    if settings.function_compute_task_url and not duplicate:
        invocation = invoke_function_compute_task(settings, job.payload, task_id=task_id)
        job.payload = {
            **job.payload,
            "function_compute_request_id": invocation.request_id,
            "function_compute_status_code": invocation.status_code,
            "dispatch_mode": "function-compute",
        }
        session.commit()
        dispatch_mode = "function-compute"
    elif settings.inline_worker and not duplicate:
        job.payload = {**job.payload, "dispatch_mode": "inline"}
        session.commit()
        dispatch_inline(job.id)
        dispatch_mode = "inline"
    elif not duplicate:
        job.payload = {**job.payload, "dispatch_mode": dispatch_mode}
        session.commit()
    checkpoint_task_status(
        session,
        job.id,
        settings,
        duplicate=duplicate,
        dispatch_mode=dispatch_mode,
    )
    return TaskSubmission(
        project_id=project.id,
        job_id=job.id,
        task_id=task_id,
        status=job.status,
        duplicate=duplicate,
        dispatch_mode=dispatch_mode,
    )


def utc_task_attempt_id(task_id: str) -> str:
    timestamp = datetime.now(UTC).strftime("%Y%m%dT%H%M%S%fZ")
    return f"{task_id}:{timestamp}"


async def wait_for_inline_tasks() -> None:
    await asyncio.sleep(0)
