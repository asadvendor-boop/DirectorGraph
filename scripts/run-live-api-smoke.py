#!/usr/bin/env python3
from __future__ import annotations

import argparse
import asyncio
import json
import os
import re
import subprocess
import sys
from datetime import UTC, datetime
from pathlib import Path
from typing import Any
from urllib.parse import urlparse


ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "services/api"))

from openai import AsyncOpenAI  # noqa: E402

from app.config import Settings  # noqa: E402
from app.providers.errors import provider_error_from_exception, redact_provider_payload  # noqa: E402


APPROVAL_ENV = "DIRECTORGRAPH_APPROVE_LIVE_API_SMOKE"
APPROVAL_VALUE = "run-live-api-smoke"
SECRET_RE = re.compile(
    r"(AKID[A-Za-z0-9]{12,}|LTAI[A-Za-z0-9]{10,}|sk-[A-Za-z0-9_-]{12,}|"
    r"BEGIN (RSA |EC |OPENSSH )?PRIVATE KEY|Bearer\s+[A-Za-z0-9._~+/=-]{8,})",
    re.IGNORECASE,
)


def parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(
        description="Run the approval-gated live Qwen/DashScope API smoke and write redacted evidence."
    )
    parser.add_argument("--output-dir", type=Path, default=Path("evidence/live-api"))
    parser.add_argument("--max-tokens", type=int, default=180)
    parser.add_argument(
        "--dry-run",
        action="store_true",
        help="Print the planned evidence contract without contacting the live API.",
    )
    return parser.parse_args()


def git_commit() -> str:
    try:
        return subprocess.check_output(["git", "rev-parse", "HEAD"], text=True).strip()
    except (OSError, subprocess.CalledProcessError):
        return "unknown"


def now() -> str:
    return datetime.now(UTC).isoformat()


def host_from_url(value: str) -> str:
    parsed = urlparse(value)
    return parsed.netloc or value


def assert_no_secret(path: Path, payload: dict[str, Any]) -> None:
    text = json.dumps(payload, sort_keys=True, ensure_ascii=False)
    if SECRET_RE.search(text):
        raise SystemExit(f"Refusing to write secret-like data to {path}")


def write_json(path: Path, payload: dict[str, Any]) -> None:
    assert_no_secret(path, payload)
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(payload, indent=2, sort_keys=True) + "\n", encoding="utf-8")
    print(f"wrote={path.as_posix()}")


def approval_or_exit(dry_run: bool) -> None:
    if dry_run:
        return
    if os.environ.get(APPROVAL_ENV) != APPROVAL_VALUE:
        raise SystemExit(
            f"Refusing live API call. Set {APPROVAL_ENV}={APPROVAL_VALUE} after explicit spend approval."
        )


async def run_live_call(settings: Settings, *, max_tokens: int) -> tuple[dict[str, Any], dict[str, Any]]:
    api_key = settings.qwen_api_key or settings.dashscope_api_key
    if not api_key:
        raise SystemExit("QWEN_API_KEY or DASHSCOPE_API_KEY is required for the live API smoke.")
    client = AsyncOpenAI(
        api_key=api_key,
        base_url=settings.qwen_base_url,
        timeout=60,
        max_retries=0,
    )
    request_payload: dict[str, Any] = {
        "model": settings.qwen_story_model,
        "messages": [
            {
                "role": "system",
                "content": (
                    "Return compact JSON only. This is a low-cost DirectorGraph connectivity "
                    "smoke for live submission evidence."
                ),
            },
            {
                "role": "user",
                "content": (
                    "Return JSON with keys ok, service, model_route, and note. "
                    "Use ok=true and service='directorgraph-live-api-smoke'."
                ),
            },
        ],
        "response_format": {"type": "json_object"},
        "max_tokens": max_tokens,
        "extra_body": {"enable_thinking": False},
    }
    try:
        completion = await client.chat.completions.create(**request_payload)
    except Exception as exc:
        raise provider_error_from_exception("Qwen", exc) from exc
    finally:
        await client.close()

    content = completion.choices[0].message.content or "{}"
    try:
        parsed_content = json.loads(content)
    except json.JSONDecodeError as exc:
        raise SystemExit("Live smoke response was not valid JSON.") from exc
    if parsed_content.get("ok") is not True:
        raise SystemExit("Live smoke response did not return ok=true.")

    response_payload = completion.model_dump(mode="json")
    return request_payload, response_payload


def dry_run_payload(settings: Settings) -> dict[str, Any]:
    return {
        "approval_env": APPROVAL_ENV,
        "approval_value": APPROVAL_VALUE,
        "base_url_host": host_from_url(settings.qwen_base_url),
        "outputs": [
            "evidence/live-api/model-smoke.json",
            "evidence/live-api/redacted-response-fixtures.json",
        ],
        "repository_commit": git_commit(),
        "schema": "directorgraph.live-api-smoke-plan.v1",
        "story_model": settings.qwen_story_model,
        "vision_model": settings.qwen_vision_model,
    }


async def main() -> None:
    args = parse_args()
    settings = Settings()
    if args.dry_run:
        print(json.dumps(dry_run_payload(settings), indent=2, sort_keys=True))
        return

    approval_or_exit(args.dry_run)
    requested_at = now()
    request_payload, response_payload = await run_live_call(settings, max_tokens=args.max_tokens)
    choice = response_payload.get("choices", [{}])[0]
    usage = response_payload.get("usage") or {}
    parsed_content = json.loads(choice.get("message", {}).get("content") or "{}")

    model_smoke = {
        "schema": "directorgraph.live-api-smoke.v1",
        "status": "pass",
        "generated_at": now(),
        "requested_at": requested_at,
        "repository_commit": git_commit(),
        "provider": "Qwen",
        "dashscope_region": settings.dashscope_region,
        "base_url_host": host_from_url(settings.qwen_base_url),
        "model": response_payload.get("model") or settings.qwen_story_model,
        "configured_models": {
            "story": settings.qwen_story_model,
            "vision": settings.qwen_vision_model,
            "tts": settings.qwen_tts_model,
            "wan_image": settings.wan_image_model,
            "wan_video": settings.wan_video_model,
            "happyhorse_video": settings.happyhorse_video_model,
        },
        "usage": {
            "prompt_tokens": usage.get("prompt_tokens"),
            "completion_tokens": usage.get("completion_tokens"),
            "total_tokens": usage.get("total_tokens"),
        },
        "response": redact_provider_payload(parsed_content),
    }
    fixtures = {
        "schema": "directorgraph.redacted-provider-fixtures.v1",
        "generated_at": now(),
        "repository_commit": git_commit(),
        "fixtures": [
            {
                "provider": "Qwen",
                "endpoint_host": host_from_url(settings.qwen_base_url),
                "request": redact_provider_payload(request_payload),
                "response": redact_provider_payload(response_payload),
            }
        ],
    }
    output_dir = args.output_dir
    write_json(output_dir / "model-smoke.json", model_smoke)
    write_json(output_dir / "redacted-response-fixtures.json", fixtures)


if __name__ == "__main__":
    asyncio.run(main())
