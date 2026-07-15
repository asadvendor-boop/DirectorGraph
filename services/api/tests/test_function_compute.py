import httpx
import pytest
from fastapi import BackgroundTasks, HTTPException

from app.api import routes as routes_module
from app.api.routes import function_compute_task_route
from app.config import Settings
from app.function_compute import FunctionComputeInvocationError, FunctionComputeTaskInvoker


def test_function_compute_invoker_sends_async_task_headers(tmp_path):
    captured = {}

    def handler(request: httpx.Request) -> httpx.Response:
        captured["headers"] = request.headers
        captured["body"] = request.content
        return httpx.Response(202, headers={"X-Fc-Request-Id": "req-123"})

    settings = Settings(
        function_compute_task_url="https://fc.example.invalid/task",
        function_compute_auth_header="Bearer test",
        media_root=tmp_path / "media",
        oss_repository_root=tmp_path / "oss",
        database_url=f"sqlite:///{tmp_path / 'fc.db'}",
    )
    invocation = FunctionComputeTaskInvoker(
        settings,
        transport=httpx.MockTransport(handler),
    ).invoke({"task_id": "dg-task-1"}, task_id="dg-task-1")

    assert invocation.request_id == "req-123"
    assert invocation.status_code == 202
    assert captured["headers"]["X-Fc-Invocation-Type"] == "Async"
    assert captured["headers"]["X-Fc-Async-Task-Id"] == "dg-task-1"
    assert captured["headers"]["Authorization"] == "Bearer test"


def test_function_compute_invoker_rejects_non_accepted_response(tmp_path):
    settings = Settings(
        function_compute_task_url="https://fc.example.invalid/task",
        media_root=tmp_path / "media",
        oss_repository_root=tmp_path / "oss",
        database_url=f"sqlite:///{tmp_path / 'fc.db'}",
    )
    invoker = FunctionComputeTaskInvoker(
        settings,
        transport=httpx.MockTransport(lambda request: httpx.Response(500, text="failed")),
    )

    with pytest.raises(FunctionComputeInvocationError):
        invoker.invoke({"task_id": "dg-task-1"}, task_id="dg-task-1")


@pytest.mark.asyncio
async def test_function_compute_task_endpoint_requires_task_mode(tmp_path):
    settings = Settings(
        app_mode="web",
        media_root=tmp_path / "media",
        oss_repository_root=tmp_path / "oss",
        database_url=f"sqlite:///{tmp_path / 'fc.db'}",
    )

    with pytest.raises(HTTPException) as exc_info:
        await function_compute_task_route({"task_id": "dg-task-1"}, settings=settings)

    assert exc_info.value.status_code == 404


@pytest.mark.asyncio
async def test_function_compute_task_endpoint_requires_configured_auth(tmp_path):
    settings = Settings(
        app_mode="task",
        function_compute_auth_header="Bearer task-secret",
        media_root=tmp_path / "media",
        oss_repository_root=tmp_path / "oss",
        database_url=f"sqlite:///{tmp_path / 'fc.db'}",
    )

    with pytest.raises(HTTPException) as exc_info:
        await function_compute_task_route(
            {"task_id": "dg-task-1"},
            settings=settings,
            authorization="Bearer wrong",
        )

    assert exc_info.value.status_code == 403


@pytest.mark.asyncio
async def test_function_compute_task_endpoint_processes_payload(monkeypatch, tmp_path):
    settings = Settings(
        app_mode="task",
        function_compute_auth_header="Bearer task-secret",
        media_root=tmp_path / "media",
        oss_repository_root=tmp_path / "oss",
        database_url=f"sqlite:///{tmp_path / 'fc.db'}",
    )
    captured = {}

    async def fake_run_task(payload):
        captured["payload"] = payload
        return {"status": "processed", "job_id": "job-123", "task_id": payload["task_id"]}

    monkeypatch.setattr(routes_module, "process_function_compute_task", fake_run_task)

    result = await function_compute_task_route(
        {"task_id": "dg-task-1"},
        settings=settings,
        authorization="Bearer task-secret",
    )

    assert captured["payload"] == {"task_id": "dg-task-1"}
    assert result == {
        "accepted": True,
        "status": "processed",
        "job_id": "job-123",
        "task_id": "dg-task-1",
    }


@pytest.mark.asyncio
async def test_function_compute_task_endpoint_async_acknowledges_without_waiting(monkeypatch, tmp_path):
    settings = Settings(
        app_mode="task",
        function_compute_auth_header="Bearer task-secret",
        media_root=tmp_path / "media",
        oss_repository_root=tmp_path / "oss",
        database_url=f"sqlite:///{tmp_path / 'fc.db'}",
    )
    captured = {}

    async def fake_run_task(payload):
        captured["payload"] = payload
        return {"status": "processed", "job_id": "job-123", "task_id": payload["task_id"]}

    monkeypatch.setattr(routes_module, "process_function_compute_task", fake_run_task)
    background_tasks = BackgroundTasks()

    result = await function_compute_task_route(
        {"task_id": "dg-task-1"},
        background_tasks=background_tasks,
        settings=settings,
        authorization="Bearer task-secret",
        invocation_type="Async",
    )

    assert captured == {}
    assert len(background_tasks.tasks) == 1
    assert result == {"accepted": True, "task_id": "dg-task-1", "status": "queued"}
