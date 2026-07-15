from __future__ import annotations

import hashlib
import json
from datetime import UTC, datetime, timedelta
from email.utils import parsedate_to_datetime
from pathlib import Path
from typing import Any, Literal
from uuid import uuid4

from pydantic import BaseModel, Field

try:
    import oss2
except ImportError:  # local mock mode does not require the OSS SDK
    oss2 = None  # type: ignore[assignment]


class OssRepositoryError(RuntimeError):
    pass


class OssConflictError(OssRepositoryError):
    pass


class OssNotFoundError(OssRepositoryError):
    pass


class ObjectRef(BaseModel):
    key: str
    etag: str
    content_type: str
    size_bytes: int
    updated_at: datetime


class SignedUrl(BaseModel):
    key: str
    url: str
    expires_at: datetime
    method: str = "GET"


class ProjectManifest(BaseModel):
    schema_version: Literal["directorgraph.project-manifest.v1"] = "directorgraph.project-manifest.v1"
    project_id: str
    title: str
    status: str
    created_at: datetime
    updated_at: datetime
    story_ir_key: str | None = None
    final_manifest_key: str | None = None
    object_keys: list[str] = Field(default_factory=list)


class EventEntry(BaseModel):
    schema_version: str = "directorgraph.event.v1"
    project_id: str
    event_id: str = Field(default_factory=lambda: uuid4().hex)
    kind: str
    agent: str
    message: str
    payload: dict[str, Any] = Field(default_factory=dict)
    created_at: datetime = Field(default_factory=lambda: datetime.now(UTC))


class LedgerEntry(BaseModel):
    schema_version: str = "directorgraph.ledger-entry.v1"
    project_id: str
    entry_id: str = Field(default_factory=lambda: uuid4().hex)
    amount_usd: float
    category: str
    description: str
    payload: dict[str, Any] = Field(default_factory=dict)
    created_at: datetime = Field(default_factory=lambda: datetime.now(UTC))


class TaskStatusRecord(BaseModel):
    schema_version: str = "directorgraph.task-status.v1"
    project_id: str
    task_id: str
    job_id: str
    operation: str
    status: str
    attempts: int = 0
    duplicate: bool = False
    dispatch_mode: str | None = None
    function_compute_request_id: str | None = None
    function_compute_status_code: int | None = None
    result: dict[str, Any] | None = None
    error: str | None = None
    updated_at: datetime = Field(default_factory=lambda: datetime.now(UTC))


class JsonObject(BaseModel):
    ref: ObjectRef
    payload: dict[str, Any]


def safe_object_key(*parts: str) -> str:
    if any(Path(part).is_absolute() for part in parts if part):
        raise ValueError("Unsafe OSS object key")
    key = "/".join(part.strip("/") for part in parts if part.strip("/"))
    path = Path(key)
    if not key or path.is_absolute() or ".." in path.parts:
        raise ValueError("Unsafe OSS object key")
    return key


def project_manifest_key(project_id: str) -> str:
    return safe_object_key("projects", project_id, "manifest.json")


def project_read_model_key(project_id: str) -> str:
    return safe_object_key("projects", project_id, "read-model.json")


def project_index_key(created_at: datetime, project_id: str) -> str:
    stamp = created_at.astimezone(UTC).strftime("%Y%m%dT%H%M%S%fZ")
    return safe_object_key("indexes", "projects", f"{stamp}-{project_id}.json")


def original_request_key(project_id: str) -> str:
    return safe_object_key("projects", project_id, "requests", "original.json")


def task_status_key(project_id: str, task_id: str) -> str:
    return safe_object_key("projects", project_id, "tasks", task_id, "status.json")


def task_index_key(task_id: str) -> str:
    return safe_object_key("indexes", "tasks", task_id, "status-ref.json")


def story_ir_key(project_id: str, version: int) -> str:
    return safe_object_key("projects", project_id, "story", f"story-ir.v{version}.json")


def shot_contract_key(project_id: str, shot_id: str, version: int) -> str:
    return safe_object_key("projects", project_id, "shots", shot_id, f"contract.v{version}.json")


def shot_status_key(project_id: str, shot_id: str) -> str:
    return safe_object_key("projects", project_id, "shots", shot_id, "status.json")


def character_asset_materialization_key(project_id: str, character_id: str) -> str:
    return safe_object_key("projects", project_id, "characters", character_id, "asset-materialization.json")


def character_provider_result_key(project_id: str, character_id: str) -> str:
    return safe_object_key("projects", project_id, "characters", character_id, "provider-result.json")


def storyboard_asset_materialization_key(project_id: str, shot_id: str) -> str:
    return safe_object_key("projects", project_id, "shots", shot_id, "storyboard-materialization.json")


def storyboard_provider_result_key(project_id: str, shot_id: str) -> str:
    return safe_object_key("projects", project_id, "shots", shot_id, "storyboard-provider-result.json")


def voice_asset_materialization_key(project_id: str, shot_id: str) -> str:
    return safe_object_key("projects", project_id, "shots", shot_id, "dialogue-materialization.json")


def voice_provider_result_key(project_id: str, shot_id: str) -> str:
    return safe_object_key("projects", project_id, "shots", shot_id, "dialogue-provider-result.json")


def provider_task_key(project_id: str, shot_id: str, attempt: int) -> str:
    return safe_object_key("projects", project_id, "shots", shot_id, "attempts", f"attempt-{attempt}", "provider-task.json")


def provider_result_key(project_id: str, shot_id: str, attempt: int) -> str:
    return safe_object_key("projects", project_id, "shots", shot_id, "attempts", f"attempt-{attempt}", "provider-result.json")


def asset_materialization_key(project_id: str, shot_id: str, attempt: int) -> str:
    return safe_object_key("projects", project_id, "shots", shot_id, "attempts", f"attempt-{attempt}", "asset-materialization.json")


def inspection_key(project_id: str, shot_id: str, attempt: int) -> str:
    return safe_object_key("projects", project_id, "shots", shot_id, "attempts", f"attempt-{attempt}", "inspection.json")


def final_manifest_key(project_id: str) -> str:
    return safe_object_key("projects", project_id, "final", "manifest.json")


def final_asset_materialization_key(project_id: str) -> str:
    return safe_object_key("projects", project_id, "final", "asset-materialization.json")


def event_key(project_id: str, created_at: datetime, event_id: str) -> str:
    stamp = created_at.astimezone(UTC).strftime("%Y%m%dT%H%M%S%fZ")
    return safe_object_key("projects", project_id, "events", f"{stamp}-{event_id}.json")


def ledger_entry_key(project_id: str, created_at: datetime, entry_id: str) -> str:
    stamp = created_at.astimezone(UTC).strftime("%Y%m%dT%H%M%S%fZ")
    return safe_object_key("projects", project_id, "ledger", "entries", f"{stamp}-{entry_id}.json")


def ledger_snapshot_key(project_id: str) -> str:
    return safe_object_key("projects", project_id, "ledger", "current.json")


class LocalOssRepository:
    """Filesystem-backed OSS emulator for contract and recovery tests."""

    def __init__(self, root: Path):
        self.root = root.resolve()
        self.root.mkdir(parents=True, exist_ok=True)

    def _path(self, key: str) -> Path:
        safe = safe_object_key(key)
        destination = (self.root / safe).resolve()
        if self.root not in destination.parents and destination != self.root:
            raise ValueError("Object key escapes repository root")
        return destination

    @staticmethod
    def _etag(data: bytes) -> str:
        return hashlib.sha256(data).hexdigest()

    def _metadata_path(self, key: str) -> Path:
        return self._path(f"{key}.meta.json")

    def _metadata(self, key: str) -> ObjectRef:
        path = self._metadata_path(key)
        if not path.exists():
            raise OssNotFoundError(key)
        return ObjectRef.model_validate(json.loads(path.read_text(encoding="utf-8")))

    def put_bytes(
        self,
        key: str,
        data: bytes,
        *,
        content_type: str,
        if_none_match: bool = False,
        if_match: str | None = None,
        max_size_bytes: int = 100 * 1024 * 1024,
    ) -> ObjectRef:
        if len(data) > max_size_bytes:
            raise ValueError("Object exceeds maximum size")
        path = self._path(key)
        metadata_path = self._metadata_path(key)
        exists = path.exists()
        if if_none_match and exists:
            raise OssConflictError(f"Object already exists: {key}")
        if if_match is not None:
            current = self._metadata(key)
            if current.etag != if_match:
                raise OssConflictError(f"ETag mismatch for {key}")
        path.parent.mkdir(parents=True, exist_ok=True)
        metadata_path.parent.mkdir(parents=True, exist_ok=True)
        path.write_bytes(data)
        ref = ObjectRef(
            key=safe_object_key(key),
            etag=self._etag(data),
            content_type=content_type,
            size_bytes=len(data),
            updated_at=datetime.now(UTC),
        )
        metadata_path.write_text(ref.model_dump_json(indent=2), encoding="utf-8")
        return ref

    def get_bytes(self, key: str) -> tuple[bytes, ObjectRef]:
        path = self._path(key)
        if not path.exists():
            raise OssNotFoundError(key)
        return path.read_bytes(), self._metadata(key)

    def put_json(
        self,
        key: str,
        payload: BaseModel | dict[str, Any],
        *,
        if_none_match: bool = False,
        if_match: str | None = None,
    ) -> ObjectRef:
        value = payload.model_dump(mode="json") if isinstance(payload, BaseModel) else payload
        data = json.dumps(value, indent=2, sort_keys=True).encode()
        return self.put_bytes(
            key,
            data,
            content_type="application/json",
            if_none_match=if_none_match,
            if_match=if_match,
        )

    def get_json(self, key: str) -> JsonObject:
        data, ref = self.get_bytes(key)
        payload = json.loads(data.decode())
        return JsonObject(ref=ref, payload=payload)

    def list_keys(self, prefix: str) -> list[str]:
        safe_prefix = safe_object_key(prefix)
        if not (self.root / safe_prefix).exists():
            return []
        keys = []
        for path in (self.root / safe_prefix).rglob("*"):
            if not path.is_file() or path.name.endswith(".meta.json"):
                continue
            keys.append(path.relative_to(self.root).as_posix())
        return sorted(keys)

    def append_event(self, event: EventEntry) -> ObjectRef:
        return self.put_json(
            event_key(event.project_id, event.created_at, event.event_id),
            event,
            if_none_match=True,
        )

    def append_ledger_entry(self, entry: LedgerEntry) -> ObjectRef:
        return self.put_json(
            ledger_entry_key(entry.project_id, entry.created_at, entry.entry_id),
            entry,
            if_none_match=True,
        )

    def put_project_manifest(self, manifest: ProjectManifest, *, if_match: str | None = None) -> ObjectRef:
        return self.put_json(project_manifest_key(manifest.project_id), manifest, if_match=if_match)

    def get_project_manifest(self, project_id: str) -> JsonObject:
        return self.get_json(project_manifest_key(project_id))

    def presign_get(self, key: str, *, expires_seconds: int = 900) -> SignedUrl:
        if expires_seconds <= 0 or expires_seconds > 604800:
            raise ValueError("Signed URL expiry must be between 1 second and 7 days")
        self._metadata(key)
        expires_at = datetime.now(UTC) + timedelta(seconds=expires_seconds)
        token = hashlib.sha256(f"{key}:{expires_at.isoformat()}".encode()).hexdigest()[:32]
        return SignedUrl(
            key=safe_object_key(key),
            url=f"local-oss://{safe_object_key(key)}?expires={int(expires_at.timestamp())}&signature={token}",
            expires_at=expires_at,
        )


class AlibabaOssRepository:
    """Alibaba OSS implementation of the DirectorGraph durable-state contract."""

    def __init__(self, bucket: Any):
        self.bucket = bucket

    @classmethod
    def from_settings(cls, settings: Any) -> AlibabaOssRepository:
        if oss2 is None:
            raise RuntimeError("Install the oss2 package to use Alibaba Cloud OSS")
        auth = oss2.Auth(settings.oss_access_key_id, settings.oss_access_key_secret)
        return cls(oss2.Bucket(auth, settings.oss_endpoint, settings.oss_bucket))

    @staticmethod
    def _etag(data: bytes) -> str:
        return hashlib.sha256(data).hexdigest()

    @staticmethod
    def _metadata_key(key: str) -> str:
        return safe_object_key(f"{key}.meta.json")

    @staticmethod
    def _updated_at(headers: Any) -> datetime:
        value = headers.get("Last-Modified") or headers.get("last-modified") if headers else None
        if not value:
            return datetime.now(UTC)
        parsed = parsedate_to_datetime(str(value))
        if parsed.tzinfo is None:
            return parsed.replace(tzinfo=UTC)
        return parsed.astimezone(UTC)

    def _metadata_from_head(self, key: str) -> ObjectRef:
        try:
            result = self.bucket.head_object(safe_object_key(key))
        except (oss2.exceptions.NoSuchKey, oss2.exceptions.NotFound) as exc:  # type: ignore[union-attr]
            raise OssNotFoundError(key) from exc
        headers = result.headers
        etag = str(headers.get("ETag") or headers.get("etag") or "").strip('"')
        return ObjectRef(
            key=safe_object_key(key),
            etag=etag,
            content_type=str(headers.get("Content-Type") or headers.get("content-type") or "application/octet-stream"),
            size_bytes=int(headers.get("Content-Length") or headers.get("content-length") or 0),
            updated_at=self._updated_at(headers),
        )

    def _metadata(self, key: str) -> ObjectRef:
        metadata_key = self._metadata_key(key)
        try:
            data = self.bucket.get_object(metadata_key).read()
        except (oss2.exceptions.NoSuchKey, oss2.exceptions.NotFound):  # type: ignore[union-attr]
            return self._metadata_from_head(key)
        return ObjectRef.model_validate(json.loads(data.decode()))

    def put_bytes(
        self,
        key: str,
        data: bytes,
        *,
        content_type: str,
        if_none_match: bool = False,
        if_match: str | None = None,
        max_size_bytes: int = 100 * 1024 * 1024,
    ) -> ObjectRef:
        key = safe_object_key(key)
        if len(data) > max_size_bytes:
            raise ValueError("Object exceeds maximum size")
        if if_none_match and self.bucket.object_exists(key):
            raise OssConflictError(f"Object already exists: {key}")
        if if_match is not None:
            current = self._metadata(key)
            if current.etag != if_match:
                raise OssConflictError(f"ETag mismatch for {key}")
        self.bucket.put_object(key, data, headers={"Content-Type": content_type})
        ref = ObjectRef(
            key=key,
            etag=self._etag(data),
            content_type=content_type,
            size_bytes=len(data),
            updated_at=datetime.now(UTC),
        )
        self.bucket.put_object(
            self._metadata_key(key),
            ref.model_dump_json(indent=2).encode(),
            headers={"Content-Type": "application/json"},
        )
        return ref

    def get_bytes(self, key: str) -> tuple[bytes, ObjectRef]:
        key = safe_object_key(key)
        try:
            data = self.bucket.get_object(key).read()
        except (oss2.exceptions.NoSuchKey, oss2.exceptions.NotFound) as exc:  # type: ignore[union-attr]
            raise OssNotFoundError(key) from exc
        return data, self._metadata(key)

    def put_json(
        self,
        key: str,
        payload: BaseModel | dict[str, Any],
        *,
        if_none_match: bool = False,
        if_match: str | None = None,
    ) -> ObjectRef:
        value = payload.model_dump(mode="json") if isinstance(payload, BaseModel) else payload
        data = json.dumps(value, indent=2, sort_keys=True).encode()
        return self.put_bytes(
            key,
            data,
            content_type="application/json",
            if_none_match=if_none_match,
            if_match=if_match,
        )

    def get_json(self, key: str) -> JsonObject:
        data, ref = self.get_bytes(key)
        return JsonObject(ref=ref, payload=json.loads(data.decode()))

    def list_keys(self, prefix: str) -> list[str]:
        prefix = safe_object_key(prefix)
        return sorted(
            item.key
            for item in oss2.ObjectIterator(self.bucket, prefix=prefix)  # type: ignore[union-attr]
            if not item.key.endswith(".meta.json")
        )

    def append_event(self, event: EventEntry) -> ObjectRef:
        return self.put_json(
            event_key(event.project_id, event.created_at, event.event_id),
            event,
            if_none_match=True,
        )

    def append_ledger_entry(self, entry: LedgerEntry) -> ObjectRef:
        return self.put_json(
            ledger_entry_key(entry.project_id, entry.created_at, entry.entry_id),
            entry,
            if_none_match=True,
        )

    def put_project_manifest(self, manifest: ProjectManifest, *, if_match: str | None = None) -> ObjectRef:
        return self.put_json(project_manifest_key(manifest.project_id), manifest, if_match=if_match)

    def get_project_manifest(self, project_id: str) -> JsonObject:
        return self.get_json(project_manifest_key(project_id))

    def presign_get(self, key: str, *, expires_seconds: int = 900) -> SignedUrl:
        if expires_seconds <= 0 or expires_seconds > 604800:
            raise ValueError("Signed URL expiry must be between 1 second and 7 days")
        key = safe_object_key(key)
        self._metadata(key)
        expires_at = datetime.now(UTC) + timedelta(seconds=expires_seconds)
        return SignedUrl(
            key=key,
            url=self.bucket.sign_url("GET", key, expires_seconds),
            expires_at=expires_at,
        )


def create_oss_repository(settings: Any) -> LocalOssRepository | AlibabaOssRepository:
    if settings.oss_ready:
        return AlibabaOssRepository.from_settings(settings)
    return LocalOssRepository(settings.oss_repository_root)
