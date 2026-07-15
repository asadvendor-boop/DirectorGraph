from __future__ import annotations

from dataclasses import dataclass
from typing import Any

import httpx

from app.config import Settings


class FunctionComputeInvocationError(RuntimeError):
    pass


@dataclass(frozen=True, slots=True)
class FunctionComputeInvocation:
    task_id: str
    request_id: str | None
    status_code: int


class FunctionComputeTaskInvoker:
    def __init__(self, settings: Settings, *, transport: httpx.BaseTransport | None = None):
        if not settings.function_compute_task_url:
            raise ValueError("FUNCTION_COMPUTE_TASK_URL is not configured")
        self.settings = settings
        self.transport = transport

    def invoke(self, payload: dict[str, Any], *, task_id: str) -> FunctionComputeInvocation:
        headers = {
            "Content-Type": "application/json",
            "X-Fc-Invocation-Type": "Async",
            "X-Fc-Async-Task-Id": task_id,
        }
        if self.settings.function_compute_auth_header:
            headers["Authorization"] = self.settings.function_compute_auth_header
        with httpx.Client(
            timeout=self.settings.function_compute_invoke_timeout_seconds,
            transport=self.transport,
        ) as client:
            response = client.post(
                self.settings.function_compute_task_url,
                headers=headers,
                json=payload,
            )
        if response.status_code != 202:
            raise FunctionComputeInvocationError(
                f"Function Compute task invocation failed with HTTP {response.status_code}: "
                f"{response.text[:500]}"
            )
        return FunctionComputeInvocation(
            task_id=task_id,
            request_id=response.headers.get("X-Fc-Request-Id"),
            status_code=response.status_code,
        )


def invoke_function_compute_task(
    settings: Settings,
    payload: dict[str, Any],
    *,
    task_id: str,
) -> FunctionComputeInvocation:
    return FunctionComputeTaskInvoker(settings).invoke(payload, task_id=task_id)
