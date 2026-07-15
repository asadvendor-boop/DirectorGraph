from datetime import UTC, datetime

import pytest

from app.oss_repository import (
    AlibabaOssRepository,
    EventEntry,
    LedgerEntry,
    LocalOssRepository,
    OssConflictError,
    ProjectManifest,
    asset_materialization_key,
    character_asset_materialization_key,
    character_provider_result_key,
    event_key,
    final_asset_materialization_key,
    final_manifest_key,
    inspection_key,
    ledger_snapshot_key,
    project_index_key,
    project_manifest_key,
    project_read_model_key,
    provider_result_key,
    provider_task_key,
    safe_object_key,
    shot_contract_key,
    shot_status_key,
    story_ir_key,
    storyboard_asset_materialization_key,
    storyboard_provider_result_key,
    task_index_key,
    task_status_key,
    voice_asset_materialization_key,
    voice_provider_result_key,
)


class FakeObject:
    def __init__(self, data: bytes):
        self.data = data

    def read(self) -> bytes:
        return self.data


class FakeBucket:
    def __init__(self):
        self.objects: dict[str, bytes] = {}

    def put_object(self, key: str, data: bytes, headers=None):
        self.objects[key] = data

    def get_object(self, key: str) -> FakeObject:
        return FakeObject(self.objects[key])

    def object_exists(self, key: str, headers=None) -> bool:
        return key in self.objects

    def sign_url(self, method: str, key: str, expires: int) -> str:
        return f"https://signed.example.invalid/{key}?method={method}&expires={expires}&signature=test"


def test_safe_object_key_rejects_escape():
    assert safe_object_key("projects", "abc", "manifest.json") == "projects/abc/manifest.json"
    assert project_read_model_key("abc") == "projects/abc/read-model.json"
    assert project_index_key(datetime(2026, 1, 2, tzinfo=UTC), "abc") == "indexes/projects/20260102T000000000000Z-abc.json"
    assert story_ir_key("abc", 2) == "projects/abc/story/story-ir.v2.json"
    assert shot_contract_key("abc", "S01", 2) == "projects/abc/shots/S01/contract.v2.json"
    assert shot_status_key("abc", "S01") == "projects/abc/shots/S01/status.json"
    assert character_asset_materialization_key("abc", "C01") == "projects/abc/characters/C01/asset-materialization.json"
    assert character_provider_result_key("abc", "C01") == "projects/abc/characters/C01/provider-result.json"
    assert storyboard_asset_materialization_key("abc", "S01") == "projects/abc/shots/S01/storyboard-materialization.json"
    assert storyboard_provider_result_key("abc", "S01") == "projects/abc/shots/S01/storyboard-provider-result.json"
    assert voice_asset_materialization_key("abc", "S01") == "projects/abc/shots/S01/dialogue-materialization.json"
    assert voice_provider_result_key("abc", "S01") == "projects/abc/shots/S01/dialogue-provider-result.json"
    assert provider_task_key("abc", "S01", 2) == "projects/abc/shots/S01/attempts/attempt-2/provider-task.json"
    assert task_status_key("abc", "dg-task-1") == "projects/abc/tasks/dg-task-1/status.json"
    assert task_index_key("dg-task-1") == "indexes/tasks/dg-task-1/status-ref.json"
    assert provider_result_key("abc", "S01", 2) == "projects/abc/shots/S01/attempts/attempt-2/provider-result.json"
    assert asset_materialization_key("abc", "S01", 2) == "projects/abc/shots/S01/attempts/attempt-2/asset-materialization.json"
    assert final_asset_materialization_key("abc") == "projects/abc/final/asset-materialization.json"
    assert inspection_key("abc", "S01", 2) == "projects/abc/shots/S01/attempts/attempt-2/inspection.json"
    assert final_manifest_key("abc") == "projects/abc/final/manifest.json"
    assert ledger_snapshot_key("abc") == "projects/abc/ledger/current.json"
    with pytest.raises(ValueError):
        safe_object_key("../secret")
    with pytest.raises(ValueError):
        safe_object_key("/absolute")


def test_project_manifest_etag_and_optimistic_concurrency(tmp_path):
    repo = LocalOssRepository(tmp_path)
    manifest = ProjectManifest(
        project_id="project-1",
        title="Manifest test",
        status="draft",
        created_at=datetime.now(UTC),
        updated_at=datetime.now(UTC),
    )
    first = repo.put_project_manifest(manifest)
    loaded = repo.get_project_manifest("project-1")

    assert loaded.ref.etag == first.etag
    assert loaded.payload["project_id"] == "project-1"

    manifest.status = "queued"
    updated = repo.put_project_manifest(manifest, if_match=first.etag)
    assert updated.etag != first.etag
    with pytest.raises(OssConflictError):
        repo.put_project_manifest(manifest, if_match=first.etag)


def test_put_if_absent_and_signed_url(tmp_path):
    repo = LocalOssRepository(tmp_path)
    key = project_manifest_key("project-2")
    ref = repo.put_json(key, {"project_id": "project-2"}, if_none_match=True)
    signed = repo.presign_get(key, expires_seconds=60)

    assert signed.url.startswith("local-oss://projects/project-2/manifest.json")
    assert signed.expires_at > datetime.now(UTC)
    assert ref.content_type == "application/json"
    with pytest.raises(OssConflictError):
        repo.put_json(key, {"project_id": "project-2"}, if_none_match=True)


def test_append_only_event_and_ledger_keys(tmp_path):
    repo = LocalOssRepository(tmp_path)
    created = datetime(2026, 6, 24, 8, 0, tzinfo=UTC)
    event = EventEntry(
        project_id="project-3",
        event_id="event-1",
        kind="project.created",
        agent="Executive Showrunner",
        message="Created",
        created_at=created,
    )
    ledger = LedgerEntry(
        project_id="project-3",
        entry_id="ledger-1",
        amount_usd=0.08,
        category="story",
        description="Story planning estimate",
        created_at=created,
    )

    event_ref = repo.append_event(event)
    ledger_ref = repo.append_ledger_entry(ledger)

    assert event_ref.key == event_key("project-3", created, "event-1")
    assert event_ref.key.endswith("event-1.json")
    assert ledger_ref.key.endswith("ledger-1.json")
    with pytest.raises(OssConflictError):
        repo.append_event(event)


def test_alibaba_repository_contract_with_fake_bucket():
    repo = AlibabaOssRepository(FakeBucket())
    key = project_manifest_key("project-4")

    ref = repo.put_json(key, {"project_id": "project-4"}, if_none_match=True)
    loaded = repo.get_json(key)
    signed = repo.presign_get(key, expires_seconds=60)

    assert loaded.ref.etag == ref.etag
    assert loaded.payload["project_id"] == "project-4"
    assert signed.url.startswith("https://signed.example.invalid/projects/project-4/manifest.json")
    with pytest.raises(OssConflictError):
        repo.put_json(key, {"project_id": "project-4"}, if_none_match=True)
