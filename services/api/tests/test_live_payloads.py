import asyncio
from copy import deepcopy
from pathlib import Path

import pytest

from app.clients.dashscope import DashScopeClient, RemoteResult
from app.clients.storage import AssetStore, StoredAsset
from app.config import Settings
from app.core.budget import route_and_budget
from app.core.story import fallback_story_plan
from app.oss_repository import (
    LocalOssRepository,
    OssNotFoundError,
    asset_materialization_key,
    character_asset_materialization_key,
    provider_result_key,
    provider_task_key,
    storyboard_asset_materialization_key,
    storyboard_provider_result_key,
    voice_asset_materialization_key,
    voice_provider_result_key,
)
from app.providers.base import AssetResult
from app.providers.errors import ProviderCallError, ProviderErrorCategory
from app.providers.live import LiveStudioProvider
from app.schemas import ProjectBrief, QualityReport
from app.task_checkpoints import (
    checkpoint_asset_materialization_object,
    checkpoint_media_asset_materialization_object,
)


@pytest.mark.asyncio
async def test_live_story_fallback_is_marked_degraded(tmp_path: Path):
    settings = Settings(
        provider_mode="live",
        dashscope_api_key="test-key",
        public_media_base_url="https://assets.example.invalid/media",
        media_root=tmp_path / "media",
        oss_repository_root=tmp_path / "oss",
    )
    provider = LiveStudioProvider(settings, AssetStore(settings))
    await provider.qwen.close()
    await provider.dashscope.close()

    class BrokenQwen:
        async def structured(self, **kwargs):
            raise TimeoutError("provider timed out")

        async def close(self):
            return None

    provider.qwen = BrokenQwen()  # type: ignore[assignment]

    result = await provider.plan_story(
        ProjectBrief(
            title="Degraded story",
            premise="A courier robot still needs a visible fallback plan when live planning fails.",
            duration_seconds=21,
        )
    )

    assert result.degraded
    assert result.degradation_reason == "TimeoutError"
    assert result.model.endswith("+validated-fallback")
    await provider.close()


@pytest.mark.asyncio
async def test_live_story_without_shot_character_bindings_falls_back_to_reference_ready_plan(tmp_path: Path):
    settings = Settings(
        provider_mode="live",
        dashscope_api_key="test-key",
        public_media_base_url="https://assets.example.invalid/media",
        media_root=tmp_path / "media",
        oss_repository_root=tmp_path / "oss",
    )
    provider = LiveStudioProvider(settings, AssetStore(settings))
    await provider.qwen.close()
    await provider.dashscope.close()
    brief = ProjectBrief(
        title="No character bindings",
        premise="A lone courier crosses a rainy platform, but the model omits per-shot character IDs.",
        duration_seconds=24,
        aspect_ratio="16:9",
        max_shots=6,
        required_prop=None,
    )
    model_plan = fallback_story_plan(brief)
    model_plan.shots = [deepcopy(shot) for shot in model_plan.shots + model_plan.shots[:3]]
    for index, shot in enumerate(model_plan.shots, 1):
        shot.id = f"S{index:02d}"
        shot.sequence = index
        shot.duration_seconds = 4
        shot.characters = []

    class NoCharacterQwen:
        async def structured(self, **kwargs):
            return type(
                "Result",
                (),
                {
                    "value": model_plan.model_dump(),
                    "model": "qwen-plus",
                    "input_tokens": 10,
                    "output_tokens": 20,
                },
            )()

        async def close(self):
            return None

    provider.qwen = NoCharacterQwen()  # type: ignore[assignment]

    result = await provider.plan_story(brief)

    assert result.degraded
    assert result.degradation_reason == "ValueError"
    assert result.planning_path == "validated_fallback"
    assert result.plan.shots
    assert all(shot.characters for shot in result.plan.shots)
    await provider.close()


@pytest.mark.asyncio
async def test_live_story_repairs_missing_character_bindings_before_fallback(tmp_path: Path):
    settings = Settings(
        provider_mode="live",
        dashscope_api_key="test-key",
        public_media_base_url="https://assets.example.invalid/media",
        media_root=tmp_path / "media",
        oss_repository_root=tmp_path / "oss",
    )
    provider = LiveStudioProvider(settings, AssetStore(settings))
    await provider.qwen.close()
    await provider.dashscope.close()
    brief = ProjectBrief(
        title="Repair missing bindings",
        premise="The model writes a good plan but omits nested shot character IDs.",
        duration_seconds=24,
        aspect_ratio="16:9",
        max_shots=6,
        required_prop="red paper crane",
    )
    invalid_plan = fallback_story_plan(brief)
    for shot in invalid_plan.shots:
        shot.characters = []
    repaired_plan = fallback_story_plan(brief)

    class RepairingQwen:
        def __init__(self):
            self.calls = []

        async def structured(self, **kwargs):
            self.calls.append(kwargs["response_model"].__name__)
            value = invalid_plan.model_dump() if len(self.calls) == 1 else repaired_plan.model_dump()
            return type(
                "Result",
                (),
                {
                    "value": value,
                    "model": "qwen-plus",
                    "input_tokens": 10,
                    "output_tokens": 20,
                },
            )()

        async def close(self):
            return None

    qwen = RepairingQwen()
    provider.qwen = qwen  # type: ignore[assignment]

    result = await provider.plan_story(brief)

    assert not result.degraded
    assert result.planning_path == "live_qwen_repaired"
    assert result.plan_repair_attempted is True
    assert result.plan_repair_reason == "character_binding_reprompt"
    assert qwen.calls == ["StoryPlan", "GeneratedStoryPlan"]
    assert all(shot.characters for shot in result.plan.shots)
    await provider.close()


@pytest.mark.asyncio
async def test_live_story_repairs_missing_audio_lines_before_fallback(tmp_path: Path):
    settings = Settings(
        provider_mode="live",
        dashscope_api_key="test-key",
        public_media_base_url="https://assets.example.invalid/media",
        media_root=tmp_path / "media",
        oss_repository_root=tmp_path / "oss",
    )
    provider = LiveStudioProvider(settings, AssetStore(settings))
    await provider.qwen.close()
    await provider.dashscope.close()
    brief = ProjectBrief(
        title="Repair missing narration",
        premise="A quiet autumn walk needs one short narration line for every rendered shot.",
        duration_seconds=24,
        aspect_ratio="16:9",
        max_shots=6,
        required_prop=None,
    )
    invalid_plan = fallback_story_plan(brief)
    for shot in invalid_plan.shots:
        shot.dialogue = None
        shot.narration = None
    repaired_plan = fallback_story_plan(brief)
    for index, shot in enumerate(repaired_plan.shots, 1):
        shot.dialogue = None
        shot.narration = f"Autumn step {index}."

    class RepairingQwen:
        def __init__(self):
            self.prompts = []

        async def structured(self, **kwargs):
            self.prompts.append(kwargs["user"])
            value = invalid_plan.model_dump() if len(self.prompts) == 1 else repaired_plan.model_dump()
            return type(
                "Result",
                (),
                {
                    "value": value,
                    "model": "qwen-plus",
                    "input_tokens": 10,
                    "output_tokens": 20,
                },
            )()

        async def close(self):
            return None

    qwen = RepairingQwen()
    provider.qwen = qwen  # type: ignore[assignment]

    result = await provider.plan_story(brief)

    assert not result.degraded
    assert result.planning_path == "live_qwen_repaired"
    assert result.plan_repair_attempted is True
    assert result.plan_repair_reason == "story_contract_reprompt"
    assert "dialogue or narration" in qwen.prompts[1]
    assert all((shot.dialogue or shot.narration) for shot in result.plan.shots)
    await provider.close()


@pytest.mark.asyncio
async def test_live_story_unknown_character_ids_are_rejected_if_repair_fails(tmp_path: Path):
    settings = Settings(
        provider_mode="live",
        dashscope_api_key="test-key",
        public_media_base_url="https://assets.example.invalid/media",
        media_root=tmp_path / "media",
        oss_repository_root=tmp_path / "oss",
    )
    provider = LiveStudioProvider(settings, AssetStore(settings))
    await provider.qwen.close()
    await provider.dashscope.close()
    brief = ProjectBrief(
        title="Reject unknown IDs",
        premise="The model uses character IDs that do not exist in the reusable character list.",
        duration_seconds=24,
        aspect_ratio="16:9",
        max_shots=6,
        required_prop="red paper crane",
    )
    bad_plan = fallback_story_plan(brief)
    for shot in bad_plan.shots:
        shot.characters = ["C99"]

    class BadRepairQwen:
        async def structured(self, **kwargs):
            return type(
                "Result",
                (),
                {
                    "value": bad_plan.model_dump(),
                    "model": "qwen-plus",
                    "input_tokens": 10,
                    "output_tokens": 20,
                },
            )()

        async def close(self):
            return None

    provider.qwen = BadRepairQwen()  # type: ignore[assignment]

    result = await provider.plan_story(brief)

    assert result.degraded
    assert result.planning_path == "validated_fallback"
    assert result.degradation_reason == "ValueError"
    assert all(set(shot.characters) <= {"C01", "C02"} for shot in result.plan.shots)
    await provider.close()


@pytest.mark.asyncio
async def test_live_story_with_character_bindings_marks_live_planning_path(tmp_path: Path):
    settings = Settings(
        provider_mode="live",
        dashscope_api_key="test-key",
        public_media_base_url="https://assets.example.invalid/media",
        media_root=tmp_path / "media",
        oss_repository_root=tmp_path / "oss",
    )
    provider = LiveStudioProvider(settings, AssetStore(settings))
    await provider.qwen.close()
    await provider.dashscope.close()
    brief = ProjectBrief(
        title="Character-bound plan",
        premise="A generated plan includes reusable characters and per-shot bindings.",
        duration_seconds=24,
        aspect_ratio="16:9",
        max_shots=6,
        required_prop=None,
    )
    model_plan = fallback_story_plan(brief)

    class CharacterBoundQwen:
        async def structured(self, **kwargs):
            return type(
                "Result",
                (),
                {
                    "value": model_plan.model_dump(),
                    "model": "qwen-plus",
                    "input_tokens": 12,
                    "output_tokens": 34,
                },
            )()

        async def close(self):
            return None

    provider.qwen = CharacterBoundQwen()  # type: ignore[assignment]

    result = await provider.plan_story(brief)

    assert not result.degraded
    assert result.planning_path == "live_qwen_character_bound"
    assert result.character_bound_shots == len(result.plan.shots)
    await provider.close()


@pytest.mark.asyncio
async def test_wan_t2i_uses_frankfurt_async_image_generation_shape(tmp_path: Path):
    settings = Settings(
        provider_mode="live",
        dashscope_api_key="test-key",
        dashscope_native_base_url="https://ws-q3cz9vsbgt4ypbbg.eu-central-1.maas.aliyuncs.com/api/v1",
        wan_image_model="wan2.6-t2i",
        media_root=tmp_path / "media",
    )
    client = DashScopeClient(settings)
    calls: list[dict] = []

    async def request(method, url, *, payload=None, async_call=False, extra_headers=None):
        calls.append({"method": method, "url": url, "payload": payload, "async_call": async_call})
        if method == "POST":
            return {"output": {"task_id": "task-image-123"}}
        return {
            "output": {
                "task_status": "SUCCEEDED",
                "choices": [
                    {
                        "message": {
                            "content": [
                                {"image": "https://signed.example.invalid/generated.png?Expires=soon"}
                            ]
                        }
                    }
                ],
            },
            "usage": {"image_count": 1},
        }

    client._request = request  # type: ignore[method-assign]
    result = await client.generate_image(
        prompt="rainy alley storyboard",
        negative_prompt="",
        size="1696*960",
        seed=20260711,
    )

    assert calls[0]["method"] == "POST"
    assert calls[0]["url"].endswith("/services/aigc/image-generation/generation")
    assert calls[0]["async_call"] is True
    assert calls[0]["payload"]["model"] == "wan2.6-t2i"
    assert calls[0]["payload"]["parameters"] == {
        "prompt_extend": True,
        "watermark": False,
        "n": 1,
        "negative_prompt": "",
        "size": "1696*960",
    }
    assert calls[1]["method"] == "GET"
    assert calls[1]["url"].endswith("/tasks/task-image-123")
    assert result.task_id == "task-image-123"
    assert result.model == "wan2.6-t2i"
    assert result.url.startswith("https://signed.example.invalid/generated.png")
    assert result.usage == {"image_count": 1}
    await client.close()


@pytest.mark.asyncio
async def test_qwen_tts_instructions_only_for_instruct_model(tmp_path: Path):
    instruct = Settings(
        provider_mode="live",
        dashscope_api_key="test-key",
        qwen_tts_model="qwen3-tts-instruct-flash",
        media_root=tmp_path / "media",
    )
    client = DashScopeClient(instruct)
    captured: dict = {}

    async def request(method, url, *, payload=None, async_call=False, extra_headers=None):
        captured["payload"] = payload
        return {"output": {"audio": {"url": "https://example.invalid/audio.wav"}}}

    client._request = request  # type: ignore[method-assign]
    await client.synthesize_speech(
        text="You came back.", language="English", instructions="Speak with relief."
    )
    assert "instructions" not in captured["payload"]["input"]
    assert captured["payload"]["parameters"] == {
        "instructions": "Speak with relief.",
        "optimize_instructions": True,
    }
    await client.close()

    plain = Settings(
        provider_mode="live",
        dashscope_api_key="test-key",
        qwen_tts_model="qwen3-tts-flash",
        media_root=tmp_path / "media-plain",
    )
    client = DashScopeClient(plain)
    captured = {}
    client._request = request  # type: ignore[method-assign]
    await client.synthesize_speech(
        text="You came back.", language="English", instructions="Speak with relief."
    )
    assert "instructions" not in captured["payload"]["input"]
    assert "parameters" not in captured["payload"]
    await client.close()


@pytest.mark.asyncio
async def test_qwen_tts_uses_dedicated_singapore_base_and_key_when_configured(tmp_path: Path):
    settings = Settings(
        provider_mode="live",
        dashscope_api_key="frankfurt-media-key",
        qwen_tts_api_key="singapore-tts-key",
        qwen_tts_base_url="https://ws-x9ufyc1pm4ck9al2.ap-southeast-1.maas.aliyuncs.com/api/v1",
        qwen_tts_model="qwen3-tts-flash",
        media_root=tmp_path / "media",
    )
    client = DashScopeClient(settings)
    captured: dict = {}

    async def request(method, url, *, payload=None, async_call=False, extra_headers=None):
        captured.update(
            {
                "method": method,
                "url": url,
                "payload": payload,
                "async_call": async_call,
                "extra_headers": extra_headers,
            }
        )
        return {"output": {"audio": {"url": "https://models.example.invalid/audio.wav"}}}

    client._request = request  # type: ignore[method-assign]
    result = await client.synthesize_speech(text="Delivery complete.", language="English")

    assert captured["url"].startswith(settings.qwen_tts_base_url)
    assert captured["url"].endswith("/services/aigc/multimodal-generation/generation")
    assert captured["async_call"] is False
    assert captured["extra_headers"]["Authorization"] == "Bearer singapore-tts-key"
    assert "frankfurt-media-key" not in captured["extra_headers"]["Authorization"]
    assert captured["payload"] == {
        "model": "qwen3-tts-flash",
        "input": {
            "text": "Delivery complete.",
            "voice": "Cherry",
            "language_type": "English",
        },
    }
    assert result.url == "https://models.example.invalid/audio.wav"
    assert result.model == "qwen3-tts-flash"
    await client.close()


@pytest.mark.asyncio
async def test_qwen_tts_retries_workspace_header_after_auth_scope_error(tmp_path: Path):
    settings = Settings(
        provider_mode="live",
        dashscope_api_key="frankfurt-media-key",
        qwen_tts_api_key="singapore-tts-key",
        qwen_tts_base_url="https://ws-x9ufyc1pm4ck9al2.ap-southeast-1.maas.aliyuncs.com/api/v1",
        qwen_tts_workspace_id="ws-x9ufyc1pm4ck9al2",
        qwen_tts_model="qwen3-tts-flash",
        media_root=tmp_path / "media",
    )
    client = DashScopeClient(settings)
    calls: list[dict] = []

    async def request(method, url, *, payload=None, async_call=False, extra_headers=None):
        calls.append({"extra_headers": dict(extra_headers or {})})
        if len(calls) == 1:
            raise ProviderCallError(
                provider="DashScope",
                category=ProviderErrorCategory.AUTH,
                status_code=403,
                code="Forbidden",
                detail={"message": "workspace scope required"},
                retryable=False,
            )
        return {"output": {"audio": {"url": "https://models.example.invalid/audio.wav"}}}

    client._request = request  # type: ignore[method-assign]
    result = await client.synthesize_speech(text="Delivery complete.", language="English")

    assert result.url == "https://models.example.invalid/audio.wav"
    assert "X-DashScope-WorkSpace" not in calls[0]["extra_headers"]
    assert calls[1]["extra_headers"]["X-DashScope-WorkSpace"] == "ws-x9ufyc1pm4ck9al2"
    await client.close()


@pytest.mark.asyncio
async def test_wan_reference_video_receives_canonical_character_references(tmp_path: Path):
    settings = Settings(
        provider_mode="live",
        dashscope_api_key="test-key",
        public_media_base_url="https://assets.example.invalid/media",
        media_root=tmp_path / "media",
        oss_repository_root=tmp_path / "oss",
        wan_reference_model="wan2.7-r2v",
    )
    store = AssetStore(settings)
    provider = LiveStudioProvider(settings, store)
    await provider.dashscope.close()

    captured: dict = {}

    class FakeDashScope:
        async def submit_video(self, *, payload, model):
            captured["payload"] = payload
            captured["model"] = model
            return "task-123"

        async def poll_video(self, task_id, *, model):
            return RemoteResult(
                url="https://models.example.invalid/generated.mp4",
                model=model,
                task_id=task_id,
            )

        async def close(self):
            return None

    provider.dashscope = FakeDashScope()  # type: ignore[assignment]

    async def save_remote(url: str, key: str) -> StoredAsset:
        path = store.path_for_key(key)
        path.write_bytes(b"video")
        return StoredAsset(key=key, local_path=path, public_url=f"https://assets.example.invalid/{key}")

    store.save_remote = save_remote  # type: ignore[method-assign]

    brief = ProjectBrief(
        title="Reference test",
        premise="Two recurring characters meet at a doorway and exchange a red paper crane.",
        duration_seconds=28,
    )
    plan = route_and_budget(fallback_story_plan(brief), brief)
    contract = plan.shots[4]
    contract.renderer = "wan_r2v"

    storyboard_path = tmp_path / "storyboard.png"
    storyboard_path.write_bytes(b"image")
    storyboard = AssetResult(
        "https://assets.example.invalid/storyboard.png",
        storyboard_path,
        "test",
        "wan-image",
    )
    references = []
    for character_id in contract.characters:
        path = tmp_path / f"{character_id}.png"
        path.write_bytes(b"reference")
        references.append(
            AssetResult(
                f"https://assets.example.invalid/{character_id}.png",
                path,
                "test",
                "wan-image",
            )
        )

    asset = await provider.generate_video(
        "project-1",
        contract,
        storyboard,
        None,
        references,
        attempt=1,
    )
    repo = LocalOssRepository(settings.oss_repository_root)
    checkpoint = repo.get_json(
        provider_task_key("project-1", contract.id, 1)
    )
    result = repo.get_json(provider_result_key("project-1", contract.id, 1))
    materialized = repo.get_json(asset_materialization_key("project-1", contract.id, 1))

    assert captured["model"] == "wan2.7-r2v"
    assert captured["payload"]["input"]["media"] == [
        {"type": "first_frame", "url": storyboard.public_url},
        {"type": "reference_image", "url": references[0].public_url},
        {"type": "reference_image", "url": references[1].public_url},
    ]
    assert "Image 1" in captured["payload"]["input"]["prompt"]
    assert "Image 2" in captured["payload"]["input"]["prompt"]
    assert captured["payload"]["parameters"] == {
        "resolution": contract.resolution,
        "ratio": contract.aspect_ratio,
        "duration": min(contract.duration_seconds, 10),
        "prompt_extend": True,
        "watermark": False,
    }
    assert checkpoint.payload["task_id"] == "task-123"
    assert result.payload["status"] == "succeeded"
    assert result.payload["provider_output_url_host"] == "models.example.invalid"
    assert "url" not in result.payload
    assert materialized.payload["object_key"] == "projects/project-1/shots/S05/attempt-1.mp4"
    assert asset.provider_result_key == provider_result_key("project-1", contract.id, 1)
    assert asset.asset_checkpoint_key == asset_materialization_key("project-1", contract.id, 1)
    await provider.close()


@pytest.mark.asyncio
async def test_r2v_regenerate_repair_rekeys_storyboard_before_rerender(tmp_path: Path):
    settings = Settings(
        provider_mode="live",
        dashscope_api_key="test-key",
        public_media_base_url="https://assets.example.invalid/media",
        media_root=tmp_path / "media",
        oss_repository_root=tmp_path / "oss",
        wan_image_model="wan2.6-t2i",
        wan_reference_model="wan2.7-r2v",
    )
    store = AssetStore(settings)
    provider = LiveStudioProvider(settings, store)
    await provider.dashscope.close()

    captured: dict = {"images": [], "videos": []}

    class FakeDashScope:
        async def generate_image(self, *, prompt, negative_prompt, size, seed):
            captured["images"].append(
                {
                    "prompt": prompt,
                    "negative_prompt": negative_prompt,
                    "size": size,
                    "seed": seed,
                }
            )
            return RemoteResult(
                url="https://models.example.invalid/reframed-storyboard.png",
                model="wan2.6-t2i",
                task_id="task-repair-storyboard",
                usage={"image_count": 1},
            )

        async def submit_video(self, *, payload, model):
            captured["videos"].append({"payload": payload, "model": model})
            return "task-r2v-repair"

        async def poll_video(self, task_id, *, model):
            return RemoteResult(
                url="https://models.example.invalid/repaired.mp4",
                model=model,
                task_id=task_id,
                usage={"video_seconds": 4},
            )

        async def close(self):
            return None

    provider.dashscope = FakeDashScope()  # type: ignore[assignment]

    async def save_remote(url: str, key: str) -> StoredAsset:
        path = store.path_for_key(key)
        path.write_bytes(b"media")
        return StoredAsset(key=key, local_path=path, public_url=f"https://assets.example.invalid/{key}")

    store.save_remote = save_remote  # type: ignore[method-assign]

    brief = ProjectBrief(
        title="Keyframe repair",
        premise="A recurring character walks through a quiet park in one continuous scene.",
        duration_seconds=24,
        max_shots=6,
    )
    contract = route_and_budget(fallback_story_plan(brief), brief).shots[0]
    contract.renderer = "wan_r2v"
    contract.characters = ["C01"]
    original_storyboard_path = tmp_path / "original-storyboard.png"
    original_storyboard_path.write_bytes(b"original")
    original_storyboard = AssetResult(
        "https://assets.example.invalid/original-storyboard.png",
        original_storyboard_path,
        "test",
        "wan2.6-t2i",
    )
    reference_path = tmp_path / "C01.png"
    reference_path.write_bytes(b"reference")
    references = [
        AssetResult(
            "https://assets.example.invalid/C01.png",
            reference_path,
            "test",
            "wan2.6-t2i",
        )
    ]
    failed_video_path = tmp_path / "failed.mp4"
    failed_video_path.write_bytes(b"failed")
    failed_video = AssetResult(
        "https://assets.example.invalid/failed.mp4",
        failed_video_path,
        "test",
        "wan2.7-r2v",
    )
    report = QualityReport(
        passed=False,
        overall_score=0.78,
        dimensions=[],
        violations=["composition"],
        repair_strategy="regenerate",
        repair_instruction="Reframe the character toward the lower-left third and soften the light.",
        evaluator_model="qwen3-vl-flash",
        attempt=1,
    )

    repaired = await provider.repair_video(
        "project-keyframe-repair",
        contract,
        failed_video,
        original_storyboard,
        references,
        report,
        attempt=2,
    )

    assert captured["images"], "regenerate repair should create a corrected t2i keyframe first"
    assert "lower-left third" in captured["images"][0]["prompt"]
    first_frame = captured["videos"][0]["payload"]["input"]["media"][0]
    assert first_frame == {
        "type": "first_frame",
        "url": "https://assets.example.invalid/projects/project-keyframe-repair/shots/S01/repair-2-storyboard.png",
    }
    assert first_frame["url"] != original_storyboard.public_url
    assert repaired.object_key == "projects/project-keyframe-repair/shots/S01/attempt-2.mp4"
    materialized = LocalOssRepository(settings.oss_repository_root).get_json(
        "projects/project-keyframe-repair/shots/S01/repair-2-storyboard-materialization.json"
    )
    assert materialized.payload["object_key"] == "projects/project-keyframe-repair/shots/S01/repair-2-storyboard.png"
    await provider.close()


@pytest.mark.asyncio
async def test_wan_i2v_uses_verified_img_url_payload_shape(tmp_path: Path):
    settings = Settings(
        provider_mode="live",
        dashscope_api_key="test-key",
        public_media_base_url="https://assets.example.invalid/media",
        media_root=tmp_path / "media",
        oss_repository_root=tmp_path / "oss",
        wan_video_model="wan2.6-i2v",
    )
    store = AssetStore(settings)
    provider = LiveStudioProvider(settings, store)
    await provider.dashscope.close()

    captured: dict = {}

    class FakeDashScope:
        async def submit_video(self, *, payload, model):
            captured["payload"] = payload
            captured["model"] = model
            return "task-i2v"

        async def poll_video(self, task_id, *, model):
            return RemoteResult(
                url="https://models.example.invalid/generated.mp4",
                model=model,
                task_id=task_id,
            )

        async def close(self):
            return None

    provider.dashscope = FakeDashScope()  # type: ignore[assignment]

    async def save_remote(url: str, key: str) -> StoredAsset:
        path = store.path_for_key(key)
        path.write_bytes(b"video")
        return StoredAsset(key=key, local_path=path, public_url=f"https://assets.example.invalid/{key}")

    store.save_remote = save_remote  # type: ignore[method-assign]
    brief = ProjectBrief(
        title="I2V payload test",
        premise="A courier robot animates a storyboard through the verified image-to-video path.",
        duration_seconds=21,
    )
    contract = route_and_budget(fallback_story_plan(brief), brief).shots[0]
    contract.renderer = "wan_i2v"
    storyboard_path = tmp_path / "storyboard.png"
    storyboard_path.write_bytes(b"image")
    storyboard = AssetResult(
        "https://assets.example.invalid/storyboard.png",
        storyboard_path,
        "test",
        "wan-image",
    )

    await provider.generate_video("project-i2v", contract, storyboard, None, [], attempt=1)

    assert captured["model"] == "wan2.6-i2v"
    assert captured["payload"]["input"]["img_url"] == storyboard.public_url
    assert "media" not in captured["payload"]["input"]
    assert captured["payload"]["parameters"]["resolution"] == contract.resolution
    assert captured["payload"]["parameters"]["duration"] == contract.duration_seconds
    assert captured["payload"]["parameters"]["prompt_extend"] is True
    await provider.close()


@pytest.mark.asyncio
async def test_live_video_task_id_checkpoint_survives_poll_failure(tmp_path: Path):
    settings = Settings(
        provider_mode="live",
        dashscope_api_key="test-key",
        public_media_base_url="https://assets.example.invalid/media",
        media_root=tmp_path / "media",
        oss_repository_root=tmp_path / "oss",
        wan_reference_model="wan2.7-r2v",
    )
    store = AssetStore(settings)
    provider = LiveStudioProvider(settings, store)
    await provider.dashscope.close()

    class FailingDashScope:
        async def submit_video(self, *, payload, model):
            return "task-before-poll-failure"

        async def poll_video(self, task_id, *, model):
            raise RuntimeError("poll failed")

        async def close(self):
            return None

    provider.dashscope = FailingDashScope()  # type: ignore[assignment]
    brief = ProjectBrief(
        title="Poll failure checkpoint",
        premise="A courier robot keeps the remote task id when polling fails.",
        duration_seconds=21,
    )
    plan = route_and_budget(fallback_story_plan(brief), brief)
    contract = plan.shots[0]
    storyboard_path = tmp_path / "storyboard.png"
    storyboard_path.write_bytes(b"image")
    storyboard = AssetResult(
        "https://assets.example.invalid/storyboard.png",
        storyboard_path,
        "test",
        "wan-image",
    )

    with pytest.raises(RuntimeError, match="poll failed"):
        await provider.generate_video("project-failure", contract, storyboard, None, [], attempt=1)

    checkpoint = LocalOssRepository(settings.oss_repository_root).get_json(
        provider_task_key("project-failure", contract.id, 1)
    )
    assert checkpoint.payload["task_id"] == "task-before-poll-failure"
    with pytest.raises(OssNotFoundError):
        LocalOssRepository(settings.oss_repository_root).get_json(
            provider_result_key("project-failure", contract.id, 1)
        )
    with pytest.raises(OssNotFoundError):
        LocalOssRepository(settings.oss_repository_root).get_json(
            f"projects/project-failure/shots/{contract.id}/attempts/attempt-1/inspection.json"
        )
    await provider.close()


@pytest.mark.asyncio
async def test_live_video_result_checkpoint_survives_download_failure(tmp_path: Path):
    settings = Settings(
        provider_mode="live",
        dashscope_api_key="test-key",
        public_media_base_url="https://assets.example.invalid/media",
        media_root=tmp_path / "media",
        oss_repository_root=tmp_path / "oss",
    )
    store = AssetStore(settings)
    provider = LiveStudioProvider(settings, store)
    await provider.dashscope.close()

    class DownloadFailingDashScope:
        submit_calls = 0
        poll_calls = 0

        async def submit_video(self, *, payload, model):
            self.submit_calls += 1
            return "task-before-download-failure"

        async def poll_video(self, task_id, *, model):
            self.poll_calls += 1
            return RemoteResult(
                url="https://models.example.invalid/generated.mp4?signature=temporary",
                model=model,
                task_id=task_id,
                usage={"video_seconds": 4},
            )

        async def close(self):
            return None

    fake_dashscope = DownloadFailingDashScope()
    provider.dashscope = fake_dashscope  # type: ignore[assignment]

    async def save_remote(url: str, key: str) -> StoredAsset:
        raise RuntimeError("download failed")

    store.save_remote = save_remote  # type: ignore[method-assign]
    brief = ProjectBrief(
        title="Download failure checkpoint",
        premise="A courier robot records provider success before media download fails.",
        duration_seconds=21,
    )
    plan = route_and_budget(fallback_story_plan(brief), brief)
    contract = plan.shots[0]
    storyboard_path = tmp_path / "storyboard.png"
    storyboard_path.write_bytes(b"image")
    storyboard = AssetResult(
        "https://assets.example.invalid/storyboard.png",
        storyboard_path,
        "test",
        "wan-image",
    )

    with pytest.raises(RuntimeError, match="download failed"):
        await provider.generate_video("project-download-failure", contract, storyboard, None, [], attempt=1)

    repo = LocalOssRepository(settings.oss_repository_root)
    task = repo.get_json(provider_task_key("project-download-failure", contract.id, 1))
    result = repo.get_json(provider_result_key("project-download-failure", contract.id, 1))

    assert task.payload["task_id"] == "task-before-download-failure"
    assert result.payload["status"] == "succeeded"
    assert result.payload["provider_output_url_present"] is True
    assert result.payload["provider_output_url_host"] == "models.example.invalid"
    assert result.payload["usage"] == {"video_seconds": 4}
    with pytest.raises(OssNotFoundError):
        repo.get_json(asset_materialization_key("project-download-failure", contract.id, 1))

    async def save_remote(url: str, key: str) -> StoredAsset:
        path = store.path_for_key(key)
        path.write_bytes(b"video-after-retry")
        return StoredAsset(key=key, local_path=path, public_url=f"https://assets.example.invalid/{key}")

    store.save_remote = save_remote  # type: ignore[method-assign]
    retry = await provider.generate_video("project-download-failure", contract, storyboard, None, [], attempt=1)
    materialized = repo.get_json(asset_materialization_key("project-download-failure", contract.id, 1))

    assert fake_dashscope.submit_calls == 1
    assert fake_dashscope.poll_calls == 2
    assert retry.task_id == "task-before-download-failure"
    assert retry.object_key == "projects/project-download-failure/shots/S01/attempt-1.mp4"
    assert materialized.payload["object_key"] == retry.object_key
    await provider.close()


@pytest.mark.asyncio
async def test_live_video_materialized_checkpoint_skips_submit_and_poll(tmp_path: Path):
    settings = Settings(
        provider_mode="live",
        dashscope_api_key="test-key",
        public_media_base_url="https://assets.example.invalid/media",
        media_root=tmp_path / "media",
        oss_repository_root=tmp_path / "oss",
    )
    store = AssetStore(settings)
    provider = LiveStudioProvider(settings, store)
    await provider.dashscope.close()

    class ForbiddenDashScope:
        async def submit_video(self, *, payload, model):
            raise AssertionError("resume should not submit a new video task")

        async def poll_video(self, task_id, *, model):
            raise AssertionError("resume should not poll an already materialized asset")

        async def close(self):
            return None

    provider.dashscope = ForbiddenDashScope()  # type: ignore[assignment]
    brief = ProjectBrief(
        title="Materialized resume",
        premise="A courier robot resumes from an already materialized clip.",
        duration_seconds=21,
    )
    plan = route_and_budget(fallback_story_plan(brief), brief)
    contract = plan.shots[0]
    object_key = "projects/project-materialized/shots/S01/attempt-1.mp4"
    path = store.path_for_key(object_key)
    path.write_bytes(b"already-materialized")
    checkpoint_asset_materialization_object(
        settings,
        "project-materialized",
        contract.id,
        1,
        object_key=object_key,
        model="wan-resume",
        task_id="task-resume",
        operation="render",
    )
    storyboard_path = tmp_path / "storyboard.png"
    storyboard_path.write_bytes(b"image")
    storyboard = AssetResult(
        "https://assets.example.invalid/storyboard.png",
        storyboard_path,
        "test",
        "wan-image",
    )

    asset = await provider.generate_video("project-materialized", contract, storyboard, None, [], attempt=1)

    assert asset.public_url == f"https://assets.example.invalid/media/{object_key}"
    assert asset.local_path == path
    assert asset.model == "wan-resume"
    assert asset.task_id == "task-resume"
    assert asset.object_key == object_key
    assert asset.asset_checkpoint_key == asset_materialization_key("project-materialized", contract.id, 1)
    await provider.close()


@pytest.mark.asyncio
async def test_live_storyboard_generation_writes_materialization_checkpoint(tmp_path: Path):
    settings = Settings(
        provider_mode="live",
        dashscope_api_key="test-key",
        public_media_base_url="https://assets.example.invalid/media",
        media_root=tmp_path / "media",
        oss_repository_root=tmp_path / "oss",
    )
    store = AssetStore(settings)
    provider = LiveStudioProvider(settings, store)
    await provider.dashscope.close()

    class FakeDashScope:
        async def generate_image(self, *, prompt, negative_prompt, size, seed):
            return RemoteResult(
                url="https://models.example.invalid/storyboard.png",
                model="wan-image-test",
                task_id="task-storyboard-1",
                usage={"image_count": 1},
            )

        async def close(self):
            return None

    provider.dashscope = FakeDashScope()  # type: ignore[assignment]

    async def save_remote(url: str, key: str) -> StoredAsset:
        path = store.path_for_key(key)
        path.write_bytes(b"storyboard")
        return StoredAsset(key=key, local_path=path, public_url=f"https://assets.example.invalid/{key}")

    store.save_remote = save_remote  # type: ignore[method-assign]
    brief = ProjectBrief(
        title="Storyboard checkpoint",
        premise="A courier robot records storyboard materialization before retry.",
        duration_seconds=21,
    )
    contract = route_and_budget(fallback_story_plan(brief), brief).shots[0]

    asset = await provider.generate_storyboard("project-storyboard", contract, seed=42)
    repo = LocalOssRepository(settings.oss_repository_root)
    checkpoint = repo.get_json(
        storyboard_asset_materialization_key("project-storyboard", contract.id)
    )
    result = repo.get_json(storyboard_provider_result_key("project-storyboard", contract.id))

    assert asset.object_key == "projects/project-storyboard/shots/S01/storyboard.png"
    assert asset.task_id == "task-storyboard-1"
    assert asset.provider_result_key == storyboard_provider_result_key("project-storyboard", contract.id)
    assert asset.asset_checkpoint_key == storyboard_asset_materialization_key("project-storyboard", contract.id)
    assert result.payload["schema_version"] == "directorgraph.media-provider-result.v1"
    assert result.payload["asset_kind"] == "storyboard"
    assert result.payload["task_id"] == "task-storyboard-1"
    assert result.payload["provider_output_url_host"] == "models.example.invalid"
    assert "url" not in result.payload
    assert checkpoint.payload["asset_kind"] == "storyboard"
    assert checkpoint.payload["object_key"] == asset.object_key
    assert checkpoint.payload["usage"] == {"image_count": 1}
    await provider.close()


@pytest.mark.asyncio
async def test_live_storyboard_image_calls_are_serialized_to_avoid_t2i_bursts(tmp_path: Path):
    settings = Settings(
        provider_mode="live",
        dashscope_api_key="test-key",
        public_media_base_url="https://assets.example.invalid/media",
        media_root=tmp_path / "media",
        oss_repository_root=tmp_path / "oss",
    )
    store = AssetStore(settings)
    provider = LiveStudioProvider(settings, store)
    await provider.dashscope.close()
    active = 0
    max_active = 0

    class FakeDashScope:
        async def generate_image(self, *, prompt, negative_prompt, size, seed):
            nonlocal active, max_active
            active += 1
            max_active = max(max_active, active)
            await asyncio.sleep(0)
            active -= 1
            return RemoteResult(
                url=f"https://models.example.invalid/storyboard-{seed}.png",
                model="wan-image-test",
                task_id=f"task-storyboard-{seed}",
                usage={"image_count": 1},
            )

        async def close(self):
            return None

    provider.dashscope = FakeDashScope()  # type: ignore[assignment]

    async def save_remote(url: str, key: str) -> StoredAsset:
        path = store.path_for_key(key)
        path.write_bytes(b"storyboard")
        return StoredAsset(key=key, local_path=path, public_url=f"https://assets.example.invalid/{key}")

    store.save_remote = save_remote  # type: ignore[method-assign]
    brief = ProjectBrief(
        title="Storyboard pacing",
        premise="A courier robot records storyboard calls without bursting the t2i endpoint.",
        duration_seconds=24,
        max_shots=6,
    )
    contracts = route_and_budget(fallback_story_plan(brief), brief).shots[:2]

    await asyncio.gather(
        provider.generate_storyboard("project-storyboard-pacing", contracts[0], seed=42),
        provider.generate_storyboard("project-storyboard-pacing", contracts[1], seed=42),
    )

    assert max_active == 1
    await provider.close()


@pytest.mark.asyncio
async def test_live_storyboard_image_call_applies_configured_pacing(monkeypatch, tmp_path: Path):
    settings = Settings(
        provider_mode="live",
        dashscope_api_key="test-key",
        public_media_base_url="https://assets.example.invalid/media",
        media_root=tmp_path / "media",
        oss_repository_root=tmp_path / "oss",
        dashscope_image_pace_seconds=1.25,
    )
    store = AssetStore(settings)
    provider = LiveStudioProvider(settings, store)
    await provider.dashscope.close()
    sleeps: list[float] = []

    class FakeDashScope:
        async def generate_image(self, *, prompt, negative_prompt, size, seed):
            return RemoteResult(
                url="https://models.example.invalid/storyboard.png",
                model="wan-image-test",
                task_id="task-storyboard-pace",
                usage={"image_count": 1},
            )

        async def close(self):
            return None

    async def fake_sleep(delay: float) -> None:
        sleeps.append(delay)

    provider.dashscope = FakeDashScope()  # type: ignore[assignment]
    monkeypatch.setattr("app.providers.live.asyncio.sleep", fake_sleep)

    async def save_remote(url: str, key: str) -> StoredAsset:
        path = store.path_for_key(key)
        path.write_bytes(b"storyboard")
        return StoredAsset(key=key, local_path=path, public_url=f"https://assets.example.invalid/{key}")

    store.save_remote = save_remote  # type: ignore[method-assign]
    brief = ProjectBrief(
        title="Storyboard pacing delay",
        premise="A courier robot leaves a small quiet interval after t2i calls.",
        duration_seconds=24,
        max_shots=6,
    )
    contract = route_and_budget(fallback_story_plan(brief), brief).shots[0]

    await provider.generate_storyboard("project-storyboard-delay", contract, seed=42)

    assert sleeps == [1.25]
    await provider.close()


@pytest.mark.asyncio
async def test_live_dialogue_provider_result_survives_download_failure(tmp_path: Path):
    settings = Settings(
        provider_mode="live",
        dashscope_api_key="test-key",
        public_media_base_url="https://assets.example.invalid/media",
        media_root=tmp_path / "media",
        oss_repository_root=tmp_path / "oss",
    )
    store = AssetStore(settings)
    provider = LiveStudioProvider(settings, store)
    await provider.dashscope.close()

    class FakeDashScope:
        async def synthesize_speech(self, *, text, language, instructions):
            return RemoteResult(
                url="https://models.example.invalid/dialogue.wav?signature=temporary",
                model="qwen-tts-test",
                task_id="speech-task-1",
                usage={"characters": len(text)},
            )

        async def close(self):
            return None

    provider.dashscope = FakeDashScope()  # type: ignore[assignment]

    async def save_remote(url: str, key: str) -> StoredAsset:
        raise RuntimeError("download failed")

    store.save_remote = save_remote  # type: ignore[method-assign]
    brief = ProjectBrief(
        title="Dialogue provider result",
        premise="A courier robot records speech provider success before download failure.",
        duration_seconds=21,
    )
    contract = route_and_budget(fallback_story_plan(brief), brief).shots[0]
    contract.dialogue = "Provider success should be checkpointed."

    with pytest.raises(RuntimeError, match="download failed"):
        await provider.synthesize_voice("project-dialogue-failure", contract, "english")

    repo = LocalOssRepository(settings.oss_repository_root)
    result = repo.get_json(voice_provider_result_key("project-dialogue-failure", contract.id))

    assert result.payload["schema_version"] == "directorgraph.media-provider-result.v1"
    assert result.payload["asset_kind"] == "dialogue"
    assert result.payload["asset_id"] == contract.id
    assert result.payload["task_id"] == "speech-task-1"
    assert result.payload["provider_output_url_present"] is True
    assert result.payload["provider_output_url_host"] == "models.example.invalid"
    assert result.payload["usage"] == {"characters": len(contract.dialogue)}
    assert "url" not in result.payload
    with pytest.raises(OssNotFoundError):
        repo.get_json(voice_asset_materialization_key("project-dialogue-failure", contract.id))
    await provider.close()


@pytest.mark.asyncio
async def test_live_tts_provider_error_does_not_create_local_voice_fallback(tmp_path: Path):
    settings = Settings(
        provider_mode="live",
        dashscope_api_key="test-key",
        public_media_base_url="https://assets.example.invalid/media",
        media_root=tmp_path / "media",
        oss_repository_root=tmp_path / "oss",
    )
    store = AssetStore(settings)
    provider = LiveStudioProvider(settings, store)
    await provider.dashscope.close()

    class UnsupportedTTS:
        async def synthesize_speech(self, *, text, language, instructions):
            raise ProviderCallError(
                provider="DashScope",
                category=ProviderErrorCategory.UNSUPPORTED_MODEL,
                status_code=404,
                code="InvalidParameter",
                detail={"message": "Model not exist."},
                retryable=False,
            )

        async def close(self):
            return None

    class ForbiddenLocalFallback:
        async def synthesize_voice(self, project_id, contract, language):
            raise AssertionError("live speech errors must not create local voice artifacts")

    provider.dashscope = UnsupportedTTS()  # type: ignore[assignment]
    provider.local_fallback = ForbiddenLocalFallback()  # type: ignore[assignment]
    brief = ProjectBrief(
        title="No local voice fallback",
        premise="A courier robot should not use local voice when live TTS is unavailable.",
        duration_seconds=21,
    )
    contract = route_and_budget(fallback_story_plan(brief), brief).shots[0]
    contract.dialogue = "Provider success should be required."

    result = await provider.synthesize_voice("project-no-local-voice", contract, "english")

    assert result is None
    with pytest.raises(OssNotFoundError):
        LocalOssRepository(settings.oss_repository_root).get_json(
            voice_asset_materialization_key("project-no-local-voice", contract.id)
        )
    await provider.close()


@pytest.mark.asyncio
async def test_live_static_media_materialized_checkpoints_skip_paid_calls(tmp_path: Path):
    settings = Settings(
        provider_mode="live",
        dashscope_api_key="test-key",
        public_media_base_url="https://assets.example.invalid/media",
        media_root=tmp_path / "media",
        oss_repository_root=tmp_path / "oss",
    )
    store = AssetStore(settings)
    provider = LiveStudioProvider(settings, store)
    await provider.dashscope.close()

    class ForbiddenDashScope:
        async def generate_image(self, *, prompt, negative_prompt, size, seed):
            raise AssertionError("resume should not generate an already materialized image")

        async def synthesize_speech(self, *, text, language, instructions):
            raise AssertionError("resume should not synthesize already materialized speech")

        async def close(self):
            return None

    provider.dashscope = ForbiddenDashScope()  # type: ignore[assignment]
    brief = ProjectBrief(
        title="Static media resume",
        premise="A courier robot resumes static media assets from materialization keys.",
        duration_seconds=21,
    )
    plan = route_and_budget(fallback_story_plan(brief), brief)
    character = plan.characters[0]
    contract = plan.shots[0]
    contract.dialogue = "We already have the line."
    project_id = "project-static-media"
    cases = [
        (
            character_asset_materialization_key(project_id, character.id),
            "character_reference",
            f"projects/{project_id}/characters/{character.id}.png",
            "character-model",
        ),
        (
            storyboard_asset_materialization_key(project_id, contract.id),
            "storyboard",
            f"projects/{project_id}/shots/{contract.id}/storyboard.png",
            "storyboard-model",
        ),
        (
            voice_asset_materialization_key(project_id, contract.id),
            "dialogue",
            f"projects/{project_id}/shots/{contract.id}/dialogue.wav",
            "voice-model",
        ),
    ]
    for checkpoint_key, asset_kind, object_key, model in cases:
        path = store.path_for_key(object_key)
        path.write_bytes(asset_kind.encode())
        checkpoint_media_asset_materialization_object(
            settings,
            checkpoint_key,
            project_id=project_id,
            asset_kind=asset_kind,
            object_key=object_key,
            model=model,
            usage={"reused": True},
        )

    character_asset = await provider.generate_character_reference(project_id, character, seed=1)
    storyboard_asset = await provider.generate_storyboard(project_id, contract, seed=1)
    voice_asset = await provider.synthesize_voice(project_id, contract, "english")

    assert character_asset.model == "character-model"
    assert character_asset.object_key == f"projects/{project_id}/characters/{character.id}.png"
    assert storyboard_asset.model == "storyboard-model"
    assert storyboard_asset.object_key == f"projects/{project_id}/shots/{contract.id}/storyboard.png"
    assert voice_asset is not None
    assert voice_asset.model == "voice-model"
    assert voice_asset.object_key == f"projects/{project_id}/shots/{contract.id}/dialogue.wav"
    await provider.close()


def test_long_multi_character_shot_uses_r2v_with_duration_capped_in_payload():
    brief = ProjectBrief(
        title="Long shot",
        premise="Two characters hold a long emotional exchange while a recurring prop changes hands.",
        duration_seconds=35,
    )
    plan = fallback_story_plan(brief)
    plan.shots[4].duration_seconds = 11
    routed = route_and_budget(plan, brief)
    assert routed.shots[4].renderer == "wan_r2v"


def test_auto_routing_uses_reference_video_for_short_multi_character_shots():
    brief = ProjectBrief(
        title="Short multi-character shot",
        premise="Two characters share a short exchange while a recurring prop changes hands.",
        duration_seconds=28,
    )
    routed = route_and_budget(fallback_story_plan(brief), brief)
    assert routed.shots[4].renderer == "wan_r2v"
