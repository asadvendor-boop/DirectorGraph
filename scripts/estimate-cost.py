#!/usr/bin/env python3
from __future__ import annotations

import argparse
import json
from dataclasses import asdict, dataclass
from datetime import UTC, datetime


RATES = {
    "story_call": 0.08,
    "vision_inspection": 0.02,
    "storyboard": 0.05,
    "tts_per_character": 0.00003,
    "wan_i2v_per_second_720": 0.08,
    "wan_i2v_per_second_1080": 0.13,
    "wan_r2v_per_second_720": 0.10,
    "wan_r2v_per_second_1080": 0.16,
    "happyhorse_t2v_per_second_720": 0.09,
    "happyhorse_t2v_per_second_1080": 0.15,
    "local_edit_per_second_720": 0.07,
    "local_edit_per_second_1080": 0.12,
}


@dataclass(frozen=True)
class Estimate:
    schema: str
    generated_at: str
    profile: str
    duration_seconds: int
    shots: int
    project_cap_usd: float
    total_cap_usd: float
    repair_reserve_percent: int
    repair_reserve_usd: float
    estimated_story_usd: float
    estimated_storyboards_usd: float
    estimated_tts_usd: float
    estimated_first_pass_video_usd: float
    estimated_inspection_usd: float
    estimated_one_repair_usd: float
    estimated_total_usd: float
    within_project_cap: bool
    within_total_cap: bool
    notes: list[str]


def money(value: float) -> float:
    return round(value, 4)


def estimate(args: argparse.Namespace) -> Estimate:
    seconds_per_shot = args.duration_seconds / max(args.shots, 1)
    hero_shots = max(1, round(args.shots * args.hero_fraction))
    base_shots = max(args.shots - hero_shots, 0)
    hero_rate = RATES["wan_r2v_per_second_1080" if args.allow_1080p else "wan_r2v_per_second_720"]
    base_rate = RATES["wan_i2v_per_second_720"]
    first_pass = money(hero_shots * seconds_per_shot * hero_rate + base_shots * seconds_per_shot * base_rate)
    one_repair = money(seconds_per_shot * RATES["local_edit_per_second_720"])
    storyboards = money((args.shots + args.characters) * RATES["storyboard"])
    tts = money(args.dialogue_characters * RATES["tts_per_character"])
    inspection = money(args.shots * RATES["vision_inspection"] * max(args.max_attempts, 1))
    reserve = money(args.project_cap_usd * args.repair_reserve_percent / 100)
    total = money(RATES["story_call"] + storyboards + tts + first_pass + inspection + one_repair)
    notes = [
        "Rates are transparent local estimates used for cap checks, not provider billing claims.",
        "Run live billing/export reconciliation after paid generation before public cost claims.",
    ]
    if args.profile == "judge_test":
        notes.append("Judge Test estimates should remain below the capped live smoke-test budget.")
    return Estimate(
        schema="directorgraph.cost-estimate.v1",
        generated_at=datetime.now(UTC).isoformat(),
        profile=args.profile,
        duration_seconds=args.duration_seconds,
        shots=args.shots,
        project_cap_usd=args.project_cap_usd,
        total_cap_usd=args.total_cap_usd,
        repair_reserve_percent=args.repair_reserve_percent,
        repair_reserve_usd=reserve,
        estimated_story_usd=money(RATES["story_call"]),
        estimated_storyboards_usd=storyboards,
        estimated_tts_usd=tts,
        estimated_first_pass_video_usd=first_pass,
        estimated_inspection_usd=inspection,
        estimated_one_repair_usd=one_repair,
        estimated_total_usd=total,
        within_project_cap=total <= args.project_cap_usd,
        within_total_cap=total <= args.total_cap_usd,
        notes=notes,
    )


def parser() -> argparse.ArgumentParser:
    item = argparse.ArgumentParser(description="Estimate DirectorGraph live-run spend before approval.")
    item.add_argument("--profile", choices=["standard", "judge_test"], default="standard")
    item.add_argument("--duration-seconds", type=int, default=45)
    item.add_argument("--shots", type=int, default=7)
    item.add_argument("--characters", type=int, default=2)
    item.add_argument("--dialogue-characters", type=int, default=280)
    item.add_argument("--project-cap-usd", type=float, default=6.0)
    item.add_argument("--total-cap-usd", type=float, default=35.0)
    item.add_argument("--repair-reserve-percent", type=int, default=20)
    item.add_argument("--max-attempts", type=int, default=2)
    item.add_argument("--hero-fraction", type=float, default=0.35)
    item.add_argument("--allow-1080p", action="store_true")
    item.add_argument("--fail-over-cap", action="store_true")
    return item


def main() -> None:
    args = parser().parse_args()
    result = estimate(args)
    print(json.dumps(asdict(result), indent=2, sort_keys=True))
    if args.fail_over_cap and not (result.within_project_cap and result.within_total_cap):
        raise SystemExit(1)


if __name__ == "__main__":
    main()
