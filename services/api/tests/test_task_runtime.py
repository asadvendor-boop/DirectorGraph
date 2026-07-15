import pytest
from sqlalchemy import create_engine
from sqlalchemy.orm import Session

from app.config import Settings
from app.core.story import fallback_story_plan
from app.db import Base
from app.models import Project
from app.repository import create_project, get_job_by_task_id, save_plan
from app.schemas import ProjectBrief, ProjectStatus, ShotStatus
from app.task_checkpoints import checkpoint_project_read_model, checkpoint_shot_status
from app.task_runtime import materialize_job_from_payload, parse_payload
from app.task_submitter import submit_project_task


def test_parse_task_payload_accepts_json_job_id():
    assert parse_payload('{"job_id": "job-123"}', None) == {"job_id": "job-123"}


def test_parse_task_payload_cli_job_id_overrides_payload():
    assert parse_payload('{"job_id": "old"}', "new") == {"job_id": "new"}


def test_parse_task_payload_requires_job_id():
    with pytest.raises(ValueError, match="job_id"):
        parse_payload("{}", None)


def test_parse_task_payload_accepts_task_id():
    assert parse_payload("{}", None, "task-123") == {"task_id": "task-123"}


def test_materialize_job_from_payload_rehydrates_task_scratch_db_from_oss(tmp_path):
    web_engine = create_engine("sqlite:///:memory:")
    task_engine = create_engine("sqlite:///:memory:")
    Base.metadata.create_all(web_engine)
    Base.metadata.create_all(task_engine)
    settings = Settings(
        inline_worker=False,
        media_root=tmp_path / "media",
        oss_repository_root=tmp_path / "oss",
        database_url=f"sqlite:///{tmp_path / 'scratch.db'}",
    )
    brief = ProjectBrief(
        title="Serverless materialization",
        premise="A task function reconstructs scratch state from durable OSS checkpoints.",
        duration_seconds=21,
    )

    with Session(web_engine) as session:
        project = create_project(session, brief)
        task = submit_project_task(session, project, "run_project", settings)
        submitted_job = get_job_by_task_id(session, task.task_id)
        assert submitted_job is not None
        payload = dict(submitted_job.payload)

    with Session(task_engine) as session:
        materialized = materialize_job_from_payload(session, settings, payload)
        reloaded = get_job_by_task_id(session, task.task_id)
        project_row = session.get(Project, task.project_id)

    assert payload["project_id"] == task.project_id
    assert payload["job_id"] == task.job_id
    assert materialized.id == task.job_id
    assert reloaded is not None
    assert reloaded.id == task.job_id
    assert reloaded.payload["materialized_from_oss"] is True
    assert project_row is not None
    assert project_row.id == task.project_id
    assert project_row.title == brief.title
    assert project_row.brief["premise"] == brief.premise


def test_materialize_patch_task_rehydrates_plan_and_shots_from_oss_read_model(tmp_path):
    web_engine = create_engine("sqlite:///:memory:")
    task_engine = create_engine("sqlite:///:memory:")
    Base.metadata.create_all(web_engine)
    Base.metadata.create_all(task_engine)
    settings = Settings(
        inline_worker=False,
        media_root=tmp_path / "media",
        oss_repository_root=tmp_path / "oss",
        database_url=f"sqlite:///{tmp_path / 'patch-scratch.db'}",
    )
    brief = ProjectBrief(
        title="Patch materialization",
        premise="A task function reconstructs an accepted project before a semantic patch.",
        duration_seconds=21,
    )

    with Session(web_engine) as session:
        project = create_project(session, brief, settings=settings)
        project_id = project.id
        plan = fallback_story_plan(brief)
        save_plan(session, project_id, plan, settings=settings)
        project = session.get(Project, project_id)
        assert project is not None
        plan.characters[0].reference_url = f"https://media.example.invalid/projects/{project_id}/characters/{plan.characters[0].id}.png"
        project.plan = plan.model_dump()
        project.status = ProjectStatus.COMPLETED.value
        project.final_video_url = f"https://media.example.invalid/projects/{project_id}/final/master.mp4"
        for index, shot in enumerate(project.shots):
            shot.status = ShotStatus.ACCEPTED.value
            shot.accepted = True
            shot.attempts = 1
            shot.storyboard_url = f"https://media.example.invalid/projects/{project_id}/shots/{shot.shot_code}/storyboard.png"
            shot.audio_url = f"https://media.example.invalid/projects/{project_id}/shots/{shot.shot_code}/dialogue.wav"
            shot.video_url = f"https://media.example.invalid/projects/{project_id}/shots/{shot.shot_code}/clip.mp4"
            checkpoint_shot_status(
                session,
                project_id,
                settings,
                shot,
                update_read_model=index == len(project.shots) - 1,
            )
        checkpoint_project_read_model(session, project_id, settings)
        task = submit_project_task(
            session,
            project,
            "patch_project",
            settings,
            payload={"instruction": "Change the ending", "affected_shot_ids": [plan.shots[-1].id]},
        )
        submitted_job = get_job_by_task_id(session, task.task_id)
        assert submitted_job is not None
        payload = dict(submitted_job.payload)

    with Session(task_engine) as session:
        materialized = materialize_job_from_payload(session, settings, payload)
        project_row = session.get(Project, task.project_id)
        shots = sorted(project_row.shots, key=lambda item: item.sequence) if project_row else []

    assert materialized.job_type == "patch_project"
    assert materialized.payload["materialized_from_oss"] is True
    assert project_row is not None
    assert project_row.plan is not None
    assert project_row.plan["characters"][0]["reference_url"].endswith(f"/characters/{plan.characters[0].id}.png")
    assert project_row.final_video_url is not None
    assert len(shots) == len(plan.shots)
    assert shots[0].status == ShotStatus.ACCEPTED.value
    assert shots[0].accepted is True
    assert shots[0].video_url and shots[0].video_url.endswith("/clip.mp4")
    assert shots[-1].contract["video_prompt"].startswith(plan.shots[-1].video_prompt[:24])
