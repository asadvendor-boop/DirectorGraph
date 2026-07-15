from __future__ import annotations

from abc import ABC, abstractmethod
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

from app.schemas import Character, ProjectBrief, QualityReport, ShotContract, StoryPlan


@dataclass(slots=True)
class AssetResult:
    public_url: str
    local_path: Path
    provider: str
    model: str
    task_id: str | None = None
    usage: dict[str, Any] = field(default_factory=dict)
    object_key: str | None = None
    provider_result_key: str | None = None
    asset_checkpoint_key: str | None = None


@dataclass(slots=True)
class PlanResult:
    plan: StoryPlan
    model: str
    input_tokens: int = 0
    output_tokens: int = 0
    degraded: bool = False
    degradation_reason: str | None = None
    planning_path: str = "unknown"
    character_bound_shots: int = 0
    plan_repair_attempted: bool = False
    plan_repair_reason: str | None = None


@dataclass(slots=True)
class InspectionResult:
    report: QualityReport
    model: str
    input_tokens: int = 0
    output_tokens: int = 0


class StudioProvider(ABC):
    @abstractmethod
    async def plan_story(self, brief: ProjectBrief) -> PlanResult: ...

    @abstractmethod
    async def generate_character_reference(
        self, project_id: str, character: Character, seed: int
    ) -> AssetResult: ...

    @abstractmethod
    async def generate_storyboard(
        self, project_id: str, contract: ShotContract, seed: int
    ) -> AssetResult: ...

    @abstractmethod
    async def synthesize_voice(
        self, project_id: str, contract: ShotContract, language: str
    ) -> AssetResult | None: ...

    @abstractmethod
    async def generate_video(
        self,
        project_id: str,
        contract: ShotContract,
        storyboard: AssetResult,
        audio: AssetResult | None,
        references: list[AssetResult],
        attempt: int,
        repair_instruction: str | None = None,
    ) -> AssetResult: ...

    @abstractmethod
    async def inspect_video(
        self, contract: ShotContract, video: AssetResult, attempt: int
    ) -> InspectionResult: ...

    @abstractmethod
    async def repair_video(
        self,
        project_id: str,
        contract: ShotContract,
        video: AssetResult,
        storyboard: AssetResult,
        references: list[AssetResult],
        report: QualityReport,
        attempt: int,
    ) -> AssetResult: ...

    async def close(self) -> None:
        return None
