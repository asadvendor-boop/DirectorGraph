from __future__ import annotations

from math import floor

from app.schemas import (
    Beat,
    CameraSpec,
    Character,
    ContinuitySpec,
    ProjectBrief,
    ShotContract,
    StoryPlan,
)

SHOWRUNNER_SYSTEM_PROMPT = """You are DirectorGraph's Executive Showrunner and Narrative Compiler.
Create a production-ready short drama, not an essay. Return only data conforming to the supplied
JSON schema. Every shot must be independently renderable while remaining consistent with a shared
story bible. Use the requested total duration, at most two principal characters, one primary
location where practical, a visible recurring prop when the brief provides one, a strong first-three-second hook, escalating
stakes, a visual reveal, and an emotionally legible ending. Camera direction must be concrete.
Avoid copyrighted characters, celebrity likenesses, logos, unsafe content, and text that must be
rendered inside generated footage. Do not require letters, numerals, signage, HUD overlays, logos,
reflected text, or other tiny readable details as acceptance criteria. Favor broad, visible checks
such as character presence, prop visibility, gesture, location, lighting, and camera movement.
Dialogue must fit its shot. State wardrobe, props, and state transitions explicitly in every
continuity contract."""


def story_user_prompt(brief: ProjectBrief) -> str:
    max_shots = brief.max_shots or (3 if brief.duration_seconds < 21 else 8)
    min_shots = 2 if max_shots <= 3 else 6
    shot_range = f"{min_shots}-{max_shots}"
    return f"""Compile this brief into StoryIR.
Title: {brief.title}
Premise: {brief.premise}
Genre: {brief.genre}
Tone: {brief.tone}
Audience: {brief.target_audience}
Duration: exactly {brief.duration_seconds} seconds across {shot_range} shots
Aspect ratio: {brief.aspect_ratio}
Language: {brief.language}
Visual style: {brief.visual_style}
Recurring prop: {brief.required_prop if brief.required_prop else 'none required; use location, character, lighting, and motion continuity instead'}
Budget: ${brief.budget_usd:.2f}; favor one location and reusable references.
Production profile: {brief.production_profile}.

Return JSON only. Shot IDs must be S01, S02, ...; beat IDs B01, B02, ... . Sum shot durations to the
requested total. Keep every shot contract renderable by current text-to-video/image-to-video models:
no logos, no readable text, no countdown numerals, no microscopic reflections, and no compliance
requirement that depends on exact typography. The Budget Governor will finalize model routing after
compilation.

Character binding is mandatory for reference-video routing:
- Populate the characters array with reusable character objects using stable IDs such as C01 and C02.
- Every shot.characters value must be a non-empty list of those exact character IDs.
- Do not leave shot.characters empty, null, omitted, or filled with character names.
- Every referenced character ID in a shot must exist in the characters array.

Audio is mandatory for this production:
- Every shot must include one short English dialogue or narration line.
- Prefer narration for quiet visual shots; use dialogue only when a visible character speaks.
- Keep each dialogue or narration line five words or fewer so Qwen-TTS can synthesize it cleanly."""


def _allocate_durations(total: int, weights: list[float], minimum: int = 3) -> list[int]:
    remaining = total - minimum * len(weights)
    if remaining < 0:
        raise ValueError("Duration too short")
    raw = [remaining * weight / sum(weights) for weight in weights]
    values = [minimum + floor(value) for value in raw]
    missing = total - sum(values)
    order = sorted(range(len(raw)), key=lambda i: raw[i] - floor(raw[i]), reverse=True)
    for i in order[:missing]:
        values[i] += 1
    return values


def fallback_story_plan(brief: ProjectBrief) -> StoryPlan:
    """Deterministic cinematic plan used for zero-key demos and as a validated fallback."""
    if brief.production_profile == "judge_test" or brief.max_shots or brief.duration_seconds < 21:
        return compact_story_plan(brief)

    prop = brief.required_prop or "red paper crane"
    d = _allocate_durations(brief.duration_seconds, [0.10, 0.12, 0.14, 0.14, 0.18, 0.17, 0.15])
    characters = [
        Character(
            id="C01",
            name="Mira",
            role="The resident who stopped opening the door",
            appearance="late twenties, expressive dark eyes, short black hair",
            wardrobe="dark forest-green coat over neutral sleepwear",
            voice_direction="quiet and guarded, then relieved warmth",
            motivation="to know whether hope can survive being unanswered",
            reference_prompt="cinematic reference, Mira, short black hair, forest-green coat, consistent face, neutral backdrop",
        ),
        Character(
            id="C02",
            name="Courier-7",
            role="An aging delivery robot completing its final route",
            appearance="small weathered cream robot with one amber status light",
            wardrobe="scratched courier shell, blue route stripe, no logos",
            voice_direction="gentle synthetic voice, precise but tender",
            motivation="to complete the promise encoded in its route",
            reference_prompt="small weathered cream courier robot, amber light, blue stripe, friendly proportions, no logo",
        ),
    ]
    beat_data = [
        ("The final route", "hook", "Signal that tonight is the robot's last chance", "curiosity to concern"),
        ("The ritual", "setup", "Establish the unanswered delivery and recurring prop", "concern to tenderness"),
        ("Time accumulated", "escalation", "Show that the robot has returned for years", "tenderness to ache"),
        ("Shutdown warning", "escalation", "Introduce an irreversible deadline", "ache to urgency"),
        ("The door opens", "reveal", "Reverse the pattern and reveal Mira", "urgency to shock"),
        ("Recognition", "climax", "Deliver the meaning of the repeated route", "shock to relief"),
        ("Promise delivered", "resolution", "End on a visual transfer of hope", "relief to bittersweet peace"),
    ]
    beats = [
        Beat(id=f"B{i:02d}", name=name, beat_type=kind, objective=obj, emotional_shift=shift, duration_seconds=d[i-1])
        for i, (name, kind, obj, shift) in enumerate(beat_data, 1)
    ]
    location = "narrow apartment hallway at night, rain-lit window at the far end"
    specs = [
        ("Last route", "Courier-7 rolls into the empty hallway as its amber light falters.", None, "weary determination", "wide establishing shot", "slow low dolly", "knee height", [], {}, {"robot": "outside 4B"}),
        ("The paper promise", f"Courier-7 places a parcel topped with a {prop} at the closed door.", None, "ritualistic tenderness", "close-up insert", "controlled lateral slide", "floor level", [prop, "parcel"], {"parcel": "in gripper"}, {"parcel": "on mat", "prop": "on parcel"}),
        ("Every night", "A compressed montage shows rain, dust, and changing seasons while the same robot returns.", None, "steadfast loneliness", "locked medium wide", "time-lapse push-in", "eye level", [prop, "parcel"], {"door": "closed"}, {"robot": "more weathered"}),
        ("Power at one percent", "The amber light flickers; Courier-7 nudges the parcel to the threshold.", "Route expires at dawn.", "quiet urgency", "macro to medium", "rack focus to door", "robot eye level", [prop, "parcel"], {"power": "one percent"}, {"power": "critical"}),
        ("The impossible sound", "The lock turns. The door opens and warm light cuts across Courier-7.", None, "stunned recognition", "medium reveal", "slow push through light", "eye level", [prop, "parcel"], {"door": "closed", "Mira": "unseen"}, {"door": "open", "Mira": "visible", "prop": "between them"}),
        ("You came back", f"Mira kneels, takes the {prop}, and meets Courier-7's flickering light.", "You came back.\nI never stopped.", "recognition into relief", "intimate close-ups", "gentle handheld drift", "eye level while kneeling", [prop, "parcel"], {"prop": "on parcel"}, {"prop": "in Mira's hands"}),
        ("Delivered", f"At dawn, Mira places the {prop} behind the robot's cracked badge plate as its light steadies once.", "Delivery complete.", "bittersweet peace", "two-shot silhouette", "slow crane backward", "waist height", [prop], {"power": "critical"}, {"prop": "on robot", "power": "one steady pulse"}),
    ]
    shots: list[ShotContract] = []
    for i, spec in enumerate(specs, 1):
        title, action, dialogue, emotion, framing, movement, angle, props, start, end = spec
        char_ids = ["C02"] if i < 5 else ["C01", "C02"]
        wardrobe = {"C02": characters[1].wardrobe}
        if i >= 5:
            wardrobe["C01"] = characters[0].wardrobe
        shot_id = f"S{i:02d}"
        shots.append(
            ShotContract(
                id=shot_id,
                sequence=i,
                beat_id=f"B{i:02d}",
                title=title,
                duration_seconds=d[i-1],
                aspect_ratio=brief.aspect_ratio,
                narrative_objective=beats[i-1].objective,
                characters=char_ids,
                action=action,
                dialogue=dialogue,
                narration=None if dialogue else f"{title}.",
                emotion=emotion,
                location=location,
                camera=CameraSpec(framing=framing, movement=movement, angle=angle),
                continuity=ContinuitySpec(
                    location=location,
                    time_of_day="night moving toward dawn" if i >= 6 else "rainy night",
                    wardrobe=wardrobe,
                    required_props=props,
                    start_state=start,
                    end_state=end,
                ),
                storyboard_prompt=f"{brief.visual_style}. Storyboard {shot_id}: {action} {location}. {framing}, {angle}, {emotion}. Show {', '.join(props) or 'the environment'} clearly.",
                video_prompt=f"{brief.visual_style}. {action} Camera: {movement}, {framing}, {angle}. Emotion: {emotion}. Preserve wardrobe and props. No logos or on-screen text.",
            )
        )
    return StoryPlan(
        title=brief.title,
        logline=brief.premise,
        theme="Hope survives through repeated acts of care.",
        synopsis="On its final route, a weathered courier robot returns to the one apartment that has never opened. The ritual finally receives an answer.",
        visual_rules=[
            "Cold hallway light contrasts with warm light behind apartment 4B.",
            f"The {prop} stays saturated and legible whenever required.",
            "Mira's green coat and Courier-7's amber light never change.",
            "No generated typography or brand marks inside footage.",
        ],
        audio_rules=[
            "Sparse rain ambience leaves space for dialogue.",
            "Courier-7 sounds gentle rather than comic.",
            "Music rises only after the door opens.",
        ],
        characters=characters,
        beats=beats,
        shots=shots,
    )


def compact_story_plan(brief: ProjectBrief) -> StoryPlan:
    """Bounded profile for judge-triggered and capped live productions."""
    prop = brief.required_prop or "red paper crane"
    max_compact_shots = 3 if brief.production_profile == "judge_test" or brief.duration_seconds < 21 else 6
    seconds_per_shot = 2 if max_compact_shots == 3 else 4
    shot_count = min(brief.max_shots or 3, max(2, brief.duration_seconds // seconds_per_shot))
    shot_count = max(2, min(shot_count, max_compact_shots))
    weights = [1 / shot_count] * shot_count
    durations = _allocate_durations(brief.duration_seconds, weights, minimum=2)
    characters = [
        Character(
            id="C01",
            name="Mira",
            role="The resident who finally answers",
            appearance="late twenties, expressive dark eyes, short black hair",
            wardrobe="dark forest-green coat over neutral sleepwear",
            voice_direction="quiet and guarded, then relieved warmth",
            motivation="to know whether hope can survive being unanswered",
            reference_prompt="cinematic reference, Mira, short black hair, forest-green coat, consistent face, neutral backdrop",
        ),
        Character(
            id="C02",
            name="Courier-7",
            role="An aging delivery robot completing its final route",
            appearance="small weathered cream robot with one amber status light",
            wardrobe="scratched courier shell, blue route stripe, no logos",
            voice_direction="gentle synthetic voice, precise but tender",
            motivation="to complete the promise encoded in its route",
            reference_prompt="small weathered cream courier robot, amber light, blue stripe, friendly proportions, no logo",
        ),
    ]
    library = [
        {
            "beat": ("Final approach", "hook", "Show the final delivery and the visible continuity anchor", "curiosity to concern"),
            "spec": ("The last parcel", f"Courier-7 arrives and places a parcel topped with a {prop} at apartment 4B.", None, "weary tenderness", "close low tracking shot", "slow dolly to the threshold", "floor level", ["parcel", prop], {"robot": "entering hallway"}, {"parcel": "on mat", "prop": "on parcel"}, ["C02"]),
        },
        {
            "beat": ("The ritual", "setup", "Show the repeated delivery pattern without relying on readable signage", "concern to tenderness"),
            "spec": (f"The {prop}", f"Courier-7 carefully centers the parcel and the {prop} under the warm door light.", None, "ritualistic tenderness", "close-up insert", "controlled lateral slide", "floor level", ["parcel", prop], {"parcel": "in gripper"}, {"parcel": "on mat", "prop": "on parcel"}, ["C02"]),
        },
        {
            "beat": ("Every night", "escalation", "Compress the passage of time while preserving the same hallway and prop", "tenderness to ache"),
            "spec": ("Every night", "Rain, dust, and changing light pass over Courier-7 returning to the same closed door.", None, "steadfast loneliness", "locked medium wide", "time-lapse push-in", "eye level", ["parcel", prop], {"door": "closed"}, {"robot": "more weathered", "prop": "still saturated"}, ["C02"]),
        },
        {
            "beat": ("Power critical", "escalation", "Introduce the irreversible deadline before the door opens", "ache to urgency"),
            "spec": ("Power at one percent", "Courier-7's amber light flickers as it nudges the parcel to the threshold.", "Route expires at dawn.", "quiet urgency", "macro to medium", "rack focus to the door", "robot eye level", ["parcel", prop], {"power": "one percent"}, {"power": "critical", "parcel": "at threshold"}, ["C02"]),
        },
        {
            "beat": ("Door opens", "reveal", "Reveal Mira and force the contract to preserve the prop", "urgency to recognition"),
            "spec": ("The door opens", f"The lock turns; warm light reveals Mira kneeling beside Courier-7 and the {prop}.", "You came back.", "stunned recognition", "medium reveal", "slow push through warm light", "eye level", ["parcel", prop], {"door": "closed", "Mira": "unseen"}, {"door": "open", "Mira": "visible", "prop": "between them"}, ["C01", "C02"]),
        },
        {
            "beat": ("Promise delivered", "resolution", "Resolve the route with a small emotional transfer", "recognition to peace"),
            "spec": ("Delivered", f"Mira places the {prop} against Courier-7's cracked badge plate as its amber light steadies.", "Delivery complete.", "bittersweet peace", "intimate two-shot", "gentle crane backward", "waist height", [prop], {"prop": "in Mira's hand"}, {"prop": "on robot", "power": "one steady pulse"}, ["C01", "C02"]),
        },
    ]
    selected_indexes = {
        2: [0, 5],
        3: [0, 4, 5],
        4: [0, 3, 4, 5],
        5: [0, 1, 3, 4, 5],
        6: [0, 1, 2, 3, 4, 5],
    }[shot_count]
    selected = [library[index] for index in selected_indexes]
    beat_data = [item["beat"] for item in selected]
    beats = [
        Beat(id=f"B{i:02d}", name=name, beat_type=kind, objective=obj, emotional_shift=shift, duration_seconds=durations[i-1])
        for i, (name, kind, obj, shift) in enumerate(beat_data, 1)
    ]
    location = "narrow apartment hallway at night, rain-lit window at the far end"
    specs = [item["spec"] for item in selected]
    shots: list[ShotContract] = []
    for i, spec in enumerate(specs, 1):
        title, action, dialogue, emotion, framing, movement, angle, props, start, end, char_ids = spec
        shot_id = f"S{i:02d}"
        wardrobe = {"C02": characters[1].wardrobe}
        if "C01" in char_ids:
            wardrobe["C01"] = characters[0].wardrobe
        shots.append(
            ShotContract(
                id=shot_id,
                sequence=i,
                beat_id=f"B{i:02d}",
                title=title,
                duration_seconds=durations[i-1],
                aspect_ratio=brief.aspect_ratio,
                narrative_objective=beats[i-1].objective,
                characters=char_ids,
                action=action,
                dialogue=dialogue,
                narration=None if dialogue else f"{title}.",
                emotion=emotion,
                location=location,
                camera=CameraSpec(framing=framing, movement=movement, angle=angle),
                continuity=ContinuitySpec(
                    location=location,
                    time_of_day="rainy night moving toward dawn",
                    wardrobe=wardrobe,
                    required_props=props,
                    start_state=start,
                    end_state=end,
                ),
                storyboard_prompt=f"{brief.visual_style}. Compact judge-test storyboard {shot_id}: {action} {location}. {framing}, {angle}. Make {', '.join(props)} clearly visible.",
                video_prompt=f"{brief.visual_style}. {action} Camera: {movement}, {framing}, {angle}. Preserve Mira, Courier-7, the {prop}, and the hallway. No logos or on-screen text.",
            )
        )
    return StoryPlan(
        title=brief.title,
        logline=brief.premise,
        theme="A promise can be fulfilled in one small visible act.",
        synopsis="A capped judge-test version of The Last Delivery preserves the prop, reveal, and emotional resolution in a few shots.",
        visual_rules=[
            "Cold hallway light contrasts with warm light behind apartment 4B.",
            f"The {prop} stays saturated and legible whenever required.",
            "Mira's green coat and Courier-7's amber light never change.",
            "No generated typography or brand marks inside footage.",
        ],
        audio_rules=[
            "Sparse rain ambience leaves space for dialogue.",
            "Dialogue remains short enough for the capped duration.",
            "Music is optional and understated.",
        ],
        characters=characters,
        beats=beats,
        shots=shots,
    )
