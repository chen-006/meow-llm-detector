from __future__ import annotations

import asyncio
import json
import math
import time
from typing import Any
from urllib.parse import urlsplit

import httpx

from .errors import AppError, RequestError
from .benchmark import normalize_parameters
from .proxies import http_client_options
from .rate_limit import RateLimitGate
from .security import SecretGuard
from .utils import normalize_api_base_url, recognized_provider, strict_json_loads

MAX_RESPONSE_BYTES = 1024 * 1024
MAX_SSE_EVENT_BYTES = 256 * 1024
CLAUDE_USER_AGENT = "claude-cli/2.1.251 (external, cli)"
GPT_USER_AGENT = "Codex Desktop/0.147.0-alpha.1.2 (Windows 10.0.26200; x86_64) unknown (codex_exec; 0.147.0-alpha.1.2)"


def build_payload(mode: str, model: str, cell: dict) -> dict:
    if cell.get("history", []) != [] or cell.get("profile") == "codex-like" or "tools" in cell:
        raise AppError("unsupported_probe_context")
    messages = [{"role": "system", "content": cell.get("system", ".")},
                {"role": "user", "content": cell["prompt"]}]
    parameters = normalize_parameters(cell.get("parameters", {}), mode)
    limit = parameters.pop("max_output_tokens", 256)
    chat_token_field = parameters.pop("chat_token_field", "max_tokens")
    effort = cell.get("effort", "low")
    common = {"model": model, "stream": True}
    if mode == "gpt":
        return {**common, "input": messages, "store": False,
                "reasoning": {"effort": effort}, "max_output_tokens": limit, **parameters}
    if mode == "claude":
        if effort not in {"low", "medium", "high", "xhigh", "max"}:
            raise AppError("unsupported_effort")
        stop = parameters.pop("stop", None)
        result = {**common, "system": cell.get("system", "."), "messages": messages[1:],
                  "max_tokens": limit, "thinking": {"type": "adaptive"},
                  "output_config": {"effort": effort}, **parameters}
        if stop is not None:
            result["stop_sequences"] = stop
        return result
    if mode == "chat":
        return {**common, "messages": messages, chat_token_field: limit,
                "reasoning_effort": effort, "stream_options": {"include_usage": True}, **parameters}
    raise AppError("invalid_mode")


class StreamParser:
    def __init__(self, mode: str):
        self.mode = mode
        self.parts: dict[tuple[int, int], str] = {}
        self.response: dict = {}
        self.usage: dict = {}
        self.finish_reason = None
        self.completed = False
        self.saw_done = False
        self.message_started = False
        self.events = 0
        self.rejection = None
        self.blocks: dict[int, str] = {}

    def feed(self, data: str) -> None:
        if data == "[DONE]":
            if self.saw_done or not self.completed:
                raise RequestError("invalid_stream")
            self.saw_done = True
            return
        if self.saw_done:
            raise RequestError("invalid_stream")
        try:
            value = strict_json_loads(data)
        except AppError as exc:
            raise RequestError("invalid_stream", retryable=True) from exc
        if not isinstance(value, dict):
            raise RequestError("invalid_stream")
        self.events += 1
        if value.get("error") or value.get("type") == "error":
            error = value.get("error") or {}
            if not isinstance(error, dict):
                raise RequestError("invalid_stream")
            status = error.get("code")
            if type(status) is not int:
                status = 429 if error.get("type") == "rate_limit_error" else None
            raise RequestError("upstream_stream_error", status=status)
        if self.mode == "gpt":
            event = value.get("type")
            key = (value.get("output_index", 0), value.get("content_index", 0))
            if event == "response.output_text.delta":
                if self.completed:
                    raise RequestError("invalid_stream")
                self.parts[key] = self.parts.get(key, "") + str(value.get("delta", ""))
            elif event == "response.refusal.delta":
                self.rejection = "response_refused"
            elif event in {"response.failed", "response.incomplete"}:
                self.response = value.get("response", {})
                self.usage = self.response.get("usage", {}) or {}
                self.rejection = "response_incomplete"
            elif event == "response.completed":
                if self.completed:
                    raise RequestError("invalid_stream")
                response = value.get("response", {})
                if response.get("status") != "completed":
                    raise RequestError("response_incomplete", retryable=False)
                final = {}
                for output_index, item in enumerate(response.get("output", [])):
                    for content_index, part in enumerate(item.get("content", [])):
                        if part.get("type") == "refusal":
                            self.rejection = "response_refused"
                        if part.get("type") == "output_text":
                            final[(output_index, content_index)] = part.get("text", "")
                for index, content in self.parts.items():
                    if final.get(index) != content:
                        raise RequestError("stream_text_conflict")
                self.parts = final
                self.response = response
                self.usage = response.get("usage", {})
                self.completed = True
        elif self.mode == "claude":
            event = value.get("type")
            if event == "message_start":
                if self.message_started:
                    raise RequestError("invalid_stream")
                self.message_started = True
                self.response = value.get("message", {})
                self.usage.update(self.response.get("usage", {}) or {})
            elif event == "content_block_start":
                if not self.message_started or self.completed:
                    raise RequestError("invalid_stream")
                block = value.get("content_block", {})
                index = value.get("index", 0)
                if index in self.blocks or (index, 0) in self.parts:
                    raise RequestError("invalid_stream")
                self.blocks[index] = block.get("type")
                if block.get("type") == "text":
                    self.parts[(value.get("index", 0), 0)] = block.get("text", "")
                elif block.get("type") in {"tool_use", "server_tool_use"}:
                    raise RequestError("unexpected_tool", retryable=False)
            elif event == "content_block_delta":
                if not self.message_started or self.completed:
                    raise RequestError("invalid_stream")
                delta = value.get("delta", {})
                if value.get("index", 0) not in self.blocks:
                    raise RequestError("invalid_stream")
                if delta.get("type") == "text_delta":
                    key = (value.get("index", 0), 0)
                    if key not in self.parts:
                        raise RequestError("invalid_stream")
                    self.parts[key] += str(delta.get("text", ""))
            elif event == "content_block_stop":
                if value.get("index", 0) not in self.blocks:
                    raise RequestError("invalid_stream")
                del self.blocks[value.get("index", 0)]
            elif event == "message_delta":
                if not self.message_started or self.completed or self.blocks:
                    raise RequestError("invalid_stream")
                self.finish_reason = value.get("delta", {}).get("stop_reason")
                self.usage.update(value.get("usage", {}) or {})
            elif event == "message_stop":
                if not self.message_started or self.completed or self.blocks:
                    raise RequestError("invalid_stream")
                self.completed = self.finish_reason in {"end_turn", "stop_sequence"}
                if not self.completed:
                    self.rejection = "response_incomplete"
                self.response["openrouter_metadata"] = value.get("openrouter_metadata", {})
        else:
            self.response.update({key: value[key] for key in ("id", "model", "provider") if key in value})
            if value.get("usage"):
                self.usage.update(value["usage"])
            for choice in value.get("choices", []):
                if choice.get("index", 0) != 0:
                    raise RequestError("unexpected_choice")
                delta = choice.get("delta", {})
                if delta.get("refusal"):
                    raise RequestError("response_refused", retryable=False)
                if delta.get("tool_calls"):
                    raise RequestError("unexpected_tool", retryable=False)
                if delta.get("content"):
                    if self.completed:
                        raise RequestError("invalid_stream")
                    self.parts[(0, 0)] = self.parts.get((0, 0), "") + str(delta["content"])
                if choice.get("finish_reason") is not None:
                    self.finish_reason = choice["finish_reason"]
                    if self.finish_reason != "stop":
                        raise RequestError("response_incomplete", retryable=False)
                    self.completed = True

    def finish(self) -> dict:
        if self.rejection:
            raise RequestError(self.rejection, retryable=False, evidence=self.evidence())
        if not self.completed or (self.mode == "chat" and not self.saw_done):
            raise RequestError("truncated_stream", evidence=self.evidence())
        answer = "".join(value for _, value in sorted(self.parts.items()))
        return {"answer": answer, "usage": normalize_usage(self.usage),
                "response_id": self.response.get("id"), "provider": self.response.get("provider"),
                "stream_events": self.events}

    def evidence(self) -> dict:
        return {"usage": normalize_usage(self.usage), "response_id": self.response.get("id"),
                "finish_reason": self.finish_reason, "protocol_completed": self.completed,
                "saw_done_marker": self.saw_done, "stream_events": self.events}


def normalize_usage(usage: dict) -> dict:
    usage = usage or {}
    if not isinstance(usage, dict):
        raise RequestError("invalid_usage", retryable=False)
    details = usage.get("output_tokens_details") or usage.get("completion_tokens_details") or {}
    if not isinstance(details, dict):
        raise RequestError("invalid_usage", retryable=False)
    result = {
        "input_tokens": usage.get("input_tokens", usage.get("prompt_tokens")),
        "output_tokens": usage.get("output_tokens", usage.get("completion_tokens")),
        "thinking_tokens": details.get("thinking_tokens", details.get("reasoning_tokens")),
        "cost": usage.get("cost"),
    }
    for name, value in result.items():
        if value is None:
            continue
        if isinstance(value, bool) or not isinstance(value, (int, float)) or not math.isfinite(value) or value < 0:
            raise RequestError("invalid_usage", retryable=False)
        if name != "cost" and type(value) is not int:
            raise RequestError("invalid_usage", retryable=False)
    return result


def parse_stream(decoded: str, mode: str, guard: SecretGuard) -> dict:
    parser = StreamParser(mode)
    data_lines = []
    try:
        for line in decoded.replace("\r\n", "\n").replace("\r", "\n").split("\n"):
            if not line:
                if data_lines:
                    event = "\n".join(data_lines)
                    if len(event.encode("utf-8")) > MAX_SSE_EVENT_BYTES:
                        raise RequestError("response_too_large", retryable=False)
                    if event != "[DONE]":
                        guard.check(strict_json_loads(event))
                    parser.feed(event)
                    data_lines.clear()
            elif line.startswith("data:"):
                data_lines.append(line[5:].removeprefix(" "))
        if data_lines:
            raise RequestError("truncated_stream")
        return parser.finish()
    except RequestError as exc:
        if not exc.evidence and exc.code != "credential_echo":
            exc.evidence = parser.evidence()
        raise
    except (AppError, KeyError, TypeError, ValueError, AttributeError) as exc:
        raise RequestError("invalid_stream") from exc


class AsyncTransport:
    def __init__(self, secrets: list[str] = (), *, timeout: float = 120, concurrency: int = 32, gates=None):
        self.guard = SecretGuard(secrets)
        self.timeout = timeout
        self._clients: dict[str, httpx.AsyncClient] = {}
        self._gates: dict[str, RateLimitGate] = gates if gates is not None else {}
        self._slots = asyncio.Semaphore(concurrency)

    async def close(self) -> None:
        for client in self._clients.values():
            await client.aclose()
        self._clients.clear()
        self.guard = SecretGuard()

    def _client(self, base: str) -> httpx.AsyncClient:
        if base not in self._clients:
            self._clients[base] = httpx.AsyncClient(
                **http_client_options(base),
                timeout=httpx.Timeout(self.timeout, connect=min(15, self.timeout)),
                limits=httpx.Limits(max_connections=32, max_keepalive_connections=16),
            )
        return self._clients[base]

    async def request(self, mode: str, base_url: str, key: str, model: str, cell: dict,
                      *, allow_insecure: bool = False, on_dispatch=None) -> dict:
        async with self._slots:
            return await self._request(mode, base_url, key, model, cell, allow_insecure=allow_insecure, on_dispatch=on_dispatch)

    async def _request(self, mode: str, base_url: str, key: str, model: str, cell: dict,
                      *, allow_insecure: bool = False, on_dispatch=None) -> dict:
        base = normalize_api_base_url(base_url, allow_insecure=allow_insecure)
        guard = self.guard.including(key)
        payload = build_payload(mode, model, cell)
        guard.check({"base":base, "payload":payload}, code="credential_in_configuration")
        origin = urlsplit(base)
        if origin.port == {"http": 80, "https": 443}.get(origin.scheme):
            host = origin.hostname
            origin = origin._replace(netloc=f"[{host}]" if ":" in host else host)
        gate = self._gates.setdefault(origin.geturl(), RateLimitGate(max_in_flight=4 if recognized_provider(base) == "openrouter" else None))
        await gate.acquire()
        response_headers, status = {}, None
        started = time.monotonic()
        raw, body_complete = bytearray(), False
        credential_echo = False

        def exchange():
            if credential_echo:
                return {"http_status": status, "body_complete": False, "redacted": True}
            return guard.redact({"request_json": payload, "response_utf8": bytes(raw).decode("utf-8", errors="replace"),
                "headers": response_headers, "http_status": status, "body_complete": body_complete})

        try:
            async with asyncio.timeout(self.timeout):
                path = {"gpt":"/responses", "claude":"/messages", "chat":"/chat/completions"}[mode]
                headers = {"Authorization": "Bearer " + key, "Content-Type":"application/json",
                           "Accept":"text/event-stream", "User-Agent": {"gpt": GPT_USER_AGENT, "claude": CLAUDE_USER_AGENT}.get(mode, "meow-llm-detector/4.5.0")}
                if mode == "claude":
                    headers["anthropic-version"] = "2023-06-01"
                client = self._client(base)
                if on_dispatch:
                    on_dispatch()
                async with client.stream("POST", base + path, json=payload, headers=headers) as response:
                    status = response.status_code
                    response_headers = dict(response.headers)
                    guard.check(response_headers)
                    async for chunk in response.aiter_bytes():
                        if len(raw) + len(chunk) > MAX_RESPONSE_BYTES:
                            raw.extend(chunk[:MAX_RESPONSE_BYTES - len(raw)])
                            raise RequestError("response_too_large", retryable=False)
                        raw.extend(chunk)
                body_complete = True
                raw = bytes(raw)
                decoded = raw.decode("utf-8", errors="strict")
                guard.check(decoded)
                if status is None or not 200 <= status < 300:
                    try:
                        error_value = strict_json_loads(decoded)
                        guard.check(error_value)
                        upstream_error = error_value.get("error", {}) if isinstance(error_value, dict) else {}
                        evidence = {"upstream_error": guard.redact(upstream_error)}
                    except AppError:
                        evidence = {}
                    raise RequestError("redirect_rejected" if status and 300 <= status < 400 else "upstream_http_error",
                                       status=status, retryable=not (status and 300 <= status < 400),
                                       headers=response_headers, evidence=evidence)
                result = parse_stream(decoded, mode, guard)
                guard.check(result)
                result.update({"http_status":status, "elapsed_ms":round((time.monotonic() - started) * 1000),
                               "request_json":payload, "response_utf8":decoded,
                               "headers":guard.redact(response_headers), "body_complete": True})
                return result
        except RequestError as exc:
            credential_echo = exc.code == "credential_echo"
            guard.check(exc.evidence)
            exc.exchange = exchange()
            if exc.status is not None:
                status = exc.status
            elif status is not None:
                exc.status = status
            if not exc.headers:
                exc.headers = response_headers
            exc.headers = guard.redact(exc.headers)
            raise
        except (httpx.TimeoutException, TimeoutError) as exc:
            error = RequestError("request_timeout", status=status)
            error.exchange = exchange()
            raise error from exc
        except (httpx.HTTPError, OSError, UnicodeError) as exc:
            error = RequestError("connection_error", status=status)
            error.exchange = exchange()
            raise error from exc
        except asyncio.CancelledError as exc:
            exc.exchange = exchange()
            raise
        finally:
            await gate.observe(response_headers, status)
