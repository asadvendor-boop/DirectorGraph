from pathlib import Path

import httpx
import pytest

from app.clients.dashscope import DashScopeClient, DashScopeError
from app.clients.storage import AssetStore
from app.config import Settings
from app.providers.errors import (
    ProviderCallError,
    ProviderErrorCategory,
    provider_payload_summary,
)
from app.providers.live import LiveStudioProvider
from app.schemas import ProjectBrief


def test_provider_payload_summary_redacts_sensitive_fields_and_signed_urls():
    summary = provider_payload_summary(
        {
            "authorization": "Bearer raw-provider-token",
            "message": (
                "failed for task_id=task-secret at "
                "https://oss.example.invalid/out.mp4?OSSAccessKeyId=LTAIrawsecret&Signature=rawsignature"
            ),
            "output": {
                "task_id": "dashscope-task-secret",
                "video_url": "https://models.example.invalid/video.mp4?x-oss-signature=rawsignature",
            },
        }
    )

    assert "raw-provider-token" not in summary
    assert "task-secret" not in summary
    assert "dashscope-task-secret" not in summary
    assert "rawsignature" not in summary
    assert "LTAIrawsecret" not in summary
    assert "https://oss.example.invalid/out.mp4?[REDACTED_QUERY]" in summary
    assert "https://models.example.invalid/video.mp4?[REDACTED_QUERY]" in summary


@pytest.mark.asyncio
async def test_dashscope_http_error_is_typed_and_redacted(tmp_path: Path):
    settings = Settings(
        provider_mode="live",
        dashscope_api_key="test-key",
        media_root=tmp_path / "media",
    )
    client = DashScopeClient(settings)
    await client.client.aclose()

    def handler(request: httpx.Request) -> httpx.Response:
        assert request.headers["Authorization"] == "Bearer test-key"
        return httpx.Response(
            401,
            json={
                "code": "InvalidApiKey",
                "message": (
                    "Authorization: Bearer raw-secret failed for task_id=task-secret "
                    "https://oss.example.invalid/object.mp4?Signature=rawsignature"
                ),
            },
        )

    client.client = httpx.AsyncClient(transport=httpx.MockTransport(handler))
    with pytest.raises(DashScopeError) as exc_info:
        await client._request("POST", "https://dashscope.example.invalid/test", payload={})

    error = exc_info.value
    text = str(error)
    assert error.category == ProviderErrorCategory.AUTH
    assert error.status_code == 401
    assert "raw-secret" not in text
    assert "task-secret" not in text
    assert "rawsignature" not in text
    assert "InvalidApiKey" in text
    await client.close()


@pytest.mark.asyncio
async def test_dashscope_payload_code_maps_quota_and_redacts_detail(tmp_path: Path):
    settings = Settings(
        provider_mode="live",
        dashscope_api_key="test-key",
        media_root=tmp_path / "media",
    )
    client = DashScopeClient(settings)
    await client.client.aclose()

    def handler(request: httpx.Request) -> httpx.Response:
        return httpx.Response(
            200,
            json={
                "code": "QuotaExceeded",
                "message": "quota exhausted for task_id=task-secret token=raw-token",
            },
        )

    client.client = httpx.AsyncClient(transport=httpx.MockTransport(handler))
    with pytest.raises(DashScopeError) as exc_info:
        await client._request("POST", "https://dashscope.example.invalid/test", payload={})

    error = exc_info.value
    assert error.category == ProviderErrorCategory.QUOTA
    assert "task-secret" not in str(error)
    assert "raw-token" not in str(error)
    await client.close()


@pytest.mark.asyncio
async def test_dashscope_retries_rate_limited_http_errors(monkeypatch, tmp_path: Path):
    settings = Settings(
        provider_mode="live",
        dashscope_api_key="test-key",
        media_root=tmp_path / "media",
    )
    client = DashScopeClient(settings)
    await client.client.aclose()
    attempts = 0
    sleeps: list[float] = []

    def handler(request: httpx.Request) -> httpx.Response:
        nonlocal attempts
        attempts += 1
        if attempts < 3:
            return httpx.Response(
                429,
                headers={"Retry-After": "0"},
                json={"code": "Throttling", "message": "rate limit"},
            )
        return httpx.Response(200, json={"output": {"task_id": "task-ok"}})

    async def fake_sleep(delay: float) -> None:
        sleeps.append(delay)

    monkeypatch.setattr("app.clients.dashscope.asyncio.sleep", fake_sleep)
    client.client = httpx.AsyncClient(transport=httpx.MockTransport(handler))

    data = await client._request("POST", "https://dashscope.example.invalid/test", payload={})

    assert data == {"output": {"task_id": "task-ok"}}
    assert attempts == 3
    assert sleeps == [0.0, 0.0]
    await client.close()


@pytest.mark.asyncio
async def test_live_story_fallback_uses_provider_error_category(tmp_path: Path):
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
            raise ProviderCallError(
                provider="Qwen",
                category=ProviderErrorCategory.TIMEOUT,
                detail={"message": "timed out for task_id=task-secret"},
            )

        async def close(self):
            return None

    provider.qwen = BrokenQwen()  # type: ignore[assignment]

    result = await provider.plan_story(
        ProjectBrief(
            title="Typed degraded story",
            premise="A courier robot records typed fallback categories when live planning fails.",
            duration_seconds=21,
        )
    )

    assert result.degraded is True
    assert result.degradation_reason == "timeout"
    await provider.close()
