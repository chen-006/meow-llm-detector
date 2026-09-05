from __future__ import annotations

from datetime import datetime, timezone
import hashlib
import ipaddress
import json
import math
import os
from pathlib import Path
import threading
import time
from typing import Any
from urllib.parse import urlsplit, urlunsplit

from .errors import AppError


def utc_now() -> str:
    return datetime.now(timezone.utc).isoformat()


def canonical_json(value: Any) -> str:
    return json.dumps(value, ensure_ascii=False, separators=(",", ":"), sort_keys=True, allow_nan=False)


def sha256_text(value: str) -> str:
    return hashlib.sha256(value.encode("utf-8")).hexdigest()


def deterministic_job_id(value: Any) -> str:
    return hashlib.sha256(canonical_json(value).encode("utf-8")).hexdigest()[:32]


def normalize_api_base_url(value: str, *, allow_insecure: bool = False) -> str:
    text = str(value).strip()
    parsed = urlsplit(text)
    if parsed.scheme not in {"http", "https"} or not parsed.hostname:
        raise AppError("invalid_url", field="base_url")
    if parsed.username is not None or parsed.password is not None or parsed.query or parsed.fragment:
        raise AppError("url_credentials_or_query", field="base_url")
    if any(ord(char) < 33 for char in text) or "\\" in text:
        raise AppError("invalid_url", field="base_url")
    try:
        port = parsed.port
        loopback = ipaddress.ip_address(parsed.hostname).is_loopback
    except ValueError:
        if ":" in parsed.netloc and not parsed.netloc.startswith("["):
            try:
                port = parsed.port
            except ValueError as exc:
                raise AppError("invalid_url", field="base_url") from exc
        else:
            port = parsed.port
        loopback = parsed.hostname.lower() == "localhost"
    if parsed.scheme == "http" and not loopback and not allow_insecure:
        raise AppError("https_required", field="base_url")
    path = parsed.path.rstrip("/")
    if not path:
        path = "/v1"
    for suffix in ("/chat/completions", "/responses", "/messages"):
        if path.endswith(suffix):
            path = path[:-len(suffix)]
            break
    host = parsed.hostname.lower()
    authority = f"[{host}]" if ":" in host else host
    if port is not None:
        authority += f":{port}"
    return urlunsplit((parsed.scheme, authority, path.rstrip("/"), "", ""))


def safe_endpoint(value: str) -> str:
    try:
        parsed = urlsplit(value)
        host = parsed.hostname or ""
        authority = f"[{host}]" if ":" in host else host
        if parsed.port is not None:
            authority += f":{parsed.port}"
        return urlunsplit((parsed.scheme, authority, parsed.path, "", ""))
    except ValueError:
        return ""


def recognized_provider(value: str) -> str | None:
    """Recognize the actual API origin, never a publisher-supplied brand name."""
    try:
        url = urlsplit(value)
        if (url.scheme == "https" and url.hostname == "openrouter.ai" and url.port in {None, 443}
                and url.username is None and url.password is None and not url.query and not url.fragment
                and url.path.rstrip("/") in {"/api/v1", "/api/v1/responses", "/api/v1/messages", "/api/v1/chat/completions"}):
            return "openrouter"
    except (TypeError, ValueError):
        pass
    return None


def strict_json_loads(value: str | bytes) -> Any:
    def pairs(items: list[tuple[str, Any]]) -> dict[str, Any]:
        result = {}
        for key, item in items:
            if key in result:
                raise AppError("duplicate_json_key")
            result[key] = item
        return result

    def invalid_constant(_value: str) -> None:
        raise AppError("non_finite_number")

    try:
        return json.loads(value, object_pairs_hook=pairs, parse_constant=invalid_constant)
    except (json.JSONDecodeError, UnicodeDecodeError, RecursionError) as exc:
        raise AppError("invalid_json") from exc


def integer(value: Any, field: str, minimum: int, maximum: int) -> int:
    if type(value) is not int or not minimum <= value <= maximum:
        raise AppError("integer_out_of_range", field=field)
    return value


def finite_number(value: Any, field: str, minimum: float, maximum: float) -> float:
    if isinstance(value, bool) or not isinstance(value, (int, float)):
        raise AppError("number_out_of_range", field=field)
    if not math.isfinite(value) or not minimum <= value <= maximum:
        raise AppError("number_out_of_range", field=field)
    return float(value)


def atomic_write_json(path: str | Path, value: Any) -> None:
    destination = Path(path)
    destination.parent.mkdir(parents=True, exist_ok=True)
    temporary = destination.with_name(
        f".{destination.name}.{os.getpid()}.{threading.get_ident()}.tmp"
    )
    encoded = (json.dumps(value, ensure_ascii=False, indent=2, allow_nan=False) + "\n").encode("utf-8")
    try:
        with temporary.open("wb") as handle:
            handle.write(encoded)
            handle.flush()
            os.fsync(handle.fileno())
        last_error: OSError | None = None
        for attempt in range(5):
            try:
                os.replace(temporary, destination)
                last_error = None
                break
            except OSError as exc:
                last_error = exc
                if attempt < 4:
                    time.sleep(0.02 * (attempt + 1))
        if last_error is not None:
            raise last_error
        if hasattr(os, "O_DIRECTORY"):
            descriptor = os.open(destination.parent, os.O_DIRECTORY)
            try:
                os.fsync(descriptor)
            finally:
                os.close(descriptor)
    finally:
        temporary.unlink(missing_ok=True)
