from __future__ import annotations

from copy import deepcopy
from typing import Any

from app.schemas import Beat, ProductionLedger, ProjectBrief, ShotContract, StoryPlan

BEAT_SALIENCE: dict[str, float] = {
    "hook": 0.96,
    "setup": 0.52,
    "escalation": 0.66,
    "reveal": 0.94,
    "climax": 1.0,
    "resolution": 0.78,
}

# Transparent estimates for routing and comparisons, not a claim about current provider billing.
ESTIMATED_COSTS = {
    "story_call": 0.08,
    "vision_inspection": 0.02,
    "storyboard": 0.05,
    "wan_i2v_per_second_720": 0.08,
    "wan_i2v_per_second_1080": 0.13,
    "wan_r2v_per_second_720": 0.10,
    "wan_r2v_per_second_1080": 0.16,
    "happyhorse_t2v_per_second_720": 0.09,
    "happyhorse_t2v_per_second_1080": 0.15,
    "local_edit_per_second_720": 0.07,
    "local_edit_per_second_1080": 0.12,
    "tts_per_character": 0.00003,
}


def _beat_map(beats: list[Beat]) -> dict[str, Beat]:
    return {beat.id: beat for beat in beats}


def route_and_budget(plan: StoryPlan, brief: ProjectBrief) -> StoryPlan:
    """Attach salience, renderer, resolution, and retry policy to every shot."""
    routed = deepcopy(plan)
    beats = _beat_map(routed.beats)
    reserve = brief.budget_usd * brief.repair_reserve_percent / 100
    production_budget = max(brief.budget_usd - reserve, 0.5)
    duration = max(sum(shot.duration_seconds for shot in routed.shots), 1)

    for shot in routed.shots:
        shot.aspect_ratio = brief.aspect_ratio
        beat = beats[shot.beat_id]
        base = BEAT_SALIENCE[beat.beat_type]
        shot.salience = round(
            min(base + (0.05 if shot.dialogue else 0) + (0.04 if len(shot.characters) > 1 else 0), 1),
            2,
        )
        shot.renderer = "wan_r2v" if shot.characters else "wan_i2v"
        proportional_budget = production_budget * shot.duration_seconds / duration
        hero = shot.salience >= 0.9
        shot.resolution = "1080P" if hero and proportional_budget >= 0.7 else "720P"
        shot.max_retries = 2 if hero else 1
        if brief.budget_usd < 8:
            shot.resolution = "720P"
            shot.max_retries = min(shot.max_retries, 1)
        shot.quality_threshold = 0.82 if hero else 0.80
    return routed


def estimate_video_cost(contract: ShotContract, seconds: float | None = None) -> float:
    seconds = seconds if seconds is not None else contract.duration_seconds
    suffix = "1080" if contract.resolution == "1080P" else "720"
    return round(ESTIMATED_COSTS[f"{contract.renderer}_per_second_{suffix}"] * seconds, 4)


def estimate_repair_cost(contract: ShotContract, local: bool) -> float:
    if not local:
        return estimate_video_cost(contract)
    suffix = "1080" if contract.resolution == "1080P" else "720"
    return round(ESTIMATED_COSTS[f"local_edit_per_second_{suffix}"] * contract.duration_seconds, 4)


def estimate_story_cost() -> float:
    return round(ESTIMATED_COSTS["story_call"], 4)


def estimate_storyboard_cost(count: int = 1) -> float:
    return round(ESTIMATED_COSTS["storyboard"] * count, 4)


def estimate_tts_cost(characters: int) -> float:
    return round(ESTIMATED_COSTS["tts_per_character"] * characters, 4)


def estimate_inspection_cost() -> float:
    return round(ESTIMATED_COSTS["vision_inspection"], 4)


def can_spend_on_repair(ledger: ProductionLedger, estimated_cost: float) -> bool:
    remaining = ledger.budget_usd - ledger.estimated_cost_usd
    return remaining >= estimated_cost


def enforce_live_spend_cap(settings: Any, ledger: ProductionLedger, estimated_cost: float) -> None:
    if settings.provider_mode != "live":
        return
    projected = round(ledger.estimated_cost_usd + estimated_cost, 4)
    project_cap = min(ledger.budget_usd, settings.max_project_spend_usd)
    if projected > project_cap:
        raise RuntimeError(
            f"Live spend cap refused provider call: projected ${projected:.4f} "
            f"exceeds project cap ${project_cap:.4f}"
        )
    if projected > settings.max_total_live_spend_usd:
        raise RuntimeError(
            f"Live spend cap refused provider call: projected ${projected:.4f} "
            f"exceeds total cap ${settings.max_total_live_spend_usd:.4f}"
        )


def record_story_usage(
    ledger: ProductionLedger,
    input_tokens: int = 0,
    output_tokens: int = 0,
    *,
    include_cost: bool = True,
) -> None:
    ledger.text_input_tokens += input_tokens
    ledger.text_output_tokens += output_tokens
    if include_cost:
        ledger.estimated_cost_usd = round(ledger.estimated_cost_usd + estimate_story_cost(), 4)


def record_storyboard(ledger: ProductionLedger, count: int = 1, *, include_cost: bool = True) -> None:
    ledger.image_count += count
    if include_cost:
        ledger.estimated_cost_usd = round(ledger.estimated_cost_usd + estimate_storyboard_cost(count), 4)


def record_tts(ledger: ProductionLedger, characters: int, *, include_cost: bool = True) -> None:
    if include_cost:
        ledger.estimated_cost_usd = round(ledger.estimated_cost_usd + estimate_tts_cost(characters), 4)


def record_inspection(
    ledger: ProductionLedger,
    vision_tokens: int = 0,
    *,
    include_cost: bool = True,
) -> None:
    ledger.vision_input_tokens += vision_tokens
    if include_cost:
        ledger.estimated_cost_usd = round(ledger.estimated_cost_usd + estimate_inspection_cost(), 4)
