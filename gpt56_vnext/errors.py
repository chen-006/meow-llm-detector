from __future__ import annotations

from typing import Any


class AppError(ValueError):
    def __init__(self, code: str, *, field: str | None = None, status: int = 400):
        super().__init__(code)
        self.code = code
        self.field = field
        self.status = status

    def public(self) -> dict[str, Any]:
        return {"code": self.code, "field": self.field}


class RequestError(Exception):
    def __init__(self, code: str, *, status: int | None = None,
                 retryable: bool = True, headers: dict | None = None,
                 evidence: dict | None = None):
        super().__init__(code)
        self.code = code
        self.status = status
        self.retryable = retryable
        self.headers = headers or {}
        self.evidence = evidence or {}

    def public(self) -> dict[str, Any]:
        return {"code": self.code, "http_status": self.status,
                "retryable": self.retryable}
