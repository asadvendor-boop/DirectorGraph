from __future__ import annotations

import asyncio
import hashlib
import json
from pathlib import Path

from app.clients.dashscope import DashScopeClient
from app.clients.ffmpeg import extract_frame
from app.clients.qwen import QwenClient
from app.clients.storage import AssetStore
from app.config import Settings
from app.core.story import SHOWRUNNER_SYSTEM_PROMPT, fallback_story_plan, story_user_prompt
from app.oss_repository import (
    asset_materialization_key,
    character_asset_materialization_key,
    character_provider_result_key,
    provider_result_key,
    safe_object_key,
    storyboard_asset_materialization_key,
    storyboard_provider_result_key,
    voice_asset_materialization_key,
    voice_provider_result_key,
)
from app.providers.base import AssetResult, InspectionResult, PlanResult, StudioProvider
from app.providers.errors import ProviderCallError
from app.providers.mock import MockStudioProvider
from app.schemas import (
    Character,
    GeneratedStoryPlan,
    ProjectBrief,
    QualityReport,
    ShotContract,
    StoryPlan,
)
from app.task_checkpoints import (
    checkpoint_asset_materialization_object,
    checkpoint_media_asset_materialization_object,
    checkpoint_media_provider_result_object,
    checkpoint_provider_result_object,
    checkpoint_provider_task_object,
    load_asset_materialization_object,
    load_media_asset_materialization_object,
    load_provider_result_object,
    load_provider_task_object,
)


class LiveStudioProvider(StudioProvider):
    """Alibaba Cloud Model Studio adapter for Qwen, Wan, HappyHorse, and Qwen-TTS."""

    def __init__(self, settings: Settings, store: AssetStore):
        if not settings.live_ready:
            raise RuntimeError("Live mode requires DASHSCOPE_API_KEY or QWEN_API_KEY")
        if not settings.oss_ready and "localhost" in settings.public_media_base_url:
            raise RuntimeError(
                "Live mode needs OSS or a publicly reachable PUBLIC_MEDIA_BASE_URL so video models can fetch assets."
            )
        self.settings = settings
        self.store = store
        self.qwen = QwenClient(settings)
        self.dashscope = DashScopeClient(settings)
        self.local_fallback = MockStudioProvider(settings, store)
        self.image_gate = asyncio.Semaphore(1)

    async def close(self) -> None:
        await self.dashscope.close()
        await self.qwen.close()

    @staticmethod
    def _fallback_reason(exc: ProviderCallError) -> str:
        return str(exc.code or exc.category.value)

    def _label_fallback_asset(
        self,
        result: AssetResult,
        *,
        attempted_model: str,
        exc: ProviderCallError,
    ) -> AssetResult:
        reason = self._fallback_reason(exc)
        result.provider = "local fallback after Alibaba Cloud Model Studio rejection"
        result.model = f"{attempted_model}+local-fallback:{reason}"
        result.usage = {
            **(result.usage or {}),
            "degraded": True,
            "degradation_reason": reason,
            "attempted_provider": "Alibaba Cloud Model Studio",
            "attempted_model": attempted_model,
        }
        return result

    @staticmethod
    def _coerce_story_plan(value: object) -> StoryPlan:
        if isinstance(value, StoryPlan):
            return StoryPlan.model_validate(value.model_dump())
        return StoryPlan.model_validate(value)

    @staticmethod
    def _validate_story_contract(brief: ProjectBrief, plan: StoryPlan) -> None:
        max_shots = brief.max_shots or (3 if brief.duration_seconds < 21 else 8)
        min_shots = 2 if max_shots <= 3 else 6
        if not min_shots <= len(plan.shots) <= max_shots:
            raise ValueError(f"Model plan violated the {min_shots}-to-{max_shots}-shot contract")
        if sum(shot.duration_seconds for shot in plan.shots) != brief.duration_seconds:
            raise ValueError("Model plan violated the exact-duration contract")
        character_ids = {character.id for character in plan.characters}
        if not character_ids:
            raise ValueError("Model plan omitted reusable character references")
        if any(not shot.characters for shot in plan.shots):
            raise ValueError("Model plan omitted per-shot character bindings")
        if any(not (shot.dialogue or shot.narration) for shot in plan.shots):
            raise ValueError("Model plan omitted per-shot dialogue or narration")
        unknown_characters = {
            character_id
            for shot in plan.shots
            for character_id in shot.characters
            if character_id not in character_ids
        }
        if unknown_characters:
            raise ValueError("Model plan referenced unknown character IDs")
        GeneratedStoryPlan.model_validate(plan.model_dump())

    @staticmethod
    def _can_repair_story_contract(exc: Exception) -> bool:
        message = str(exc)
        return (
            "per-shot character bindings" in message
            or "unknown character IDs" in message
            or "characters" in message
            or "dialogue or narration" in message
        )

    @staticmethod
    def _story_contract_repair_prompt(brief: ProjectBrief, plan: StoryPlan, reason: str) -> str:
        return f"""Repair only invalid shot.characters bindings and missing shot dialogue/narration in this StoryIR.

Reason the previous StoryIR failed validation: {reason}

Allowed changes:
- If any shot.characters value is empty or invalid, fill it with one or more existing character IDs.
- Use only character IDs already present in the top-level characters array.
- If any shot lacks dialogue or narration, add one short English dialogue or narration line, five words or fewer.
- Keep shot IDs, sequence, beat IDs, durations, title, action, location, camera, continuity, prompts, and all other fields unchanged.
- Do not add, remove, rename, or reorder characters, beats, or shots.
- Do not invent new character IDs.
- Return the complete StoryIR JSON object only.

Brief title: {brief.title}
Required duration seconds: {brief.duration_seconds}
Previous StoryIR:
{json.dumps(plan.model_dump(mode="json"), ensure_ascii=False)}"""

    async def plan_story(self, brief: ProjectBrief) -> PlanResult:
        try:
            result = await self.qwen.structured(
                model=self.settings.qwen_story_model,
                system=SHOWRUNNER_SYSTEM_PROMPT,
                user=story_user_prompt(brief),
                response_model=StoryPlan,
                max_tokens=9000,
            )
            plan = self._coerce_story_plan(result.value)
            repair_attempted = False
            repair_reason = None
            planning_path = "live_qwen_character_bound"
            try:
                self._validate_story_contract(brief, plan)
            except Exception as validation_exc:
                if not self._can_repair_story_contract(validation_exc):
                    raise
                repair_attempted = True
                repair_reason = (
                    "character_binding_reprompt"
                    if "dialogue or narration" not in str(validation_exc)
                    else "story_contract_reprompt"
                )
                repaired = await self.qwen.structured(
                    model=self.settings.qwen_story_model,
                    system=SHOWRUNNER_SYSTEM_PROMPT,
                    user=self._story_contract_repair_prompt(brief, plan, str(validation_exc)),
                    response_model=GeneratedStoryPlan,
                    max_tokens=9000,
                )
                plan = self._coerce_story_plan(repaired.value)
                self._validate_story_contract(brief, plan)
                planning_path = "live_qwen_repaired"
                result = type(
                    "RepairedStructuredResult",
                    (),
                    {
                        "model": f"{repaired.model}+character-binding-repair",
                        "input_tokens": result.input_tokens + repaired.input_tokens,
                        "output_tokens": result.output_tokens + repaired.output_tokens,
                    },
                )()
            character_bound_shots = sum(1 for shot in plan.shots if shot.characters)
            return PlanResult(
                plan,
                result.model,
                result.input_tokens,
                result.output_tokens,
                planning_path=planning_path,
                character_bound_shots=character_bound_shots,
                plan_repair_attempted=repair_attempted,
                plan_repair_reason=repair_reason,
            )
        except ProviderCallError as exc:
            # The fallback remains visible in events and manifests; it is not live planning evidence.
            plan = fallback_story_plan(brief)
            return PlanResult(
                plan,
                f"{self.settings.qwen_story_model}+validated-fallback",
                degraded=True,
                degradation_reason=exc.category.value,
                planning_path="validated_fallback",
                character_bound_shots=sum(1 for shot in plan.shots if shot.characters),
            )
        except Exception as exc:
            # The fallback remains visible in events and manifests; it is not live planning evidence.
            plan = fallback_story_plan(brief)
            return PlanResult(
                plan,
                f"{self.settings.qwen_story_model}+validated-fallback",
                degraded=True,
                degradation_reason=type(exc).__name__,
                planning_path="validated_fallback",
                character_bound_shots=sum(1 for shot in plan.shots if shot.characters),
            )

    @staticmethod
    def _image_size(ratio: str) -> str:
        return {"9:16": "960*1696", "16:9": "1696*960", "1:1": "1280*1280"}[ratio]

    @staticmethod
    def _video_size(ratio: str, resolution: str) -> str:
        if resolution == "1080P":
            return {"9:16": "1080*1920", "16:9": "1920*1080", "1:1": "1440*1440"}[ratio]
        return {"9:16": "720*1280", "16:9": "1280*720", "1:1": "960*960"}[ratio]

    @staticmethod
    def _tts_language(language: str) -> str:
        supported = {
            "chinese": "Chinese",
            "english": "English",
            "german": "German",
            "italian": "Italian",
            "portuguese": "Portuguese",
            "spanish": "Spanish",
            "japanese": "Japanese",
            "korean": "Korean",
            "french": "French",
            "russian": "Russian",
        }
        return supported.get(language.strip().lower(), "English")

    async def _generate_image_with_pacing(
        self,
        *,
        prompt: str,
        negative_prompt: str,
        size: str,
        seed: int,
    ):
        async with self.image_gate:
            try:
                return await self.dashscope.generate_image(
                    prompt=prompt,
                    negative_prompt=negative_prompt,
                    size=size,
                    seed=seed,
                )
            finally:
                if self.settings.dashscope_image_pace_seconds > 0:
                    await asyncio.sleep(self.settings.dashscope_image_pace_seconds)

    async def _restore_materialized_video(
        self,
        project_id: str,
        contract: ShotContract,
        attempt: int,
    ) -> AssetResult | None:
        materialized = load_asset_materialization_object(
            self.settings,
            project_id,
            contract.id,
            attempt,
        )
        if not materialized:
            return None
        object_key = str(materialized.get("object_key") or "")
        if not object_key:
            return None
        try:
            stored = await asyncio.to_thread(self.store.ensure_local, object_key)
        except FileNotFoundError:
            return None
        result = load_provider_result_object(self.settings, project_id, contract.id, attempt)
        return AssetResult(
            stored.public_url,
            stored.local_path,
            "Alibaba Cloud Model Studio",
            str(materialized.get("model") or (result or {}).get("model") or "unknown"),
            task_id=materialized.get("task_id") or (result or {}).get("task_id"),
            usage=(result or {}).get("usage", {}),
            object_key=stored.key,
            provider_result_key=provider_result_key(project_id, contract.id, attempt) if result else None,
            asset_checkpoint_key=asset_materialization_key(project_id, contract.id, attempt),
        )

    async def _restore_materialized_media_asset(self, checkpoint_key: str) -> AssetResult | None:
        materialized = load_media_asset_materialization_object(self.settings, checkpoint_key)
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
            "Alibaba Cloud Model Studio",
            str(materialized.get("model") or "unknown"),
            usage=materialized.get("usage", {}),
            object_key=stored.key,
            asset_checkpoint_key=checkpoint_key,
        )

    async def _video_from_task(
        self,
        project_id: str,
        contract: ShotContract,
        attempt: int,
        *,
        model: str,
        payload: dict,
        operation: str,
        object_key: str,
    ) -> AssetResult:
        restored = await self._restore_materialized_video(project_id, contract, attempt)
        if restored:
            return restored

        task = load_provider_task_object(self.settings, project_id, contract.id, attempt)
        if task:
            task_id = str(task["task_id"])
            model = str(task.get("model") or model)
        else:
            task_id = await self.dashscope.submit_video(payload=payload, model=model)
            checkpoint_provider_task_object(
                self.settings,
                project_id,
                contract.id,
                attempt,
                model=model,
                task_id=task_id,
                operation=operation,
                renderer=contract.renderer,
                resolution=contract.resolution,
                duration_seconds=contract.duration_seconds,
            )

        remote = await self.dashscope.poll_video(task_id, model=model)
        result_key = checkpoint_provider_result_object(
            self.settings,
            project_id,
            contract.id,
            attempt,
            model=model,
            task_id=task_id,
            operation=operation,
            remote_url=remote.url,
            usage=remote.usage or {},
        )
        stored = await self.store.save_remote(remote.url, object_key)
        asset_checkpoint_key = checkpoint_asset_materialization_object(
            self.settings,
            project_id,
            contract.id,
            attempt,
            object_key=stored.key,
            model=model,
            task_id=task_id,
            operation=operation,
        )
        return AssetResult(
            stored.public_url,
            stored.local_path,
            "Alibaba Cloud Model Studio",
            model,
            task_id=task_id,
            usage=remote.usage or {},
            object_key=stored.key,
            provider_result_key=result_key,
            asset_checkpoint_key=asset_checkpoint_key,
        )

    async def generate_character_reference(
        self, project_id: str, character: Character, seed: int
    ) -> AssetResult:
        checkpoint_key = character_asset_materialization_key(project_id, character.id)
        restored = await self._restore_materialized_media_asset(checkpoint_key)
        if restored:
            return restored
        try:
            remote = await self._generate_image_with_pacing(
                prompt=(
                    f"{character.reference_prompt}. Single main character only, full-body identity sheet, "
                    "neutral studio background, front three-quarter view, no text, no logo. Preserve these "
                    f"canonical details: appearance={character.appearance}; wardrobe={character.wardrobe}."
                ),
                negative_prompt=(
                    "multiple people, duplicate character, alternate wardrobe, identity drift, text, logo, "
                    "watermark, cropped face, deformed anatomy"
                ),
                size="1024*1024",
                seed=seed,
            )
        except ProviderCallError as exc:
            fallback = await self.local_fallback.generate_character_reference(project_id, character, seed)
            return self._label_fallback_asset(fallback, attempted_model=self.settings.wan_image_model, exc=exc)
        result_key = checkpoint_media_provider_result_object(
            self.settings,
            character_provider_result_key(project_id, character.id),
            project_id=project_id,
            asset_kind="character_reference",
            asset_id=character.id,
            model=remote.model,
            task_id=remote.task_id,
            remote_url=remote.url,
            usage=remote.usage or {},
        )
        stored = await self.store.save_remote(
            remote.url, f"projects/{project_id}/characters/{character.id}.png"
        )
        asset_checkpoint_key = checkpoint_media_asset_materialization_object(
            self.settings,
            checkpoint_key,
            project_id=project_id,
            asset_kind="character_reference",
            object_key=stored.key,
            model=remote.model,
            usage=remote.usage or {},
        )
        return AssetResult(
            stored.public_url,
            stored.local_path,
            "Alibaba Cloud Model Studio",
            remote.model,
            task_id=remote.task_id,
            usage=remote.usage or {},
            object_key=stored.key,
            provider_result_key=result_key,
            asset_checkpoint_key=asset_checkpoint_key,
        )

    async def generate_storyboard(
        self, project_id: str, contract: ShotContract, seed: int
    ) -> AssetResult:
        checkpoint_key = storyboard_asset_materialization_key(project_id, contract.id)
        restored = await self._restore_materialized_media_asset(checkpoint_key)
        if restored:
            return restored
        try:
            remote = await self._generate_image_with_pacing(
                prompt=contract.storyboard_prompt,
                negative_prompt=contract.negative_prompt,
                size=self._image_size(contract.aspect_ratio),
                seed=seed + contract.sequence,
            )
        except ProviderCallError as exc:
            fallback = await self.local_fallback.generate_storyboard(project_id, contract, seed)
            return self._label_fallback_asset(fallback, attempted_model=self.settings.wan_image_model, exc=exc)
        result_key = checkpoint_media_provider_result_object(
            self.settings,
            storyboard_provider_result_key(project_id, contract.id),
            project_id=project_id,
            asset_kind="storyboard",
            asset_id=contract.id,
            model=remote.model,
            task_id=remote.task_id,
            remote_url=remote.url,
            usage=remote.usage or {},
        )
        stored = await self.store.save_remote(
            remote.url, f"projects/{project_id}/shots/{contract.id}/storyboard.png"
        )
        asset_checkpoint_key = checkpoint_media_asset_materialization_object(
            self.settings,
            checkpoint_key,
            project_id=project_id,
            asset_kind="storyboard",
            object_key=stored.key,
            model=remote.model,
            usage=remote.usage or {},
        )
        return AssetResult(
            stored.public_url,
            stored.local_path,
            "Alibaba Cloud Model Studio",
            remote.model,
            task_id=remote.task_id,
            usage=remote.usage or {},
            object_key=stored.key,
            provider_result_key=result_key,
            asset_checkpoint_key=asset_checkpoint_key,
        )

    async def _generate_repair_storyboard(
        self,
        project_id: str,
        contract: ShotContract,
        report: QualityReport,
        attempt: int,
    ) -> AssetResult:
        checkpoint_key = safe_object_key(
            "projects",
            project_id,
            "shots",
            contract.id,
            f"repair-{attempt}-storyboard-materialization.json",
        )
        restored = await self._restore_materialized_media_asset(checkpoint_key)
        if restored:
            return restored
        instruction = (
            report.repair_instruction
            or "Regenerate the first frame to satisfy the failed composition, lighting, and continuity dimensions."
        )
        prompt = "\n".join(
            [
                contract.storyboard_prompt,
                f"Qwen-VL repair direction: {instruction}",
                (
                    "Create a corrected still first frame for the next Wan reference-video render. "
                    "Fix composition, subject placement, framing, lighting, and visible continuity at the keyframe level. "
                    "Keep the same character identity, wardrobe, location, aspect ratio, and safe no-text/no-logo constraints."
                ),
            ]
        )
        remote = await self._generate_image_with_pacing(
            prompt=prompt,
            negative_prompt=contract.negative_prompt,
            size=self._image_size(contract.aspect_ratio),
            seed=attempt * 10_000 + contract.sequence,
        )
        result_key = checkpoint_media_provider_result_object(
            self.settings,
            safe_object_key(
                "projects",
                project_id,
                "shots",
                contract.id,
                f"repair-{attempt}-storyboard-provider-result.json",
            ),
            project_id=project_id,
            asset_kind="repair_storyboard",
            asset_id=f"{contract.id}:attempt-{attempt}",
            model=remote.model,
            task_id=remote.task_id,
            remote_url=remote.url,
            usage=remote.usage or {},
        )
        stored = await self.store.save_remote(
            remote.url, f"projects/{project_id}/shots/{contract.id}/repair-{attempt}-storyboard.png"
        )
        asset_checkpoint_key = checkpoint_media_asset_materialization_object(
            self.settings,
            checkpoint_key,
            project_id=project_id,
            asset_kind="repair_storyboard",
            object_key=stored.key,
            model=remote.model,
            usage=remote.usage or {},
        )
        return AssetResult(
            stored.public_url,
            stored.local_path,
            "Alibaba Cloud Model Studio",
            remote.model,
            task_id=remote.task_id,
            usage=remote.usage or {},
            object_key=stored.key,
            provider_result_key=result_key,
            asset_checkpoint_key=asset_checkpoint_key,
        )

    async def synthesize_voice(
        self, project_id: str, contract: ShotContract, language: str
    ) -> AssetResult | None:
        text = (contract.dialogue or contract.narration or "").replace("\n", " ").strip()
        if not text:
            return None
        checkpoint_key = voice_asset_materialization_key(project_id, contract.id)
        restored = await self._restore_materialized_media_asset(checkpoint_key)
        if restored:
            return restored
        try:
            remote = await self.dashscope.synthesize_speech(
                text=text,
                language=self._tts_language(language),
                instructions=f"Perform with {contract.emotion}. Fit the allocated shot duration.",
            )
        except ProviderCallError:
            return None
        result_key = checkpoint_media_provider_result_object(
            self.settings,
            voice_provider_result_key(project_id, contract.id),
            project_id=project_id,
            asset_kind="dialogue",
            asset_id=contract.id,
            model=remote.model,
            task_id=remote.task_id,
            remote_url=remote.url,
            usage=remote.usage or {},
        )
        stored = await self.store.save_remote(
            remote.url, f"projects/{project_id}/shots/{contract.id}/dialogue.wav"
        )
        asset_checkpoint_key = checkpoint_media_asset_materialization_object(
            self.settings,
            checkpoint_key,
            project_id=project_id,
            asset_kind="dialogue",
            object_key=stored.key,
            model=remote.model,
            usage=remote.usage or {},
        )
        return AssetResult(
            stored.public_url,
            stored.local_path,
            "Alibaba Cloud Model Studio",
            remote.model,
            task_id=remote.task_id,
            usage=remote.usage or {},
            object_key=stored.key,
            provider_result_key=result_key,
            asset_checkpoint_key=asset_checkpoint_key,
        )

    async def generate_video(
        self,
        project_id: str,
        contract: ShotContract,
        storyboard: AssetResult,
        audio: AssetResult | None,
        references: list[AssetResult],
        attempt: int,
        repair_instruction: str | None = None,
    ) -> AssetResult:
        prompt = contract.video_prompt + (
            f"\nCorrection: {repair_instruction}" if repair_instruction else ""
        )
        if contract.renderer == "happyhorse_t2v":
            model = self.settings.happyhorse_video_model
            payload = {
                "input": {"prompt": prompt},
                "parameters": {
                    "resolution": contract.resolution,
                    "ratio": contract.aspect_ratio,
                    "duration": contract.duration_seconds,
                },
            }
        elif contract.renderer == "wan_r2v" and references:
            model = self.settings.wan_reference_model
            character_media = references[:4]
            media = [{"type": "first_frame", "url": storyboard.public_url}]
            media.extend({"type": "reference_image", "url": item.public_url} for item in character_media)
            mapped = "; ".join(
                f"Image {index + 2} is the single-character reference for {character_id}"
                for index, character_id in enumerate(contract.characters[: len(character_media)])
            )
            r2v_prompt = (
                f"Image 1 is the locked first-frame composition. {mapped}. "
                f"Animate Image 1 using the referenced character identity. {prompt}"
            )
            payload = {
                "input": {
                    "prompt": r2v_prompt,
                    "media": media,
                },
                "parameters": {
                    "resolution": contract.resolution,
                    "ratio": contract.aspect_ratio,
                    "duration": min(contract.duration_seconds, 10),
                    "prompt_extend": True,
                    "watermark": False,
                },
            }
        else:
            model = self.settings.wan_video_model
            payload = {
                "input": {
                    "prompt": prompt,
                    "img_url": storyboard.public_url,
                },
                "parameters": {
                    "resolution": contract.resolution,
                    "duration": contract.duration_seconds,
                    "prompt_extend": True,
                },
            }
        try:
            return await self._video_from_task(
                project_id,
                contract,
                attempt,
                model=model,
                payload=payload,
                operation="render",
                object_key=f"projects/{project_id}/shots/{contract.id}/attempt-{attempt}.mp4",
            )
        except ProviderCallError as exc:
            fallback = await self.local_fallback.generate_video(
                project_id,
                contract,
                storyboard,
                audio,
                references,
                attempt,
                repair_instruction=repair_instruction,
            )
            return self._label_fallback_asset(fallback, attempted_model=model, exc=exc)

    async def inspect_video(
        self, contract: ShotContract, video: AssetResult, attempt: int
    ) -> InspectionResult:
        try:
            frame_key = self._inspection_frame_key(video, contract, attempt)
            frame_path = self.store.path_for_key(frame_key)
            if not frame_path.exists():
                await asyncio.to_thread(
                    extract_frame,
                    video.local_path,
                    frame_path,
                    min(max(contract.duration_seconds / 2, 0.5), 2.0),
                )
            frame = await asyncio.to_thread(self.store.put_file, frame_path, frame_key)
            result = await self.qwen.inspect_image(
                image_url=frame.public_url,
                contract_json=json.dumps(contract.model_dump(), ensure_ascii=False),
                response_model=QualityReport,
                attempt=attempt,
            )
            report = QualityReport.model_validate(result.value)
            report.evaluator_model = result.model
            report.attempt = attempt
            report.passed = report.passed and report.overall_score >= contract.quality_threshold
            if not report.passed and report.repair_strategy == "none":
                report.repair_strategy = "regenerate"
                report.repair_instruction = "Regenerate to satisfy all failed contract dimensions."
            if not report.passed and report.repair_strategy == "local_edit":
                report.repair_strategy = "regenerate"
                report.repair_instruction = (
                    report.repair_instruction
                    or "Regenerate the shot while preserving the accepted storyboard continuity."
                )
            return InspectionResult(
                report, result.model, result.input_tokens, result.output_tokens
            )
        except ProviderCallError as exc:
            fallback = await self.local_fallback.inspect_video(contract, video, attempt)
            fallback.model = f"{self.settings.qwen_vision_model}+local-fallback:{self._fallback_reason(exc)}"
            fallback.report.evaluator_model = fallback.model
            return fallback

    @staticmethod
    def _inspection_frame_key(video: AssetResult, contract: ShotContract, attempt: int) -> str:
        if video.object_key and video.object_key.startswith("projects/"):
            source = Path(video.object_key)
            return str(source.with_name(f"{source.stem}-inspection-frame.jpg"))
        digest = hashlib.sha256(f"{video.public_url}:{contract.id}:{attempt}".encode()).hexdigest()[:16]
        return f"inspection-frames/{contract.id}/attempt-{attempt}-{digest}.jpg"

    async def repair_video(
        self,
        project_id: str,
        contract: ShotContract,
        video: AssetResult,
        storyboard: AssetResult,
        references: list[AssetResult],
        report: QualityReport,
        attempt: int,
    ) -> AssetResult:
        if report.repair_strategy != "local_edit":
            if contract.renderer in {"wan_r2v", "wan_i2v"}:
                storyboard = await self._generate_repair_storyboard(
                    project_id,
                    contract,
                    report,
                    attempt,
                )
            return await self.generate_video(
                project_id,
                contract,
                storyboard,
                None,
                references,
                attempt,
                repair_instruction=report.repair_instruction,
            )
        model = self.settings.happyhorse_edit_model
        media = [
            {"type": "video", "url": video.public_url},
            {"type": "reference_image", "url": storyboard.public_url},
        ]
        media.extend(
            {"type": "reference_image", "url": item.public_url}
            for item in references[:4]
        )
        payload = {
            "input": {
                "prompt": report.repair_instruction
                or "Correct the continuity defect while preserving everything else.",
                "media": media,
            },
            "parameters": {"resolution": contract.resolution},
        }
        try:
            return await self._video_from_task(
                project_id,
                contract,
                attempt,
                model=model,
                payload=payload,
                operation="local_repair",
                object_key=f"projects/{project_id}/shots/{contract.id}/repair-{attempt}.mp4",
            )
        except ProviderCallError as exc:
            fallback = await self.local_fallback.repair_video(
                project_id,
                contract,
                video,
                storyboard,
                references,
                report,
                attempt,
            )
            return self._label_fallback_asset(fallback, attempted_model=model, exc=exc)
