import asyncio
import json
from datetime import UTC, datetime

import pytest
from fastapi import HTTPException
from sqlalchemy import create_engine
from sqlalchemy.orm import Session, sessionmaker

from app.api import routes as routes_module
from app.api.routes import (
    create_project_route,
    get_project_route,
    get_task_by_id,
    judge_test_route,
    list_projects_route,
    production_manifest,
    public_demo_route,
    public_demo_storage_manifest_route,
    run_project_route,
    stop_task_by_id,
    storage_manifest_route,
)
from app.config import Settings
from app.db import Base
from app.function_compute import FunctionComputeInvocation
from app.models import Project
from app.oss_repository import (
    LocalOssRepository,
    original_request_key,
    project_manifest_key,
    task_index_key,
    task_status_key,
)
from app.repository import add_event, create_project, get_job_by_task_id
from app.schemas import JobStatus, JudgeTestRequest, ProductionLedger, ProjectBrief, ProjectStatus
from app.task_checkpoints import checkpoint_project_read_model
from app.task_submitter import deterministic_task_id, submit_project_task


def test_task_id_is_deterministic_and_payload_order_independent():
    first = deterministic_task_id(
        "project-1",
        "patch_project",
        {"instruction": "make dawn warmer", "affected_shot_ids": ["S07"]},
    )
    second = deterministic_task_id(
        "project-1",
        "patch_project",
        {"affected_shot_ids": ["S07"], "instruction": "make dawn warmer"},
    )

    assert first == second
    assert first.startswith("dg-patch-project-")


def test_submit_project_task_reuses_duplicate_task_id(tmp_path):
    engine = create_engine("sqlite:///:memory:")
    Base.metadata.create_all(engine)
    settings = Settings(
        inline_worker=False,
        media_root=tmp_path / "media",
        oss_repository_root=tmp_path / "oss",
        database_url=f"sqlite:///{tmp_path / 'tasks.db'}",
    )
    brief = ProjectBrief(
        title="Task submitter",
        premise="A courier robot queues one deterministic serverless task.",
        duration_seconds=21,
    )

    with Session(engine) as session:
        project = create_project(session, brief)
        project_id = project.id
        first = submit_project_task(session, project, "run_project", settings)
        second = submit_project_task(session, project, "run_project", settings)
        job = get_job_by_task_id(session, first.task_id)

    repo = LocalOssRepository(settings.oss_repository_root)
    manifest = repo.get_project_manifest(project_id)
    task_status = repo.get_json(task_status_key(project_id, first.task_id)).payload
    task_index = repo.get_json(task_index_key(first.task_id)).payload
    event_files = [
        path for path in (settings.oss_repository_root / "projects" / project_id / "events").glob("*.json")
        if not path.name.endswith(".meta.json")
    ]
    ledger_files = [
        path for path in (settings.oss_repository_root / "projects" / project_id / "ledger" / "entries").glob("*.json")
        if not path.name.endswith(".meta.json")
    ]

    assert first.task_id == second.task_id
    assert first.job_id == second.job_id
    assert not first.duplicate
    assert second.duplicate
    assert second.dispatch_mode == "external-task"
    assert job is not None
    assert job.payload["task_id"] == first.task_id
    assert job.payload["checkpoint_manifest_key"] == project_manifest_key(project_id)
    assert manifest.payload["status"] == "queued"
    assert original_request_key(project_id) in manifest.payload["object_keys"]
    assert task_status_key(project_id, first.task_id) in manifest.payload["object_keys"]
    assert task_status["task_id"] == first.task_id
    assert task_status["status"] == "pending"
    assert task_status["duplicate"] is True
    assert task_status["dispatch_mode"] == "external-task"
    assert task_index["project_id"] == project_id
    assert task_index["status_key"] == task_status_key(project_id, first.task_id)
    event_kinds = {
        repo.get_json(path.relative_to(settings.oss_repository_root).as_posix()).payload["kind"]
        for path in event_files
    }
    assert {
        "job.queued",
        "project.queued",
        "task.run_project.submitted",
    }.issubset(event_kinds)
    assert len(ledger_files) == 1


def test_run_route_duplicate_returns_existing_task(tmp_path):
    engine = create_engine("sqlite:///:memory:")
    Base.metadata.create_all(engine)
    settings = Settings(
        inline_worker=False,
        media_root=tmp_path / "media",
        oss_repository_root=tmp_path / "oss",
        database_url=f"sqlite:///{tmp_path / 'tasks.db'}",
    )
    brief = ProjectBrief(
        title="Duplicate route",
        premise="A courier robot submits the same task twice without duplicate work.",
        duration_seconds=21,
    )

    with Session(engine) as session:
        project = create_project(session, brief)
        first = run_project_route(project.id, db=session, settings=settings)
        second = run_project_route(project.id, db=session, settings=settings)

    assert first.task_id == second.task_id
    assert first.job_id == second.job_id
    assert second.status == "pending"


def test_task_status_route_maps_deterministic_task_id(tmp_path):
    engine = create_engine("sqlite:///:memory:")
    Base.metadata.create_all(engine)
    settings = Settings(
        inline_worker=False,
        media_root=tmp_path / "media",
        oss_repository_root=tmp_path / "oss",
        database_url=f"sqlite:///{tmp_path / 'tasks.db'}",
    )
    brief = ProjectBrief(
        title="Task status route",
        premise="A courier robot polls one deterministic serverless task.",
        duration_seconds=21,
    )

    with Session(engine) as session:
        project = create_project(session, brief)
        run = run_project_route(project.id, db=session, settings=settings)
        task = get_task_by_id(run.task_id, db=session, settings=settings)

    assert task["task_id"] == run.task_id
    assert task["id"] == run.job_id
    assert task["status"] == "pending"
    assert task["durable_status"]["task_id"] == run.task_id
    assert task["durable_status"]["status"] == "pending"


def test_task_status_route_recovers_from_oss_index_without_sql_job(tmp_path):
    web_engine = create_engine("sqlite:///:memory:")
    cold_web_engine = create_engine("sqlite:///:memory:")
    Base.metadata.create_all(web_engine)
    Base.metadata.create_all(cold_web_engine)
    settings = Settings(
        inline_worker=False,
        media_root=tmp_path / "media",
        oss_repository_root=tmp_path / "oss",
        database_url=f"sqlite:///{tmp_path / 'tasks.db'}",
    )
    brief = ProjectBrief(
        title="Task index route",
        premise="A web function cold start recovers task status from the durable index.",
        duration_seconds=21,
    )

    with Session(web_engine) as session:
        project = create_project(session, brief)
        run = run_project_route(project.id, db=session, settings=settings)

    with Session(cold_web_engine) as session:
        task = get_task_by_id(run.task_id, db=session, settings=settings)

    assert task["id"] == run.job_id
    assert task["task_id"] == run.task_id
    assert task["project_id"] == run.project_id
    assert task["status"] == "pending"
    assert task["durable_status"]["task_id"] == run.task_id


def test_stop_pending_task_cancels_and_checkpoints_status(tmp_path):
    engine = create_engine("sqlite:///:memory:")
    Base.metadata.create_all(engine)
    settings = Settings(
        inline_worker=False,
        media_root=tmp_path / "media",
        oss_repository_root=tmp_path / "oss",
        database_url=f"sqlite:///{tmp_path / 'task-stop.db'}",
    )
    brief = ProjectBrief(
        title="Task stop route",
        premise="A courier robot cancels a queued task before paid work can begin.",
        duration_seconds=21,
    )

    with Session(engine) as session:
        project = create_project(session, brief)
        run = run_project_route(project.id, db=session, settings=settings)
        stopped = stop_task_by_id(run.task_id, db=session, settings=settings)
        task = get_task_by_id(run.task_id, db=session, settings=settings)
        job = get_job_by_task_id(session, run.task_id)
        restored_project = session.get(Project, project.id)

    repo = LocalOssRepository(settings.oss_repository_root)
    durable_status = repo.get_json(task_status_key(run.project_id, run.task_id)).payload
    event_files = [
        path for path in (settings.oss_repository_root / "projects" / run.project_id / "events").glob("*.json")
        if not path.name.endswith(".meta.json")
    ]
    event_kinds = {
        repo.get_json(path.relative_to(settings.oss_repository_root).as_posix()).payload["kind"]
        for path in event_files
    }

    assert stopped["status"] == JobStatus.CANCELED.value
    assert task["status"] == JobStatus.CANCELED.value
    assert task["result"]["safe_stop"] == "pending-local-task"
    assert task["durable_status"]["status"] == JobStatus.CANCELED.value
    assert durable_status["status"] == JobStatus.CANCELED.value
    assert durable_status["error"] == "Canceled before execution by operator request"
    assert job is not None
    assert job.status == JobStatus.CANCELED.value
    assert restored_project is not None
    assert restored_project.status == ProjectStatus.DRAFT.value
    assert "task.canceled" in event_kinds


def test_stop_running_task_is_refused_as_not_locally_safe(tmp_path):
    engine = create_engine("sqlite:///:memory:")
    Base.metadata.create_all(engine)
    settings = Settings(
        inline_worker=False,
        media_root=tmp_path / "media",
        oss_repository_root=tmp_path / "oss",
        database_url=f"sqlite:///{tmp_path / 'task-stop-running.db'}",
    )
    brief = ProjectBrief(
        title="Running task stop",
        premise="A courier robot refuses unsafe cancellation once a task is running.",
        duration_seconds=21,
    )

    with Session(engine) as session:
        project = create_project(session, brief)
        run = run_project_route(project.id, db=session, settings=settings)
        job = get_job_by_task_id(session, run.task_id)
        assert job is not None
        job.status = JobStatus.RUNNING.value
        session.commit()
        with pytest.raises(HTTPException) as exc_info:
            stop_task_by_id(run.task_id, db=session, settings=settings)

    assert exc_info.value.status_code == 409
    assert "only pending tasks" in exc_info.value.detail


def test_storage_manifest_mints_signed_urls_at_read_time(tmp_path):
    engine = create_engine("sqlite:///:memory:")
    Base.metadata.create_all(engine)
    settings = Settings(
        inline_worker=False,
        media_root=tmp_path / "media",
        oss_repository_root=tmp_path / "oss",
        database_url=f"sqlite:///{tmp_path / 'tasks.db'}",
    )
    brief = ProjectBrief(
        title="Signed URL route",
        premise="A courier robot reads durable object keys through short lived URLs.",
        duration_seconds=21,
    )

    with Session(engine) as session:
        project = create_project(session, brief)
        project_id = project.id
        run_project_route(project_id, db=session, settings=settings)
        payload = storage_manifest_route(project_id, expires_seconds=60, db=session, settings=settings)

    assert payload["schema"] == "directorgraph.storage-manifest-read.v1"
    assert payload["manifest"]["project_id"] == project_id
    assert payload["signed_objects"]
    assert all(item["url"].startswith("local-oss://") for item in payload["signed_objects"])
    assert "signature=" not in json.dumps(payload["manifest"])


def test_project_read_routes_recover_from_oss_without_sql_rows(tmp_path):
    web_engine = create_engine("sqlite:///:memory:")
    cold_web_engine = create_engine("sqlite:///:memory:")
    Base.metadata.create_all(web_engine)
    Base.metadata.create_all(cold_web_engine)
    settings = Settings(
        inline_worker=False,
        media_root=tmp_path / "media",
        oss_repository_root=tmp_path / "oss",
        database_url=f"sqlite:///{tmp_path / 'read-model.db'}",
    )
    brief = ProjectBrief(
        title="OSS read model",
        premise="A cold web function reads project state from durable OSS read models.",
        duration_seconds=21,
    )

    with Session(web_engine) as session:
        created = create_project_route(brief, db=session, settings=settings)
        run_project_route(created.id, db=session, settings=settings)

    with Session(cold_web_engine) as session:
        projects = list_projects_route(db=session, settings=settings)
        recovered = get_project_route(created.id, db=session, settings=settings)
        manifest = production_manifest(created.id, db=session, settings=settings)
        storage = storage_manifest_route(created.id, expires_seconds=60, db=session, settings=settings)

    assert [project.id for project in projects] == [created.id]
    assert recovered.id == created.id
    assert recovered.status == ProjectStatus.QUEUED
    event_kinds = {event.kind for event in recovered.events}
    assert "project.created" in event_kinds
    assert "job.queued" in event_kinds
    assert "project.queued" in event_kinds
    assert "task.run_project.submitted" in event_kinds
    assert manifest["project"]["id"] == created.id
    assert storage["manifest"]["project_id"] == created.id
    assert any(item["key"].endswith("/read-model.json") for item in storage["signed_objects"])


@pytest.mark.asyncio
async def test_event_stream_recovers_oss_events_without_sql_rows(tmp_path, monkeypatch):
    web_engine = create_engine("sqlite:///:memory:")
    cold_web_engine = create_engine("sqlite:///:memory:")
    Base.metadata.create_all(web_engine)
    Base.metadata.create_all(cold_web_engine)
    cold_session_local = sessionmaker(bind=cold_web_engine, expire_on_commit=False, autoflush=False)
    monkeypatch.setattr(routes_module, "SessionLocal", cold_session_local)
    settings = Settings(
        inline_worker=False,
        media_root=tmp_path / "media",
        oss_repository_root=tmp_path / "oss",
        database_url=f"sqlite:///{tmp_path / 'event-stream.db'}",
    )
    brief = ProjectBrief(
        title="OSS event stream",
        premise="A cold web function streams the durable production event trace.",
        duration_seconds=21,
    )

    with Session(web_engine) as session:
        project = create_project(session, brief, settings=settings)
        project_id = project.id
        add_event(
            session,
            project_id,
            "shot.accepted",
            "S01 passed continuity",
            {"shot": "S01", "score": 0.94},
            agent="Continuity Supervisor",
            settings=settings,
        )
        session.commit()

    response = await routes_module.event_stream(project_id, after=0, settings=settings)
    chunks: list[str] = []
    try:
        for _ in range(4):
            chunk = await asyncio.wait_for(response.body_iterator.__anext__(), timeout=1)
            text = chunk.decode() if isinstance(chunk, bytes) else chunk
            chunks.append(text)
            if "shot.accepted" in text:
                break
    finally:
        await response.body_iterator.aclose()

    body = "".join(chunks)
    assert "event: production" in body
    assert "project.created" in body
    assert "shot.accepted" in body
    assert "S01 passed continuity" in body


def test_project_read_routes_prefer_fresher_oss_over_stale_sql_rows(tmp_path):
    web_engine = create_engine("sqlite:///:memory:")
    task_engine = create_engine("sqlite:///:memory:")
    Base.metadata.create_all(web_engine)
    Base.metadata.create_all(task_engine)
    settings = Settings(
        inline_worker=False,
        media_root=tmp_path / "media",
        oss_repository_root=tmp_path / "oss",
        database_url=f"sqlite:///{tmp_path / 'stale-web-read.db'}",
    )
    brief = ProjectBrief(
        title="Fresh OSS read model",
        premise="A web function has stale scratch state while the task function has finished.",
        duration_seconds=21,
    )

    with Session(web_engine) as session:
        created = create_project_route(brief, db=session, settings=settings)
        run_project_route(created.id, db=session, settings=settings)
        stale = session.get(Project, created.id)
        assert stale is not None
        assert stale.status == ProjectStatus.QUEUED.value

    with Session(task_engine) as session:
        completed = Project(
            id=created.id,
            title=created.title,
            status=ProjectStatus.COMPLETED.value,
            brief=created.brief.model_dump(mode="json"),
            ledger=ProductionLedger(budget_usd=brief.budget_usd).model_dump(
                mode="json",
                exclude_computed_fields=True,
            ),
            final_video_url=f"https://media.example.invalid/projects/{created.id}/final/master.mp4",
            created_at=created.created_at,
            updated_at=datetime.now(UTC),
        )
        session.add(completed)
        session.flush()
        checkpoint_project_read_model(session, created.id, settings)
        session.commit()

    configured = Settings(
        inline_worker=False,
        public_demo_project_id=created.id,
        media_root=tmp_path / "media",
        oss_repository_root=tmp_path / "oss",
        database_url=f"sqlite:///{tmp_path / 'stale-web-read.db'}",
    )
    with Session(web_engine) as session:
        detail = get_project_route(created.id, db=session, settings=settings)
        projects = list_projects_route(db=session, settings=settings)
        manifest = production_manifest(created.id, db=session, settings=settings)
        demo = public_demo_route(db=session, settings=configured)
        storage = storage_manifest_route(created.id, expires_seconds=60, db=session, settings=settings)
        still_stale = session.get(Project, created.id)

    assert still_stale is not None
    assert still_stale.status == ProjectStatus.QUEUED.value
    assert detail.status == ProjectStatus.COMPLETED
    assert detail.final_video_url and detail.final_video_url.endswith("/final/master.mp4")
    assert projects[0].status == ProjectStatus.COMPLETED
    assert manifest["project"]["status"] == ProjectStatus.COMPLETED.value
    assert demo.status == ProjectStatus.COMPLETED
    assert storage["manifest"]["project_id"] == created.id


def test_public_demo_routes_are_read_only_and_config_scoped(tmp_path):
    engine = create_engine("sqlite:///:memory:")
    Base.metadata.create_all(engine)
    settings = Settings(
        inline_worker=False,
        media_root=tmp_path / "media",
        oss_repository_root=tmp_path / "oss",
        database_url=f"sqlite:///{tmp_path / 'tasks.db'}",
    )
    brief = ProjectBrief(
        title="Public demo",
        premise="A courier robot can be inspected without starting paid work.",
        duration_seconds=21,
    )

    with Session(engine) as session:
        project = create_project(session, brief)
        project_id = project.id
        run_project_route(project_id, db=session, settings=settings)
        project.status = ProjectStatus.COMPLETED.value
        session.commit()
        checkpoint_project_read_model(session, project_id, settings)
        configured = Settings(
            inline_worker=False,
            public_demo_project_id=project_id,
            media_root=tmp_path / "media",
            oss_repository_root=tmp_path / "oss",
            database_url=f"sqlite:///{tmp_path / 'tasks.db'}",
        )
        demo = public_demo_route(db=session, settings=configured)
        manifest = public_demo_storage_manifest_route(expires_seconds=60, db=session, settings=configured)
        jobs_after_reads = session.execute(Base.metadata.tables["jobs"].select()).all()

    assert demo.id == project_id
    assert manifest["project_id"] == project_id
    assert manifest["signed_objects"]
    assert len(jobs_after_reads) == 1


def test_public_demo_routes_recover_from_oss_without_sql_rows(tmp_path):
    web_engine = create_engine("sqlite:///:memory:")
    cold_web_engine = create_engine("sqlite:///:memory:")
    Base.metadata.create_all(web_engine)
    Base.metadata.create_all(cold_web_engine)
    settings = Settings(
        inline_worker=False,
        media_root=tmp_path / "media",
        oss_repository_root=tmp_path / "oss",
        database_url=f"sqlite:///{tmp_path / 'public-demo.db'}",
    )
    brief = ProjectBrief(
        title="Public demo OSS read",
        premise="A public demo can be inspected from durable OSS read model state.",
        duration_seconds=21,
    )

    with Session(web_engine) as session:
        project = create_project(session, brief)
        project.status = ProjectStatus.COMPLETED.value
        session.commit()
        checkpoint_project_read_model(session, project.id, settings)
        project_id = project.id

    configured = Settings(
        inline_worker=False,
        public_demo_project_id=project_id,
        media_root=tmp_path / "media",
        oss_repository_root=tmp_path / "oss",
        database_url=f"sqlite:///{tmp_path / 'public-demo.db'}",
    )
    with Session(cold_web_engine) as session:
        demo = public_demo_route(db=session, settings=configured)
        manifest = public_demo_storage_manifest_route(expires_seconds=60, db=session, settings=configured)

    assert demo.id == project_id
    assert demo.status == ProjectStatus.COMPLETED
    assert manifest["project_id"] == project_id


def test_public_demo_requires_configuration(tmp_path):
    engine = create_engine("sqlite:///:memory:")
    Base.metadata.create_all(engine)
    settings = Settings(
        media_root=tmp_path / "media",
        oss_repository_root=tmp_path / "oss",
        database_url=f"sqlite:///{tmp_path / 'tasks.db'}",
    )

    with Session(engine) as session:
        with pytest.raises(HTTPException) as exc_info:
            public_demo_route(db=session, settings=settings)

    assert exc_info.value.status_code == 404


def test_submit_project_task_invokes_function_compute(monkeypatch, tmp_path):
    engine = create_engine("sqlite:///:memory:")
    Base.metadata.create_all(engine)
    captured = {}

    def fake_invoke(settings, payload, *, task_id):
        captured["payload"] = payload
        captured["task_id"] = task_id
        return FunctionComputeInvocation(task_id=task_id, request_id="req-123", status_code=202)

    monkeypatch.setattr("app.task_submitter.invoke_function_compute_task", fake_invoke)
    settings = Settings(
        inline_worker=False,
        function_compute_task_url="https://fc.example.invalid/task",
        media_root=tmp_path / "media",
        oss_repository_root=tmp_path / "oss",
        database_url=f"sqlite:///{tmp_path / 'tasks.db'}",
    )
    brief = ProjectBrief(
        title="Function Compute route",
        premise="A courier robot submits one deterministic serverless task.",
        duration_seconds=21,
    )

    with Session(engine) as session:
        project = create_project(session, brief)
        project_id = project.id
        task = submit_project_task(session, project, "run_project", settings)
        job = get_job_by_task_id(session, task.task_id)

    repo = LocalOssRepository(settings.oss_repository_root)
    task_status = repo.get_json(task_status_key(project_id, task.task_id)).payload

    assert task.dispatch_mode == "function-compute"
    assert captured["task_id"] == task.task_id
    assert captured["payload"]["task_id"] == task.task_id
    assert job is not None
    assert job.payload["function_compute_request_id"] == "req-123"
    assert task_status["dispatch_mode"] == "function-compute"
    assert task_status["function_compute_request_id"] == "req-123"
    assert task_status["function_compute_status_code"] == 202


def test_live_run_requires_judge_access_code(tmp_path):
    engine = create_engine("sqlite:///:memory:")
    Base.metadata.create_all(engine)
    settings = Settings(
        provider_mode="live",
        judge_create_access_code="judge-secret",
        inline_worker=False,
        media_root=tmp_path / "media",
        oss_repository_root=tmp_path / "oss",
        database_url=f"sqlite:///{tmp_path / 'tasks.db'}",
    )
    brief = ProjectBrief(
        title="Judge gate",
        premise="A courier robot refuses paid work without the judge code.",
        duration_seconds=21,
    )

    with Session(engine) as session:
        project = create_project(session, brief)
        with pytest.raises(HTTPException) as exc_info:
            run_project_route(project.id, db=session, settings=settings)
        assert exc_info.value.status_code == 403

        with pytest.raises(HTTPException) as config_exc:
            run_project_route(
                project.id,
                db=session,
                settings=settings,
                judge_code="judge-secret",
            )

    assert config_exc.value.status_code == 503
    assert "OSS credentials" in config_exc.value.detail
    assert "FUNCTION_COMPUTE_TASK_URL" in config_exc.value.detail


def test_live_run_with_serverless_config_invokes_function_compute(monkeypatch, tmp_path):
    engine = create_engine("sqlite:///:memory:")
    Base.metadata.create_all(engine)
    captured = {}

    def fake_invoke(settings, payload, *, task_id):
        captured["payload"] = payload
        captured["task_id"] = task_id
        return FunctionComputeInvocation(task_id=task_id, request_id="req-live", status_code=202)

    monkeypatch.setattr("app.task_submitter.invoke_function_compute_task", fake_invoke)
    monkeypatch.setattr(
        "app.task_checkpoints.create_oss_repository",
        lambda settings: LocalOssRepository(settings.oss_repository_root),
    )
    settings = Settings(
        provider_mode="live",
        judge_create_access_code="judge-secret",
        inline_worker=False,
        function_compute_task_url="https://fc.example.invalid/task",
        public_media_base_url="https://media.example.invalid",
        oss_endpoint="https://oss.example.invalid",
        oss_bucket="directorgraph-private",
        oss_access_key_id="test-access-key",
        oss_access_key_secret="test-access-secret",
        media_root=tmp_path / "media",
        oss_repository_root=tmp_path / "oss",
        database_url=f"sqlite:///{tmp_path / 'tasks.db'}",
    )
    brief = ProjectBrief(
        title="Live serverless gate",
        premise="A courier robot submits only when live serverless settings are present.",
        duration_seconds=21,
    )

    with Session(engine) as session:
        project = create_project(session, brief)
        run = run_project_route(
            project.id,
            db=session,
            settings=settings,
            judge_code="judge-secret",
        )

    assert run.status == "pending"
    assert captured["task_id"] == run.task_id
    assert captured["payload"]["project_id"] == run.project_id


def test_judge_test_requires_access_code_when_configured(tmp_path):
    engine = create_engine("sqlite:///:memory:")
    Base.metadata.create_all(engine)
    settings = Settings(
        judge_create_access_code="judge-secret",
        inline_worker=False,
        media_root=tmp_path / "media",
        oss_repository_root=tmp_path / "oss",
        database_url=f"sqlite:///{tmp_path / 'judge-test.db'}",
    )

    with Session(engine) as session:
        with pytest.raises(HTTPException) as exc_info:
            judge_test_route(db=session, settings=settings)

    assert exc_info.value.status_code == 403


def test_judge_test_creates_bounded_project_and_task(tmp_path):
    engine = create_engine("sqlite:///:memory:")
    Base.metadata.create_all(engine)
    settings = Settings(
        judge_create_access_code="judge-secret",
        judge_run_max_duration_seconds=12,
        judge_run_max_shots=3,
        max_project_spend_usd=4,
        inline_worker=False,
        media_root=tmp_path / "media",
        oss_repository_root=tmp_path / "oss",
        database_url=f"sqlite:///{tmp_path / 'judge-test.db'}",
    )

    with Session(engine) as session:
        run = judge_test_route(
            request=JudgeTestRequest(
                premise="Courier-7 must preserve the red paper crane during one final judge test."
            ),
            db=session,
            settings=settings,
            judge_code="judge-secret",
        )
        job = get_job_by_task_id(session, run.task_id)
        project_row = session.get(Project, run.project_id)

    assert run.status == "pending"
    assert job is not None
    assert project_row is not None
    assert project_row.brief["production_profile"] == "judge_test"
    assert project_row.brief["duration_seconds"] == 12
    assert project_row.brief["max_shots"] == 3
    # Judge runs take the tighter of the $5 judge ceiling (base run plus two full
    # repair cycles) and the deployment's own project cap — here $4 binds.
    assert project_row.brief["budget_usd"] == 4.0
