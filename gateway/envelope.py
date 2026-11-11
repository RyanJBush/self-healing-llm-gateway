"""Response bodies. Every message is fixed text: no client or upstream content."""

from __future__ import annotations

from typing import Any

STATUS_FOR_CODE = {
    "invalid_request": 422,
    "upstream_rejected": 502,
    "providers_unavailable": 503,
    "deadline_exceeded": 504,
    "internal_error": 500,
    # Outside the contract table (DECISIONS.md D5):
    "not_found": 404,
    "method_not_allowed": 405,
}

MESSAGE_FOR_CODE = {
    "upstream_rejected": "The upstream provider rejected the request.",
    "providers_unavailable": "No provider could complete the request.",
    "deadline_exceeded": "The request deadline was exceeded.",
    "internal_error": "Internal gateway error.",
    "not_found": "Resource not found.",
    "method_not_allowed": "Method not allowed.",
}

REASON_FOR_CODE = {
    "invalid_request": "validation_failed",
    "upstream_rejected": "non_retryable_upstream_outcome",
    "providers_unavailable": "providers_failed_or_skipped",
    "deadline_exceeded": "deadline_reached",
    "internal_error": "gateway_defect",
    "not_found": "unknown_path",
    "method_not_allowed": "unsupported_method",
}

MESSAGE_UPSTREAM_UNUSABLE = "The upstream provider returned an unusable response."


def error_body(
    request_id: str | None,
    code: str,
    message: str,
    attempts: int,
    elapsed_ms: int,
) -> dict[str, Any]:
    return {
        "request_id": request_id,
        "error": {"code": code, "message": message},
        "attempts": attempts,
        "elapsed_ms": elapsed_ms,
    }


def success_body(
    *,
    request_id: str,
    output: str,
    provider: str,
    model: str,
    execution_mode: str,
    attempts: int,
    fallback_used: bool,
    elapsed_ms: int,
) -> dict[str, Any]:
    return {
        "request_id": request_id,
        "output": output,
        "provider": provider,
        "model": model,
        "execution_mode": execution_mode,
        "attempts": attempts,
        "fallback_used": fallback_used,
        "elapsed_ms": elapsed_ms,
    }
