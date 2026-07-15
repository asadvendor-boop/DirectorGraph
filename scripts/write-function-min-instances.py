#!/usr/bin/env python3
from __future__ import annotations

import argparse
import json
import subprocess
from datetime import UTC, datetime
from pathlib import Path
from typing import Any


DEFAULT_OUTPUT = Path("evidence/deployment/function-min-instances.json")
MIN_KEYS = ("minimum_instances", "min_instances", "minInstanceCount", "minimumInstanceCount")
PROVISION_KEYS = ("provisioned_instances", "provisioned_target", "provisionedInstanceCount")
NAME_KEYS = ("name", "function_name", "functionName", "resource", "role")


def parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(
        description="Write Function Compute zero minimum/provisioned instance evidence."
    )
    parser.add_argument("--input", type=Path, help="JSON export containing web/task function records.")
    parser.add_argument("--output", type=Path, default=DEFAULT_OUTPUT)
    parser.add_argument("--dry-run", action="store_true")
    return parser.parse_args()


def git_commit() -> str:
    try:
        return subprocess.check_output(["git", "rev-parse", "HEAD"], text=True).strip()
    except (OSError, subprocess.CalledProcessError):
        return "unknown"


def numeric_zero(value: Any) -> bool:
    if isinstance(value, bool):
        return False
    if isinstance(value, int | float):
        return value == 0
    if isinstance(value, str):
        return value.strip() in {"0", "0.0"}
    return False


def walk_dicts(value: Any):
    if isinstance(value, dict):
        yield value
        for item in value.values():
            yield from walk_dicts(item)
    elif isinstance(value, list):
        for item in value:
            yield from walk_dicts(item)


def record_name(record: dict[str, Any], index: int) -> str:
    for key in NAME_KEYS:
        value = record.get(key)
        if isinstance(value, str) and value.strip():
            return value.strip()
    return f"function-{index}"


def extract_records(value: Any) -> list[dict[str, Any]]:
    records = [item for item in walk_dicts(value) if any(key in item for key in MIN_KEYS)]
    if len(records) < 2:
        raise ValueError("Input must contain at least two function records with minimum instance counts")
    normalized = []
    for index, record in enumerate(records, start=1):
        min_values = {key: record[key] for key in MIN_KEYS if key in record}
        provision_values = {key: record[key] for key in PROVISION_KEYS if key in record}
        for key, raw in {**min_values, **provision_values}.items():
            if not numeric_zero(raw):
                raise ValueError(f"{record_name(record, index)} has nonzero {key}: {raw!r}")
        normalized.append(
            {
                "function_name": record_name(record, index),
                "minimum_instances": 0,
                "provisioned_instances": 0,
                "source": record,
            }
        )
    return normalized


def build_payload(path: Path) -> dict:
    source = json.loads(path.read_text(encoding="utf-8"))
    records = extract_records(source)
    return {
        "schema": "directorgraph.function-min-instances.v1",
        "generated_at": datetime.now(UTC).isoformat(),
        "repository_commit": git_commit(),
        "source_file": path.as_posix(),
        "functions": records,
    }


def dry_run_payload() -> dict:
    return {
        "schema": "directorgraph.function-min-instances-plan.v1",
        "output": DEFAULT_OUTPUT.as_posix(),
        "required_input": "JSON export with web and task Function Compute records",
        "accepted_minimum_keys": list(MIN_KEYS),
        "accepted_provisioned_keys": list(PROVISION_KEYS),
        "policy": "all minimum and provisioned instance values must be zero",
    }


def main() -> None:
    args = parse_args()
    if args.dry_run:
        print(json.dumps(dry_run_payload(), indent=2, sort_keys=True))
        return
    if args.input is None:
        raise SystemExit("--input is required unless --dry-run is used")
    payload = build_payload(args.input)
    args.output.parent.mkdir(parents=True, exist_ok=True)
    args.output.write_text(json.dumps(payload, indent=2, sort_keys=True) + "\n", encoding="utf-8")
    print(f"function_min_instances={args.output.as_posix()}")


if __name__ == "__main__":
    main()
