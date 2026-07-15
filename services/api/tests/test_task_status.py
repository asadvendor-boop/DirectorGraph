import pytest
from sqlalchemy import create_engine
from sqlalchemy.orm import sessionmaker

from app import worker as worker_module
from app.config import Settings
from app.db import Base
from app.oss_repository import LocalOssRepository, task_status_key
from app.repository import create_project
from app.schemas import ProjectBrief
from app.task_submitter import submit_project_task


@pytest.mark.asyncio
async def test_worker_updates_durable_task_status_lifecycle(tmp_path, monkeypatch):
    engine = create_engine("sqlite:///:memory:")
    Base.metadata.create_all(engine)
    test_session_local = sessionmaker(bind=engine, expire_on_commit=False, autoflush=False)
    settings = Settings(
        inline_worker=False,
        media_root=tmp_path / "media",
        oss_repository_root=tmp_path / "oss",
        database_url=f"sqlite:///{tmp_path / 'task-status.db'}",
    )
    seen: dict[str, str] = {}

    with test_session_local() as session:
        project = create_project(
            session,
            ProjectBrief(
                title="Task status lifecycle",
                premise="A courier robot records running and completed task status.",
                duration_seconds=21,
            ),
        )
        project_id = project.id
        task = submit_project_task(session, project, "run_project", settings)
        task_id = task.task_id

    class DummyOrchestrator:
        def __init__(self, settings):
            self.settings = settings

        async def run_project(self, project_id):
            repo = LocalOssRepository(settings.oss_repository_root)
            status = repo.get_json(task_status_key(project_id, task_id)).payload
            seen["during_run"] = status["status"]
            return {
                "project_id": project_id,
                "final_video_url": f"https://assets.example.invalid/media/projects/{project_id}/final/master.mp4?signature=secret",
            }

        async def patch_project(self, project_id, instruction, affected_shot_ids):
            raise AssertionError("patch should not run")

        async def close(self):
            return None

    monkeypatch.setattr(worker_module, "get_settings", lambda: settings)
    monkeypatch.setattr(worker_module, "SessionLocal", test_session_local)
    monkeypatch.setattr(worker_module, "DirectorGraphOrchestrator", DummyOrchestrator)

    await worker_module.execute_job(task.job_id)

    repo = LocalOssRepository(settings.oss_repository_root)
    final_status = repo.get_json(task_status_key(project_id, task_id)).payload

    assert seen["during_run"] == "running"
    assert final_status["status"] == "succeeded"
    assert final_status["attempts"] == 1
    assert "final_video_url" not in final_status["result"]
    assert "signature=secret" not in str(final_status["result"])
    assert final_status["result"]["media_refs"]["final"]["object_key"] == f"projects/{project_id}/final/master.mp4"
