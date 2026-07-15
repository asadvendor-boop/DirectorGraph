from __future__ import annotations

from datetime import datetime
from enum import StrEnum
from typing import Any, Literal

from pydantic import BaseModel, ConfigDict, Field, computed_field, model_validator


class ProjectStatus(StrEnum):
    DRAFT = "draft"
    QUEUED = "queued"
    PLANNING = "planning"
    STORYBOARDING = "storyboarding"
    PRODUCING = "producing"
    INSPECTING = "inspecting"
    EDITING = "editing"
    COMPLETED = "completed"
    FAILED = "failed"


class ShotStatus(StrEnum):
    PLANNED = "planned"
    STORYBOARDING = "storyboarding"
    STORYBOARDED = "storyboarded"
    RENDERING = "rendering"
    INSPECTING = "inspecting"
    REPAIRING = "repairing"
    ACCEPTED = "accepted"
    FAILED = "failed"


class JobStatus(StrEnum):
    PENDING = "pending"
    RUNNING = "running"
    SUCCEEDED = "succeeded"
    FAILED = "failed"
    CANCELED = "canceled"


class ProjectBrief(BaseModel):
    title: str = Field(min_length=2, max_length=120)
    premise: str = Field(min_length=12, max_length=1500)
    genre: str = Field(default="science-fiction drama", max_length=80)
    tone: str = Field(default="cinematic, intimate, emotionally resonant", max_length=160)
    target_audience: str = Field(default="global short-form drama viewers", max_length=160)
    duration_seconds: int = Field(default=42, ge=5, le=120)
    aspect_ratio: Literal["9:16", "16:9", "1:1"] = "9:16"
    language: str = Field(default="English", max_length=40)
    visual_style: str = Field(
        default="grounded cinematic realism, controlled lighting, shallow depth of field",
        max_length=300,
    )
    budget_usd: float = Field(default=20.0, ge=1.0, le=1000.0)
    repair_reserve_percent: int = Field(default=18, ge=5, le=40)
    seed: int = Field(default=20260710, ge=0, le=2_147_483_647)
    required_prop: str | None = Field(default="red paper crane", max_length=120)
    max_shots: int | None = Field(default=None, ge=2, le=8)
    production_profile: Literal["standard", "judge_test"] = "standard"


class Character(BaseModel):
    id: str
    name: str
    role: str
    appearance: str
    wardrobe: str
    voice_direction: str
    motivation: str
    reference_prompt: str
    reference_url: str | None = None


class Beat(BaseModel):
    id: str
    name: str
    beat_type: Literal["hook", "setup", "escalation", "reveal", "climax", "resolution"]
    objective: str
    emotional_shift: str
    duration_seconds: int = Field(ge=1, le=30)


class CameraSpec(BaseModel):
    framing: str
    movement: str
    angle: str
    lens: str = "50mm cinematic"


class ContinuitySpec(BaseModel):
    location: str
    time_of_day: str
    wardrobe: dict[str, str] = Field(default_factory=dict)
    required_props: list[str] = Field(default_factory=list)
    start_state: dict[str, str] = Field(default_factory=dict)
    end_state: dict[str, str] = Field(default_factory=dict)


class ShotContract(BaseModel):
    id: str
    sequence: int = Field(ge=1)
    beat_id: str
    title: str
    duration_seconds: int = Field(ge=2, le=15)
    aspect_ratio: Literal["9:16", "16:9", "1:1"] = "9:16"
    narrative_objective: str
    characters: list[str] = Field(default_factory=list)
    action: str
    dialogue: str | None = None
    narration: str | None = None
    emotion: str
    location: str
    camera: CameraSpec
    continuity: ContinuitySpec
    storyboard_prompt: str
    video_prompt: str
    negative_prompt: str = "identity drift, extra fingers, deformed face, illegible text, flicker"
    salience: float = Field(default=0.5, ge=0, le=1)
    renderer: Literal["wan_i2v", "wan_r2v", "happyhorse_t2v"] = "wan_i2v"
    resolution: Literal["720P", "1080P"] = "720P"
    max_retries: int = Field(default=1, ge=0, le=3)
    quality_threshold: float = Field(default=0.82, ge=0.5, le=1)


class GeneratedShotContract(ShotContract):
    characters: list[str] = Field(min_length=1)


class StoryPlan(BaseModel):
    schema_version: str = "1.0"
    title: str
    logline: str
    theme: str
    synopsis: str
    visual_rules: list[str]
    audio_rules: list[str]
    characters: list[Character]
    beats: list[Beat]
    shots: list[ShotContract]

    @model_validator(mode="after")
    def validate_graph(self) -> StoryPlan:
        beat_ids = {beat.id for beat in self.beats}
        shot_ids = [shot.id for shot in self.shots]
        if len(shot_ids) != len(set(shot_ids)):
            raise ValueError("Shot IDs must be unique")
        if any(shot.beat_id not in beat_ids for shot in self.shots):
            raise ValueError("Every shot must reference a known beat")
        if [shot.sequence for shot in self.shots] != list(range(1, len(self.shots) + 1)):
            raise ValueError("Shot sequence must be contiguous and one-indexed")
        return self


class GeneratedStoryPlan(StoryPlan):
    characters: list[Character] = Field(min_length=1)
    shots: list[GeneratedShotContract]


class QualityDimension(BaseModel):
    name: Literal["narrative", "identity", "continuity", "camera", "motion", "dialogue", "safety"]
    score: float = Field(ge=0, le=1)
    evidence: str


class QualityReport(BaseModel):
    passed: bool
    overall_score: float = Field(ge=0, le=1)
    dimensions: list[QualityDimension]
    violations: list[str] = Field(default_factory=list)
    repair_strategy: Literal["none", "local_edit", "regenerate", "human_review"] = "none"
    repair_instruction: str | None = None
    evaluator_model: str
    attempt: int = 1


class ProductionLedger(BaseModel):
    text_input_tokens: int = 0
    text_output_tokens: int = 0
    vision_input_tokens: int = 0
    video_seconds_generated: float = 0
    video_seconds_accepted: float = 0
    rejected_generation_seconds: float = 0
    image_count: int = 0
    full_regenerations: int = 0
    local_repairs: int = 0
    estimated_cost_usd: float = 0
    budget_usd: float = 20
    repair_reserve_usd: float = 3.6

    @computed_field
    @property
    def budget_remaining_usd(self) -> float:
        return round(max(self.budget_usd - self.estimated_cost_usd, 0), 4)

    @computed_field
    @property
    def acceptance_ratio(self) -> float:
        if self.video_seconds_generated == 0:
            return 0
        return round(self.video_seconds_accepted / self.video_seconds_generated, 4)


class EventRead(BaseModel):
    model_config = ConfigDict(from_attributes=True)
    id: int
    kind: str
    message: str
    agent: str
    payload: dict[str, Any]
    created_at: datetime


class ShotRead(BaseModel):
    id: str
    shot_code: str
    sequence: int
    status: ShotStatus
    contract: ShotContract
    storyboard_url: str | None = None
    audio_url: str | None = None
    video_url: str | None = None
    quality: QualityReport | None = None
    attempts: int
    accepted: bool


class ProjectRead(BaseModel):
    id: str
    title: str
    status: ProjectStatus
    brief: ProjectBrief
    plan: StoryPlan | None = None
    ledger: ProductionLedger
    final_video_url: str | None = None
    error: str | None = None
    created_at: datetime
    updated_at: datetime
    shots: list[ShotRead] = Field(default_factory=list)
    events: list[EventRead] = Field(default_factory=list)


class RunResponse(BaseModel):
    project_id: str
    job_id: str
    task_id: str
    status: JobStatus


class PatchRequest(BaseModel):
    instruction: str = Field(min_length=4, max_length=1000)
    affected_shot_ids: list[str] = Field(default_factory=list)


class JudgeTestRequest(BaseModel):
    premise: str | None = Field(default=None, min_length=12, max_length=500)


class PublicConfig(BaseModel):
    provider_mode: str
    live_ready: bool
    oss_ready: bool
    public_demo_project_id: str | None = None
    judge_access_code_configured: bool = False
    models: dict[str, str]
