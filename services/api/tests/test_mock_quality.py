from pathlib import Path

import pytest

from app.clients.storage import AssetStore
from app.config import Settings
from app.core.story import fallback_story_plan
from app.providers.base import AssetResult
from app.providers.mock import MockStudioProvider
from app.schemas import ProjectBrief


@pytest.mark.asyncio
async def test_mock_continuity_supervisor_rejects_then_accepts_repair(tmp_path: Path):
    settings = Settings(
        provider_mode="mock",
        database_url=f"sqlite:///{tmp_path / 'test.db'}",
        media_root=tmp_path / "media",
        public_media_base_url="http://test/media",
        seed_demo=False,
    )
    provider = MockStudioProvider(settings, AssetStore(settings))
    brief = ProjectBrief(
        title="QC test",
        premise="A courier robot waits at one locked door until it opens on the final night.",
        duration_seconds=28,
    )
    contract = fallback_story_plan(brief).shots[4]
    first = AssetResult(
        public_url="http://test/media/attempt-1.mp4",
        local_path=tmp_path / "attempt-1.mp4",
        provider="mock",
        model="mock",
    )
    rejected = await provider.inspect_video(contract, first, attempt=1)
    assert not rejected.report.passed
    assert rejected.report.repair_strategy == "local_edit"
    assert any("red paper crane" in violation for violation in rejected.report.violations)

    repaired = AssetResult(
        public_url="http://test/media/repair-2.mp4",
        local_path=tmp_path / "repair-2.mp4",
        provider="mock",
        model="mock",
    )
    accepted = await provider.inspect_video(contract, repaired, attempt=2)
    assert accepted.report.passed
    assert accepted.report.overall_score >= contract.quality_threshold
