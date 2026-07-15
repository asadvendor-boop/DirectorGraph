from __future__ import annotations

import json
import re
from typing import Any

from mcp.server.fastmcp import FastMCP

mcp = FastMCP(
    "DirectorGraph Studio",
    instructions=(
        "Production-control tools for validating shot contracts, estimating budget, building edit "
        "decision lists, and limiting semantic revisions to affected shots."
    ),
)

COST_PER_SECOND = {
    ("wan_i2v", "720P"): 0.08,
    ("wan_i2v", "1080P"): 0.13,
    ("wan_r2v", "720P"): 0.10,
    ("wan_r2v", "1080P"): 0.16,
    ("happyhorse_t2v", "720P"): 0.09,
    ("happyhorse_t2v", "1080P"): 0.15,
    ("local_edit", "720P"): 0.07,
    ("local_edit", "1080P"): 0.12,
}


def _loads(value: str) -> Any:
    try:
        return json.loads(value)
    except json.JSONDecodeError as exc:
        raise ValueError(f"Invalid JSON: {exc.msg}") from exc


@mcp.tool()
def validate_shot_contract(contract_json: str) -> dict[str, Any]:
    """Validate the minimum machine-readable contract required before rendering a shot."""
    contract = _loads(contract_json)
    required = {
        "id",
        "sequence",
        "duration_seconds",
        "narrative_objective",
        "action",
        "emotion",
        "camera",
        "continuity",
        "storyboard_prompt",
        "video_prompt",
    }
    missing = sorted(required - set(contract))
    continuity = contract.get("continuity") or {}
    continuity_missing = sorted(
        {"location", "time_of_day", "required_props", "start_state", "end_state"}
        - set(continuity)
    )
    duration = contract.get("duration_seconds")
    errors = [f"missing field: {name}" for name in missing]
    errors.extend(f"continuity missing field: {name}" for name in continuity_missing)
    if not isinstance(duration, int) or not 2 <= duration <= 15:
        errors.append("duration_seconds must be an integer from 2 to 15")
    return {
        "valid": not errors,
        "errors": errors,
        "contract_id": contract.get("id"),
        "quality_gate_ready": not errors,
    }


@mcp.tool()
def estimate_render_budget(
    renderer: str, resolution: str, duration_seconds: float, attempts: int = 1
) -> dict[str, Any]:
    """Estimate render spend and reserve exposure for one shot. Rates are replaceable telemetry defaults."""
    key = (renderer, resolution)
    if key not in COST_PER_SECOND:
        raise ValueError(f"Unsupported renderer/resolution pair: {key}")
    if duration_seconds <= 0 or attempts <= 0:
        raise ValueError("duration_seconds and attempts must be positive")
    cost = round(COST_PER_SECOND[key] * duration_seconds * attempts, 4)
    return {
        "renderer": renderer,
        "resolution": resolution,
        "seconds_generated": duration_seconds * attempts,
        "attempts": attempts,
        "estimated_cost_usd": cost,
        "rate_kind": "configurable_estimate",
    }


@mcp.tool()
def build_edit_decision_list(shot_contracts_json: str) -> dict[str, Any]:
    """Convert ordered Shot Contracts into a deterministic edit decision list with timecodes."""
    shots = sorted(_loads(shot_contracts_json), key=lambda item: item["sequence"])
    cursor = 0.0
    entries = []
    for shot in shots:
        start = cursor
        end = start + float(shot["duration_seconds"])
        entries.append(
            {
                "shot_id": shot["id"],
                "timeline_in": round(start, 3),
                "timeline_out": round(end, 3),
                "duration": round(end - start, 3),
                "caption": shot.get("dialogue") or shot.get("narration"),
            }
        )
        cursor = end
    return {"duration_seconds": round(cursor, 3), "entries": entries}


@mcp.tool()
def semantic_impact_analysis(instruction: str, shot_contracts_json: str) -> dict[str, Any]:
    """Rank shots affected by a creative revision so untouched footage can be preserved."""
    shots = _loads(shot_contracts_json)
    tokens = {
        token
        for token in re.findall(r"[a-z0-9-]+", instruction.lower())
        if len(token) > 3 and token not in {"change", "make", "shot", "scene", "more", "with"}
    }
    ranked = []
    for shot in shots:
        haystack = json.dumps(shot, ensure_ascii=False).lower()
        matches = sorted(token for token in tokens if token in haystack)
        score = len(matches) / max(len(tokens), 1)
        if matches:
            ranked.append(
                {
                    "shot_id": shot["id"],
                    "impact_score": round(score, 3),
                    "matched_concepts": matches,
                    "reason": "Revision concepts overlap this shot contract.",
                }
            )
    if not ranked and shots:
        ranked = [
            {
                "shot_id": shots[-1]["id"],
                "impact_score": 0.25,
                "matched_concepts": [],
                "reason": "No direct semantic match; defaulting to the resolution shot for human review.",
            }
        ]
    ranked.sort(key=lambda item: item["impact_score"], reverse=True)
    return {
        "instruction": instruction,
        "affected_shots": ranked,
        "preserved_shot_count": max(len(shots) - len(ranked), 0),
    }


if __name__ == "__main__":
    mcp.run(transport="stdio")
