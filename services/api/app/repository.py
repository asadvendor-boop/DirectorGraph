from __future__ import annotations

from datetime import UTC, datetime
from typing import TYPE_CHECKING, Any

from sqlalchemy import select
from sqlalchemy.orm import Session, selectinload

from app.models import Event, Job, Project, Shot
from app.schemas import (
    EventRead,
    JobStatus,
    ProductionLedger,
    ProjectBrief,
    ProjectRead,
    ProjectStatus,
    QualityReport,
    ShotContract,
    ShotRead,
    ShotStatus,
    StoryPlan,
)

if TYPE_CHECKING:
    from app.config import Settings


def add_event(
    session: Session,
    project_id: str,
    kind: str,
    message: str,
    payload: dict[str, Any] | None = None,
    *,
    agent: str = "System",
    settings: Settings | None = None,
) -> Event:
    event = Event(
        project_id=project_id,
        kind=kind,
        message=message,
        payload=payload or {},
        agent=agent,
    )
    session.add(event)
    session.flush()
    if settings is not None:
        from app.task_checkpoints import checkpoint_event

        checkpoint_event(session, project_id, settings, event)
    return event


def create_project(
    session: Session,
    brief: ProjectBrief,
    *,
    settings: Settings | None = None,
) -> Project:
    ledger = ProductionLedger(
        budget_usd=brief.budget_usd,
        repair_reserve_usd=round(brief.budget_usd * brief.repair_reserve_percent / 100, 4),
    )
    project = Project(
        title=brief.title,
        brief=brief.model_dump(),
        ledger=ledger.model_dump(exclude_computed_fields=True),
    )
    session.add(project)
    session.flush()
    add_event(
        session,
        project.id,
        "project.created",
        "Creative brief registered",
        brief.model_dump(),
        agent="Executive Showrunner",
        settings=settings,
    )
    session.commit()
    return get_project(session, project.id)


def get_project(session: Session, project_id: str) -> Project:
    query = (
        select(Project)
        .where(Project.id == project_id)
        .options(selectinload(Project.shots), selectinload(Project.events))
    )
    project = session.scalar(query)
    if project is None:
        raise KeyError(project_id)
    return project


def list_projects(session: Session) -> list[Project]:
    return list(
        session.scalars(
            select(Project)
            .options(selectinload(Project.shots), selectinload(Project.events))
            .order_by(Project.created_at.desc())
        )
    )


def set_project_status(
    session: Session,
    project_id: str,
    status: ProjectStatus,
    message: str | None = None,
    *,
    agent: str = "Executive Showrunner",
    settings: Settings | None = None,
) -> None:
    project = session.get(Project, project_id)
    if project is None:
        raise KeyError(project_id)
    project.status = status.value
    project.updated_at = datetime.now(UTC)
    if message:
        add_event(
            session,
            project_id,
            f"project.{status.value}",
            message,
            {"status": status.value},
            agent=agent,
            settings=settings,
        )
    if settings is not None:
        from app.task_checkpoints import checkpoint_project_read_model

        checkpoint_project_read_model(session, project_id, settings)
    session.commit()


def set_project_error(
    session: Session,
    project_id: str,
    error: str,
    *,
    settings: Settings | None = None,
) -> None:
    project = session.get(Project, project_id)
    if project is None:
        return
    project.status = ProjectStatus.FAILED.value
    project.error = error
    add_event(
        session,
        project_id,
        "project.failed",
        "Production halted after an unrecoverable error",
        {"error": error},
        agent="Production Manager",
        settings=settings,
    )
    if settings is not None:
        from app.task_checkpoints import checkpoint_project_read_model

        checkpoint_project_read_model(session, project_id, settings)
    session.commit()


def save_plan(
    session: Session,
    project_id: str,
    plan: StoryPlan,
    *,
    settings: Settings | None = None,
) -> None:
    project = session.get(Project, project_id)
    if project is None:
        raise KeyError(project_id)
    project.plan = plan.model_dump()
    for existing in list(project.shots):
        session.delete(existing)
    session.flush()
    shots: list[Shot] = []
    for contract in plan.shots:
        shot = Shot(
            project_id=project_id,
            shot_code=contract.id,
            sequence=contract.sequence,
            status=ShotStatus.PLANNED.value,
            contract=contract.model_dump(),
        )
        session.add(shot)
        shots.append(shot)
    add_event(
        session,
        project_id,
        "story.plan.created",
        f"Narrative compiler produced {len(plan.beats)} beats and {len(plan.shots)} shot contracts",
        {"beats": len(plan.beats), "shots": len(plan.shots)},
        agent="Story Architect",
        settings=settings,
    )
    session.flush()
    if settings is not None:
        from app.task_checkpoints import checkpoint_project_read_model, checkpoint_shot_status

        for shot in shots:
            checkpoint_shot_status(session, project_id, settings, shot, update_read_model=False)
        checkpoint_project_read_model(session, project_id, settings)
    session.commit()


def save_ledger(
    session: Session,
    project_id: str,
    ledger: ProductionLedger,
    *,
    settings: Settings | None = None,
) -> None:
    project = session.get(Project, project_id)
    if project is None:
        raise KeyError(project_id)
    project.ledger = ledger.model_dump(exclude_computed_fields=True)
    project.updated_at = datetime.now(UTC)
    if settings is not None:
        from app.task_checkpoints import checkpoint_ledger_snapshot

        checkpoint_ledger_snapshot(session, project_id, settings, ledger)
    session.commit()


def enqueue_job(
    session: Session,
    project_id: str,
    job_type: str,
    idempotency_key: str,
    payload: dict[str, Any] | None = None,
    *,
    settings: Settings | None = None,
) -> Job:
    existing = session.scalar(select(Job).where(Job.idempotency_key == idempotency_key))
    if existing:
        return existing
    payload = payload or {}
    payload.setdefault("task_id", idempotency_key)
    job = Job(
        project_id=project_id,
        job_type=job_type,
        idempotency_key=idempotency_key,
        payload=payload,
    )
    session.add(job)
    add_event(
        session,
        project_id,
        "job.queued",
        f"Queued {job_type}",
        {"job_type": job_type},
        agent="Production Manager",
        settings=settings,
    )
    session.commit()
    session.refresh(job)
    return job


def claim_next_job(session: Session) -> Job | None:
    query = select(Job).where(Job.status == JobStatus.PENDING.value).order_by(Job.created_at).limit(1)
    if session.bind and session.bind.dialect.name == "postgresql":
        query = query.with_for_update(skip_locked=True)
    job = session.scalar(query)
    if not job:
        return None
    job.status = JobStatus.RUNNING.value
    job.attempts += 1
    job.locked_at = datetime.now(UTC)
    session.commit()
    session.refresh(job)
    return job


def finish_job(
    session: Session,
    job_id: str,
    result: dict[str, Any] | None = None,
    error: str | None = None,
) -> None:
    job = session.get(Job, job_id)
    if job is None:
        return
    job.status = JobStatus.FAILED.value if error else JobStatus.SUCCEEDED.value
    job.result = result
    job.error = error
    session.commit()


def get_job_by_task_id(session: Session, task_id: str) -> Job | None:
    return session.scalar(select(Job).where(Job.idempotency_key == task_id))


def project_to_read(project: Project, event_limit: int = 100) -> ProjectRead:
    plan = StoryPlan.model_validate(project.plan) if project.plan else None
    shots = [
        ShotRead(
            id=shot.id,
            shot_code=shot.shot_code,
            sequence=shot.sequence,
            status=ShotStatus(shot.status),
            contract=ShotContract.model_validate(shot.contract),
            storyboard_url=shot.storyboard_url,
            audio_url=shot.audio_url,
            video_url=shot.video_url,
            quality=QualityReport.model_validate(shot.quality) if shot.quality else None,
            attempts=shot.attempts,
            accepted=shot.accepted,
        )
        for shot in sorted(project.shots, key=lambda item: item.sequence)
    ]
    events = [EventRead.model_validate(event) for event in project.events[-event_limit:]]
    return ProjectRead(
        id=project.id,
        title=project.title,
        status=ProjectStatus(project.status),
        brief=ProjectBrief.model_validate(project.brief),
        plan=plan,
        ledger=ProductionLedger.model_validate(project.ledger),
        final_video_url=project.final_video_url,
        error=project.error,
        created_at=project.created_at,
        updated_at=project.updated_at,
        shots=shots,
        events=events,
    )
