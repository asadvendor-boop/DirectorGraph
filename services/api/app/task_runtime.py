from __future__ import annotations

import argparse
import asyncio
import json
import sys
from datetime import UTC, datetime
from typing import Any

from sqlalchemy.orm import Session

from app.config import Settings, get_settings
from app.db import SessionLocal
from app.models import Job, Project, Shot
from app.oss_repository import OssNotFoundError, create_oss_repository, original_request_key
from app.repository import get_job_by_task_id
from app.schemas import JobStatus, ProductionLedger, ProjectBrief, ProjectStatus
from app.task_checkpoints import load_project_read_model
from app.worker import execute_job


def parse_payload(raw: str | None, job_id: str | None, task_id: str | None = None) -> dict[str, Any]:
    if raw:
        payload = json.loads(raw)
        if not isinstance(payload, dict):
            raise ValueError("Task payload must be a JSON object")
    else:
        payload = {}
    if job_id:
        payload["job_id"] = job_id
    if task_id:
        payload["task_id"] = task_id
    if not payload.get("job_id") and not payload.get("task_id"):
        raise ValueError("Task payload requires job_id or task_id")
    return payload


def _parse_checkpoint_datetime(value: Any) -> datetime:
    if isinstance(value, datetime):
        if value.tzinfo is None:
            return value.replace(tzinfo=UTC)
        return value.astimezone(UTC)
    if isinstance(value, str):
        parsed = datetime.fromisoformat(value)
        if parsed.tzinfo is None:
            return parsed.replace(tzinfo=UTC)
        return parsed.astimezone(UTC)
    return datetime.now(UTC)


def _materialize_project_from_read_model(session: Session, settings: Settings, project_id: str) -> Project | None:
    read = load_project_read_model(settings, project_id)
    if read is None:
        return None

    project = session.get(Project, project_id)
    if project is None:
        project = Project(id=project_id)
        session.add(project)

    project.title = read.title
    project.status = read.status.value
    project.brief = read.brief.model_dump(mode="json")
    project.plan = read.plan.model_dump(mode="json") if read.plan else None
    project.ledger = read.ledger.model_dump(mode="json", exclude_computed_fields=True)
    project.final_video_url = read.final_video_url
    project.error = read.error
    project.created_at = _parse_checkpoint_datetime(read.created_at)
    project.updated_at = _parse_checkpoint_datetime(read.updated_at)
    session.flush()

    existing = {
        shot.shot_code: shot
        for shot in session.query(Shot).filter(Shot.project_id == project_id).all()
    }
    for shot_read in read.shots:
        shot = existing.get(shot_read.shot_code)
        if shot is None:
            shot = Shot(
                id=shot_read.id,
                project_id=project_id,
                shot_code=shot_read.shot_code,
                sequence=shot_read.sequence,
                contract=shot_read.contract.model_dump(mode="json"),
            )
            session.add(shot)
        shot.sequence = shot_read.sequence
        shot.status = shot_read.status.value
        shot.contract = shot_read.contract.model_dump(mode="json")
        shot.storyboard_url = shot_read.storyboard_url
        shot.audio_url = shot_read.audio_url
        shot.video_url = shot_read.video_url
        shot.quality = shot_read.quality.model_dump(mode="json") if shot_read.quality else None
        shot.attempts = shot_read.attempts
        shot.accepted = shot_read.accepted
    session.flush()
    return project


def materialize_job_from_payload(
    session: Session,
    settings: Settings,
    payload: dict[str, Any],
) -> Job:
    task_id = str(payload.get("task_id") or "")
    project_id = str(payload.get("project_id") or "")
    operation = str(payload.get("operation") or "")
    if not task_id:
        raise ValueError("Task payload requires task_id")
    if operation not in {"run_project", "patch_project"}:
        raise ValueError("Task payload requires a supported operation")
    if not project_id:
        raise ValueError("Task payload requires project_id for OSS-backed task materialization")

    existing = get_job_by_task_id(session, task_id)
    if existing is not None:
        return existing

    project = _materialize_project_from_read_model(session, settings, project_id)
    if project is None:
        repo = create_oss_repository(settings)
        try:
            request = repo.get_json(original_request_key(project_id)).payload
        except OssNotFoundError as exc:
            raise ValueError(f"Original request checkpoint was not found for project {project_id}") from exc

        brief = ProjectBrief.model_validate(request.get("brief"))
        project = session.get(Project, project_id)
        if project is None:
            ledger = ProductionLedger(
                budget_usd=brief.budget_usd,
                repair_reserve_usd=round(brief.budget_usd * brief.repair_reserve_percent / 100, 4),
            )
            created_at = _parse_checkpoint_datetime(request.get("created_at"))
            project = Project(
                id=project_id,
                title=str(request.get("title") or brief.title),
                status=ProjectStatus.QUEUED.value,
                brief=brief.model_dump(),
                ledger=ledger.model_dump(exclude_computed_fields=True),
                created_at=created_at,
                updated_at=datetime.now(UTC),
            )
            session.add(project)
            session.flush()

    job_kwargs: dict[str, Any] = {
        "project_id": project_id,
        "job_type": operation,
        "idempotency_key": task_id,
        "payload": {
            **payload,
            "project_id": project_id,
            "task_id": task_id,
            "operation": operation,
            "materialized_from_oss": True,
        },
        "status": JobStatus.PENDING.value,
    }
    if payload.get("job_id"):
        job_kwargs["id"] = str(payload["job_id"])
        job_kwargs["payload"]["job_id"] = str(payload["job_id"])
    job = Job(**job_kwargs)
    session.add(job)
    session.commit()
    session.refresh(job)
    return job


async def run_task(payload: dict[str, Any]) -> dict[str, str]:
    settings = get_settings()
    if settings.app_mode != "task":
        raise RuntimeError("Task runtime requires APP_MODE=task")
    job_id = str(payload["job_id"]) if payload.get("job_id") else ""
    with SessionLocal() as session:
        job = session.get(Job, job_id) if job_id else None
        if job is None and payload.get("task_id"):
            job = get_job_by_task_id(session, str(payload["task_id"]))
        if job is None:
            job = materialize_job_from_payload(session, settings, payload)
        job_id = job.id
    await execute_job(job_id)
    return {"status": "processed", "job_id": job_id, "task_id": str(payload.get("task_id") or "")}


def main() -> None:
    parser = argparse.ArgumentParser(description="DirectorGraph local task runtime")
    parser.add_argument("--payload", help="JSON task payload; must contain job_id or task_id")
    parser.add_argument("--job-id", help="Queued DirectorGraph job id")
    parser.add_argument("--task-id", help="Deterministic DirectorGraph task id")
    args = parser.parse_args()
    raw = args.payload
    if raw is None and not args.job_id and not sys.stdin.isatty():
        raw = sys.stdin.read().strip() or None
    try:
        result = asyncio.run(run_task(parse_payload(raw, args.job_id, args.task_id)))
    except Exception as exc:
        print(json.dumps({"status": "failed", "error": str(exc)}), file=sys.stderr)
        raise SystemExit(1) from exc
    print(json.dumps(result))


if __name__ == "__main__":
    main()
