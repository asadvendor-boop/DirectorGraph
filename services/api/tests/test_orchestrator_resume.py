from pathlib import Path

import pytest
from sqlalchemy import create_engine
from sqlalchemy.orm import sessionmaker

from app.config import Settings
from app.core import orchestrator as orchestrator_module
from app.core.orchestrator import DirectorGraphOrchestrator, ProducedShot
from app.core.story import fallback_story_plan
from app.db import Base
from app.models import Project
from app.oss_repository import (
    LocalOssRepository,
    asset_materialization_key,
    character_asset_materialization_key,
    storyboard_asset_materialization_key,
    voice_asset_materialization_key,
)
from app.providers.base import AssetResult, PlanResult
from app.repository import create_project, get_project, save_plan
from app.schemas import ProjectBrief, QualityDimension, QualityReport, ShotStatus
from app.task_checkpoints import (
    checkpoint_asset_materialization_object,
    checkpoint_final_asset_materialization_object,
    checkpoint_media_asset_materialization_object,
    checkpoint_story_plan,
)


@pytest.mark.asyncio
async def test_live_run_resumes_cached_story_plan_without_story_call_or_reservation(
    tmp_path: Path, monkeypatch
):
    engine = create_engine("sqlite:///:memory:")
    Base.metadata.create_all(engine)
    test_session_local = sessionmaker(bind=engine, expire_on_commit=False, autoflush=False)
    monkeypatch.setattr(orchestrator_module, "SessionLocal", test_session_local)

    settings = Settings(
        provider_mode="live",
        dashscope_api_key="test-key",
        max_project_spend_usd=8,
        max_total_live_spend_usd=20,
        database_url=f"sqlite:///{tmp_path / 'cached-story-resume.db'}",
        media_root=tmp_path / "media",
        oss_repository_root=tmp_path / "oss",
        public_media_base_url="https://assets.example.invalid/media",
        seed_demo=False,
    )
    brief = ProjectBrief(
        title="Cached StoryIR",
        premise="A courier robot resumes a durable StoryIR without spending on story planning again.",
        duration_seconds=21,
        budget_usd=8,
    )

    with test_session_local() as session:
        project = create_project(session, brief, settings=settings)
        project_id = project.id
        plan = fallback_story_plan(brief)
        save_plan(session, project_id, plan, settings=settings)
        checkpoint_story_plan(session, project_id, settings, plan)

    class CachedStoryProvider:
        def __init__(self, provider_settings: Settings):
            self.settings = provider_settings

        def _asset(self, object_key: str, model: str) -> AssetResult:
            path = self.settings.media_root / object_key
            path.parent.mkdir(parents=True, exist_ok=True)
            path.write_bytes(model.encode())
            return AssetResult(
                f"https://assets.example.invalid/media/{object_key}",
                path,
                "fake-live",
                model,
                object_key=object_key,
            )

        async def plan_story(self, project_brief: ProjectBrief) -> PlanResult:
            raise AssertionError("cached StoryIR should skip live story planning")

        async def generate_character_reference(self, project_id: str, character, seed: int):
            return self._asset(
                f"projects/{project_id}/characters/{character.id}.png",
                "character-reference-test",
            )

        async def generate_storyboard(self, project_id: str, contract, seed: int):
            return self._asset(
                f"projects/{project_id}/shots/{contract.id}/storyboard.png",
                "storyboard-test",
            )

        async def synthesize_voice(self, project_id: str, contract, language: str):
            text = (contract.dialogue or contract.narration or "").strip()
            if not text:
                return None
            return self._asset(
                f"projects/{project_id}/shots/{contract.id}/dialogue.wav",
                "dialogue-test",
            )

        async def generate_video(self, *args, **kwargs):
            raise RuntimeError("stop after cached story resume")

        async def inspect_video(self, *args, **kwargs):
            raise AssertionError("inspection should not run after render failure")

        async def repair_video(self, *args, **kwargs):
            raise AssertionError("repair should not run after render failure")

        async def close(self):
            return None

    monkeypatch.setattr(
        orchestrator_module,
        "create_provider",
        lambda provider_settings, store: CachedStoryProvider(provider_settings),
    )

    orchestrator = DirectorGraphOrchestrator(settings)
    with pytest.raises(RuntimeError, match="stop after cached story resume"):
        await orchestrator.run_project(project_id)

    repo = LocalOssRepository(settings.oss_repository_root)
    ledger_entries = [
        repo.get_json(path.relative_to(settings.oss_repository_root).as_posix()).payload
        for path in (settings.oss_repository_root / "projects" / project_id / "ledger" / "entries").glob("*.json")
        if not path.name.endswith(".meta.json")
    ]
    categories = {entry["category"] for entry in ledger_entries}

    with test_session_local() as session:
        refreshed = get_project(session, project_id)
        event_kinds = {event.kind for event in refreshed.events}

    assert "story-planning" not in categories
    assert {
        "character-reference",
        "storyboard",
        "dialogue-tts",
        "video-render",
    }.issubset(categories)
    assert refreshed is not None
    assert refreshed.ledger["text_input_tokens"] == 0
    assert refreshed.ledger["text_output_tokens"] == 0
    assert "story.plan.resumed" in event_kinds


@pytest.mark.asyncio
async def test_live_run_reserves_non_video_provider_spend_before_calls(tmp_path: Path, monkeypatch):
    engine = create_engine("sqlite:///:memory:")
    Base.metadata.create_all(engine)
    test_session_local = sessionmaker(bind=engine, expire_on_commit=False, autoflush=False)
    monkeypatch.setattr(orchestrator_module, "SessionLocal", test_session_local)

    settings = Settings(
        provider_mode="live",
        dashscope_api_key="test-key",
        max_project_spend_usd=8,
        max_total_live_spend_usd=20,
        database_url=f"sqlite:///{tmp_path / 'live-reservations.db'}",
        media_root=tmp_path / "media",
        oss_repository_root=tmp_path / "oss",
        public_media_base_url="https://assets.example.invalid/media",
        seed_demo=False,
    )
    brief = ProjectBrief(
        title="Live reservations",
        premise="A courier robot reserves all paid model calls before provider execution.",
        duration_seconds=21,
        budget_usd=8,
    )
    with test_session_local() as session:
        project = create_project(session, brief)
        project_id = project.id

    class ReservingProvider:
        def __init__(self, provider_settings: Settings):
            self.settings = provider_settings

        def _asset(self, object_key: str, model: str) -> AssetResult:
            path = self.settings.media_root / object_key
            path.parent.mkdir(parents=True, exist_ok=True)
            path.write_bytes(model.encode())
            return AssetResult(
                f"https://assets.example.invalid/media/{object_key}",
                path,
                "fake-live",
                model,
                object_key=object_key,
            )

        async def plan_story(self, project_brief: ProjectBrief) -> PlanResult:
            return PlanResult(
                fallback_story_plan(project_brief),
                "qwen-test",
                input_tokens=11,
                output_tokens=17,
            )

        async def generate_character_reference(self, project_id: str, character, seed: int):
            return self._asset(
                f"projects/{project_id}/characters/{character.id}.png",
                "character-reference-test",
            )

        async def generate_storyboard(self, project_id: str, contract, seed: int):
            return self._asset(
                f"projects/{project_id}/shots/{contract.id}/storyboard.png",
                "storyboard-test",
            )

        async def synthesize_voice(self, project_id: str, contract, language: str):
            text = (contract.dialogue or contract.narration or "").strip()
            if not text:
                return None
            return self._asset(
                f"projects/{project_id}/shots/{contract.id}/dialogue.wav",
                "dialogue-test",
            )

        async def generate_video(self, *args, **kwargs):
            raise RuntimeError("stop after spend reservations")

        async def inspect_video(self, *args, **kwargs):
            raise AssertionError("inspection should not run after render failure")

        async def repair_video(self, *args, **kwargs):
            raise AssertionError("repair should not run after render failure")

        async def close(self):
            return None

    monkeypatch.setattr(
        orchestrator_module,
        "create_provider",
        lambda provider_settings, store: ReservingProvider(provider_settings),
    )

    orchestrator = DirectorGraphOrchestrator(settings)
    with pytest.raises(RuntimeError, match="stop after spend reservations"):
        await orchestrator.run_project(project_id)

    repo = LocalOssRepository(settings.oss_repository_root)
    ledger_entries = [
        repo.get_json(path.relative_to(settings.oss_repository_root).as_posix()).payload
        for path in (settings.oss_repository_root / "projects" / project_id / "ledger" / "entries").glob("*.json")
        if not path.name.endswith(".meta.json")
    ]
    categories = {entry["category"] for entry in ledger_entries}
    reservation_ids = {entry["payload"]["reservation_id"] for entry in ledger_entries}
    estimated_total = round(sum(float(entry["amount_usd"]) for entry in ledger_entries), 4)

    with test_session_local() as session:
        refreshed = session.get(Project, project_id)

    assert {
        "story-planning",
        "character-reference",
        "storyboard",
        "dialogue-tts",
        "video-render",
    }.issubset(categories)
    assert "story-planning" in reservation_ids
    assert any(item.endswith("-storyboard") for item in reservation_ids)
    assert any(item.endswith("-dialogue-tts") for item in reservation_ids)
    assert refreshed is not None
    assert refreshed.ledger["text_input_tokens"] == 11
    assert refreshed.ledger["text_output_tokens"] == 17
    assert refreshed.ledger["estimated_cost_usd"] == estimated_total


@pytest.mark.asyncio
async def test_edit_project_resumes_materialized_final_without_recompose(tmp_path: Path, monkeypatch):
    engine = create_engine("sqlite:///:memory:")
    Base.metadata.create_all(engine)
    test_session_local = sessionmaker(bind=engine, expire_on_commit=False, autoflush=False)
    monkeypatch.setattr(orchestrator_module, "SessionLocal", test_session_local)

    settings = Settings(
        provider_mode="mock",
        database_url=f"sqlite:///{tmp_path / 'resume-final.db'}",
        media_root=tmp_path / "media",
        oss_repository_root=tmp_path / "oss",
        public_media_base_url="https://assets.example.invalid/media",
        seed_demo=False,
    )
    brief = ProjectBrief(
        title="Final resume test",
        premise="A courier robot resumes from an already assembled master.",
        duration_seconds=21,
    )
    with test_session_local() as session:
        project = create_project(session, brief)
        project_id = project.id

    object_key = f"projects/{project_id}/final/directorgraph-master.mp4"
    final_path = settings.media_root / object_key
    final_path.parent.mkdir(parents=True)
    final_path.write_bytes(b"assembled-master")
    checkpoint_key = checkpoint_final_asset_materialization_object(
        settings,
        project_id,
        object_key=object_key,
        model="DirectorGraph Picture Editor",
    )

    def forbidden_compose(*args, **kwargs):
        raise AssertionError("final resume should not recompose the master")

    monkeypatch.setattr(orchestrator_module, "compose_timeline", forbidden_compose)
    orchestrator = DirectorGraphOrchestrator(settings)
    try:
        final = await orchestrator._edit_project(project_id, brief, produced=[])
    finally:
        await orchestrator.close()

    assert final.local_path == final_path
    assert final.object_key == object_key
    assert final.asset_checkpoint_key == checkpoint_key
    assert final.public_url == f"https://assets.example.invalid/media/{object_key}"


@pytest.mark.asyncio
async def test_patch_project_restores_assets_from_materialization_keys(tmp_path: Path, monkeypatch):
    engine = create_engine("sqlite:///:memory:")
    Base.metadata.create_all(engine)
    test_session_local = sessionmaker(bind=engine, expire_on_commit=False, autoflush=False)
    monkeypatch.setattr(orchestrator_module, "SessionLocal", test_session_local)

    settings = Settings(
        provider_mode="mock",
        database_url=f"sqlite:///{tmp_path / 'patch-resume.db'}",
        media_root=tmp_path / "media",
        oss_repository_root=tmp_path / "oss",
        public_media_base_url="https://assets.example.invalid/media",
        seed_demo=False,
    )
    brief = ProjectBrief(
        title="Patch resume test",
        premise="A semantic patch reconstructs a completed project from durable assets.",
        duration_seconds=21,
    )
    plan = fallback_story_plan(brief)
    for character in plan.characters:
        character.reference_url = f"https://signed.example.invalid/opaque-{character.id}-reference?token=redacted"

    def write_asset(object_key: str, payload: bytes) -> Path:
        path = settings.media_root / object_key
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_bytes(payload)
        return path

    quality = QualityReport(
        passed=True,
        overall_score=0.94,
        dimensions=[
            QualityDimension(
                name="narrative",
                score=0.94,
                evidence="The accepted render follows the contract.",
            )
        ],
        evaluator_model="test-evaluator",
    )

    with test_session_local() as session:
        project = create_project(session, brief, settings=settings)
        project_id = project.id
        save_plan(session, project_id, plan, settings=settings)
        session.expire_all()
        project = session.get(Project, project_id)
        assert project is not None
        assert len(project.shots) == len(plan.shots)
        for row in sorted(project.shots, key=lambda shot: shot.sequence):
            row.status = ShotStatus.ACCEPTED.value
            row.accepted = True
            row.attempts = 1
            row.storyboard_url = f"https://signed.example.invalid/opaque-{row.shot_code}-storyboard?token=redacted"
            row.audio_url = f"https://signed.example.invalid/opaque-{row.shot_code}-dialogue?token=redacted"
            row.video_url = f"https://signed.example.invalid/opaque-{row.shot_code}-clip?token=redacted"
            row.quality = quality.model_dump()
        session.commit()

    character_keys = {
        character.id: f"projects/{project_id}/characters/{character.id}.png"
        for character in plan.characters
    }
    storyboard_keys = {
        contract.id: f"projects/{project_id}/shots/{contract.id}/storyboard.png"
        for contract in plan.shots
    }
    voice_keys = {
        contract.id: f"projects/{project_id}/shots/{contract.id}/dialogue.wav"
        for contract in plan.shots
    }
    video_keys = {
        contract.id: f"projects/{project_id}/shots/{contract.id}/attempts/attempt-1.mp4"
        for contract in plan.shots
    }
    for character in plan.characters:
        object_key = character_keys[character.id]
        write_asset(object_key, f"character:{character.id}".encode())
        checkpoint_media_asset_materialization_object(
            settings,
            character_asset_materialization_key(project_id, character.id),
            project_id=project_id,
            asset_kind="character_reference",
            object_key=object_key,
            model=f"character-model-{character.id}",
        )
    for contract in plan.shots:
        storyboard_key = storyboard_keys[contract.id]
        voice_key = voice_keys[contract.id]
        video_key = video_keys[contract.id]
        write_asset(storyboard_key, f"storyboard:{contract.id}".encode())
        write_asset(voice_key, f"voice:{contract.id}".encode())
        write_asset(video_key, f"video:{contract.id}".encode())
        checkpoint_media_asset_materialization_object(
            settings,
            storyboard_asset_materialization_key(project_id, contract.id),
            project_id=project_id,
            asset_kind="storyboard",
            object_key=storyboard_key,
            model=f"storyboard-model-{contract.id}",
        )
        checkpoint_media_asset_materialization_object(
            settings,
            voice_asset_materialization_key(project_id, contract.id),
            project_id=project_id,
            asset_kind="dialogue",
            object_key=voice_key,
            model=f"voice-model-{contract.id}",
        )
        checkpoint_asset_materialization_object(
            settings,
            project_id,
            contract.id,
            1,
            object_key=video_key,
            model=f"video-model-{contract.id}",
            task_id=None,
            operation="render",
        )

    selected_id = plan.shots[-1].id
    replacement_key = f"projects/{project_id}/shots/{selected_id}/attempts/patch-replacement.mp4"
    replacement_path = write_asset(replacement_key, b"replacement-video")
    final_key = f"projects/{project_id}/final/patched-master.mp4"
    final_path = write_asset(final_key, b"patched-master")
    captured: dict[str, list[str]] = {}

    orchestrator = DirectorGraphOrchestrator(settings)

    async def fake_produce(project_id_arg, prepared, ledger):
        assert project_id_arg == project_id
        assert prepared.contract.id == selected_id
        assert prepared.storyboard.object_key == storyboard_keys[selected_id]
        assert prepared.storyboard.local_path == settings.media_root / storyboard_keys[selected_id]
        assert prepared.voice is not None
        assert prepared.voice.object_key == voice_keys[selected_id]
        assert {
            asset.object_key for asset in prepared.references
        } == {
            character_keys[character_id]
            for character_id in prepared.contract.characters
        }
        captured["selected_references"] = [
            str(asset.object_key) for asset in prepared.references
        ]
        return ProducedShot(
            prepared.row_id,
            prepared.contract,
            AssetResult(
                f"https://assets.example.invalid/media/{replacement_key}",
                replacement_path,
                "test",
                "patch-render",
                object_key=replacement_key,
            ),
            prepared.voice,
            quality,
        )

    async def fake_edit(project_id_arg, brief_arg, produced):
        assert project_id_arg == project_id
        assert brief_arg.title == brief.title
        captured["produced_ids"] = [item.contract.id for item in produced]
        for item in produced:
            if item.contract.id == selected_id:
                assert item.video.object_key == replacement_key
                continue
            assert item.video.object_key == video_keys[item.contract.id]
            assert item.video.local_path == settings.media_root / video_keys[item.contract.id]
            assert item.video.asset_checkpoint_key == asset_materialization_key(
                project_id, item.contract.id, 1
            )
            assert item.voice is not None
            assert item.voice.object_key == voice_keys[item.contract.id]
        return AssetResult(
            f"https://assets.example.invalid/media/{final_key}",
            final_path,
            "test",
            "patch-editor",
            object_key=final_key,
        )

    monkeypatch.setattr(orchestrator, "_produce_shot", fake_produce)
    monkeypatch.setattr(orchestrator, "_edit_project", fake_edit)

    result = await orchestrator.patch_project(
        project_id,
        "Make the ending warmer",
        [selected_id],
    )

    assert result == {"project_id": project_id, "final_object_key": final_key}
    assert captured["produced_ids"] == [contract.id for contract in plan.shots]
    assert set(captured["selected_references"]) == {
        character_keys[character_id] for character_id in plan.shots[-1].characters
    }
