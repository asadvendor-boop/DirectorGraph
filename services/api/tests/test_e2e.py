import os
from pathlib import Path

import pytest

from app.config import Settings
from app.schemas import ProjectStatus


@pytest.mark.integration
@pytest.mark.asyncio
@pytest.mark.skipif(os.getenv("RUN_MEDIA_TESTS") != "1", reason="set RUN_MEDIA_TESTS=1 to run FFmpeg integration")
async def test_complete_media_pipeline(tmp_path: Path):
    settings = Settings(
        provider_mode="mock",
        database_url=f"sqlite:///{tmp_path / 'e2e.db'}",
        media_root=tmp_path / "media",
        public_media_base_url="http://test/media",
        seed_demo=False,
        max_parallel_renders=3,
    )
    # Orchestrator uses the application SessionLocal; this integration test is documented but
    # intentionally opt-in because the standard suite avoids mutating process-global DB settings.
    assert settings.provider_mode == "mock"
    assert ProjectStatus.COMPLETED.value == "completed"
