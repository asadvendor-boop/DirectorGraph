from __future__ import annotations

import asyncio
from dataclasses import dataclass
from typing import Any

import httpx

from app.config import Settings
from app.providers.errors import (
    ProviderCallError,
    ProviderErrorCategory,
    categorize_provider_error,
)


class DashScopeError(ProviderCallError):
    pass


@dataclass(slots=True)
class RemoteResult:
    url: str
    model: str
    task_id: str | None = None
    usage: dict[str, Any] | None = None
    raw: dict[str, Any] | None = None


class DashScopeClient:
    """Native DashScope client for image, video, and speech endpoints."""

    def __init__(self, settings: Settings):
        api_key = settings.dashscope_api_key or settings.qwen_api_key
        if not api_key:
            raise RuntimeError("DASHSCOPE_API_KEY or QWEN_API_KEY is required in live mode")
        self.settings = settings
        self.client = httpx.AsyncClient(timeout=httpx.Timeout(180, connect=30))
        self.headers = {
            "Authorization": f"Bearer {api_key}",
            "Content-Type": "application/json",
        }

    async def close(self) -> None:
        await self.client.aclose()

    @staticmethod
    def _retry_delay_seconds(response: httpx.Response, attempt: int) -> float:
        retry_after = response.headers.get("Retry-After")
        if retry_after is not None:
            try:
                return max(float(retry_after), 0.0)
            except ValueError:
                pass
        return float(2**attempt)

    async def _request(
        self,
        method: str,
        url: str,
        *,
        payload: dict[str, Any] | None = None,
        async_call: bool = False,
        extra_headers: dict[str, str] | None = None,
    ) -> dict[str, Any]:
        headers = dict(self.headers)
        if extra_headers:
            headers.update(extra_headers)
        if async_call:
            headers["X-DashScope-Async"] = "enable"
        last_error: Exception | None = None
        for attempt in range(3):
            try:
                response = await self.client.request(method, url, headers=headers, json=payload)
                if response.status_code >= 400:
                    try:
                        error_payload: Any = response.json()
                    except ValueError:
                        error_payload = {"body": response.text}
                    code = error_payload.get("code") if isinstance(error_payload, dict) else None
                    error = DashScopeError(
                        provider="DashScope",
                        category=categorize_provider_error(
                            status_code=response.status_code,
                            code=str(code) if code else None,
                            message=error_payload,
                        ),
                        status_code=response.status_code,
                        code=str(code) if code else None,
                        detail=error_payload,
                    )
                    if response.status_code == 429 and attempt < 2:
                        last_error = error
                        await asyncio.sleep(self._retry_delay_seconds(response, attempt))
                        continue
                    raise error
                try:
                    data = response.json()
                except ValueError as exc:
                    raise DashScopeError(
                        provider="DashScope",
                        category=ProviderErrorCategory.INVALID_RESPONSE,
                        status_code=response.status_code,
                        detail={"message": "Provider returned non-JSON response"},
                        retryable=False,
                    ) from exc
                if data.get("code"):
                    raise DashScopeError(
                        provider="DashScope",
                        category=categorize_provider_error(
                            code=str(data.get("code")),
                            message=data,
                        ),
                        code=str(data.get("code")),
                        detail=data,
                    )
                return data
            except httpx.TimeoutException as exc:
                last_error = exc
                if attempt == 2:
                    raise DashScopeError(
                        provider="DashScope",
                        category=ProviderErrorCategory.TIMEOUT,
                        detail={"exception": type(exc).__name__},
                    ) from exc
                await asyncio.sleep(2 ** attempt)
            except httpx.TransportError as exc:
                last_error = exc
                if attempt == 2:
                    raise DashScopeError(
                        provider="DashScope",
                        category=ProviderErrorCategory.TRANSPORT,
                        detail={"exception": type(exc).__name__},
                    ) from exc
                await asyncio.sleep(2 ** attempt)
        raise DashScopeError(
            provider="DashScope",
            category=ProviderErrorCategory.TRANSPORT,
            detail={"exception": type(last_error).__name__ if last_error else "unknown"},
        )

    async def generate_image(self, *, prompt: str, negative_prompt: str, size: str, seed: int) -> RemoteResult:
        url = f"{self.settings.dashscope_native_base}/services/aigc/image-generation/generation"
        payload = {
            "model": self.settings.wan_image_model,
            "input": {"messages": [{"role": "user", "content": [{"text": prompt}]}]},
            "parameters": {
                "prompt_extend": True,
                "watermark": False,
                "n": 1,
                "negative_prompt": negative_prompt,
                "size": size,
            },
        }
        data = await self._request("POST", url, payload=payload, async_call=True)
        task_id = data.get("output", {}).get("task_id")
        if not task_id:
            raise DashScopeError(
                provider="DashScope",
                category=ProviderErrorCategory.INVALID_RESPONSE,
                detail={"message": "Unexpected image task response", "response": data},
                retryable=False,
            )
        return await self.poll_image(str(task_id), model=self.settings.wan_image_model)

    async def poll_image(self, task_id: str, *, model: str, timeout_seconds: int = 600) -> RemoteResult:
        url = f"{self.settings.dashscope_native_base}/tasks/{task_id}"
        deadline = asyncio.get_running_loop().time() + timeout_seconds
        interval = 8.0
        while asyncio.get_running_loop().time() < deadline:
            data = await self._request("GET", url)
            output = data.get("output", {})
            status = output.get("task_status")
            if status == "SUCCEEDED":
                image_url = self._extract_image_url(output)
                if not image_url:
                    raise DashScopeError(
                        provider="DashScope",
                        category=ProviderErrorCategory.INVALID_RESPONSE,
                        detail={"message": "Successful image task without image URL", "response": data},
                        retryable=False,
                    )
                return RemoteResult(
                    url=image_url,
                    model=model,
                    task_id=task_id,
                    usage=data.get("usage", {}),
                    raw=data,
                )
            if status in {"FAILED", "CANCELED", "UNKNOWN"}:
                detail = {
                    "task_status": status,
                    "message": output.get("message"),
                    "code": output.get("code"),
                }
                raise DashScopeError(
                    provider="DashScope",
                    category=categorize_provider_error(
                        code=str(output.get("code")) if output.get("code") else None,
                        message=detail,
                    ),
                    code=str(output.get("code")) if output.get("code") else None,
                    detail=detail,
                    retryable=False,
                )
            await asyncio.sleep(interval)
            interval = min(interval * 1.25, 15)
        raise DashScopeError(
            provider="DashScope",
            category=ProviderErrorCategory.TIMEOUT,
            detail={"message": "Image task timed out", "timeout_seconds": timeout_seconds},
        )

    @staticmethod
    def _extract_image_url(output: dict[str, Any]) -> str | None:
        candidates = []
        try:
            candidates.append(output["choices"][0]["message"]["content"][0].get("image"))
        except (KeyError, IndexError, TypeError, AttributeError):
            pass
        if output.get("results"):
            candidates.append(output["results"][0].get("url"))
        return next((value for value in candidates if value), None)

    def _tts_url(self) -> str:
        base_url = self.settings.qwen_tts_base_url or self.settings.dashscope_native_base
        return f"{base_url.rstrip('/')}/services/aigc/multimodal-generation/generation"

    def _tts_headers(self, *, include_workspace: bool = False) -> dict[str, str]:
        api_key = self.settings.qwen_tts_api_key or self.settings.dashscope_api_key or self.settings.qwen_api_key
        if not api_key:
            raise RuntimeError("QWEN_TTS_API_KEY, DASHSCOPE_API_KEY, or QWEN_API_KEY is required for TTS")
        headers = {
            "Authorization": f"Bearer {api_key}",
            "Content-Type": "application/json",
        }
        if include_workspace and self.settings.qwen_tts_workspace_id:
            headers["X-DashScope-WorkSpace"] = self.settings.qwen_tts_workspace_id
        return headers

    async def synthesize_speech(self, *, text: str, language: str, instructions: str | None = None) -> RemoteResult:
        url = self._tts_url()
        input_data: dict[str, Any] = {
            "text": text,
            "voice": self.settings.qwen_tts_voice,
            "language_type": language,
        }
        payload: dict[str, Any] = {"model": self.settings.qwen_tts_model, "input": input_data}
        if instructions and "instruct" in self.settings.qwen_tts_model.lower():
            payload["parameters"] = {
                "instructions": instructions,
                "optimize_instructions": True,
            }
        try:
            data = await self._request("POST", url, payload=payload, extra_headers=self._tts_headers())
        except ProviderCallError as exc:
            if exc.status_code not in {401, 403} or not self.settings.qwen_tts_workspace_id:
                raise
            data = await self._request(
                "POST",
                url,
                payload=payload,
                extra_headers=self._tts_headers(include_workspace=True),
            )
        output = data.get("output", {})
        audio_url = None
        if isinstance(output.get("audio"), dict):
            audio_url = output["audio"].get("url")
        if not audio_url and output.get("audio_url"):
            audio_url = output["audio_url"]
        if not audio_url:
            raise DashScopeError(
                provider="DashScope",
                category=ProviderErrorCategory.INVALID_RESPONSE,
                detail={"message": "Unexpected TTS response", "response": data},
                retryable=False,
            )
        return RemoteResult(url=audio_url, model=self.settings.qwen_tts_model, usage=data.get("usage", {}), raw=data)

    async def submit_video(self, *, payload: dict[str, Any], model: str) -> str:
        url = f"{self.settings.dashscope_native_base}/services/aigc/video-generation/video-synthesis"
        data = await self._request("POST", url, payload={"model": model, **payload}, async_call=True)
        task_id = data.get("output", {}).get("task_id")
        if not task_id:
            raise DashScopeError(
                provider="DashScope",
                category=ProviderErrorCategory.INVALID_RESPONSE,
                detail={"message": "Unexpected video task response", "response": data},
                retryable=False,
            )
        return str(task_id)

    async def poll_video(self, task_id: str, *, model: str, timeout_seconds: int = 1200) -> RemoteResult:
        url = f"{self.settings.dashscope_native_base}/tasks/{task_id}"
        deadline = asyncio.get_running_loop().time() + timeout_seconds
        interval = 5.0
        while asyncio.get_running_loop().time() < deadline:
            data = await self._request("GET", url)
            output = data.get("output", {})
            status = output.get("task_status")
            if status == "SUCCEEDED":
                video_url = output.get("video_url")
                for collection in (output.get("results"), output.get("videos")):
                    if not video_url and collection:
                        video_url = collection[0].get("url") or collection[0].get("video_url")
                if not video_url:
                    raise DashScopeError(
                        provider="DashScope",
                        category=ProviderErrorCategory.INVALID_RESPONSE,
                        detail={"message": "Successful task without video URL", "response": data},
                        retryable=False,
                    )
                return RemoteResult(
                    url=video_url,
                    model=model,
                    task_id=task_id,
                    usage=data.get("usage", {}),
                    raw=data,
                )
            if status in {"FAILED", "CANCELED", "UNKNOWN"}:
                detail = {
                    "task_status": status,
                    "message": output.get("message"),
                    "code": output.get("code"),
                }
                raise DashScopeError(
                    provider="DashScope",
                    category=categorize_provider_error(
                        code=str(output.get("code")) if output.get("code") else None,
                        message=detail,
                    ),
                    code=str(output.get("code")) if output.get("code") else None,
                    detail=detail,
                    retryable=False,
                )
            await asyncio.sleep(interval)
            interval = min(interval * 1.25, 15)
        raise DashScopeError(
            provider="DashScope",
            category=ProviderErrorCategory.TIMEOUT,
            detail={"message": "Video task timed out", "timeout_seconds": timeout_seconds},
        )
