from __future__ import annotations

import json
from dataclasses import dataclass
from typing import TypeVar

from openai import AsyncOpenAI, BadRequestError
from pydantic import BaseModel

from app.config import Settings
from app.providers.errors import (
    ProviderCallError,
    ProviderErrorCategory,
    provider_error_from_exception,
)

T = TypeVar("T", bound=BaseModel)


@dataclass(slots=True)
class StructuredResult:
    value: BaseModel
    input_tokens: int
    output_tokens: int
    model: str


class QwenClient:
    def __init__(self, settings: Settings):
        api_key = settings.qwen_api_key or settings.dashscope_api_key
        if not api_key:
            raise RuntimeError("QWEN_API_KEY or DASHSCOPE_API_KEY is required in live mode")
        self.settings = settings
        self.client = AsyncOpenAI(
            api_key=api_key,
            base_url=settings.qwen_base_url,
            timeout=180,
            max_retries=2,
        )

    async def close(self) -> None:
        await self.client.close()

    @staticmethod
    def _validate_response(content: str, response_model: type[T], *, provider: str, model: str) -> T:
        try:
            payload = json.loads(content)
        except json.JSONDecodeError as exc:
            raise ProviderCallError(
                provider=provider,
                category=ProviderErrorCategory.INVALID_RESPONSE,
                detail={
                    "message": "Provider returned non-JSON structured content",
                    "model": model,
                },
                retryable=False,
            ) from exc
        if response_model.__name__ == "QualityReport":
            payload = QwenClient._coerce_quality_report(payload, model=model)
        try:
            return response_model.model_validate(payload)
        except ValueError as exc:
            raise ProviderCallError(
                provider=provider,
                category=ProviderErrorCategory.INVALID_RESPONSE,
                detail={
                    "message": "Provider response failed schema validation",
                    "model": model,
                    "exception": type(exc).__name__,
                },
                retryable=False,
            ) from exc

    @staticmethod
    def _coerce_quality_report(payload: object, *, model: str) -> object:
        if not isinstance(payload, dict):
            return payload
        existing_dimensions = payload.get("dimensions")
        if "passed" in payload and isinstance(existing_dimensions, list) and existing_dimensions:
            return payload
        dimension_names = ("narrative", "identity", "continuity", "camera", "motion", "dialogue", "safety")
        dimensions = []
        violations = []

        def add_dimension(name: str, raw: dict) -> None:
            passed = raw.get("passed", raw.get("pass", raw.get("ok", True)))
            score = raw.get("score", raw.get("rating", raw.get("value")))
            if score is None:
                score = 1.0 if bool(passed) else 0.0
            try:
                numeric_score = float(score)
            except (TypeError, ValueError):
                numeric_score = 1.0 if bool(passed) else 0.0
            if numeric_score > 1:
                numeric_score = numeric_score / 100
            evidence = (
                raw.get("evidence")
                or raw.get("reason")
                or raw.get("comment")
                or raw.get("explanation")
                or ("passed" if bool(passed) else "failed")
            )
            dimensions.append({"name": name, "score": max(0.0, min(numeric_score, 1.0)), "evidence": str(evidence)})
            if not bool(passed) or numeric_score < 0.6:
                violations.append(f"{name}: {evidence}")

        if isinstance(existing_dimensions, list) and existing_dimensions:
            # Canonical dimensions list, but the payload is missing other required
            # keys (the live endpoint omits "passed" even under strict schema mode).
            # PRESERVE the model's own per-dimension judgments instead of discarding
            # them — this exact drop is how the flagship shipped dimensions: [].
            for raw in existing_dimensions:
                if isinstance(raw, dict) and raw.get("name") in dimension_names:
                    add_dimension(str(raw.get("name")), raw)
        elif isinstance(existing_dimensions, dict):
            # dimensions provided as a name->details mapping.
            for name in dimension_names:
                raw = existing_dimensions.get(name)
                if isinstance(raw, dict):
                    add_dimension(name, raw)
        else:
            for name in dimension_names:
                raw = payload.get(name)
                if isinstance(raw, dict):
                    add_dimension(name, raw)
        if "passed" in payload and not dimensions:
            # Canonical shape with a genuinely empty breakdown: nothing to coerce.
            return payload
        # Structured signals gate acceptance; prose only informs. Dimension-derived
        # violations (a hard failure or a sub-0.6 score) can block a shot, but the
        # model's free-text violation notes are kept visible without vetoing a shot
        # the numeric rubric accepts — otherwise stylistic nitpicks trigger repair
        # loops that burn the capped judge budget.
        blocking_violations = list(violations)
        payload_violations = payload.get("violations")
        if isinstance(payload_violations, list):
            for item in payload_violations:
                text = str(item).strip()
                if text and text not in violations:
                    violations.append(text)
        overall = payload.get("overall_score", payload.get("final_score", payload.get("score")))
        try:
            overall_score = float(overall)
        except (TypeError, ValueError):
            overall_score = sum(item["score"] for item in dimensions) / len(dimensions) if dimensions else 0.0
        if overall_score > 1:
            overall_score = overall_score / 100
        overall_score = max(0.0, min(overall_score, 1.0))
        passed = bool(payload.get("passed", payload.get("pass", overall_score >= 0.8 and not blocking_violations)))
        repair_instruction = payload.get("repair_instruction") or payload.get("repair") or payload.get("recommendation")
        return {
            "passed": passed,
            "overall_score": overall_score,
            "dimensions": dimensions,
            "violations": [str(item) for item in violations],
            "repair_strategy": "none" if passed else "regenerate",
            "repair_instruction": None if passed else str(repair_instruction or "Regenerate to satisfy the failed visual dimensions."),
            "evaluator_model": model,
            "attempt": int(payload.get("attempt") or 1),
        }

    async def structured(
        self,
        *,
        model: str,
        system: str,
        user: str,
        response_model: type[T],
        max_tokens: int = 7000,
    ) -> StructuredResult:
        schema = response_model.model_json_schema()
        kwargs = dict(
            model=model,
            messages=[{"role": "system", "content": system}, {"role": "user", "content": user}],
            response_format={"type": "json_schema", "json_schema": {"name": response_model.__name__.lower(), "schema": schema, "strict": True}},
            max_tokens=max_tokens,
            extra_body={"enable_thinking": False},
        )
        try:
            completion = await self.client.chat.completions.create(**kwargs)
        except BadRequestError:
            kwargs["response_format"] = {"type": "json_object"}
            try:
                completion = await self.client.chat.completions.create(**kwargs)
            except Exception as exc:
                raise provider_error_from_exception("Qwen", exc) from exc
        except Exception as exc:
            raise provider_error_from_exception("Qwen", exc) from exc
        content = completion.choices[0].message.content or "{}"
        usage = completion.usage
        return StructuredResult(
            value=self._validate_response(content, response_model, provider="Qwen", model=completion.model),
            input_tokens=getattr(usage, "prompt_tokens", 0) or 0,
            output_tokens=getattr(usage, "completion_tokens", 0) or 0,
            model=completion.model,
        )

    async def _quality_completion(
        self,
        *,
        media_content: dict,
        contract_json: str,
        response_model: type[T],
        attempt: int,
    ) -> StructuredResult:
        # The prose prompt demands all seven per-dimension entries, but under strict
        # constrained decoding the model emits the minimal schema-valid object — so an
        # unconstrained array yields dimensions: []. Encode the requirement in the
        # request schema itself; the stored/validation model stays unconstrained so
        # historical reports remain loadable.
        request_schema = response_model.model_json_schema()
        dimensions_property = request_schema.get("properties", {}).get("dimensions")
        if isinstance(dimensions_property, dict):
            dimensions_property["minItems"] = 7
            dimensions_property["maxItems"] = 7
        kwargs = dict(
            model=self.settings.qwen_vision_model,
            messages=[
                {"role": "system", "content": "You are a strict film continuity supervisor. Compare visible evidence against the shot contract. Return JSON only with keys passed, overall_score, dimensions, violations, repair_strategy, repair_instruction, evaluator_model, and attempt. dimensions must be an array of {name, score, evidence} for narrative, identity, continuity, camera, motion, dialogue, and safety. Contract dialogue and narration lines are delivered as voice-over audio in the final edit and are never visible in frames: score the dialogue dimension high when no readable on-screen text appears, and score it low ONLY when readable text, captions, or subtitles are burned into the frame. Never penalize a frame for lacking visual evidence of a spoken line. Never require or suggest adding generated logos, readable text, HUD numerals, countdown digits, or exact typography as a repair. If a contract mentions readable text or logos, evaluate the closest safe visual equivalent instead."},
                {"role": "user", "content": [
                    media_content,
                    {"type": "text", "text": f"Attempt {attempt}. Shot Contract:\n{contract_json}\nReturn only the required QualityReport JSON object."},
                ]},
            ],
            response_format={"type": "json_schema", "json_schema": {"name": "quality_report", "schema": request_schema, "strict": True}},
            max_tokens=1800,
            extra_body={"enable_thinking": False},
        )
        try:
            completion = await self.client.chat.completions.create(**kwargs)
        except BadRequestError:
            kwargs["response_format"] = {"type": "json_object"}
            try:
                completion = await self.client.chat.completions.create(**kwargs)
            except Exception as exc:
                raise provider_error_from_exception("Qwen-VL", exc) from exc
        except Exception as exc:
            raise provider_error_from_exception("Qwen-VL", exc) from exc
        content = completion.choices[0].message.content or "{}"
        usage = completion.usage
        return StructuredResult(
            value=self._validate_response(
                content,
                response_model,
                provider="Qwen-VL",
                model=completion.model,
            ),
            input_tokens=getattr(usage, "prompt_tokens", 0) or 0,
            output_tokens=getattr(usage, "completion_tokens", 0) or 0,
            model=completion.model,
        )

    async def inspect_video(
        self,
        *,
        video_url: str,
        contract_json: str,
        response_model: type[T],
        attempt: int,
    ) -> StructuredResult:
        return await self._quality_completion(
            media_content={"type": "video_url", "video_url": {"url": video_url}},
            contract_json=contract_json,
            response_model=response_model,
            attempt=attempt,
        )

    async def inspect_image(
        self,
        *,
        image_url: str,
        contract_json: str,
        response_model: type[T],
        attempt: int,
    ) -> StructuredResult:
        return await self._quality_completion(
            media_content={"type": "image_url", "image_url": {"url": image_url}},
            contract_json=contract_json,
            response_model=response_model,
            attempt=attempt,
        )
