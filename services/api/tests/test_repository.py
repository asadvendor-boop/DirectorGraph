from sqlalchemy import create_engine
from sqlalchemy.orm import Session

from app.config import Settings
from app.core.story import fallback_story_plan
from app.db import Base
from app.oss_repository import LocalOssRepository, project_manifest_key
from app.repository import create_project, get_project, project_to_read, save_plan
from app.schemas import ProjectBrief, ProjectStatus


def test_project_and_story_graph_round_trip():
    engine = create_engine("sqlite:///:memory:")
    Base.metadata.create_all(engine)
    brief = ProjectBrief(
        title="Repository test",
        premise="A small robot brings the same letter to an unopened door every night.",
        duration_seconds=28,
    )
    with Session(engine) as session:
        project = create_project(session, brief)
        assert project.status == ProjectStatus.DRAFT.value
        plan = fallback_story_plan(brief)
        save_plan(session, project.id, plan)
        read = project_to_read(get_project(session, project.id))
        assert read.plan is not None
        assert len(read.shots) == 7
        assert read.shots[4].contract.id == "S05"
        assert read.events[0].agent == "Executive Showrunner"


def test_repository_events_can_checkpoint_to_oss(tmp_path):
    engine = create_engine("sqlite:///:memory:")
    Base.metadata.create_all(engine)
    settings = Settings(
        inline_worker=False,
        media_root=tmp_path / "media",
        oss_repository_root=tmp_path / "oss",
        database_url=f"sqlite:///{tmp_path / 'repository-events.db'}",
    )
    brief = ProjectBrief(
        title="Durable event test",
        premise="A courier robot keeps its production trace outside scratch SQL.",
        duration_seconds=21,
    )

    with Session(engine) as session:
        project = create_project(session, brief, settings=settings)
        project_id = project.id
        plan = fallback_story_plan(brief)
        save_plan(session, project_id, plan, settings=settings)

    repo = LocalOssRepository(settings.oss_repository_root)
    manifest = repo.get_json(project_manifest_key(project_id)).payload
    event_keys = [
        key for key in manifest["object_keys"]
        if key.startswith(f"projects/{project_id}/events/")
    ]
    event_payloads = [repo.get_json(key).payload for key in event_keys]
    event_kinds = {payload["kind"] for payload in event_payloads}

    assert "project.created" in event_kinds
    assert "story.plan.created" in event_kinds
    assert all("signature=" not in str(payload) for payload in event_payloads)
