from __future__ import annotations

import argparse
import asyncio
import shutil
from pathlib import Path

from app.config import get_settings
from app.core.orchestrator import DirectorGraphOrchestrator
from app.db import SessionLocal, init_db
from app.repository import create_project, get_project
from app.schemas import ProjectBrief


async def main(output: Path | None) -> None:
    settings = get_settings()
    if settings.provider_mode != "mock":
        raise RuntimeError("The local demo command is intentionally restricted to PROVIDER_MODE=mock")
    init_db()
    with SessionLocal() as session:
        project = create_project(
            session,
            ProjectBrief(
                title="The Last Delivery",
                premise="Every night a small courier robot leaves a package at the same apartment. On its final night before decommissioning, the door finally opens.",
                duration_seconds=21,
                budget_usd=18,
            ),
            settings=settings,
        )
        project_id = project.id
    result = await DirectorGraphOrchestrator(settings).run_project(project_id)
    print(result)
    if output:
        with SessionLocal() as session:
            project = get_project(session, project_id)
            source = DirectorGraphOrchestrator(settings).store.local_path_from_url(project.final_video_url)
            if not source:
                raise RuntimeError("Could not resolve generated master")
            output.parent.mkdir(parents=True, exist_ok=True)
            shutil.copy2(source, output)
            print(f"Copied preview to {output}")


if __name__ == "__main__":
    parser = argparse.ArgumentParser()
    parser.add_argument("--output", type=Path)
    args = parser.parse_args()
    asyncio.run(main(args.output))
