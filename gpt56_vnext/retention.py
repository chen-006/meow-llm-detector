"""Optional exchange evidence committed with its attempt in the same SQLite transaction.

No separate mutable raw/index/manifest files to reconcile after a crash.
Historical v4.1.1 retention files remain untouched.
"""
from __future__ import annotations

from .security import SecretGuard


def exchange_record(value: dict, guard: SecretGuard) -> dict:
    guard.check(value)
    record = {"request": value.get("request_json"), "response_utf8": value.get("response_utf8"),
              "response_headers": guard.redact(value.get("headers", {})),
              "http_status": value.get("http_status"), "error": value.get("error"),
              "body_complete": value.get("body_complete", False), "redacted": value.get("redacted", False),
              "request_representation": "logical_json_not_wire_bytes",
              "response_representation": "decoded_utf8_after_http_content_decoding",
              "body_available": value.get("response_utf8") is not None}
    guard.check(record)
    return record
