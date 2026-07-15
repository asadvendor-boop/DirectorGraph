from __future__ import annotations

from functools import lru_cache
from pathlib import Path
from typing import Literal

from pydantic import Field, computed_field
from pydantic_settings import BaseSettings, SettingsConfigDict

Region = Literal["singapore", "virginia", "beijing", "hongkong"]
ProviderMode = Literal["mock", "live"]
AppMode = Literal["web", "task"]
StateBackend = Literal["local", "oss"]


class Settings(BaseSettings):
    model_config = SettingsConfigDict(
        env_file=(".env", "../../.env"),
        env_file_encoding="utf-8",
        case_sensitive=False,
        extra="ignore",
        populate_by_name=True,
    )

    app_name: str = "DirectorGraph"
    app_version: str = "0.1.0"
    app_mode: AppMode = "web"
    state_backend: StateBackend = "local"
    environment: str = "development"
    build_sha: str = "local"
    build_timestamp: str = "local"
    provider_mode: ProviderMode = "mock"
    database_url: str = "sqlite:///./data/directorgraph.db"
    media_root: Path = Path("./media")
    oss_repository_root: Path = Path("./data/oss-repository")
    frontend_dist: Path | None = None
    public_media_base_url: str = "http://localhost:8000/media"
    cors_origins: str = "http://localhost:5173,http://localhost:3000"
    seed_demo: bool = True
    inline_worker: bool = True
    worker_poll_seconds: float = 1.0
    max_parallel_renders: int = Field(default=3, ge=1, le=12)
    log_level: str = "INFO"
    max_total_live_spend_usd: float = Field(default=35.0, ge=0)
    max_project_spend_usd: float = Field(default=6.0, ge=0)
    repair_reserve_percent: int = Field(default=20, ge=5, le=40)
    max_render_attempts_per_shot: int = Field(default=2, ge=1, le=5)
    judge_run_max_duration_seconds: int = Field(default=15, ge=5, le=30)
    judge_run_max_shots: int = Field(default=3, ge=1, le=5)
    public_demo_project_id: str | None = None
    judge_create_access_code: str | None = None
    function_compute_task_url: str | None = None
    function_compute_auth_header: str | None = None
    function_compute_invoke_timeout_seconds: float = Field(default=10.0, ge=1, le=60)

    dashscope_api_key: str | None = None
    qwen_api_key: str | None = None
    qwen_base_url_override: str | None = Field(default=None, alias="QWEN_BASE_URL")
    dashscope_native_base_url: str | None = None
    dashscope_region: Region = "singapore"
    qwen_story_model: str = "qwen-plus"
    qwen_vision_model: str = "qwen3-vl-flash"
    wan_image_model: str = "wan2.6-t2i"
    wan_video_model: str = "wan2.6-i2v"
    wan_reference_model: str = "wan2.7-r2v"
    happyhorse_video_model: str = "happyhorse-1.1-t2v"
    happyhorse_edit_model: str = "happyhorse-1.1-t2v"
    qwen_tts_api_key: str | None = None
    qwen_tts_base_url: str | None = None
    qwen_tts_workspace_id: str | None = None
    qwen_tts_model: str = "qwen3-tts-instruct-flash"
    qwen_tts_voice: str = "Cherry"
    dashscope_image_pace_seconds: float = Field(default=1.25, ge=0, le=30)

    oss_endpoint: str | None = None
    oss_bucket: str | None = None
    oss_access_key_id: str | None = None
    oss_access_key_secret: str | None = None
    oss_public_base_url: str | None = None

    @computed_field
    @property
    def qwen_base_url(self) -> str:
        if self.qwen_base_url_override:
            return self.qwen_base_url_override.rstrip("/")
        values = {
            "singapore": "https://dashscope-intl.aliyuncs.com/compatible-mode/v1",
            "virginia": "https://dashscope-us.aliyuncs.com/compatible-mode/v1",
            "beijing": "https://dashscope.aliyuncs.com/compatible-mode/v1",
            "hongkong": "https://cn-hongkong.dashscope.aliyuncs.com/compatible-mode/v1",
        }
        return values[self.dashscope_region]

    @computed_field
    @property
    def dashscope_native_base(self) -> str:
        if self.dashscope_native_base_url:
            return self.dashscope_native_base_url.rstrip("/")
        values = {
            "singapore": "https://dashscope-intl.aliyuncs.com/api/v1",
            "virginia": "https://dashscope-us.aliyuncs.com/api/v1",
            "beijing": "https://dashscope.aliyuncs.com/api/v1",
            "hongkong": "https://cn-hongkong.dashscope.aliyuncs.com/api/v1",
        }
        return values[self.dashscope_region]

    @property
    def cors_origin_list(self) -> list[str]:
        return [value.strip() for value in self.cors_origins.split(",") if value.strip()]

    @property
    def live_ready(self) -> bool:
        return bool(self.dashscope_api_key or self.qwen_api_key)

    @property
    def oss_ready(self) -> bool:
        return all(
            [
                self.oss_endpoint,
                self.oss_bucket,
                self.oss_access_key_id,
                self.oss_access_key_secret,
            ]
        )

    def prepare_directories(self) -> None:
        self.media_root.mkdir(parents=True, exist_ok=True)
        if self.state_backend != "oss" or not self.oss_ready:
            self.oss_repository_root.mkdir(parents=True, exist_ok=True)
        if self.state_backend != "oss" and self.database_url.startswith("sqlite"):
            value = self.database_url.split("///", maxsplit=1)[-1]
            if value not in (":memory:", ""):
                Path(value).expanduser().parent.mkdir(parents=True, exist_ok=True)


@lru_cache
def get_settings() -> Settings:
    settings = Settings()
    settings.prepare_directories()
    return settings
