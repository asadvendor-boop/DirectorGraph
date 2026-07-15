from __future__ import annotations

import mimetypes
from dataclasses import dataclass
from datetime import UTC, datetime
from typing import Any
from urllib.parse import urlparse

from sqlalchemy.orm import Session

from app.config import Settings
from app.models import Job, Project, Shot
from app.oss_repository import (
    EventEntry,
    LedgerEntry,
    LocalOssRepository,
    OssConflictError,
    OssNotFoundError,
    ProjectManifest,
    TaskStatusRecord,
    asset_materialization_key,
    character_asset_materialization_key,
    create_oss_repository,
    event_key,
    final_asset_materialization_key,
    final_manifest_key,
    inspection_key,
    ledger_entry_key,
    ledger_snapshot_key,
    original_request_key,
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
    task_index_key,
    task_status_key,
    voice_asset_materialization_key,
)
from app.providers.base import AssetResult
from app.schemas import (
    ProductionLedger,
    ProjectRead,
    QualityReport,
    ShotContract,
    ShotStatus,
    StoryPlan,
)


@dataclass(frozen=True, slots=True)
class TaskCheckpoint:
    project_id: str
    task_id: str
    manifest_key: str
    object_keys: list[str]


@dataclass(frozen=True, slots=True)
class SpendReservation:
    project_id: str
    reservation_id: str
    ledger_key: str
    amount_usd: float
    duplicate: bool


def _as_utc(value: datetime | None) -> datetime:
    if value is None:
        return datetime.now(UTC)
    if value.tzinfo is None:
        return value.replace(tzinfo=UTC)
    return value.astimezone(UTC)


def _put_json_once(repo, key: str, payload: dict[str, Any]) -> str:
    try:
        return repo.put_json(key, payload, if_none_match=True).key
    except OssConflictError:
        return repo.get_json(key).ref.key


def _append_event_once(repo, event: EventEntry) -> str:
    try:
        return repo.append_event(event).key
    except OssConflictError:
        return repo.get_json(event_key(event.project_id, event.created_at, event.event_id)).ref.key


def _append_ledger_once(repo, entry: LedgerEntry) -> str:
    try:
        return repo.append_ledger_entry(entry).key
    except OssConflictError:
        return repo.get_json(ledger_entry_key(entry.project_id, entry.created_at, entry.entry_id)).ref.key


def _copy_ledger_values(target: ProductionLedger, source: ProductionLedger) -> None:
    for field in ProductionLedger.model_fields:
        setattr(target, field, getattr(source, field))


def _upsert_manifest(
    repo,
    project: Project,
    object_keys: list[str],
    *,
    story_ir_object_key: str | None = None,
    final_manifest_object_key: str | None = None,
) -> str:
    manifest = ProjectManifest(
        project_id=project.id,
        title=project.title,
        status=project.status,
        created_at=_as_utc(project.created_at),
        updated_at=_as_utc(project.updated_at),
        story_ir_key=story_ir_object_key,
        final_manifest_key=final_manifest_object_key,
        object_keys=sorted(set(object_keys)),
    )

    def merge_existing_payload(existing_payload: dict[str, Any] | ProjectManifest) -> None:
        if isinstance(existing_payload, ProjectManifest):
            payload = existing_payload.model_dump(mode="json")
            created_at = existing_payload.created_at
        else:
            payload = existing_payload
            created_at = _as_utc(datetime.fromisoformat(str(payload["created_at"])))
        manifest.created_at = created_at
        manifest.story_ir_key = story_ir_object_key or payload.get("story_ir_key")
        manifest.final_manifest_key = final_manifest_object_key or payload.get("final_manifest_key")
        manifest.object_keys = safe_existing_object_keys(payload.get("object_keys", []), object_keys)

    def safe_existing_object_keys(*groups: Any) -> list[str]:
        keys: set[str] = set()
        for group in groups:
            if not isinstance(group, list):
                continue
            for item in group:
                if not isinstance(item, str):
                    continue
                try:
                    keys.add(safe_object_key(item))
                except ValueError:
                    continue
        return sorted(keys)

    def salvage_existing_payload(payload: dict[str, Any]) -> None:
        if isinstance(payload.get("story_ir_key"), str) and story_ir_object_key is None:
            manifest.story_ir_key = payload["story_ir_key"]
        if isinstance(payload.get("final_manifest_key"), str) and final_manifest_object_key is None:
            manifest.final_manifest_key = payload["final_manifest_key"]
        manifest.object_keys = safe_existing_object_keys(payload.get("object_keys", []), object_keys)

    for _ in range(2):
        try:
            existing = repo.get_json(project_manifest_key(project.id))
        except OssNotFoundError:
            try:
                return repo.put_json(project_manifest_key(project.id), manifest, if_none_match=True).key
            except OssConflictError:
                continue

        try:
            merge_existing_payload(ProjectManifest.model_validate(existing.payload))
        except (KeyError, TypeError, ValueError):
            salvage_existing_payload(existing.payload)
        return repo.put_project_manifest(manifest, if_match=existing.ref.etag).key

    existing = repo.get_json(project_manifest_key(project.id))
    try:
        merge_existing_payload(ProjectManifest.model_validate(existing.payload))
    except (KeyError, TypeError, ValueError):
        salvage_existing_payload(existing.payload)
    return repo.put_project_manifest(manifest, if_match=existing.ref.etag).key


def _without_transient_urls(value: Any) -> Any:
    if isinstance(value, dict):
        return {
            key: _without_transient_urls(item)
            for key, item in value.items()
            if not key.endswith("_url") and key not in {"url", "signed_url"}
        }
    if isinstance(value, list):
        return [_without_transient_urls(item) for item in value]
    return value


def _object_keys_from_manifest(
    project_id: str,
    storage_manifest: dict[str, Any] | None,
    *,
    include_final_manifest: bool,
) -> list[str]:
    keys = set(storage_manifest.get("object_keys", []) if storage_manifest else [])
    if include_final_manifest:
        keys.add(final_manifest_key(project_id))
    return sorted(str(key) for key in keys)


def _shot_checkpoint_keys(project_id: str, shot_code: str, attempts: int, object_keys: set[str]) -> dict[str, str]:
    candidates = {
        "status": shot_status_key(project_id, shot_code),
        "storyboard_materialization": storyboard_asset_materialization_key(project_id, shot_code),
        "dialogue_materialization": voice_asset_materialization_key(project_id, shot_code),
    }
    if attempts > 0:
        candidates["video_materialization"] = asset_materialization_key(project_id, shot_code, attempts)
    return {name: key for name, key in candidates.items() if key in object_keys}


def build_production_manifest_payload(
    project: ProjectRead,
    storage_manifest: dict[str, Any] | None = None,
    *,
    include_final_manifest: bool = False,
) -> dict[str, Any]:
    object_keys = _object_keys_from_manifest(
        project.id,
        storage_manifest,
        include_final_manifest=include_final_manifest,
    )
    object_key_set = set(object_keys)
    stored_final_manifest_key = storage_manifest.get("final_manifest_key") if storage_manifest else None
    storage = {
        "schema": "directorgraph.production-storage-audit-trail.v1",
        "available": storage_manifest is not None,
        "manifest_key": project_manifest_key(project.id),
        "read_model_key": project_read_model_key(project.id),
        "story_ir_key": storage_manifest.get("story_ir_key") if storage_manifest else None,
        "final_manifest_key": stored_final_manifest_key
        or (final_manifest_key(project.id) if include_final_manifest else None),
        "object_keys": object_keys,
    }
    return {
        "schema": "directorgraph.production-manifest.v1",
        "generated_at": datetime.now(UTC).isoformat(),
        "project": _without_transient_urls(project.model_dump(mode="json")),
        "storage": storage,
        "audit_trail": [
            {
                "shot": shot.shot_code,
                "renderer": shot.contract.renderer,
                "resolution": shot.contract.resolution,
                "attempts": shot.attempts,
                "quality": shot.quality.overall_score if shot.quality else None,
                "checkpoint_keys": _shot_checkpoint_keys(project.id, shot.shot_code, shot.attempts, object_key_set),
            }
            for shot in project.shots
        ],
    }


def _object_key_from_media_url(url: str | None) -> str | None:
    if not url:
        return None
    parsed = urlparse(url)
    if parsed.scheme == "local-oss":
        candidate = f"{parsed.netloc}{parsed.path}"
    else:
        candidate = parsed.path
    candidate = candidate.lstrip("/")
    marker = "projects/"
    if marker in candidate:
        candidate = candidate[candidate.index(marker):]
    try:
        return safe_object_key(candidate)
    except ValueError:
        return None


def _object_key_from_checkpoint(repo, checkpoint_key: str | None) -> str | None:
    if not checkpoint_key:
        return None
    try:
        payload = repo.get_json(safe_object_key(checkpoint_key)).payload
    except (OssNotFoundError, ValueError):
        return None
    object_key = payload.get("object_key")
    if not object_key:
        return None
    try:
        return safe_object_key(str(object_key))
    except ValueError:
        return None


def _media_ref(
    repo,
    *,
    checkpoint_key: str | None,
    fallback_url: str | None,
) -> dict[str, str] | None:
    object_key = _object_key_from_checkpoint(repo, checkpoint_key) or _object_key_from_media_url(fallback_url)
    if not object_key:
        return None
    ref: dict[str, str] = {"object_key": object_key}
    if checkpoint_key:
        ref["checkpoint_key"] = checkpoint_key
    return ref


def _project_read_media_refs(repo, project: Project) -> dict[str, Any]:
    refs: dict[str, Any] = {
        "schema": "directorgraph.project-read-media-refs.v1",
        "final": _media_ref(
            repo,
            checkpoint_key=final_asset_materialization_key(project.id),
            fallback_url=project.final_video_url,
        ),
        "characters": {},
        "shots": {},
    }
    if project.plan:
        try:
            plan = StoryPlan.model_validate(project.plan)
        except ValueError:
            plan = None
        if plan is not None:
            for character in plan.characters:
                ref = _media_ref(
                    repo,
                    checkpoint_key=character_asset_materialization_key(project.id, character.id),
                    fallback_url=character.reference_url,
                )
                if ref is not None:
                    refs["characters"][character.id] = ref
    for shot in sorted(project.shots, key=lambda item: item.sequence):
        attempt = shot.attempts or 0
        shot_refs = {
            "storyboard": _media_ref(
                repo,
                checkpoint_key=storyboard_asset_materialization_key(project.id, shot.shot_code),
                fallback_url=shot.storyboard_url,
            ),
            "audio": _media_ref(
                repo,
                checkpoint_key=voice_asset_materialization_key(project.id, shot.shot_code),
                fallback_url=shot.audio_url,
            ),
            "video": _media_ref(
                repo,
                checkpoint_key=asset_materialization_key(project.id, shot.shot_code, attempt) if attempt else None,
                fallback_url=shot.video_url,
            ),
        }
        refs["shots"][shot.shot_code] = {
            key: value for key, value in shot_refs.items() if value is not None
        }
    return refs


def _url_for_media_ref(settings: Settings, ref: Any) -> str | None:
    if not isinstance(ref, dict) or not ref.get("object_key"):
        return None
    from app.clients.storage import AssetStore

    key = safe_object_key(str(ref["object_key"]))
    try:
        return AssetStore(settings).reference_for_key(key).public_url
    except (RuntimeError, ValueError):
        pass

    repo = create_oss_repository(settings)
    if settings.oss_ready and not isinstance(repo, LocalOssRepository):
        try:
            return repo.presign_get(key, expires_seconds=900).url
        except OssNotFoundError:
            return None
    return None


def _apply_media_refs(read: ProjectRead, settings: Settings, media_refs: Any) -> None:
    if not isinstance(media_refs, dict):
        return
    read.final_video_url = _url_for_media_ref(settings, media_refs.get("final"))
    character_refs = media_refs.get("characters")
    if read.plan is not None and isinstance(character_refs, dict):
        for character in read.plan.characters:
            ref = character_refs.get(character.id)
            if isinstance(ref, dict):
                character.reference_url = _url_for_media_ref(settings, ref)
    shot_refs = media_refs.get("shots")
    if not isinstance(shot_refs, dict):
        return
    for shot in read.shots:
        refs = shot_refs.get(shot.shot_code)
        if not isinstance(refs, dict):
            continue
        shot.storyboard_url = _url_for_media_ref(settings, refs.get("storyboard"))
        shot.audio_url = _url_for_media_ref(settings, refs.get("audio"))
        shot.video_url = _url_for_media_ref(settings, refs.get("video"))


def _durable_media_refs_from_payload(value: Any) -> dict[str, Any]:
    if not isinstance(value, dict):
        return {}
    refs: dict[str, Any] = {}
    final_object_key = value.get("final_object_key") or _object_key_from_media_url(value.get("final_video_url"))
    if final_object_key:
        try:
            refs["final"] = {"object_key": safe_object_key(str(final_object_key))}
        except ValueError:
            pass
    return refs


def _durable_payload(value: Any) -> Any:
    payload = _without_transient_urls(value)
    media_refs = _durable_media_refs_from_payload(value)
    if isinstance(payload, dict) and media_refs:
        payload["media_refs"] = media_refs
    return payload


def checkpoint_story_plan(
    session: Session,
    project_id: str,
    settings: Settings,
    plan: StoryPlan,
    *,
    version: int = 1,
) -> str:
    project = session.get(Project, project_id)
    if project is None:
        raise KeyError(project_id)

    repo = create_oss_repository(settings)
    key = story_ir_key(project_id, version)
    ref = repo.put_json(
        key,
        {
            "schema_version": "directorgraph.story-ir-checkpoint.v1",
            "project_id": project_id,
            "version": version,
            "created_at": datetime.now(UTC).isoformat(),
            "plan": plan.model_dump(mode="json"),
        },
    )
    contract_keys = []
    for contract in plan.shots:
        contract_ref = repo.put_json(
            shot_contract_key(project_id, contract.id, version),
            {
                "schema_version": "directorgraph.shot-contract-checkpoint.v1",
                "project_id": project_id,
                "shot_id": contract.id,
                "version": version,
                "created_at": datetime.now(UTC).isoformat(),
                "contract": contract.model_dump(mode="json"),
            },
        )
        contract_keys.append(contract_ref.key)
    _upsert_manifest(repo, project, [ref.key, *contract_keys], story_ir_object_key=ref.key)
    checkpoint_project_read_model(session, project_id, settings)
    return ref.key


def checkpoint_project_read_model(
    session: Session,
    project_id: str,
    settings: Settings,
) -> str:
    from app.repository import project_to_read

    project = session.get(Project, project_id)
    if project is None:
        raise KeyError(project_id)

    read = project_to_read(project)
    repo = create_oss_repository(settings)
    read_ref = repo.put_json(
        project_read_model_key(project_id),
        {
            "schema_version": "directorgraph.project-read-model.v1",
            "project": _without_transient_urls(read.model_dump(mode="json")),
            "media_refs": _project_read_media_refs(repo, project),
            "updated_at": datetime.now(UTC).isoformat(),
        },
    )
    repo.put_json(
        project_index_key(_as_utc(project.created_at), project_id),
        {
            "schema_version": "directorgraph.project-index.v1",
            "project_id": project_id,
            "read_model_key": read_ref.key,
            "title": project.title,
            "status": project.status,
            "created_at": _as_utc(project.created_at).isoformat(),
            "updated_at": _as_utc(project.updated_at).isoformat(),
        },
    )
    _upsert_manifest(repo, project, [read_ref.key])
    return read_ref.key


def checkpoint_event(
    session: Session,
    project_id: str,
    settings: Settings,
    event,
) -> str:
    project = session.get(Project, project_id)
    if project is None:
        raise KeyError(project_id)
    if event.id is None:
        session.flush()

    repo = create_oss_repository(settings)
    entry = EventEntry(
        project_id=project_id,
        event_id=f"sql-{event.id}",
        kind=event.kind,
        agent=event.agent,
        message=event.message,
        payload=_durable_payload(event.payload or {}),
        created_at=_as_utc(event.created_at),
    )
    ref_key = _append_event_once(repo, entry)
    _upsert_manifest(repo, project, [ref_key])
    return ref_key


def checkpoint_ledger_snapshot(
    session: Session,
    project_id: str,
    settings: Settings,
    ledger: ProductionLedger,
) -> str:
    project = session.get(Project, project_id)
    if project is None:
        raise KeyError(project_id)

    repo = create_oss_repository(settings)
    key = ledger_snapshot_key(project_id)
    ref = repo.put_json(
        key,
        {
            "schema_version": "directorgraph.ledger-snapshot.v1",
            "project_id": project_id,
            "updated_at": datetime.now(UTC).isoformat(),
            "ledger": ledger.model_dump(mode="json"),
        },
    )
    _upsert_manifest(repo, project, [ref.key])
    checkpoint_project_read_model(session, project_id, settings)
    return ref.key


def checkpoint_shot_status(
    session: Session,
    project_id: str,
    settings: Settings,
    shot: Shot,
    *,
    update_read_model: bool = True,
) -> str:
    project = session.get(Project, project_id)
    if project is None:
        raise KeyError(project_id)
    if shot.id is None:
        session.flush()

    attempt = shot.attempts or 0
    materialization_keys = {
        "storyboard": storyboard_asset_materialization_key(project_id, shot.shot_code)
        if shot.storyboard_url
        else None,
        "voice": voice_asset_materialization_key(project_id, shot.shot_code)
        if shot.audio_url
        else None,
        "video": asset_materialization_key(project_id, shot.shot_code, attempt)
        if attempt and shot.video_url
        else None,
        "inspection": inspection_key(project_id, shot.shot_code, attempt)
        if attempt and shot.quality
        else None,
    }
    repo = create_oss_repository(settings)
    ref = repo.put_json(
        shot_status_key(project_id, shot.shot_code),
        {
            "schema_version": "directorgraph.shot-status.v1",
            "project_id": project_id,
            "shot_id": shot.shot_code,
            "row_id": shot.id,
            "sequence": shot.sequence,
            "status": shot.status,
            "attempts": attempt,
            "accepted": shot.accepted,
            "contract_key": shot_contract_key(project_id, shot.shot_code, 1),
            "materialization_keys": materialization_keys,
            "quality": (
                QualityReport.model_validate(shot.quality).model_dump(mode="json")
                if shot.quality
                else None
            ),
            "updated_at": datetime.now(UTC).isoformat(),
        },
    )
    _upsert_manifest(repo, project, [ref.key])
    if update_read_model:
        checkpoint_project_read_model(session, project_id, settings)
    return ref.key


def checkpoint_final_manifest(
    session: Session,
    project_id: str,
    settings: Settings,
) -> str:
    from app.repository import project_to_read

    project = session.get(Project, project_id)
    if project is None:
        raise KeyError(project_id)

    read = project_to_read(project)
    repo = create_oss_repository(settings)
    try:
        storage_manifest = repo.get_project_manifest(project_id).payload
    except OssNotFoundError:
        storage_manifest = None
    key = final_manifest_key(project_id)
    ref = repo.put_json(
        key,
        build_production_manifest_payload(read, storage_manifest, include_final_manifest=True),
    )
    _upsert_manifest(repo, project, [ref.key], final_manifest_object_key=ref.key)
    checkpoint_project_read_model(session, project_id, settings)
    return ref.key


def reserve_live_spend(
    session: Session,
    project_id: str,
    settings: Settings,
    ledger: ProductionLedger,
    *,
    reservation_id: str,
    amount_usd: float,
    category: str,
    description: str,
    payload: dict[str, Any] | None = None,
    preserve_repair_reserve: bool = False,
) -> SpendReservation | None:
    if settings.provider_mode != "live":
        return None
    amount_usd = round(amount_usd, 4)
    project = session.get(Project, project_id)
    if project is None:
        raise KeyError(project_id)

    repo = create_oss_repository(settings)
    created_at = _as_utc(project.created_at)
    key = ledger_entry_key(project_id, created_at, reservation_id)
    try:
        repo.get_json(key)
        _copy_ledger_values(ledger, ProductionLedger.model_validate(project.ledger))
        return SpendReservation(project_id, reservation_id, key, amount_usd, duplicate=True)
    except OssNotFoundError:
        pass

    authoritative = ProductionLedger.model_validate(project.ledger)
    projected = round(authoritative.estimated_cost_usd + amount_usd, 4)
    project_cap = min(authoritative.budget_usd, settings.max_project_spend_usd)
    if projected > project_cap:
        raise RuntimeError(
            f"Live spend reservation refused: projected ${projected:.4f} "
            f"exceeds project cap ${project_cap:.4f}"
        )
    if projected > settings.max_total_live_spend_usd:
        raise RuntimeError(
            f"Live spend reservation refused: projected ${projected:.4f} "
            f"exceeds total cap ${settings.max_total_live_spend_usd:.4f}"
        )
    if preserve_repair_reserve:
        reserve = min(
            authoritative.repair_reserve_usd,
            round(project_cap * settings.repair_reserve_percent / 100, 4),
        )
        general_cap = max(round(project_cap - reserve, 4), 0)
        if projected > general_cap:
            raise RuntimeError(
                f"Live spend reservation refused: projected ${projected:.4f} "
                f"would consume protected repair reserve ${reserve:.4f}"
            )

    entry = LedgerEntry(
        project_id=project_id,
        entry_id=reservation_id,
        amount_usd=amount_usd,
        category=category,
        description=description,
        payload={
            "reservation_id": reservation_id,
            "estimated": True,
            **(payload or {}),
        },
        created_at=created_at,
    )
    ledger_ref_key = _append_ledger_once(repo, entry)
    authoritative.estimated_cost_usd = projected
    project.ledger = authoritative.model_dump(exclude_computed_fields=True)
    _copy_ledger_values(ledger, authoritative)
    _upsert_manifest(repo, project, [ledger_ref_key])
    checkpoint_ledger_snapshot(session, project_id, settings, authoritative)
    session.commit()
    return SpendReservation(
        project_id=project_id,
        reservation_id=reservation_id,
        ledger_key=ledger_ref_key,
        amount_usd=amount_usd,
        duplicate=False,
    )


def checkpoint_provider_task_object(
    settings: Settings,
    project_id: str,
    shot_id: str,
    attempt: int,
    *,
    model: str,
    task_id: str,
    operation: str,
    renderer: str,
    resolution: str,
    duration_seconds: int,
) -> str:
    repo = create_oss_repository(settings)
    key = provider_task_key(project_id, shot_id, attempt)
    payload = {
        "schema_version": "directorgraph.provider-task.v1",
        "project_id": project_id,
        "shot_id": shot_id,
        "attempt": attempt,
        "operation": operation,
        "model": model,
        "task_id": task_id,
        "renderer": renderer,
        "resolution": resolution,
        "duration_seconds": duration_seconds,
        "submitted_at": datetime.now(UTC).isoformat(),
    }
    try:
        return repo.put_json(key, payload, if_none_match=True).key
    except OssConflictError:
        return repo.get_json(key).ref.key


def load_provider_task_object(
    settings: Settings,
    project_id: str,
    shot_id: str,
    attempt: int,
) -> dict[str, Any] | None:
    try:
        return create_oss_repository(settings).get_json(
            provider_task_key(project_id, shot_id, attempt)
        ).payload
    except OssNotFoundError:
        return None


def checkpoint_provider_result_object(
    settings: Settings,
    project_id: str,
    shot_id: str,
    attempt: int,
    *,
    model: str,
    task_id: str,
    operation: str,
    remote_url: str,
    usage: dict[str, Any] | None = None,
) -> str:
    repo = create_oss_repository(settings)
    key = provider_result_key(project_id, shot_id, attempt)
    parsed = urlparse(remote_url)
    payload = {
        "schema_version": "directorgraph.provider-result.v1",
        "project_id": project_id,
        "shot_id": shot_id,
        "attempt": attempt,
        "operation": operation,
        "model": model,
        "task_id": task_id,
        "status": "succeeded",
        "provider_output_url_present": bool(remote_url),
        "provider_output_url_host": parsed.netloc or None,
        "usage": usage or {},
        "completed_at": datetime.now(UTC).isoformat(),
    }
    try:
        return repo.put_json(key, payload, if_none_match=True).key
    except OssConflictError:
        return repo.get_json(key).ref.key


def checkpoint_media_provider_result_object(
    settings: Settings,
    result_key: str,
    *,
    project_id: str,
    asset_kind: str,
    asset_id: str,
    model: str,
    remote_url: str,
    task_id: str | None = None,
    usage: dict[str, Any] | None = None,
) -> str:
    repo = create_oss_repository(settings)
    key = safe_object_key(result_key)
    parsed = urlparse(remote_url)
    payload = {
        "schema_version": "directorgraph.media-provider-result.v1",
        "project_id": project_id,
        "asset_kind": asset_kind,
        "asset_id": asset_id,
        "model": model,
        "task_id": task_id,
        "status": "succeeded",
        "provider_output_url_present": bool(remote_url),
        "provider_output_url_host": parsed.netloc or None,
        "usage": usage or {},
        "completed_at": datetime.now(UTC).isoformat(),
    }
    try:
        return repo.put_json(key, payload, if_none_match=True).key
    except OssConflictError:
        return repo.get_json(key).ref.key


def load_provider_result_object(
    settings: Settings,
    project_id: str,
    shot_id: str,
    attempt: int,
) -> dict[str, Any] | None:
    try:
        return create_oss_repository(settings).get_json(
            provider_result_key(project_id, shot_id, attempt)
        ).payload
    except OssNotFoundError:
        return None


def checkpoint_asset_materialization_object(
    settings: Settings,
    project_id: str,
    shot_id: str,
    attempt: int,
    *,
    object_key: str,
    model: str,
    task_id: str | None,
    operation: str,
) -> str:
    repo = create_oss_repository(settings)
    key = asset_materialization_key(project_id, shot_id, attempt)
    payload = {
        "schema_version": "directorgraph.asset-materialization.v1",
        "project_id": project_id,
        "shot_id": shot_id,
        "attempt": attempt,
        "operation": operation,
        "model": model,
        "task_id": task_id,
        "object_key": safe_object_key(object_key),
        "materialized_at": datetime.now(UTC).isoformat(),
    }
    try:
        return repo.put_json(key, payload, if_none_match=True).key
    except OssConflictError:
        return repo.get_json(key).ref.key


def checkpoint_media_asset_materialization_object(
    settings: Settings,
    checkpoint_key: str,
    *,
    project_id: str,
    asset_kind: str,
    object_key: str,
    model: str,
    usage: dict[str, Any] | None = None,
) -> str:
    repo = create_oss_repository(settings)
    key = safe_object_key(checkpoint_key)
    payload = {
        "schema_version": "directorgraph.media-asset-materialization.v1",
        "project_id": project_id,
        "asset_kind": asset_kind,
        "model": model,
        "object_key": safe_object_key(object_key),
        "usage": usage or {},
        "materialized_at": datetime.now(UTC).isoformat(),
    }
    try:
        return repo.put_json(key, payload, if_none_match=True).key
    except OssConflictError:
        return repo.get_json(key).ref.key


def load_media_asset_materialization_object(
    settings: Settings,
    checkpoint_key: str,
) -> dict[str, Any] | None:
    try:
        return create_oss_repository(settings).get_json(safe_object_key(checkpoint_key)).payload
    except OssNotFoundError:
        return None


def load_asset_materialization_object(
    settings: Settings,
    project_id: str,
    shot_id: str,
    attempt: int,
) -> dict[str, Any] | None:
    try:
        return create_oss_repository(settings).get_json(
            asset_materialization_key(project_id, shot_id, attempt)
        ).payload
    except OssNotFoundError:
        return None


def checkpoint_final_asset_materialization_object(
    settings: Settings,
    project_id: str,
    *,
    object_key: str,
    model: str,
) -> str:
    repo = create_oss_repository(settings)
    key = final_asset_materialization_key(project_id)
    payload = {
        "schema_version": "directorgraph.final-asset-materialization.v1",
        "project_id": project_id,
        "model": model,
        "object_key": safe_object_key(object_key),
        "materialized_at": datetime.now(UTC).isoformat(),
    }
    return repo.put_json(key, payload).key


def load_final_asset_materialization_object(
    settings: Settings,
    project_id: str,
) -> dict[str, Any] | None:
    try:
        return create_oss_repository(settings).get_json(
            final_asset_materialization_key(project_id)
        ).payload
    except OssNotFoundError:
        return None


def checkpoint_provider_task(
    session: Session,
    project_id: str,
    settings: Settings,
    contract: ShotContract,
    asset: AssetResult,
    attempt: int,
    *,
    operation: str,
) -> str | None:
    if not asset.task_id:
        return None
    project = session.get(Project, project_id)
    if project is None:
        raise KeyError(project_id)
    key = checkpoint_provider_task_object(
        settings,
        project_id,
        contract.id,
        attempt,
        model=asset.model,
        task_id=asset.task_id,
        operation=operation,
        renderer=contract.renderer,
        resolution=contract.resolution,
        duration_seconds=contract.duration_seconds,
    )
    _upsert_manifest(create_oss_repository(settings), project, [key])
    return key


def checkpoint_existing_json_object(
    session: Session,
    project_id: str,
    settings: Settings,
    object_key: str | None,
) -> str | None:
    if not object_key:
        return None
    project = session.get(Project, project_id)
    if project is None:
        raise KeyError(project_id)

    object_key = safe_object_key(object_key)
    repo = create_oss_repository(settings)
    try:
        repo.get_json(object_key)
    except OssNotFoundError:
        return None
    _upsert_manifest(repo, project, [object_key])
    return object_key


def checkpoint_asset_key(
    session: Session,
    project_id: str,
    settings: Settings,
    object_key: str | None,
) -> str | None:
    if not object_key:
        return None
    project = session.get(Project, project_id)
    if project is None:
        raise KeyError(project_id)

    object_key = safe_object_key(object_key)
    repo = create_oss_repository(settings)
    if isinstance(repo, LocalOssRepository):
        media_path = settings.media_root / object_key
        if media_path.exists():
            content_type = mimetypes.guess_type(object_key)[0] or "application/octet-stream"
            repo.put_bytes(object_key, media_path.read_bytes(), content_type=content_type)
    _upsert_manifest(repo, project, [object_key])
    return object_key


def checkpoint_inspection(
    session: Session,
    project_id: str,
    settings: Settings,
    contract: ShotContract,
    report: QualityReport,
    attempt: int,
    *,
    model: str,
    input_tokens: int,
) -> str:
    project = session.get(Project, project_id)
    if project is None:
        raise KeyError(project_id)

    repo = create_oss_repository(settings)
    key = inspection_key(project_id, contract.id, attempt)
    ref = repo.put_json(
        key,
        {
            "schema_version": "directorgraph.inspection.v1",
            "project_id": project_id,
            "shot_id": contract.id,
            "attempt": attempt,
            "model": model,
            "input_tokens": input_tokens,
            "created_at": datetime.now(UTC).isoformat(),
            "report": report.model_dump(mode="json"),
        },
    )
    _upsert_manifest(repo, project, [ref.key])
    return ref.key


def checkpoint_task_submission(
    session: Session,
    job_id: str,
    settings: Settings,
    *,
    operation: str,
    payload: dict[str, Any] | None,
    duplicate: bool,
) -> TaskCheckpoint:
    job = session.get(Job, job_id)
    if job is None:
        raise KeyError(job_id)
    project = session.get(Project, job.project_id)
    if project is None:
        raise KeyError(job.project_id)

    repo = create_oss_repository(settings)
    task_id = str(job.payload.get("task_id") or job.idempotency_key)
    checkpoint_created_at = _as_utc(project.created_at)
    request_key = original_request_key(project.id)
    request_ref_key = _put_json_once(
        repo,
        request_key,
        {
            "schema_version": "directorgraph.original-request.v1",
            "project_id": project.id,
            "title": project.title,
            "brief": project.brief,
            "created_at": _as_utc(project.created_at).isoformat(),
        },
    )
    event_ref_key = _append_event_once(
        repo,
        EventEntry(
            project_id=project.id,
            event_id=task_id,
            kind=f"task.{operation}.submitted",
            agent="Production Manager",
            message="Durable task checkpoint registered",
            payload={
                "task_id": task_id,
                "job_id": job.id,
                "operation": operation,
                "duplicate": duplicate,
                "payload": payload or {},
            },
            created_at=checkpoint_created_at,
        ),
    )
    ledger_ref_key = _append_ledger_once(
        repo,
        LedgerEntry(
            project_id=project.id,
            entry_id=f"{task_id}-submission",
            amount_usd=0,
            category="task-submission",
            description="Task submission checkpoint; no paid provider spend reserved yet.",
            payload={"task_id": task_id, "operation": operation, "duplicate": duplicate},
            created_at=checkpoint_created_at,
        ),
    )
    manifest_key = _upsert_manifest(
        repo,
        project,
        [request_ref_key, event_ref_key, ledger_ref_key],
    )
    checkpoint = TaskCheckpoint(
        project_id=project.id,
        task_id=task_id,
        manifest_key=manifest_key,
        object_keys=[request_ref_key, event_ref_key, ledger_ref_key],
    )
    job.payload = {
        **job.payload,
        "checkpoint_manifest_key": checkpoint.manifest_key,
        "checkpoint_object_keys": checkpoint.object_keys,
    }
    session.commit()
    return checkpoint


def checkpoint_task_status(
    session: Session,
    job_id: str,
    settings: Settings,
    *,
    duplicate: bool = False,
    dispatch_mode: str | None = None,
) -> str:
    job = session.get(Job, job_id)
    if job is None:
        raise KeyError(job_id)
    project = session.get(Project, job.project_id)
    if project is None:
        raise KeyError(job.project_id)

    payload = dict(job.payload or {})
    task_id = str(payload.get("task_id") or job.idempotency_key)
    record = TaskStatusRecord(
        project_id=project.id,
        task_id=task_id,
        job_id=job.id,
        operation=job.job_type,
        status=job.status,
        attempts=job.attempts,
        duplicate=duplicate,
        dispatch_mode=dispatch_mode or payload.get("dispatch_mode"),
        function_compute_request_id=payload.get("function_compute_request_id"),
        function_compute_status_code=payload.get("function_compute_status_code"),
        result=_durable_payload(job.result) if job.result is not None else None,
        error=job.error,
    )
    repo = create_oss_repository(settings)
    key = task_status_key(project.id, task_id)
    ref = repo.put_json(key, record)
    index_ref = repo.put_json(
        task_index_key(task_id),
        {
            "schema_version": "directorgraph.task-index.v1",
            "project_id": project.id,
            "task_id": task_id,
            "status_key": ref.key,
            "updated_at": datetime.now(UTC).isoformat(),
        },
    )
    _upsert_manifest(repo, project, [ref.key])
    return index_ref.key


def load_task_status_object(
    settings: Settings,
    project_id: str,
    task_id: str,
) -> dict[str, Any] | None:
    try:
        return create_oss_repository(settings).get_json(
            task_status_key(project_id, task_id)
        ).payload
    except OssNotFoundError:
        return None


def load_task_status_by_task_id(settings: Settings, task_id: str) -> dict[str, Any] | None:
    repo = create_oss_repository(settings)
    try:
        index = repo.get_json(task_index_key(task_id)).payload
        status_key = safe_object_key(str(index["status_key"]))
        return repo.get_json(status_key).payload
    except (KeyError, OssNotFoundError, ValueError):
        return None


def load_project_ledger_snapshot(settings: Settings, project_id: str) -> dict[str, Any] | None:
    try:
        return create_oss_repository(settings).get_json(ledger_snapshot_key(project_id)).payload
    except (OssNotFoundError, ValueError):
        return None


def load_project_shot_statuses(settings: Settings, project_id: str) -> list[dict[str, Any]]:
    repo = create_oss_repository(settings)
    statuses: list[dict[str, Any]] = []
    for key in repo.list_keys(safe_object_key("projects", project_id, "shots")):
        if not key.endswith("/status.json"):
            continue
        try:
            statuses.append(repo.get_json(key).payload)
        except (OssNotFoundError, ValueError):
            continue
    return sorted(statuses, key=lambda item: int(item.get("sequence") or 0))


def load_project_read_model(settings: Settings, project_id: str):
    from app.schemas import ProjectRead

    try:
        payload = create_oss_repository(settings).get_json(project_read_model_key(project_id)).payload
        read = ProjectRead.model_validate(payload["project"])
        _apply_media_refs(read, settings, payload.get("media_refs"))
        ledger_snapshot = load_project_ledger_snapshot(settings, project_id)
        if ledger_snapshot and ledger_snapshot.get("ledger"):
            read.ledger = ProductionLedger.model_validate(ledger_snapshot["ledger"])
        shot_statuses = {
            str(status.get("shot_id")): status
            for status in load_project_shot_statuses(settings, project_id)
            if status.get("shot_id")
        }
        for shot in read.shots:
            status = shot_statuses.get(shot.shot_code)
            if not status:
                continue
            shot.status = ShotStatus(str(status.get("status") or shot.status))
            shot.attempts = int(status.get("attempts") or 0)
            shot.accepted = bool(status.get("accepted"))
            shot.quality = (
                QualityReport.model_validate(status["quality"])
                if status.get("quality")
                else None
            )
        durable_events = load_project_events(settings, project_id)
        if durable_events:
            read.events = durable_events
        return read
    except (KeyError, OssNotFoundError, ValueError):
        return None


def load_project_events(settings: Settings, project_id: str, *, limit: int = 100):
    from app.schemas import EventRead

    repo = create_oss_repository(settings)
    events: list[dict[str, Any]] = []
    for key in repo.list_keys(safe_object_key("projects", project_id, "events")):
        try:
            payload = repo.get_json(key).payload
        except (OssNotFoundError, ValueError):
            continue
        events.append(payload)

    events.sort(key=lambda item: str(item.get("created_at") or ""))
    reads = []
    for index, payload in enumerate(events[-limit:], 1):
        reads.append(
            EventRead.model_validate(
                {
                    "id": index,
                    "kind": payload.get("kind") or "event",
                    "message": payload.get("message") or "",
                    "agent": payload.get("agent") or "System",
                    "payload": payload.get("payload") or {},
                    "created_at": payload.get("created_at") or datetime.now(UTC).isoformat(),
                }
            )
        )
    return reads


def list_project_read_models(settings: Settings, *, limit: int = 50):
    repo = create_oss_repository(settings)
    reads = []
    for key in sorted(repo.list_keys("indexes/projects"), reverse=True):
        try:
            index = repo.get_json(key).payload
            read = load_project_read_model(settings, str(index["project_id"]))
            if read is not None:
                reads.append(read)
        except (KeyError, OssNotFoundError, ValueError):
            continue
        if len(reads) >= limit:
            break
    return reads


__all__ = [
    "SpendReservation",
    "TaskCheckpoint",
    "checkpoint_asset_materialization_object",
    "checkpoint_asset_key",
    "checkpoint_event",
    "checkpoint_existing_json_object",
    "checkpoint_final_asset_materialization_object",
    "checkpoint_final_manifest",
    "checkpoint_inspection",
    "checkpoint_ledger_snapshot",
    "checkpoint_media_asset_materialization_object",
    "checkpoint_media_provider_result_object",
    "checkpoint_project_read_model",
    "checkpoint_provider_task",
    "checkpoint_provider_task_object",
    "checkpoint_provider_result_object",
    "checkpoint_shot_status",
    "checkpoint_story_plan",
    "checkpoint_task_submission",
    "checkpoint_task_status",
    "load_asset_materialization_object",
    "load_final_asset_materialization_object",
    "load_media_asset_materialization_object",
    "load_project_events",
    "load_project_ledger_snapshot",
    "load_project_read_model",
    "load_project_shot_statuses",
    "list_project_read_models",
    "load_provider_result_object",
    "load_provider_task_object",
    "load_task_status_by_task_id",
    "load_task_status_object",
    "reserve_live_spend",
]
