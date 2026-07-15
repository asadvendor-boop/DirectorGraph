from __future__ import annotations

import asyncio
import logging
from datetime import UTC, datetime, timedelta

from sqlalchemy import select

from app.config import get_settings
from app.core.orchestrator import DirectorGraphOrchestrator
from app.db import SessionLocal, init_db
from app.models import Job
from app.repository import claim_next_job, finish_job
from app.schemas import JobStatus
from app.task_checkpoints import checkpoint_task_status

logger = logging.getLogger("directorgraph.worker")
_background_tasks: set[asyncio.Task] = set()


async def execute_job(job_id: str, *, already_claimed: bool = False) -> None:
    settings = get_settings()
    with SessionLocal() as session:
        job = session.get(Job, job_id)
        if job is None or job.status in {JobStatus.SUCCEEDED.value, JobStatus.CANCELED.value}:
            return
        if not already_claimed:
            if job.status != JobStatus.PENDING.value:
                return
            job.status = JobStatus.RUNNING.value
            job.attempts += 1
            job.locked_at = datetime.now(UTC)
            session.commit()
        checkpoint_task_status(session, job_id, settings)
        job_type = job.job_type
        project_id = job.project_id
        payload = dict(job.payload or {})

    orchestrator = DirectorGraphOrchestrator(settings)
    try:
        if job_type == "run_project":
            result = await orchestrator.run_project(project_id)
        elif job_type == "patch_project":
            result = await orchestrator.patch_project(
                project_id,
                payload["instruction"],
                payload.get("affected_shot_ids", []),
            )
        else:
            raise ValueError(f"Unknown job type: {job_type}")
        with SessionLocal() as session:
            finish_job(session, job_id, result=result)
            checkpoint_task_status(session, job_id, settings)
    except Exception as exc:
        logger.exception("Job %s failed", job_id)
        with SessionLocal() as session:
            finish_job(session, job_id, error=str(exc))
            checkpoint_task_status(session, job_id, settings)


def dispatch_inline(job_id: str) -> None:
    task = asyncio.create_task(execute_job(job_id))
    _background_tasks.add(task)
    task.add_done_callback(_background_tasks.discard)


def recover_stale_jobs() -> int:
    cutoff = datetime.now(UTC) - timedelta(minutes=30)
    with SessionLocal() as session:
        jobs = list(
            session.scalars(
                select(Job).where(
                    Job.status == JobStatus.RUNNING.value,
                    Job.locked_at.is_not(None),
                    Job.locked_at < cutoff,
                )
            )
        )
        for job in jobs:
            job.status = JobStatus.PENDING.value
            job.locked_at = None
            job.error = "Recovered after stale worker lock"
        session.commit()
        return len(jobs)


async def run_forever() -> None:
    settings = get_settings()
    init_db()
    recovered = recover_stale_jobs()
    logger.info("Worker started; recovered %s stale jobs", recovered)
    while True:
        with SessionLocal() as session:
            job = claim_next_job(session)
        if job:
            await execute_job(job.id, already_claimed=True)
        else:
            await asyncio.sleep(settings.worker_poll_seconds)


if __name__ == "__main__":
    logging.basicConfig(level=get_settings().log_level)
    asyncio.run(run_forever())
