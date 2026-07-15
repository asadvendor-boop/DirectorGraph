from __future__ import annotations

import json
import re
from enum import StrEnum
from typing import Any
from urllib.parse import urlsplit, urlunsplit


class ProviderErrorCategory(StrEnum):
    AUTH = "auth"
    QUOTA = "quota"
    UNSUPPORTED_MODEL = "unsupported_model"
    MODERATION = "moderation"
    TIMEOUT = "timeout"
    INVALID_RESPONSE = "invalid_response"
    TRANSPORT = "transport"
    PROVIDER_FAILURE = "provider_failure"
    BUDGET_REFUSAL = "budget_refusal"


SENSITIVE_KEY_PARTS = {
    "authorization",
    "api_key",
    "apikey",
    "access_key",
    "accesskey",
    "secret",
    "signature",
    "security_token",
    "security-token",
    "credential",
    "password",
    "token",
    "task_id",
    "taskid",
}
URL_KEY_PARTS = {"url", "uri", "object", "media"}
URL_RE = re.compile(r"https?://[^\s'\"<>]+")
AUTH_RE = re.compile(r"(?i)\b(authorization\s*[:=]\s*)(bearer\s+)?[^\s,'\"}]+")
BEARER_RE = re.compile(r"(?i)\bbearer\s+[a-z0-9._~+/=-]{8,}")
QUERY_SECRET_RE = re.compile(
    r"(?i)\b("
    r"signature|x-oss-signature|ossaccesskeyid|accesskeyid|security-token|"
    r"x-oss-security-token|token|api[_-]?key|access[_-]?key[_-]?secret|credential"
    r")=([^&\s,'\"]+)"
)
ACCESS_KEY_RE = re.compile(r"\b(AKID|LTAI)[A-Za-z0-9]{10,}\b")
OPENAI_KEY_RE = re.compile(r"\bsk-[A-Za-z0-9_-]{12,}\b")
TASK_ID_TEXT_RE = re.compile(r"(?i)(task[_-]?id\s*[:=]\s*)[A-Za-z0-9._:-]+")


class ProviderCallError(RuntimeError):
    def __init__(
        self,
        *,
        provider: str,
        category: ProviderErrorCategory,
        detail: Any = "",
        status_code: int | None = None,
        code: str | None = None,
        retryable: bool | None = None,
    ):
        self.provider = provider
        self.category = category
        self.status_code = status_code
        self.code = redact_text(code) if code else None
        self.redacted_detail = provider_payload_summary(detail)
        self.retryable = retryable if retryable is not None else category in {
            ProviderErrorCategory.TIMEOUT,
            ProviderErrorCategory.TRANSPORT,
            ProviderErrorCategory.PROVIDER_FAILURE,
        }
        super().__init__(str(self))

    def __str__(self) -> str:
        parts = [f"{self.provider} provider error", f"category={self.category.value}"]
        if self.status_code is not None:
            parts.append(f"http_status={self.status_code}")
        if self.code:
            parts.append(f"code={self.code}")
        if self.redacted_detail:
            parts.append(f"detail={self.redacted_detail}")
        return "; ".join(parts)

    def public_payload(self) -> dict[str, Any]:
        return {
            "provider": self.provider,
            "category": self.category.value,
            "status_code": self.status_code,
            "code": self.code,
            "retryable": self.retryable,
            "detail": self.redacted_detail,
        }


def redact_url(value: str) -> str:
    try:
        parts = urlsplit(value)
    except ValueError:
        return "[REDACTED_URL]"
    if not parts.scheme or not parts.netloc:
        return redact_text(value)
    query_marker = "?[REDACTED_QUERY]" if parts.query else ""
    return urlunsplit((parts.scheme, parts.netloc, parts.path, "", "")) + query_marker


def redact_text(value: str | None) -> str:
    if value is None:
        return ""
    text = str(value)
    text = URL_RE.sub(lambda match: redact_url(match.group(0)), text)
    text = AUTH_RE.sub(r"\1[REDACTED]", text)
    text = BEARER_RE.sub("Bearer [REDACTED]", text)
    text = QUERY_SECRET_RE.sub(lambda match: f"{match.group(1)}=[REDACTED]", text)
    text = ACCESS_KEY_RE.sub("[REDACTED_ACCESS_KEY]", text)
    text = OPENAI_KEY_RE.sub("[REDACTED_API_KEY]", text)
    text = TASK_ID_TEXT_RE.sub(r"\1[REDACTED]", text)
    return text


def _sensitive_key(key: str) -> bool:
    normalized = key.lower().replace("-", "_")
    return any(part in normalized for part in SENSITIVE_KEY_PARTS)


def _url_key(key: str) -> bool:
    normalized = key.lower().replace("-", "_")
    return any(part in normalized for part in URL_KEY_PARTS)


def redact_provider_payload(value: Any) -> Any:
    if isinstance(value, dict):
        redacted: dict[str, Any] = {}
        for key, item in value.items():
            key_text = str(key)
            if _sensitive_key(key_text):
                redacted[key_text] = "[REDACTED]"
            elif _url_key(key_text) and isinstance(item, str):
                redacted[key_text] = redact_url(item)
            else:
                redacted[key_text] = redact_provider_payload(item)
        return redacted
    if isinstance(value, list):
        return [redact_provider_payload(item) for item in value[:25]]
    if isinstance(value, tuple):
        return [redact_provider_payload(item) for item in value[:25]]
    if isinstance(value, str):
        return redact_text(value)
    return value


def provider_payload_summary(value: Any, *, max_chars: int = 700) -> str:
    redacted = redact_provider_payload(value)
    try:
        text = json.dumps(redacted, ensure_ascii=False, sort_keys=True)
    except TypeError:
        text = redact_text(str(redacted))
    if len(text) > max_chars:
        return f"{text[:max_chars]}...[truncated]"
    return text


def categorize_provider_error(
    *,
    status_code: int | None = None,
    code: str | None = None,
    message: Any = None,
    exception: BaseException | None = None,
) -> ProviderErrorCategory:
    haystack = " ".join(
        value.lower()
        for value in [
            str(code or ""),
            provider_payload_summary(message),
            type(exception).__name__ if exception else "",
            str(exception or ""),
        ]
        if value
    )
    if status_code in {408, 504} or "timeout" in haystack or "timed out" in haystack:
        return ProviderErrorCategory.TIMEOUT
    if any(term in haystack for term in ["moderation", "safety", "policy", "prohibited", "risk control"]):
        return ProviderErrorCategory.MODERATION
    if "model" in haystack and any(
        term in haystack for term in ["not found", "not exist", "unsupported", "invalid", "unavailable"]
    ):
        return ProviderErrorCategory.UNSUPPORTED_MODEL
    if status_code == 429 or any(
        term in haystack for term in ["quota", "balance", "billing", "credit", "rate limit", "throttl"]
    ):
        return ProviderErrorCategory.QUOTA
    if status_code in {401, 403} or any(
        term in haystack for term in ["unauthorized", "forbidden", "invalid api", "apikey", "api key", "access denied"]
    ):
        return ProviderErrorCategory.AUTH
    if status_code is not None and status_code >= 500:
        return ProviderErrorCategory.PROVIDER_FAILURE
    if exception is not None and status_code is None:
        return ProviderErrorCategory.TRANSPORT
    return ProviderErrorCategory.PROVIDER_FAILURE


def provider_error_from_exception(provider: str, exc: BaseException) -> ProviderCallError:
    status_code = getattr(exc, "status_code", None)
    code = getattr(exc, "code", None)
    body = getattr(exc, "body", None)
    detail = {
        "exception": type(exc).__name__,
        "message": str(exc),
    }
    if body is not None:
        detail["body"] = body
    return ProviderCallError(
        provider=provider,
        category=categorize_provider_error(
            status_code=status_code,
            code=code,
            message=detail,
            exception=exc,
        ),
        status_code=status_code,
        code=str(code) if code else None,
        detail=detail,
    )
