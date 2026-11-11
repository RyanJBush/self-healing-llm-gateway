"""Request parsing and validation (contract section 1).

One documented envelope: ``InvalidRequest`` always becomes a 422 ``invalid_request``
with a fixed message. Messages never reflect client-supplied text.
"""

from __future__ import annotations

import json
import re
from dataclasses import dataclass
from typing import Any

from .config import INPUT_MAX_CHARS, INPUT_MIN_CHARS, KNOWN_ROUTES

REQUIRED_FIELDS = frozenset({"request_id", "input", "route"})

_UUID_RE = re.compile(
    r"[0-9a-f]{8}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{12}"
)

MSG_CONTENT_TYPE = "Content-Type must be application/json."
MSG_JSON = "Request body must be valid UTF-8 JSON."
MSG_OBJECT = "Request body must be a JSON object."
MSG_MISSING = "Request body is missing required fields."
MSG_UNKNOWN = "Request body contains unknown fields."
MSG_REQUEST_ID = "Field 'request_id' must be a canonical lowercase UUID."
MSG_INPUT = (
    "Field 'input' must be valid text of 1 to 4000 characters "
    "that is not only whitespace."
)
MSG_ROUTE = "Field 'route' must be one of: standard."
MSG_TOO_LARGE = "Request body is too large."


class InvalidRequest(Exception):
    """Validation failure. ``request_id`` is set only if it validated."""

    def __init__(self, message: str, request_id: str | None = None) -> None:
        super().__init__(message)
        self.message = message
        self.request_id = request_id


@dataclass(frozen=True)
class ParsedRequest:
    request_id: str
    input: str
    route: str


def _reject_duplicates(pairs: list[tuple[str, Any]]) -> dict[str, Any]:
    out: dict[str, Any] = {}
    for key, value in pairs:
        if key in out:
            raise ValueError("duplicate key")
        out[key] = value
    return out


def _reject_constant(_name: str) -> Any:
    raise ValueError("non-finite number")


def _is_json_content_type(value: str | None) -> bool:
    if value is None:
        return False
    media, _, params = value.partition(";")
    if media.strip().lower() != "application/json":
        return False
    params = params.strip()
    if not params:
        return True
    name, _, val = params.partition("=")
    return name.strip().lower() == "charset" and val.strip().strip('"').lower() == "utf-8"


def _valid_request_id(value: Any) -> str | None:
    if isinstance(value, str) and _UUID_RE.fullmatch(value):
        return value
    return None


def _valid_input(value: Any) -> bool:
    if not isinstance(value, str):
        return False
    if not (INPUT_MIN_CHARS <= len(value) <= INPUT_MAX_CHARS):
        return False
    if value.strip() == "":
        return False
    try:
        value.encode("utf-8")  # rejects lone surrogates
    except UnicodeEncodeError:
        return False
    return True


def parse_request(content_type: str | None, raw: bytes) -> ParsedRequest:
    """Validate a raw request body. Raises ``InvalidRequest``."""
    if not _is_json_content_type(content_type):
        raise InvalidRequest(MSG_CONTENT_TYPE)
    try:
        text = raw.decode("utf-8")
        data = json.loads(
            text,
            object_pairs_hook=_reject_duplicates,
            parse_constant=_reject_constant,
        )
    except (UnicodeDecodeError, ValueError, RecursionError):
        raise InvalidRequest(MSG_JSON) from None
    if not isinstance(data, dict):
        raise InvalidRequest(MSG_OBJECT)

    # Only a validated id is ever echoed in an error envelope.
    safe_id = _valid_request_id(data.get("request_id"))

    keys = set(data)
    if REQUIRED_FIELDS - keys:
        raise InvalidRequest(MSG_MISSING, safe_id)
    if keys - REQUIRED_FIELDS:
        raise InvalidRequest(MSG_UNKNOWN, safe_id)
    if safe_id is None:
        raise InvalidRequest(MSG_REQUEST_ID)
    if not _valid_input(data["input"]):
        raise InvalidRequest(MSG_INPUT, safe_id)
    route = data["route"]
    if not isinstance(route, str) or route not in KNOWN_ROUTES:
        raise InvalidRequest(MSG_ROUTE, safe_id)
    return ParsedRequest(request_id=safe_id, input=data["input"], route=route)
