import json
from types import SimpleNamespace

import pytest
from sqlalchemy import create_engine
from sqlalchemy.orm import Session

from app import task_checkpoints as checkpoints_module
from app.config import Settings
from app.core.story import fallback_story_plan
from app.db import Base
from app.oss_repository import (
    LocalOssRepository,
    OssNotFoundError,
    asset_materialization_key,
    final_asset_materialization_key,
    final_manifest_key,
    inspection_key,
    ledger_snapshot_key,
    project_manifest_key,
    project_read_model_key,
    provider_result_key,
    provider_task_key,
    shot_contract_key,
    shot_status_key,
    story_ir_key,
)
from app.providers.base import AssetResult
from app.repository import add_event, create_project, project_to_read, save_ledger, save_plan
from app.schemas import (
    ProductionLedger,
    ProjectBrief,
    ProjectStatus,
    QualityDimension,
    QualityReport,
    ShotStatus,
)
from app.task_checkpoints import (
    checkpoint_asset_key,
    checkpoint_existing_json_object,
    checkpoint_final_asset_materialization_object,
    checkpoint_final_manifest,
    checkpoint_inspection,
    checkpoint_project_read_model,
    checkpoint_provider_task,
    checkpoint_shot_status,
    checkpoint_story_plan,
    load_project_events,
    load_project_read_model,
    reserve_live_spend,
)


def test_story_and_final_manifest_checkpoints_update_project_manifest(tmp_path):
    engine = create_engine("sqlite:///:memory:")
    Base.metadata.create_all(engine)
    settings = Settings(
        inline_worker=False,
        media_root=tmp_path / "media",
        oss_repository_root=tmp_path / "oss",
        database_url=f"sqlite:///{tmp_path / 'checkpoints.db'}",
    )
    brief = ProjectBrief(
        title="Checkpoint repository test",
        premise="A courier robot records recovery checkpoints for a finished film.",
        duration_seconds=21,
    )

    with Session(engine) as session:
        project = create_project(session, brief)
        project_id = project.id
        plan = fallback_story_plan(brief)
        save_plan(session, project_id, plan)
        story_key = checkpoint_story_plan(session, project_id, settings, plan)
        project = session.get(type(project), project_id)
        assert project is not None
        project.status = ProjectStatus.COMPLETED.value
        project.final_video_url = "https://signed.example.invalid/final.mp4?signature=secret"
        project.shots[0].storyboard_url = "https://signed.example.invalid/storyboard.png?signature=secret"
        project.shots[0].video_url = "https://signed.example.invalid/clip.mp4?signature=secret"
        session.commit()
        final_key = checkpoint_final_manifest(session, project_id, settings)

    repo = LocalOssRepository(settings.oss_repository_root)
    manifest = repo.get_project_manifest(project_id).payload
    final_manifest = repo.get_json(final_manifest_key(project_id)).payload
    contract_keys = [shot_contract_key(project_id, contract.id, 1) for contract in plan.shots]

    assert story_key == story_ir_key(project_id, 1)
    assert final_key == final_manifest_key(project_id)
    assert manifest["story_ir_key"] == story_key
    assert manifest["final_manifest_key"] == final_key
    assert story_key in manifest["object_keys"]
    assert set(contract_keys).issubset(set(manifest["object_keys"]))
    assert final_key in manifest["object_keys"]
    assert repo.get_json(contract_keys[0]).payload["contract"]["id"] == plan.shots[0].id
    assert final_manifest["project"]["status"] == "completed"
    assert final_manifest["storage"]["manifest_key"] == project_manifest_key(project_id)
    assert final_manifest["storage"]["final_manifest_key"] == final_key
    assert final_key in final_manifest["storage"]["object_keys"]
    final_manifest_json = json.dumps(final_manifest)
    assert "signature=secret" not in final_manifest_json
    assert "final_video_url" not in final_manifest_json
    assert "storyboard_url" not in final_manifest_json
    assert "video_url" not in final_manifest_json


def test_project_manifest_checkpoint_recovers_corrupt_local_manifest(tmp_path):
    engine = create_engine("sqlite:///:memory:")
    Base.metadata.create_all(engine)
    settings = Settings(
        inline_worker=False,
        media_root=tmp_path / "media",
        oss_repository_root=tmp_path / "oss",
        database_url=f"sqlite:///{tmp_path / 'corrupt-manifest.db'}",
    )
    brief = ProjectBrief(
        title="Manifest recovery",
        premise="A courier robot repairs a stale durable manifest before continuing.",
        duration_seconds=21,
    )

    with Session(engine) as session:
        project = create_project(session, brief)
        project_id = project.id
        repo = LocalOssRepository(settings.oss_repository_root)
        local_key = f"projects/{project_id}/local/recovery-note.json"
        first = repo.put_json(
            project_manifest_key(project_id),
            {
                "schema_version": "directorgraph.project-manifest.v0",
                "project_id": project_id,
                "created_at": "not-a-date",
                "story_ir_key": "projects/local/story-ir.json",
                "object_keys": [local_key, "../secret.json"],
            },
        )

        checkpoint_project_read_model(session, project_id, settings)

    recovered = repo.get_project_manifest(project_id)
    payload = recovered.payload

    assert recovered.ref.etag != first.etag
    assert payload["schema_version"] == "directorgraph.project-manifest.v1"
    assert payload["project_id"] == project_id
    assert payload["title"] == brief.title
    assert local_key in payload["object_keys"]
    assert "../secret.json" not in payload["object_keys"]
    assert project_read_model_key(project_id) in payload["object_keys"]


def test_project_read_model_stores_media_refs_without_persisted_urls(tmp_path):
    engine = create_engine("sqlite:///:memory:")
    Base.metadata.create_all(engine)
    settings = Settings(
        inline_worker=False,
        media_root=tmp_path / "media",
        oss_repository_root=tmp_path / "oss",
        database_url=f"sqlite:///{tmp_path / 'read-model-media-refs.db'}",
    )
    brief = ProjectBrief(
        title="Read model media refs",
        premise="A serverless web function regenerates media URLs from durable object keys.",
        duration_seconds=21,
    )

    with Session(engine) as session:
        project = create_project(session, brief)
        project_id = project.id
        plan = fallback_story_plan(brief)
        save_plan(session, project_id, plan)
        project = session.get(type(project), project_id)
        assert project is not None
        plan.characters[0].reference_url = f"https://signed.example.invalid/media/projects/{project_id}/characters/{plan.characters[0].id}.png?signature=secret"
        project.plan = plan.model_dump()
        first_shot = project.shots[0]
        shot_code = first_shot.shot_code
        project.status = ProjectStatus.COMPLETED.value
        project.final_video_url = f"https://signed.example.invalid/media/projects/{project_id}/final/master.mp4?signature=secret"
        first_shot.storyboard_url = f"https://signed.example.invalid/media/projects/{project_id}/shots/{shot_code}/storyboard.png?signature=secret"
        first_shot.audio_url = f"https://signed.example.invalid/media/projects/{project_id}/shots/{shot_code}/dialogue.wav?signature=secret"
        first_shot.video_url = f"https://signed.example.invalid/media/projects/{project_id}/shots/{shot_code}/clip.mp4?signature=secret"
        first_shot.attempts = 1
        checkpoint_project_read_model(session, project_id, settings)

    repo = LocalOssRepository(settings.oss_repository_root)
    payload = repo.get_json(project_read_model_key(project_id)).payload
    payload_json = json.dumps(payload)
    recovered = load_project_read_model(settings, project_id)

    assert "signature=secret" not in payload_json
    assert "final_video_url" not in payload_json
    assert "storyboard_url" not in payload_json
    assert "audio_url" not in payload_json
    assert "video_url" not in payload_json
    assert "reference_url" not in payload_json
    assert payload["media_refs"]["characters"][plan.characters[0].id]["object_key"] == (
        f"projects/{project_id}/characters/{plan.characters[0].id}.png"
    )
    assert payload["media_refs"]["final"]["object_key"] == f"projects/{project_id}/final/master.mp4"
    assert payload["media_refs"]["shots"][shot_code]["storyboard"]["object_key"].endswith(
        "/storyboard.png"
    )
    assert recovered is not None
    assert recovered.plan is not None
    assert recovered.plan.characters[0].reference_url
    assert recovered.plan.characters[0].reference_url.endswith(f"/characters/{plan.characters[0].id}.png")
    assert recovered.final_video_url and recovered.final_video_url.endswith("/final/master.mp4")
    assert recovered.shots[0].storyboard_url and recovered.shots[0].storyboard_url.endswith("/storyboard.png")
    assert recovered.shots[0].audio_url and recovered.shots[0].audio_url.endswith("/dialogue.wav")
    assert recovered.shots[0].video_url and recovered.shots[0].video_url.endswith("/clip.mp4")


def test_project_read_model_live_oss_prefers_public_media_base_url(tmp_path, monkeypatch):
    engine = create_engine("sqlite:///:memory:")
    Base.metadata.create_all(engine)
    settings = Settings(
        inline_worker=False,
        media_root=tmp_path / "media",
        oss_repository_root=tmp_path / "oss",
        oss_endpoint="https://oss.example.invalid",
        oss_bucket="directorgraph",
        oss_access_key_id="test-key",
        oss_access_key_secret="test-secret",
        public_media_base_url="https://directorgraph.example.invalid/media",
        database_url=f"sqlite:///{tmp_path / 'live-presign-read-model.db'}",
    )
    brief = ProjectBrief(
        title="Live presigned read model",
        premise="A live web function mints short-lived OSS URLs from durable object keys.",
        duration_seconds=21,
    )

    with Session(engine) as session:
        project = create_project(session, brief)
        project_id = project.id
        read_payload = project_to_read(project).model_dump(mode="json")

    object_key = f"projects/{project_id}/final/master.mp4"
    read_model_payload = {
        "schema_version": "directorgraph.project-read-model.v1",
        "project": read_payload,
        "media_refs": {
            "schema": "directorgraph.project-read-media-refs.v1",
            "final": {"object_key": object_key},
            "shots": {},
        },
        "updated_at": read_payload["updated_at"],
    }
    seen: dict[str, object] = {}

    class FakeLiveOssRepository:
        def get_json(self, key):
            if key == project_read_model_key(project_id):
                return SimpleNamespace(payload=read_model_payload)
            raise OssNotFoundError(key)

        def list_keys(self, prefix):
            return []

        def presign_get(self, key, *, expires_seconds=900):
            seen["key"] = key
            seen["expires_seconds"] = expires_seconds
            return SimpleNamespace(url=f"https://signed.example.invalid/{key}?expires={expires_seconds}")

    monkeypatch.setattr(
        checkpoints_module,
        "create_oss_repository",
        lambda configured_settings: FakeLiveOssRepository(),
    )

    recovered = checkpoints_module.load_project_read_model(settings, project_id)

    assert recovered is not None
    assert recovered.final_video_url == f"https://directorgraph.example.invalid/media/{object_key}"
    assert seen == {}


def test_durable_event_payload_strips_media_urls(tmp_path):
    engine = create_engine("sqlite:///:memory:")
    Base.metadata.create_all(engine)
    settings = Settings(
        inline_worker=False,
        media_root=tmp_path / "media",
        oss_repository_root=tmp_path / "oss",
        database_url=f"sqlite:///{tmp_path / 'event-media-refs.db'}",
    )
    brief = ProjectBrief(
        title="Event media refs",
        premise="Append-only event evidence records object keys instead of signed URLs.",
        duration_seconds=21,
    )

    with Session(engine) as session:
        project = create_project(session, brief)
        project_id = project.id
        add_event(
            session,
            project_id,
            "edit.master.created",
            "Master assembled",
            {
                "final_video_url": f"https://signed.example.invalid/media/projects/{project_id}/final/master.mp4?signature=secret",
                "acceptance_ratio": 1,
            },
            agent="Picture Editor",
            settings=settings,
        )

    events = load_project_events(settings, project_id)
    matching = [event for event in events if event.kind == "edit.master.created"]

    assert matching
    payload = matching[-1].payload
    assert "final_video_url" not in payload
    assert "signature=secret" not in json.dumps(payload)
    assert payload["media_refs"]["final"]["object_key"] == f"projects/{project_id}/final/master.mp4"
    assert payload["acceptance_ratio"] == 1


def test_provider_task_and_inspection_checkpoints_update_manifest(tmp_path):
    engine = create_engine("sqlite:///:memory:")
    Base.metadata.create_all(engine)
    settings = Settings(
        inline_worker=False,
        media_root=tmp_path / "media",
        oss_repository_root=tmp_path / "oss",
        database_url=f"sqlite:///{tmp_path / 'provider-checkpoints.db'}",
    )
    brief = ProjectBrief(
        title="Provider checkpoint test",
        premise="A courier robot records provider and inspection checkpoints.",
        duration_seconds=21,
    )

    with Session(engine) as session:
        project = create_project(session, brief)
        project_id = project.id
        plan = fallback_story_plan(brief)
        save_plan(session, project_id, plan)
        contract = plan.shots[0]
        video_path = tmp_path / "clip.mp4"
        video_path.write_bytes(b"video")
        provider_key = checkpoint_provider_task(
            session,
            project_id,
            settings,
            contract,
            AssetResult(
                "https://assets.example.invalid/clip.mp4",
                video_path,
                "Alibaba Cloud Model Studio",
                "wan-test",
                task_id="task-abc",
            ),
            1,
            operation="render",
        )
        report = QualityReport(
            passed=True,
            overall_score=0.91,
            dimensions=[
                QualityDimension(name="narrative", score=0.91, evidence="Objective visible"),
                QualityDimension(name="identity", score=0.91, evidence="Identity stable"),
                QualityDimension(name="continuity", score=0.91, evidence="Prop present"),
                QualityDimension(name="camera", score=0.91, evidence="Framing matched"),
                QualityDimension(name="motion", score=0.91, evidence="Motion stable"),
                QualityDimension(name="dialogue", score=0.91, evidence="Dialogue aligned"),
                QualityDimension(name="safety", score=1, evidence="No safety issue"),
            ],
            evaluator_model="qwen-vl-test",
            attempt=1,
        )
        inspection_ref = checkpoint_inspection(
            session,
            project_id,
            settings,
            contract,
            report,
            1,
            model="qwen-vl-test",
            input_tokens=540,
        )

    repo = LocalOssRepository(settings.oss_repository_root)
    manifest = repo.get_project_manifest(project_id).payload
    provider_payload = repo.get_json(provider_task_key(project_id, contract.id, 1)).payload
    inspection_payload = repo.get_json(inspection_key(project_id, contract.id, 1)).payload

    assert provider_key == provider_task_key(project_id, contract.id, 1)
    assert inspection_ref == inspection_key(project_id, contract.id, 1)
    assert provider_key in manifest["object_keys"]
    assert inspection_ref in manifest["object_keys"]
    assert provider_payload["task_id"] == "task-abc"
    assert inspection_payload["report"]["overall_score"] == 0.91


def test_ledger_snapshot_updates_manifest_and_overlays_stale_read_model(tmp_path):
    engine = create_engine("sqlite:///:memory:")
    Base.metadata.create_all(engine)
    settings = Settings(
        inline_worker=False,
        media_root=tmp_path / "media",
        oss_repository_root=tmp_path / "oss",
        database_url=f"sqlite:///{tmp_path / 'ledger-snapshot.db'}",
    )
    brief = ProjectBrief(
        title="Ledger snapshot test",
        premise="A courier robot records the latest production ledger for recovery.",
        duration_seconds=21,
        budget_usd=8,
    )

    with Session(engine) as session:
        project = create_project(session, brief)
        project_id = project.id
        ledger = ProductionLedger.model_validate(project.ledger)
        ledger.video_seconds_generated = 6
        ledger.video_seconds_accepted = 3
        ledger.estimated_cost_usd = 1.25
        save_ledger(session, project_id, ledger, settings=settings)

    repo = LocalOssRepository(settings.oss_repository_root)
    payload = repo.get_json(ledger_snapshot_key(project_id)).payload
    manifest = repo.get_project_manifest(project_id).payload
    stale_read = repo.get_json(project_read_model_key(project_id)).payload
    stale_read["project"]["ledger"]["estimated_cost_usd"] = 0
    stale_read["project"]["ledger"]["video_seconds_generated"] = 0
    repo.put_json(project_read_model_key(project_id), stale_read)

    recovered = load_project_read_model(settings, project_id)

    assert payload["ledger"]["estimated_cost_usd"] == 1.25
    assert payload["ledger"]["acceptance_ratio"] == 0.5
    assert ledger_snapshot_key(project_id) in manifest["object_keys"]
    assert recovered is not None
    assert recovered.ledger.estimated_cost_usd == 1.25
    assert recovered.ledger.acceptance_ratio == 0.5


def test_shot_status_snapshot_updates_manifest_and_overlays_stale_read_model(tmp_path):
    engine = create_engine("sqlite:///:memory:")
    Base.metadata.create_all(engine)
    settings = Settings(
        inline_worker=False,
        media_root=tmp_path / "media",
        oss_repository_root=tmp_path / "oss",
        database_url=f"sqlite:///{tmp_path / 'shot-status-snapshot.db'}",
    )
    brief = ProjectBrief(
        title="Shot status snapshot test",
        premise="A courier robot records shot state without persisting signed URLs.",
        duration_seconds=21,
    )
    report = QualityReport(
        passed=True,
        overall_score=0.93,
        dimensions=[
            QualityDimension(name="narrative", score=0.93, evidence="Objective visible"),
            QualityDimension(name="identity", score=0.93, evidence="Identity stable"),
            QualityDimension(name="continuity", score=0.93, evidence="Prop present"),
            QualityDimension(name="camera", score=0.93, evidence="Framing matched"),
            QualityDimension(name="motion", score=0.93, evidence="Motion stable"),
            QualityDimension(name="dialogue", score=0.93, evidence="Dialogue aligned"),
            QualityDimension(name="safety", score=1, evidence="No safety issue"),
        ],
        evaluator_model="qwen-vl-test",
        attempt=2,
    )

    with Session(engine) as session:
        project = create_project(session, brief)
        project_id = project.id
        plan = fallback_story_plan(brief)
        save_plan(session, project_id, plan, settings=settings)
        shot = project.shots[0]
        shot.status = ShotStatus.ACCEPTED.value
        shot.attempts = 2
        shot.accepted = True
        shot.video_url = "https://signed.example.invalid/clip.mp4?signature=secret"
        shot.quality = report.model_dump()
        checkpoint_shot_status(session, project_id, settings, shot)
        shot_code = shot.shot_code
        session.commit()

    repo = LocalOssRepository(settings.oss_repository_root)
    key = shot_status_key(project_id, shot_code)
    payload = repo.get_json(key).payload
    manifest = repo.get_project_manifest(project_id).payload
    stale_read = repo.get_json(project_read_model_key(project_id)).payload
    stale_read["project"]["shots"][0]["status"] = ShotStatus.RENDERING.value
    stale_read["project"]["shots"][0]["attempts"] = 1
    stale_read["project"]["shots"][0]["accepted"] = False
    stale_read["project"]["shots"][0]["quality"] = None
    repo.put_json(project_read_model_key(project_id), stale_read)

    recovered = load_project_read_model(settings, project_id)

    assert key in manifest["object_keys"]
    assert payload["status"] == ShotStatus.ACCEPTED.value
    assert payload["attempts"] == 2
    assert payload["quality"]["overall_score"] == 0.93
    assert payload["materialization_keys"]["video"] == asset_materialization_key(project_id, shot_code, 2)
    assert "video_url" not in payload
    assert "storyboard_url" not in payload
    assert recovered is not None
    assert recovered.shots[0].status == ShotStatus.ACCEPTED
    assert recovered.shots[0].attempts == 2
    assert recovered.shots[0].accepted is True
    assert recovered.shots[0].quality is not None
    assert recovered.shots[0].quality.overall_score == 0.93


def test_asset_object_key_checkpoint_updates_manifest_and_local_repo(tmp_path):
    engine = create_engine("sqlite:///:memory:")
    Base.metadata.create_all(engine)
    settings = Settings(
        inline_worker=False,
        media_root=tmp_path / "media",
        oss_repository_root=tmp_path / "oss",
        database_url=f"sqlite:///{tmp_path / 'asset-checkpoints.db'}",
    )
    brief = ProjectBrief(
        title="Asset checkpoint test",
        premise="A courier robot stores media object keys for signed readback.",
        duration_seconds=21,
    )
    object_key = "projects/demo-project/shots/S01/storyboard.png"
    media_path = settings.media_root / object_key
    media_path.parent.mkdir(parents=True)
    media_path.write_bytes(b"image-bytes")

    with Session(engine) as session:
        project = create_project(session, brief)
        project_id = project.id
        first = checkpoint_asset_key(session, project_id, settings, object_key)
        second = checkpoint_asset_key(session, project_id, settings, object_key)

    repo = LocalOssRepository(settings.oss_repository_root)
    manifest = repo.get_project_manifest(project_id).payload
    data, ref = repo.get_bytes(object_key)
    signed = repo.presign_get(object_key)

    assert first == object_key
    assert second == object_key
    assert manifest["object_keys"].count(object_key) == 1
    assert data == b"image-bytes"
    assert ref.content_type == "image/png"
    assert signed.key == object_key


def test_existing_json_checkpoint_link_updates_manifest(tmp_path):
    engine = create_engine("sqlite:///:memory:")
    Base.metadata.create_all(engine)
    settings = Settings(
        inline_worker=False,
        media_root=tmp_path / "media",
        oss_repository_root=tmp_path / "oss",
        database_url=f"sqlite:///{tmp_path / 'json-link-checkpoints.db'}",
    )
    brief = ProjectBrief(
        title="JSON checkpoint link test",
        premise="A courier robot links existing recovery JSON objects into the manifest.",
        duration_seconds=21,
    )

    with Session(engine) as session:
        project = create_project(session, brief)
        project_id = project.id
        key = provider_result_key(project_id, "S01", 1)
        assert checkpoint_existing_json_object(session, project_id, settings, key) is None

    repo = LocalOssRepository(settings.oss_repository_root)
    repo.put_json(key, {"project_id": project_id, "shot_id": "S01"})

    with Session(engine) as session:
        linked = checkpoint_existing_json_object(session, project_id, settings, key)

    manifest = repo.get_project_manifest(project_id).payload
    assert linked == key
    assert key in manifest["object_keys"]


def test_final_asset_materialization_checkpoint_links_manifest(tmp_path):
    engine = create_engine("sqlite:///:memory:")
    Base.metadata.create_all(engine)
    settings = Settings(
        inline_worker=False,
        media_root=tmp_path / "media",
        oss_repository_root=tmp_path / "oss",
        database_url=f"sqlite:///{tmp_path / 'final-materialization.db'}",
    )
    brief = ProjectBrief(
        title="Final asset materialization test",
        premise="A courier robot records the final master key before manifest export.",
        duration_seconds=21,
    )
    object_key = "projects/final-demo/final/directorgraph-master.mp4"

    with Session(engine) as session:
        project = create_project(session, brief)
        project_id = project.id
        key = checkpoint_final_asset_materialization_object(
            settings,
            project_id,
            object_key=object_key,
            model="DirectorGraph Picture Editor",
        )
        linked = checkpoint_existing_json_object(session, project_id, settings, key)

    repo = LocalOssRepository(settings.oss_repository_root)
    payload = repo.get_json(final_asset_materialization_key(project_id)).payload
    manifest = repo.get_project_manifest(project_id).payload

    assert key == final_asset_materialization_key(project_id)
    assert linked == key
    assert payload["object_key"] == object_key
    assert payload["model"] == "DirectorGraph Picture Editor"
    assert key in manifest["object_keys"]


def test_live_spend_reservation_updates_ledger_and_is_idempotent(tmp_path):
    engine = create_engine("sqlite:///:memory:")
    Base.metadata.create_all(engine)
    settings = Settings(
        provider_mode="live",
        max_project_spend_usd=10,
        max_total_live_spend_usd=10,
        repair_reserve_percent=20,
        inline_worker=False,
        media_root=tmp_path / "media",
        oss_repository_root=tmp_path / "oss",
        database_url=f"sqlite:///{tmp_path / 'spend-reservation.db'}",
    )
    brief = ProjectBrief(
        title="Spend reservation test",
        premise="A courier robot reserves spend before a paid render starts.",
        duration_seconds=21,
        budget_usd=8,
        repair_reserve_percent=20,
    )

    with Session(engine) as session:
        project = create_project(session, brief)
        project_id = project.id
        production_ledger = ProductionLedger.model_validate(project.ledger)
        first = reserve_live_spend(
            session,
            project_id,
            settings,
            production_ledger,
            reservation_id="S01-attempt-1-render",
            amount_usd=1.5,
            category="video-render",
            description="Reserve render",
            payload={"shot_id": "S01", "attempt": 1},
            preserve_repair_reserve=True,
        )
        second = reserve_live_spend(
            session,
            project_id,
            settings,
            production_ledger,
            reservation_id="S01-attempt-1-render",
            amount_usd=1.5,
            category="video-render",
            description="Reserve render",
            payload={"shot_id": "S01", "attempt": 1},
            preserve_repair_reserve=True,
        )
        refreshed = session.get(type(project), project_id)

    repo = LocalOssRepository(settings.oss_repository_root)
    assert first is not None
    key = first.ledger_key
    payload = repo.get_json(key).payload
    ledger_snapshot = repo.get_json(ledger_snapshot_key(project_id)).payload
    manifest = repo.get_project_manifest(project_id).payload

    assert not first.duplicate
    assert second is not None and second.duplicate
    assert refreshed is not None
    assert refreshed.ledger["estimated_cost_usd"] == 1.5
    assert production_ledger.estimated_cost_usd == 1.5
    assert payload["category"] == "video-render"
    assert payload["amount_usd"] == 1.5
    assert payload["payload"]["estimated"] is True
    assert key in manifest["object_keys"]
    assert ledger_snapshot_key(project_id) in manifest["object_keys"]
    assert ledger_snapshot["ledger"]["estimated_cost_usd"] == 1.5


def test_live_spend_reservation_preserves_repair_reserve(tmp_path):
    engine = create_engine("sqlite:///:memory:")
    Base.metadata.create_all(engine)
    settings = Settings(
        provider_mode="live",
        max_project_spend_usd=8,
        max_total_live_spend_usd=10,
        repair_reserve_percent=20,
        inline_worker=False,
        media_root=tmp_path / "media",
        oss_repository_root=tmp_path / "oss",
        database_url=f"sqlite:///{tmp_path / 'spend-reserve-refusal.db'}",
    )
    brief = ProjectBrief(
        title="Repair reserve test",
        premise="A courier robot protects repair budget before new renders.",
        duration_seconds=21,
        budget_usd=8,
        repair_reserve_percent=20,
    )

    with Session(engine) as session:
        project = create_project(session, brief)
        project_id = project.id
        ledger = project.ledger | {"estimated_cost_usd": 6.3}
        project.ledger = ledger
        session.commit()

        production_ledger = ProductionLedger.model_validate(project.ledger)
        with pytest.raises(RuntimeError, match="protected repair reserve"):
            reserve_live_spend(
                session,
                project_id,
                settings,
                production_ledger,
                reservation_id="S02-attempt-1-render",
                amount_usd=0.2,
                category="video-render",
                description="Reserve render",
                preserve_repair_reserve=True,
            )

    repo = LocalOssRepository(settings.oss_repository_root)
    ledger_root = repo.root / "projects" / project_id / "ledger" / "entries"
    assert not list(ledger_root.glob("*S02-attempt-1-render.json"))
