from __future__ import annotations

import asyncio
from collections.abc import Iterable
from dataclasses import dataclass
from pathlib import Path

from sqlalchemy import select

from app.clients.ffmpeg import compose_timeline, extract_frame
from app.clients.storage import AssetStore
from app.config import Settings
from app.core.budget import (
    can_spend_on_repair,
    estimate_inspection_cost,
    estimate_repair_cost,
    estimate_story_cost,
    estimate_storyboard_cost,
    estimate_tts_cost,
    estimate_video_cost,
    record_inspection,
    record_story_usage,
    record_storyboard,
    record_tts,
    route_and_budget,
)
from app.db import SessionLocal
from app.models import Project, Shot
from app.oss_repository import (
    asset_materialization_key,
    character_asset_materialization_key,
    final_asset_materialization_key,
    storyboard_asset_materialization_key,
    voice_asset_materialization_key,
)
from app.providers.base import AssetResult, StudioProvider
from app.providers.factory import create_provider
from app.repository import (
    add_event,
    get_project,
    save_ledger,
    save_plan,
    set_project_error,
    set_project_status,
)
from app.schemas import (
    ProductionLedger,
    ProjectBrief,
    ProjectStatus,
    QualityReport,
    ShotContract,
    ShotStatus,
    StoryPlan,
)
from app.task_checkpoints import (
    checkpoint_asset_key,
    checkpoint_existing_json_object,
    checkpoint_final_asset_materialization_object,
    checkpoint_final_manifest,
    checkpoint_inspection,
    checkpoint_provider_task,
    checkpoint_shot_status,
    checkpoint_story_plan,
    load_asset_materialization_object,
    load_final_asset_materialization_object,
    load_media_asset_materialization_object,
    reserve_live_spend,
)


@dataclass(slots=True)
class PreparedShot:
    row_id: str
    contract: ShotContract
    storyboard: AssetResult
    voice: AssetResult | None
    references: list[AssetResult]


@dataclass(slots=True)
class ProducedShot:
    row_id: str
    contract: ShotContract
    video: AssetResult
    voice: AssetResult | None
    quality: QualityReport


class DirectorGraphOrchestrator:
    def __init__(self, settings: Settings):
        self.settings = settings
        self.store = AssetStore(settings)
        self.provider: StudioProvider = create_provider(settings, self.store)
        self.render_gate = asyncio.Semaphore(settings.max_parallel_renders)
        self.ledger_lock = asyncio.Lock()

    async def close(self) -> None:
        await self.provider.close()

    def _uses_live_spend_reservations(self) -> bool:
        return self.settings.provider_mode == "live"

    def _reserve_live_model_spend(
        self,
        project_id: str,
        ledger: ProductionLedger,
        *,
        reservation_id: str,
        amount_usd: float,
        category: str,
        description: str,
        payload: dict | None = None,
        preserve_repair_reserve: bool = True,
    ) -> None:
        if amount_usd <= 0:
            return
        with SessionLocal() as session:
            reserve_live_spend(
                session,
                project_id,
                self.settings,
                ledger,
                reservation_id=reservation_id,
                amount_usd=amount_usd,
                category=category,
                description=description,
                payload=payload,
                preserve_repair_reserve=preserve_repair_reserve,
            )

    async def run_project(self, project_id: str) -> dict[str, str]:
        try:
            with SessionLocal() as session:
                project = get_project(session, project_id)
                brief = ProjectBrief.model_validate(project.brief)
                ledger = ProductionLedger.model_validate(project.ledger)
                cached_plan = (
                    route_and_budget(StoryPlan.model_validate(project.plan), brief)
                    if project.plan
                    else None
                )
                set_project_status(
                    session,
                    project_id,
                    ProjectStatus.PLANNING,
                    (
                        "Executive Showrunner is restoring the durable StoryIR checkpoint"
                        if cached_plan
                        else "Executive Showrunner is compiling the creative brief into StoryIR"
                    ),
                    agent="Executive Showrunner",
                    settings=self.settings,
                )

            if cached_plan:
                plan = cached_plan
                with SessionLocal() as session:
                    project = get_project(session, project_id)
                    if not project.shots:
                        save_plan(session, project_id, plan, settings=self.settings)
                    add_event(
                        session,
                        project_id,
                        "story.plan.resumed",
                        "Story Architect resumed from the durable StoryIR checkpoint",
                        {"shots": len(plan.shots), "characters": len(plan.characters)},
                        agent="Story Architect",
                        settings=self.settings,
                    )
                    session.commit()
                    set_project_status(
                        session,
                        project_id,
                        ProjectStatus.STORYBOARDING,
                        "Visual Director is generating production references and dialogue",
                        agent="Visual Director",
                        settings=self.settings,
                    )
            else:
                self._reserve_live_model_spend(
                    project_id,
                    ledger,
                    reservation_id="story-planning",
                    amount_usd=estimate_story_cost(),
                    category="story-planning",
                    description="Estimated live StoryIR planning reservation.",
                    payload={"operation": "story_plan"},
                )
                plan_result = await self.provider.plan_story(brief)
                plan = route_and_budget(plan_result.plan, brief)
                record_story_usage(
                    ledger,
                    plan_result.input_tokens,
                    plan_result.output_tokens,
                    include_cost=not self._uses_live_spend_reservations(),
                )
                with SessionLocal() as session:
                    save_plan(session, project_id, plan, settings=self.settings)
                    save_ledger(session, project_id, ledger, settings=self.settings)
                    checkpoint_story_plan(session, project_id, self.settings, plan)
                    add_event(
                        session,
                        project_id,
                        "model.route.story.degraded" if plan_result.degraded else "model.route.story",
                        (
                            f"Story architecture used a validated fallback after {plan_result.model} degraded"
                            if plan_result.degraded
                            else f"Story architecture completed with {plan_result.model}"
                        ),
                        {
                            "model": plan_result.model,
                            "input_tokens": plan_result.input_tokens,
                            "output_tokens": plan_result.output_tokens,
                            "degraded": plan_result.degraded,
                            "degradation_reason": plan_result.degradation_reason,
                            "planning_path": plan_result.planning_path,
                            "character_bound_shots": plan_result.character_bound_shots,
                            "total_shots": len(plan.shots),
                            "plan_repair_attempted": plan_result.plan_repair_attempted,
                            "plan_repair_reason": plan_result.plan_repair_reason,
                        },
                        agent="Story Architect",
                        settings=self.settings,
                    )
                    session.commit()
                    set_project_status(
                        session,
                        project_id,
                        ProjectStatus.STORYBOARDING,
                        "Visual Director is generating production references and dialogue",
                        agent="Visual Director",
                        settings=self.settings,
                    )

            for index, character in enumerate(plan.characters):
                self._reserve_live_model_spend(
                    project_id,
                    ledger,
                    reservation_id=f"character-{character.id}-reference",
                    amount_usd=estimate_storyboard_cost(),
                    category="character-reference",
                    description=f"Estimated live character reference reservation for {character.id}.",
                    payload={
                        "operation": "character_reference",
                        "character_id": character.id,
                        "seed": brief.seed + 1000 + index,
                    },
                )
            character_assets = await asyncio.gather(
                *[
                    self.provider.generate_character_reference(
                        project_id, character, brief.seed + 1000 + index
                    )
                    for index, character in enumerate(plan.characters)
                ]
            )
            character_references = {
                character.id: asset
                for character, asset in zip(plan.characters, character_assets, strict=True)
            }
            with SessionLocal() as session:
                for asset in character_assets:
                    checkpoint_existing_json_object(
                        session,
                        project_id,
                        self.settings,
                        asset.provider_result_key,
                    )
                    checkpoint_existing_json_object(
                        session,
                        project_id,
                        self.settings,
                        asset.asset_checkpoint_key,
                    )
                    checkpoint_asset_key(session, project_id, self.settings, asset.object_key)
            for character, asset in zip(plan.characters, character_assets, strict=True):
                character.reference_url = asset.public_url
                record_storyboard(ledger, include_cost=not self._uses_live_spend_reservations())
            with SessionLocal() as session:
                project = session.get(Project, project_id)
                if project is None:
                    raise KeyError(project_id)
                project.plan = plan.model_dump()
                add_event(
                    session,
                    project_id,
                    "characters.references.locked",
                    f"Visual Director locked {len(character_assets)} canonical character references",
                    {
                        "characters": [
                            {
                                "id": character.id,
                                "name": character.name,
                                "reference_url": character.reference_url,
                                "model": character_references[character.id].model,
                            }
                            for character in plan.characters
                        ]
                    },
                    agent="Visual Director",
                    settings=self.settings,
                )
                save_ledger(session, project_id, ledger, settings=self.settings)
                session.commit()

            for contract in plan.shots:
                self._reserve_live_model_spend(
                    project_id,
                    ledger,
                    reservation_id=f"{contract.id}-storyboard",
                    amount_usd=estimate_storyboard_cost(),
                    category="storyboard",
                    description=f"Estimated live storyboard reservation for {contract.id}.",
                    payload={"operation": "storyboard", "shot_id": contract.id},
                )
                dialogue_text = (contract.dialogue or contract.narration or "").replace("\n", " ").strip()
                if dialogue_text:
                    self._reserve_live_model_spend(
                        project_id,
                        ledger,
                        reservation_id=f"{contract.id}-dialogue-tts",
                        amount_usd=estimate_tts_cost(len(dialogue_text)),
                        category="dialogue-tts",
                        description=f"Estimated live dialogue/TTS reservation for {contract.id}.",
                        payload={
                            "operation": "dialogue_tts",
                            "shot_id": contract.id,
                            "characters": len(dialogue_text),
                            "language": brief.language,
                        },
                    )
            prepared = await asyncio.gather(
                *[
                    self._prepare_shot(
                        project_id, brief, contract, character_references
                    )
                    for contract in plan.shots
                ]
            )
            for item in prepared:
                record_storyboard(ledger, include_cost=not self._uses_live_spend_reservations())
                if item.voice:
                    record_tts(
                        ledger,
                        len((item.contract.dialogue or item.contract.narration or "").replace("\n", " ").strip()),
                        include_cost=not self._uses_live_spend_reservations(),
                    )
            with SessionLocal() as session:
                save_ledger(session, project_id, ledger, settings=self.settings)
                set_project_status(
                    session,
                    project_id,
                    ProjectStatus.PRODUCING,
                    "Production Manager is rendering independent shots in parallel",
                    agent="Production Manager",
                    settings=self.settings,
                )

            produced = []
            for item in sorted(prepared, key=lambda shot: shot.contract.sequence):
                seeded = await self._seed_i2v_from_previous_acceptance(project_id, item, produced)
                produced.append(await self._produce_shot(project_id, seeded, ledger))
            produced = sorted(produced, key=lambda item: item.contract.sequence)
            if not all(item.quality.passed for item in produced):
                failed = [item.contract.id for item in produced if not item.quality.passed]
                raise RuntimeError(f"Quality gate exhausted for shots: {', '.join(failed)}")

            final = await self._edit_project(project_id, brief, produced)
            with SessionLocal() as session:
                project = session.get(Project, project_id)
                if project is None:
                    raise KeyError(project_id)
                project.final_video_url = final.public_url
                project.error = None
                add_event(
                    session,
                    project_id,
                    "edit.master.created",
                    "Picture Editor assembled the accepted clips, dialogue, and timed captions",
                    {
                        "final_object_key": final.object_key,
                        "acceptance_ratio": ledger.acceptance_ratio,
                        "estimated_cost_usd": ledger.estimated_cost_usd,
                    },
                    agent="Picture Editor",
                    settings=self.settings,
                )
                session.commit()
                set_project_status(
                    session,
                    project_id,
                    ProjectStatus.COMPLETED,
                    "Verified production master passed every contract gate",
                    agent="Executive Showrunner",
                    settings=self.settings,
                )
                checkpoint_asset_key(session, project_id, self.settings, final.object_key)
                checkpoint_existing_json_object(
                    session,
                    project_id,
                    self.settings,
                    final.asset_checkpoint_key,
                )
                checkpoint_final_manifest(session, project_id, self.settings)
            return {"project_id": project_id, "final_object_key": final.object_key}
        except Exception as exc:
            with SessionLocal() as session:
                set_project_error(session, project_id, str(exc), settings=self.settings)
            raise
        finally:
            await self.close()

    async def _prepare_shot(
        self,
        project_id: str,
        brief: ProjectBrief,
        contract: ShotContract,
        character_references: dict[str, AssetResult],
    ) -> PreparedShot:
        with SessionLocal() as session:
            shot = self._shot(session, project_id, contract.id)
            shot.status = ShotStatus.STORYBOARDING.value
            add_event(
                session,
                project_id,
                "shot.storyboarding",
                f"{contract.id} · {contract.title}: generating continuity reference",
                {"shot": contract.id, "seed": brief.seed + contract.sequence},
                agent="Visual Director",
                settings=self.settings,
            )
            checkpoint_shot_status(session, project_id, self.settings, shot)
            session.commit()
        async with self.render_gate:
            storyboard, voice = await asyncio.gather(
                self.provider.generate_storyboard(project_id, contract, brief.seed),
                self.provider.synthesize_voice(project_id, contract, brief.language),
            )
        with SessionLocal() as session:
            checkpoint_existing_json_object(
                session,
                project_id,
                self.settings,
                storyboard.provider_result_key,
            )
            checkpoint_existing_json_object(
                session,
                project_id,
                self.settings,
                storyboard.asset_checkpoint_key,
            )
            checkpoint_asset_key(session, project_id, self.settings, storyboard.object_key)
            checkpoint_existing_json_object(
                session,
                project_id,
                self.settings,
                voice.provider_result_key if voice else None,
            )
            checkpoint_existing_json_object(
                session,
                project_id,
                self.settings,
                voice.asset_checkpoint_key if voice else None,
            )
            checkpoint_asset_key(session, project_id, self.settings, voice.object_key if voice else None)
            shot = self._shot(session, project_id, contract.id)
            shot.storyboard_url = storyboard.public_url
            shot.audio_url = voice.public_url if voice else None
            shot.status = ShotStatus.STORYBOARDED.value
            add_event(
                session,
                project_id,
                "shot.storyboarded",
                f"{contract.id} reference locked; continuity contract is ready",
                {"shot": contract.id, "storyboard_model": storyboard.model, "voice_model": voice.model if voice else None},
                agent="Visual Director",
                settings=self.settings,
            )
            checkpoint_shot_status(session, project_id, self.settings, shot)
            session.commit()
            references = [
                character_references[character_id]
                for character_id in contract.characters
                if character_id in character_references
            ]
            return PreparedShot(shot.id, contract, storyboard, voice, references)

    async def _seed_i2v_from_previous_acceptance(
        self,
        project_id: str,
        prepared: PreparedShot,
        produced: list[ProducedShot],
    ) -> PreparedShot:
        if self.settings.provider_mode != "live":
            return prepared
        if prepared.contract.renderer != "wan_i2v" or not produced:
            return prepared
        previous = produced[-1]
        if not previous.quality.passed:
            return prepared
        seed_key = f"projects/{project_id}/shots/{prepared.contract.id}/continuity-seed-from-{previous.contract.id}.jpg"
        seed_path = self.store.path_for_key(seed_key)
        if not seed_path.exists():
            await asyncio.to_thread(
                extract_frame,
                previous.video.local_path,
                seed_path,
                min(max(previous.contract.duration_seconds / 2, 0.5), 2.0),
            )
        seed_asset = await asyncio.to_thread(self.store.put_file, seed_path, seed_key)
        continuity_seed = AssetResult(
            seed_asset.public_url,
            seed_asset.local_path,
            "DirectorGraph continuity seed",
            f"accepted-frame:{previous.contract.id}",
            object_key=seed_asset.key,
        )
        with SessionLocal() as session:
            shot = self._shot(session, project_id, prepared.contract.id)
            shot.storyboard_url = continuity_seed.public_url
            add_event(
                session,
                project_id,
                "shot.continuity.seeded",
                f"{prepared.contract.id} Wan i2v seed frame extracted from accepted {previous.contract.id}",
                {
                    "shot": prepared.contract.id,
                    "source_shot": previous.contract.id,
                    "object_key": continuity_seed.object_key,
                },
                agent="Visual Director",
                settings=self.settings,
            )
            checkpoint_asset_key(session, project_id, self.settings, continuity_seed.object_key)
            checkpoint_shot_status(session, project_id, self.settings, shot)
            session.commit()
        return PreparedShot(
            prepared.row_id,
            prepared.contract,
            continuity_seed,
            prepared.voice,
            prepared.references,
        )

    async def _produce_shot(
        self,
        project_id: str,
        prepared: PreparedShot,
        ledger: ProductionLedger,
    ) -> ProducedShot:
        contract = prepared.contract
        attempt = 1
        repair_instruction: str | None = None
        video: AssetResult | None = None
        report: QualityReport | None = None

        while attempt <= contract.max_retries + 1:
            operation = "render"
            with SessionLocal() as session:
                shot = session.get(Shot, prepared.row_id)
                if shot is None:
                    raise KeyError(prepared.row_id)
                shot.status = ShotStatus.RENDERING.value if attempt == 1 else ShotStatus.REPAIRING.value
                shot.attempts = attempt
                add_event(
                    session,
                    project_id,
                    "shot.rendering" if attempt == 1 else "shot.repairing",
                    f"{contract.id} {'rendering' if attempt == 1 else 'repairing'} with {contract.renderer} at {contract.resolution}",
                    {"shot": contract.id, "attempt": attempt, "renderer": contract.renderer, "salience": contract.salience},
                    agent="Production Manager",
                    settings=self.settings,
                )
                checkpoint_shot_status(session, project_id, self.settings, shot)
                session.commit()

            async with self.render_gate:
                if attempt == 1:
                    render_cost = estimate_video_cost(contract)
                    with SessionLocal() as session:
                        reserve_live_spend(
                            session,
                            project_id,
                            self.settings,
                            ledger,
                            reservation_id=f"{contract.id}-attempt-{attempt}-render",
                            amount_usd=render_cost,
                            category="video-render",
                            description=f"Estimated live render reservation for {contract.id} attempt {attempt}.",
                            payload={
                                "shot_id": contract.id,
                                "attempt": attempt,
                                "operation": "render",
                                "renderer": contract.renderer,
                                "resolution": contract.resolution,
                                "duration_seconds": contract.duration_seconds,
                            },
                            preserve_repair_reserve=True,
                        )
                    video = await self.provider.generate_video(
                        project_id,
                        contract,
                        prepared.storyboard,
                        prepared.voice,
                        prepared.references,
                        attempt,
                        repair_instruction,
                    )
                else:
                    assert video is not None and report is not None
                    local = report.repair_strategy == "local_edit"
                    render_cost = estimate_repair_cost(contract, local)
                    operation = "local_repair" if local else "regeneration"
                    with SessionLocal() as session:
                        reserve_live_spend(
                            session,
                            project_id,
                            self.settings,
                            ledger,
                            reservation_id=f"{contract.id}-attempt-{attempt}-{operation}",
                            amount_usd=render_cost,
                            category="video-repair",
                            description=f"Estimated live {operation} reservation for {contract.id} attempt {attempt}.",
                            payload={
                                "shot_id": contract.id,
                                "attempt": attempt,
                                "operation": operation,
                                "renderer": contract.renderer,
                                "resolution": contract.resolution,
                                "duration_seconds": contract.duration_seconds,
                                "repair_strategy": report.repair_strategy,
                            },
                        )
                    video = await self.provider.repair_video(
                        project_id,
                        contract,
                        video,
                        prepared.storyboard,
                        prepared.references,
                        report,
                        attempt,
                    )

            with SessionLocal() as session:
                checkpoint_provider_task(
                    session,
                    project_id,
                    self.settings,
                    contract,
                    video,
                    attempt,
                    operation=operation,
                )
                checkpoint_existing_json_object(
                    session,
                    project_id,
                    self.settings,
                    video.provider_result_key,
                )
                checkpoint_existing_json_object(
                    session,
                    project_id,
                    self.settings,
                    video.asset_checkpoint_key,
                )
                checkpoint_asset_key(session, project_id, self.settings, video.object_key)

            async with self.ledger_lock:
                ledger.video_seconds_generated += contract.duration_seconds
                if self.settings.provider_mode != "live":
                    ledger.estimated_cost_usd = round(ledger.estimated_cost_usd + render_cost, 4)
                if attempt > 1:
                    if report and report.repair_strategy == "local_edit":
                        ledger.local_repairs += 1
                    else:
                        ledger.full_regenerations += 1
                with SessionLocal() as session:
                    save_ledger(session, project_id, ledger, settings=self.settings)

            with SessionLocal() as session:
                shot = session.get(Shot, prepared.row_id)
                if shot is None:
                    raise KeyError(prepared.row_id)
                shot.video_url = video.public_url
                shot.status = ShotStatus.INSPECTING.value
                add_event(
                    session,
                    project_id,
                    "shot.inspecting",
                    f"Continuity Supervisor is checking {contract.id} against its machine-readable contract",
                    {"shot": contract.id, "attempt": attempt, "video_model": video.model},
                    agent="Continuity Supervisor",
                    settings=self.settings,
                )
                checkpoint_shot_status(session, project_id, self.settings, shot)
                session.commit()

            self._reserve_live_model_spend(
                project_id,
                ledger,
                reservation_id=f"{contract.id}-attempt-{attempt}-inspection",
                amount_usd=estimate_inspection_cost(),
                category="vision-inspection",
                description=f"Estimated live Qwen-VL inspection reservation for {contract.id} attempt {attempt}.",
                payload={
                    "operation": "inspection",
                    "shot_id": contract.id,
                    "attempt": attempt,
                },
            )
            inspection = await self.provider.inspect_video(contract, video, attempt)
            report = inspection.report
            with SessionLocal() as session:
                checkpoint_inspection(
                    session,
                    project_id,
                    self.settings,
                    contract,
                    report,
                    attempt,
                    model=inspection.model,
                    input_tokens=inspection.input_tokens,
                )
            async with self.ledger_lock:
                record_inspection(
                    ledger,
                    inspection.input_tokens,
                    include_cost=not self._uses_live_spend_reservations(),
                )
                with SessionLocal() as session:
                    save_ledger(session, project_id, ledger, settings=self.settings)

            with SessionLocal() as session:
                shot = session.get(Shot, prepared.row_id)
                if shot is None:
                    raise KeyError(prepared.row_id)
                shot.quality = report.model_dump()
                if report.passed:
                    shot.status = ShotStatus.ACCEPTED.value
                    shot.accepted = True
                    add_event(
                        session,
                        project_id,
                        "shot.accepted",
                        f"{contract.id} passed at {report.overall_score:.0%}",
                        {"shot": contract.id, "score": report.overall_score, "attempt": attempt},
                        agent="Continuity Supervisor",
                        settings=self.settings,
                    )
                else:
                    shot.accepted = False
                    add_event(
                        session,
                        project_id,
                        "shot.rejected",
                        f"{contract.id} failed continuity: {report.violations[0] if report.violations else 'quality threshold not met'}",
                        {
                            "shot": contract.id,
                            "score": report.overall_score,
                            "repair_strategy": report.repair_strategy,
                            "repair_instruction": report.repair_instruction,
                        },
                        agent="Continuity Supervisor",
                        settings=self.settings,
                    )
                checkpoint_shot_status(session, project_id, self.settings, shot)
                session.commit()

            if report.passed:
                async with self.ledger_lock:
                    ledger.video_seconds_accepted += contract.duration_seconds
                    with SessionLocal() as session:
                        save_ledger(session, project_id, ledger, settings=self.settings)
                return ProducedShot(prepared.row_id, contract, video, prepared.voice, report)

            async with self.ledger_lock:
                ledger.rejected_generation_seconds += contract.duration_seconds
                with SessionLocal() as session:
                    save_ledger(session, project_id, ledger, settings=self.settings)
            if attempt >= contract.max_retries + 1:
                break
            local = report.repair_strategy == "local_edit"
            repair_cost = estimate_repair_cost(contract, local)
            if not can_spend_on_repair(ledger, repair_cost):
                break
            repair_instruction = report.repair_instruction
            attempt += 1

        assert video is not None and report is not None
        with SessionLocal() as session:
            shot = session.get(Shot, prepared.row_id)
            if shot:
                shot.status = ShotStatus.FAILED.value
                checkpoint_shot_status(session, project_id, self.settings, shot)
                session.commit()
        return ProducedShot(prepared.row_id, contract, video, prepared.voice, report)

    async def _edit_project(
        self,
        project_id: str,
        brief: ProjectBrief,
        produced: list[ProducedShot],
    ) -> AssetResult:
        with SessionLocal() as session:
            set_project_status(
                session,
                project_id,
                ProjectStatus.EDITING,
                "Picture Editor is building the final timeline and caption track",
                agent="Picture Editor",
                settings=self.settings,
            )
        restored = await self._restore_final_asset(project_id)
        if restored:
            return restored
        key = f"projects/{project_id}/final/directorgraph-master.mp4"
        output = self.store.path_for_key(key)
        await asyncio.to_thread(
            compose_timeline,
            [item.video.local_path for item in produced],
            [item.voice.local_path if item.voice else None for item in produced],
            [item.contract for item in produced],
            output,
            brief,
        )
        final = self._asset_from_store(output, key, "ffmpeg", "DirectorGraph Picture Editor")
        if final.object_key:
            final.asset_checkpoint_key = checkpoint_final_asset_materialization_object(
                self.settings,
                project_id,
                object_key=final.object_key,
                model=final.model,
            )
        return final

    async def _restore_final_asset(self, project_id: str) -> AssetResult | None:
        materialized = load_final_asset_materialization_object(self.settings, project_id)
        if not materialized:
            return None
        object_key = str(materialized.get("object_key") or "")
        if not object_key:
            return None
        try:
            stored = await asyncio.to_thread(self.store.ensure_local, object_key)
        except FileNotFoundError:
            return None
        return AssetResult(
            stored.public_url,
            stored.local_path,
            "ffmpeg",
            str(materialized.get("model") or "DirectorGraph Picture Editor"),
            object_key=stored.key,
            asset_checkpoint_key=final_asset_materialization_key(project_id),
        )

    def _cached_asset_from_url(
        self,
        url: str | None,
        *,
        provider: str,
        model: str,
    ) -> AssetResult | None:
        path = self.store.local_path_from_url(url)
        if not path:
            return None
        return AssetResult(url or "", path, provider, model)

    async def _asset_from_materialization(
        self,
        materialized: dict | None,
        checkpoint_key: str,
        *,
        provider: str,
        model: str,
    ) -> AssetResult | None:
        if not materialized:
            return None
        object_key = str(materialized.get("object_key") or "")
        if not object_key:
            return None
        try:
            stored = await asyncio.to_thread(self.store.ensure_local, object_key)
        except (FileNotFoundError, RuntimeError, ValueError):
            return None
        usage = materialized.get("usage", {})
        return AssetResult(
            stored.public_url,
            stored.local_path,
            provider,
            str(materialized.get("model") or model),
            task_id=materialized.get("task_id"),
            usage=usage if isinstance(usage, dict) else {},
            object_key=stored.key,
            asset_checkpoint_key=checkpoint_key,
        )

    async def _restore_media_asset(
        self,
        checkpoint_key: str,
        fallback_url: str | None,
        *,
        provider: str = "cache",
        model: str,
    ) -> AssetResult | None:
        restored = await self._asset_from_materialization(
            load_media_asset_materialization_object(self.settings, checkpoint_key),
            checkpoint_key,
            provider=provider,
            model=model,
        )
        if restored:
            return restored
        return self._cached_asset_from_url(fallback_url, provider=provider, model=model)

    async def _restore_shot_video_asset(
        self,
        project_id: str,
        shot_id: str,
        attempt: int,
        fallback_url: str | None,
        *,
        provider: str = "cache",
        model: str,
    ) -> AssetResult | None:
        if attempt > 0:
            checkpoint_key = asset_materialization_key(project_id, shot_id, attempt)
            restored = await self._asset_from_materialization(
                load_asset_materialization_object(self.settings, project_id, shot_id, attempt),
                checkpoint_key,
                provider=provider,
                model=model,
            )
            if restored:
                return restored
        return self._cached_asset_from_url(fallback_url, provider=provider, model=model)

    def _asset_from_store(self, path: Path, key: str, provider: str, model: str) -> AssetResult:
        stored = self.store.put_file(path, key)
        return AssetResult(stored.public_url, stored.local_path, provider, model, object_key=stored.key)

    @staticmethod
    def _shot(session, project_id: str, shot_code: str) -> Shot:
        shot = session.scalar(select(Shot).where(Shot.project_id == project_id, Shot.shot_code == shot_code))
        if shot is None:
            raise KeyError(f"{project_id}:{shot_code}")
        return shot

    async def patch_project(
        self,
        project_id: str,
        instruction: str,
        affected_shot_ids: Iterable[str],
    ) -> dict[str, str]:
        """Semantic Patch Rendering: revise only impacted shots, then rebuild the master."""
        try:
            with SessionLocal() as session:
                project = get_project(session, project_id)
                if not project.plan:
                    raise RuntimeError("Project has no StoryIR to patch")
                brief = ProjectBrief.model_validate(project.brief)
                plan = StoryPlan.model_validate(project.plan)
                ledger = ProductionLedger.model_validate(project.ledger)
                selected = set(affected_shot_ids)
                if not selected:
                    terms = {word.strip(".,!?").lower() for word in instruction.split() if len(word) > 3}
                    ranked = sorted(
                        plan.shots,
                        key=lambda shot: len(terms & set((shot.title + " " + shot.action + " " + shot.narrative_objective).lower().split())),
                        reverse=True,
                    )
                    selected = {ranked[0].id if ranked else plan.shots[-1].id}
                selected &= {shot.id for shot in plan.shots}
                patched_rows = []
                for contract in plan.shots:
                    if contract.id in selected:
                        contract.video_prompt += f"\nSemantic revision: {instruction}"
                        row = self._shot(session, project_id, contract.id)
                        row.contract = contract.model_dump()
                        row.status = ShotStatus.PLANNED.value
                        row.attempts = 0
                        row.accepted = False
                        row.video_url = None
                        row.quality = None
                        patched_rows.append(row)
                project.plan = plan.model_dump()
                add_event(
                    session,
                    project_id,
                    "patch.impact.analysis",
                    f"Dependency analysis limited the revision to {', '.join(sorted(selected))}",
                    {"instruction": instruction, "affected_shots": sorted(selected)},
                    agent="Executive Showrunner",
                    settings=self.settings,
                )
                for index, row in enumerate(patched_rows):
                    checkpoint_shot_status(
                        session,
                        project_id,
                        self.settings,
                        row,
                        update_read_model=index == len(patched_rows) - 1,
                    )
                session.commit()

            reference_assets: dict[str, AssetResult] = {}
            for character in plan.characters:
                reference = await self._restore_media_asset(
                    character_asset_materialization_key(project_id, character.id),
                    character.reference_url,
                    model="locked-character-reference",
                )
                if reference:
                    reference_assets[character.id] = reference

            prepared: dict[str, PreparedShot] = {}
            with SessionLocal() as session:
                project = get_project(session, project_id)
                for contract in plan.shots:
                    if contract.id not in selected:
                        continue
                    row = self._shot(session, project_id, contract.id)
                    storyboard = await self._restore_media_asset(
                        storyboard_asset_materialization_key(project_id, contract.id),
                        row.storyboard_url,
                        model="locked-reference",
                    )
                    if not storyboard:
                        raise RuntimeError(f"Missing cached storyboard for {contract.id}")
                    voice = await self._restore_media_asset(
                        voice_asset_materialization_key(project_id, contract.id),
                        row.audio_url,
                        model="locked-voice",
                    )
                    references = [
                        reference_assets[character_id]
                        for character_id in contract.characters
                        if character_id in reference_assets
                    ]
                    prepared[contract.id] = PreparedShot(
                        row.id, contract, storyboard, voice, references
                    )

            replacements = await asyncio.gather(
                *[self._produce_shot(project_id, prepared[shot_id], ledger) for shot_id in sorted(selected)]
            )
            replacement_map = {item.contract.id: item for item in replacements}
            all_produced: list[ProducedShot] = []
            with SessionLocal() as session:
                project = get_project(session, project_id)
                for row in sorted(project.shots, key=lambda shot: shot.sequence):
                    if row.shot_code in replacement_map:
                        all_produced.append(replacement_map[row.shot_code])
                        continue
                    video = await self._restore_shot_video_asset(
                        project_id,
                        row.shot_code,
                        row.attempts or 0,
                        row.video_url,
                        model="accepted-clip",
                    )
                    if not video or not row.quality:
                        raise RuntimeError(f"Missing accepted cached asset for {row.shot_code}")
                    voice = await self._restore_media_asset(
                        voice_asset_materialization_key(project_id, row.shot_code),
                        row.audio_url,
                        model="locked-voice",
                    )
                    all_produced.append(
                        ProducedShot(
                            row.id,
                            ShotContract.model_validate(row.contract),
                            video,
                            voice,
                            QualityReport.model_validate(row.quality),
                        )
                    )
            all_produced.sort(key=lambda item: item.contract.sequence)
            final = await self._edit_project(project_id, brief, all_produced)
            with SessionLocal() as session:
                project = session.get(Project, project_id)
                if project:
                    project.final_video_url = final.public_url
                    project.status = ProjectStatus.COMPLETED.value
                    add_event(
                        session,
                        project_id,
                        "patch.completed",
                        "Semantic patch rendered and master rebuilt without touching unaffected shots",
                        {"affected_shots": sorted(selected), "final_object_key": final.object_key},
                        agent="Picture Editor",
                        settings=self.settings,
                    )
                    session.commit()
                    checkpoint_asset_key(session, project_id, self.settings, final.object_key)
                    checkpoint_existing_json_object(
                        session,
                        project_id,
                        self.settings,
                        final.asset_checkpoint_key,
                    )
                    checkpoint_final_manifest(session, project_id, self.settings)
            return {"project_id": project_id, "final_object_key": final.object_key}
        except Exception as exc:
            with SessionLocal() as session:
                set_project_error(session, project_id, str(exc), settings=self.settings)
            raise
        finally:
            await self.close()
