from app import db as db_module
from app import main as main_module
from app.api.routes import readiness_payload
from app.config import Settings
from app.main import discover_frontend_dist_path, initialize_startup_state, task_mode_allows_path


def test_health_metadata_settings_are_explicit(tmp_path):
    settings = Settings(
        app_mode="web",
        build_sha="abc123",
        build_timestamp="2026-06-24T00:00:00Z",
        media_root=tmp_path / "media",
        database_url=f"sqlite:///{tmp_path / 'runtime.db'}",
    )

    payload = readiness_payload(settings)

    assert payload["status"] == "ready"
    assert payload["mode"] == "web"
    assert payload["state_backend"] == "local"
    assert payload["build"]["sha"] == "abc123"
    assert payload["checks"]["provider_configured"]


def test_live_readiness_is_degraded_without_credentials(tmp_path):
    settings = Settings(
        app_mode="task",
        provider_mode="live",
        dashscope_api_key=None,
        media_root=tmp_path / "media",
        database_url=f"sqlite:///{tmp_path / 'runtime.db'}",
    )

    payload = readiness_payload(settings)

    assert payload["status"] == "degraded"
    assert payload["mode"] == "task"
    assert not payload["checks"]["live_credentials_ready"]


def test_workspace_endpoint_overrides_are_used_for_live_clients(tmp_path):
    settings = Settings(
        app_mode="web",
        provider_mode="live",
        dashscope_api_key="media-key",
        qwen_api_key="text-key",
        qwen_base_url_override="https://text-workspace.example.com/compatible-mode/v1/",
        dashscope_native_base_url="https://media-workspace.example.com/api/v1/",
        media_root=tmp_path / "media",
        database_url=f"sqlite:///{tmp_path / 'runtime.db'}",
    )

    assert settings.qwen_base_url == "https://text-workspace.example.com/compatible-mode/v1"
    assert settings.dashscope_native_base == "https://media-workspace.example.com/api/v1"
    assert settings.live_ready


def test_frontend_dist_discovery_tolerates_api_only_container_layout(tmp_path):
    api_file = tmp_path / "app" / "main.py"
    api_file.parent.mkdir(parents=True)
    api_file.write_text("", encoding="utf-8")

    assert discover_frontend_dist_path(api_file) == api_file.parent / "_missing_frontend_dist"


def test_oss_state_backend_readiness_does_not_require_database_url(tmp_path):
    settings = Settings(
        app_mode="web",
        state_backend="oss",
        database_url="",
        media_root=tmp_path / "media",
        oss_repository_root=tmp_path / "oss",
    )

    payload = readiness_payload(settings)

    assert payload["status"] == "ready"
    assert payload["state_backend"] == "oss"
    assert not payload["checks"]["database_configured"]
    assert not payload["checks"]["sql_startup_required"]


def test_oss_state_backend_skips_sql_startup(monkeypatch, tmp_path):
    settings = Settings(
        app_mode="web",
        state_backend="oss",
        database_url=f"sqlite:///{tmp_path / 'should-not-be-created.db'}",
        media_root=tmp_path / "media",
        oss_repository_root=tmp_path / "oss",
    )
    monkeypatch.setattr(main_module, "settings", settings)
    monkeypatch.setattr(main_module, "init_db", lambda: (_ for _ in ()).throw(AssertionError("init_db called")))

    initialize_startup_state()


def test_oss_state_backend_allows_missing_database_url(monkeypatch, tmp_path):
    settings = Settings(
        app_mode="task",
        state_backend="oss",
        database_url="",
        media_root=tmp_path / "media",
        oss_repository_root=tmp_path / "oss",
    )
    monkeypatch.setattr(db_module, "settings", settings)

    assert db_module.resolve_database_url() == "sqlite:///:memory:"


def test_task_mode_route_guard_allows_only_task_operational_paths():
    assert task_mode_allows_path("/api/health")
    assert task_mode_allows_path("/api/readiness")
    assert task_mode_allows_path("/api/function-compute/tasks")
    assert not task_mode_allows_path("/")
    assert not task_mode_allows_path("/api/projects")
    assert not task_mode_allows_path("/media/projects/example/final.mp4")
