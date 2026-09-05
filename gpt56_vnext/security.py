from __future__ import annotations

import json
import re
from typing import Any

from .errors import RequestError

AUTH_PATTERN = re.compile(r"(?i)(?:bearer\s+)?sk-[a-z0-9_-]{8,}")
SENSITIVE_FIELDS = {"authorization", "proxy-authorization", "x-api-key", "api-key",
                    "api_key", "apikey", "cookie", "set-cookie"}


class SecretGuard:
    def __init__(self, secrets: list[str] | tuple[str, ...] = ()):
        self._values = tuple(value for value in secrets if value)

    def __repr__(self) -> str:
        return "SecretGuard(<redacted>)"

    def including(self, secret: str) -> "SecretGuard":
        return SecretGuard((*self._values, secret))

    def check(self, value: Any, *, code: str = "credential_echo") -> None:
        text = value if isinstance(value, str) else json.dumps(value, ensure_ascii=False)
        if any(secret in text for secret in self._values):
            raise RequestError(code, retryable=False)

    def redact(self, value: Any) -> Any:
        if isinstance(value, dict):
            return {key: self.redact(item) for key, item in value.items()
                    if str(key).casefold() not in SENSITIVE_FIELDS}
        if isinstance(value, list):
            return [self.redact(item) for item in value]
        if isinstance(value, str):
            for secret in self._values:
                value = value.replace(secret, "[REDACTED]")
            return AUTH_PATTERN.sub("[REDACTED]", value)
        return value
