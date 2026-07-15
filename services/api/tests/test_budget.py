import pytest

from app.config import Settings
from app.core.budget import (
    can_spend_on_repair,
    enforce_live_spend_cap,
    estimate_inspection_cost,
    estimate_repair_cost,
    estimate_story_cost,
    estimate_storyboard_cost,
    estimate_tts_cost,
    estimate_video_cost,
    record_inspection,
    record_story_usage,
    record_storyboard,
    record_tts,
    route_and_budget,
)
from app.core.story import fallback_story_plan
from app.schemas import ProductionLedger, ProjectBrief


def make_brief(budget: float = 18) -> ProjectBrief:
    return ProjectBrief(
        title="Budget test",
        premise="A courier completes a final delivery before its battery disappears forever.",
        duration_seconds=42,
        budget_usd=budget,
    )


def test_salience_routes_character_shots_to_reference_video_and_high_quality():
    brief = make_brief()
    plan = route_and_budget(fallback_story_plan(brief), brief)
    climax = next(shot for shot in plan.shots if shot.beat_id == "B06")
    setup = next(shot for shot in plan.shots if shot.beat_id == "B02")
    assert climax.salience == 1
    assert climax.renderer == "wan_r2v"
    assert climax.resolution == "1080P"
    assert climax.max_retries == 2
    assert setup.salience < climax.salience
    assert setup.resolution == "720P"


def test_low_budget_forces_720p_and_restricts_retries():
    brief = make_brief(budget=5)
    plan = route_and_budget(fallback_story_plan(brief), brief)
    assert all(shot.resolution == "720P" for shot in plan.shots)
    assert all(shot.max_retries <= 1 for shot in plan.shots)


def test_cost_and_repair_guard_are_deterministic():
    brief = make_brief()
    shot = route_and_budget(fallback_story_plan(brief), brief).shots[0]
    render_cost = estimate_video_cost(shot)
    local_cost = estimate_repair_cost(shot, local=True)
    assert render_cost > 0
    assert local_cost > 0
    assert estimate_story_cost() > 0
    assert estimate_storyboard_cost(2) == estimate_storyboard_cost() * 2
    assert estimate_tts_cost(100) > 0
    assert estimate_inspection_cost() > 0
    ledger = ProductionLedger(budget_usd=10, estimated_cost_usd=9.95)
    assert not can_spend_on_repair(ledger, 0.1)
    assert can_spend_on_repair(ledger, 0.04)


def test_usage_recorders_can_skip_reserved_live_cost():
    ledger = ProductionLedger(budget_usd=10)

    record_story_usage(ledger, input_tokens=10, output_tokens=20, include_cost=False)
    record_storyboard(ledger, include_cost=False)
    record_tts(ledger, 100, include_cost=False)
    record_inspection(ledger, vision_tokens=30, include_cost=False)

    assert ledger.text_input_tokens == 10
    assert ledger.text_output_tokens == 20
    assert ledger.image_count == 1
    assert ledger.vision_input_tokens == 30
    assert ledger.estimated_cost_usd == 0


def test_live_spend_cap_refuses_project_overrun(tmp_path):
    settings = Settings(
        provider_mode="live",
        max_project_spend_usd=1,
        max_total_live_spend_usd=10,
        media_root=tmp_path / "media",
        oss_repository_root=tmp_path / "oss",
        database_url=f"sqlite:///{tmp_path / 'budget.db'}",
    )
    ledger = ProductionLedger(budget_usd=5, estimated_cost_usd=0.95)

    enforce_live_spend_cap(settings, ledger, 0.04)
    with pytest.raises(RuntimeError, match="project cap"):
        enforce_live_spend_cap(settings, ledger, 0.06)
