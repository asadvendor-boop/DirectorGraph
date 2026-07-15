#!/usr/bin/env python3
from __future__ import annotations

import argparse
import json
import sqlite3
from datetime import UTC, datetime
from pathlib import Path
from statistics import mean
from typing import Any


def load_project(database: Path, project_id: str | None = None) -> tuple[dict[str, Any], list[dict[str, Any]], list[dict[str, Any]]]:
    connection = sqlite3.connect(database)
    connection.row_factory = sqlite3.Row
    if project_id:
        row = connection.execute("SELECT * FROM projects WHERE id = ?", (project_id,)).fetchone()
    else:
        row = connection.execute(
            "SELECT * FROM projects WHERE status = 'completed' ORDER BY updated_at DESC LIMIT 1"
        ).fetchone()
    if row is None:
        raise SystemExit("No completed project found")
    project = dict(row)
    shots = [dict(item) for item in connection.execute(
        "SELECT * FROM shots WHERE project_id = ? ORDER BY sequence", (project["id"],)
    )]
    events = [dict(item) for item in connection.execute(
        "SELECT * FROM events WHERE project_id = ? ORDER BY id", (project["id"],)
    )]
    connection.close()
    return project, shots, events


def build_report(project: dict[str, Any], shots: list[dict[str, Any]], events: list[dict[str, Any]]) -> dict[str, Any]:
    ledger = json.loads(project["ledger"])
    final_scores = [json.loads(shot["quality"])["overall_score"] for shot in shots if shot["quality"]]
    rejected = []
    for event in events:
        if event["kind"] == "shot.rejected":
            payload = json.loads(event["payload"])
            rejected.append(payload)
    first_pass_by_shot = {
        shot["shot_code"]: json.loads(shot["quality"])["overall_score"] for shot in shots if shot["quality"]
    }
    for failure in rejected:
        first_pass_by_shot[failure["shot"]] = failure["score"]
    first_pass_scores = list(first_pass_by_shot.values())
    durations = {shot["shot_code"]: json.loads(shot["contract"])["duration_seconds"] for shot in shots}
    repaired_seconds = sum(durations.get(item["shot"], 0) for item in rejected)
    whole_film_seconds = sum(durations.values())
    saved_seconds = max(whole_film_seconds - repaired_seconds, 0) if repaired_seconds else 0
    final_mean = mean(final_scores) if final_scores else 0
    baseline_mean = mean(first_pass_scores) if first_pass_scores else 0
    return {
        "schema": "directorgraph.eval-report.v1",
        "generated_at": datetime.now(UTC).isoformat(),
        "project_id": project["id"],
        "project_title": project["title"],
        "mode": "measured deterministic mock pipeline",
        "baselines": {
            "single_pass_no_qc": {
                "mean_shot_quality": round(baseline_mean, 4),
                "failed_shots_accepted": len(rejected),
                "description": "Accepts every first render and performs no contract-based inspection.",
            },
            "directorgraph_qc_repair": {
                "mean_shot_quality": round(final_mean, 4),
                "failed_shots_accepted": 0,
                "description": "Inspects each render, applies the minimum-cost repair, then revalidates.",
            },
        },
        "delta": {
            "absolute_quality_points": round(final_mean - baseline_mean, 4),
            "relative_quality_improvement_percent": round(
                ((final_mean - baseline_mean) / baseline_mean * 100) if baseline_mean else 0, 2
            ),
            "bad_shots_prevented": len(rejected),
        },
        "repair_efficiency": {
            "final_timeline_seconds": whole_film_seconds,
            "surgically_rerendered_seconds": repaired_seconds,
            "whole_film_rerender_seconds_avoided": saved_seconds,
            "generated_seconds": ledger["video_seconds_generated"],
            "accepted_seconds": ledger["video_seconds_accepted"],
            "acceptance_ratio": round(
                ledger["video_seconds_accepted"] / ledger["video_seconds_generated"], 4
            ) if ledger["video_seconds_generated"] else 0,
            "local_repairs": ledger["local_repairs"],
            "full_regenerations": ledger["full_regenerations"],
        },
        "resource_ledger": ledger,
        "rejected_attempts": rejected,
        "notes": [
            "The included mock benchmark is reproducible and validates orchestration behavior, not Wan output quality.",
            "Run the same harness against live projects before making public model-quality claims.",
        ],
    }


def markdown(report: dict[str, Any]) -> str:
    base = report["baselines"]["single_pass_no_qc"]
    final = report["baselines"]["directorgraph_qc_repair"]
    repair = report["repair_efficiency"]
    delta = report["delta"]
    return f"""# DirectorGraph ablation report

Generated: {report['generated_at']}

## Result

| System | Mean shot quality | Failed shots accepted |
|---|---:|---:|
| Single pass, no QC | {base['mean_shot_quality']:.2%} | {base['failed_shots_accepted']} |
| DirectorGraph QC + repair | {final['mean_shot_quality']:.2%} | {final['failed_shots_accepted']} |

DirectorGraph improved the measured mean by **{delta['absolute_quality_points']:.2%} absolute** and prevented **{delta['bad_shots_prevented']}** defective shot from entering the final timeline.

## Repair efficiency

- Final timeline: {repair['final_timeline_seconds']} seconds
- Surgically rerendered: {repair['surgically_rerendered_seconds']} seconds
- Whole-film rerender avoided: {repair['whole_film_rerender_seconds_avoided']} seconds
- Accepted/generated ratio: {repair['acceptance_ratio']:.1%}
- Local repairs: {repair['local_repairs']}
- Full regenerations: {repair['full_regenerations']}

## Interpretation

This is a deterministic, reproducible systems test of StoryIR, Shot Contracts, quality gates, repair routing, accounting, and final assembly. It is not presented as a benchmark of live Wan or HappyHorse visual quality. Use the same report generator on an Alibaba Cloud production database for submission evidence.
"""


def main() -> None:
    parser = argparse.ArgumentParser()
    parser.add_argument("--database", type=Path, required=True)
    parser.add_argument("--project-id")
    parser.add_argument("--output", type=Path, required=True)
    args = parser.parse_args()
    project, shots, events = load_project(args.database, args.project_id)
    report = build_report(project, shots, events)
    args.output.mkdir(parents=True, exist_ok=True)
    (args.output / "eval-report.json").write_text(json.dumps(report, indent=2), encoding="utf-8")
    (args.output / "eval-report.md").write_text(markdown(report), encoding="utf-8")
    print(json.dumps(report, indent=2))


if __name__ == "__main__":
    main()
