from app.core.story import fallback_story_plan, story_user_prompt
from app.schemas import GeneratedStoryPlan, ProjectBrief


def brief(**overrides):
    values = {
        "title": "The Last Delivery",
        "premise": "A courier robot completes the one delivery nobody has ever accepted.",
        "duration_seconds": 42,
        "budget_usd": 18,
        "required_prop": "red paper crane",
    }
    values.update(overrides)
    return ProjectBrief(**values)


def test_fallback_story_is_valid_and_exact_duration():
    project_brief = brief(duration_seconds=42)
    plan = fallback_story_plan(project_brief)
    assert len(plan.shots) == 7
    assert sum(shot.duration_seconds for shot in plan.shots) == 42
    assert [shot.id for shot in plan.shots] == [f"S{index:02d}" for index in range(1, 8)]
    assert [shot.sequence for shot in plan.shots] == list(range(1, 8))
    assert {shot.beat_id for shot in plan.shots} == {beat.id for beat in plan.beats}


def test_judge_test_story_is_compact_and_exact_duration():
    project_brief = brief(
        duration_seconds=15,
        budget_usd=3,
        max_shots=3,
        production_profile="judge_test",
    )
    plan = fallback_story_plan(project_brief)

    assert len(plan.shots) == 3
    assert sum(shot.duration_seconds for shot in plan.shots) == 15
    assert [shot.id for shot in plan.shots] == ["S01", "S02", "S03"]
    assert all("red paper crane" in shot.video_prompt for shot in plan.shots)


def test_standard_capped_story_can_fallback_to_six_reference_ready_shots():
    project_brief = brief(
        duration_seconds=24,
        max_shots=6,
        production_profile="standard",
    )
    plan = fallback_story_plan(project_brief)

    assert len(plan.shots) == 6
    assert sum(shot.duration_seconds for shot in plan.shots) == 24
    assert [shot.id for shot in plan.shots] == [f"S{index:02d}" for index in range(1, 7)]
    assert all(shot.characters for shot in plan.shots)
    assert all((shot.dialogue or shot.narration) for shot in plan.shots)
    assert plan.shots[-1].continuity.end_state["power"] == "one steady pulse"


def test_story_prompt_requires_reusable_characters_and_per_shot_bindings():
    prompt = story_user_prompt(brief(duration_seconds=24, max_shots=6))

    assert "characters array" in prompt
    assert "shot.characters" in prompt
    assert "C01" in prompt
    assert "character IDs" in prompt
    assert "empty" in prompt


def test_story_prompt_requires_per_shot_speakable_audio_line():
    prompt = story_user_prompt(brief(duration_seconds=24, max_shots=6))

    assert "Every shot must include one short English dialogue or narration line" in prompt
    assert "dialogue or narration" in prompt


def test_generation_story_schema_requires_per_shot_character_ids():
    schema = GeneratedStoryPlan.model_json_schema()
    shot_schema = schema["$defs"]["GeneratedShotContract"]

    assert shot_schema["properties"]["characters"]["minItems"] == 1


def test_recurring_prop_is_a_machine_readable_continuity_requirement():
    plan = fallback_story_plan(brief())
    reveal = next(shot for shot in plan.shots if shot.id == "S05")
    climax = next(shot for shot in plan.shots if shot.id == "S06")
    assert "red paper crane" in reveal.continuity.required_props
    assert "red paper crane" in climax.continuity.required_props
    assert reveal.continuity.start_state["door"] == "closed"
    assert reveal.continuity.end_state["door"] == "open"


def test_plan_adapts_aspect_ratio_and_language_input():
    project_brief = brief(aspect_ratio="16:9", language="Urdu")
    plan = fallback_story_plan(project_brief)
    assert all(shot.aspect_ratio == "16:9" for shot in plan.shots)
