from copy import deepcopy
import math
from typing import Any
from .errors import AppError, RequestError, upstream_detail, http_error_detail
from .security import SecretGuard
from .utils import integer, finite_number, strict_json_loads

MAX_SSE_EVENT_BYTES = 256 * 1024
MAX_TEXT_BYTES = 65536
CLAUDE_USER_AGENT = "claude-cli/2.1.251 (external, cli)"
GPT_USER_AGENT = "Codex Desktop/0.147.0-alpha.1.2 (Windows 10.0.26200; x86_64) unknown (codex_exec; 0.147.0-alpha.1.2)"

def text(value: Any, field: str, *, empty: bool = False, limit: int = MAX_TEXT_BYTES) -> str:
    if not isinstance(value, str) or (not empty and not value.strip()) or len(value.encode("utf-8")) > limit:
        raise AppError("invalid_text", field=field)
    return value

def normalize_parameters(value: dict, mode: str, *, _legacy=False) -> dict:
    parameters = deepcopy(value)
    if not isinstance(parameters, dict) or set(parameters) - {"max_output_tokens", "temperature", "top_p", "stop", "chat_token_field"}:
        raise AppError("unsupported_parameter")
    integer(parameters.get("max_output_tokens", 256), "max_output_tokens", 1, 65536)
    if "temperature" in parameters:
        finite_number(parameters["temperature"], "temperature", 0, 2)
    if "top_p" in parameters:
        finite_number(parameters["top_p"], "top_p", 0, 1)
    if "stop" in parameters:
        if not isinstance(parameters["stop"], list) or not 1 <= len(parameters["stop"]) <= 4:
            raise AppError("unsupported_parameter", field="stop")
        for stop in parameters["stop"]:
            text(stop, "stop", limit=256)
    if parameters.get("chat_token_field", "max_tokens") not in {"max_tokens", "max_completion_tokens"}:
        raise AppError("unsupported_parameter")
    if not _legacy:
        if (mode == "gpt" and "stop" in parameters) or (mode != "chat" and "chat_token_field" in parameters):
            raise AppError("unsupported_parameter")
        parameters.setdefault("max_output_tokens", 256)
        if mode == "chat":
            parameters.setdefault("chat_token_field", "max_tokens")
    return parameters

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

def incomplete_code(reason):
    return {"max_output_tokens": "response_token_limit", "max_tokens": "response_token_limit",
            "length": "response_token_limit", "content_filter": "response_filtered",
            "refusal": "response_refused", "server_error": "upstream_response_failed"}.get(reason, "response_incomplete")


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
        self.error_detail = {}

    def _merge_usage(self, usage):
        if isinstance(usage, dict):
            self.usage.update(usage)
        elif usage is not None:
            self.usage["_invalid_usage"] = True

    def feed(self, data: str) -> None:
        if data == "[DONE]":
            if self.saw_done:
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
            raise RequestError("upstream_stream_error", evidence={"upstream": upstream_detail(error)})
        if self.mode == "gpt":
            event = value.get("type")
            key = (value.get("output_index", 0), value.get("content_index", 0))
            if event == "response.output_text.delta":
                if self.completed:
                    raise RequestError("invalid_stream")
                self.parts[key] = self.parts.get(key, "") + str(value.get("delta", ""))
            elif event in {"response.refusal.delta", "response.refusal.done"}:
                self.rejection = "response_refused"
            elif event in {"response.failed", "response.incomplete"}:
                self.response = value.get("response", {})
                self.error_detail = upstream_detail(self.response.get("error") or self.response.get("incomplete_details") or {})
                self.usage = self.response.get("usage", {}) or {}
                self.rejection = incomplete_code((self.response.get("incomplete_details") or {}).get("reason")
                    or (self.response.get("error") or {}).get("code"))
            elif event == "response.completed":
                if self.completed:
                    raise RequestError("invalid_stream")
                response = value.get("response", {})
                if response.get("status") != "completed":
                    raise RequestError(incomplete_code(self.finish_reason), evidence={"upstream": {"reason": self.finish_reason}})
                final = {}
                for output_index, item in enumerate(response.get("output", [])):
                    for content_index, part in enumerate(item.get("content", [])):
                        if part.get("type") == "refusal":
                            self.rejection = "response_refused"
                        if part.get("type") == "output_text":
                            final[(output_index, content_index)] = part.get("text", "")
                # Only tolerate an omitted final output container after successful completion.
                # Explicit final text (including empty text) remains authoritative.
                output_missing = "output" not in response or response["output"] == []
                if (output_missing and not self.rejection
                        and not response.get("error") and not response.get("incomplete_details")):
                    final = self.parts.copy()
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
                self._merge_usage(self.response.get("usage"))
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
                    raise RequestError("unexpected_tool")
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
                self._merge_usage(value.get("usage"))
            elif event == "message_stop":
                if not self.message_started or self.completed or self.blocks:
                    raise RequestError("invalid_stream")
                self.completed = self.finish_reason in {"end_turn", "stop_sequence"}
                if not self.completed:
                    self.rejection = incomplete_code(self.finish_reason)
                    self.error_detail = {"reason": self.finish_reason} if self.finish_reason else {}
                self.response["openrouter_metadata"] = value.get("openrouter_metadata", {})
        else:
            self.response.update({key: value[key] for key in ("id", "model", "provider") if key in value})
            self._merge_usage(value.get("usage"))
            for choice in value.get("choices", []):
                if choice.get("index", 0) != 0:
                    raise RequestError("unexpected_choice")
                delta = choice.get("delta", {})
                if delta.get("refusal"):
                    raise RequestError("response_refused")
                if delta.get("tool_calls"):
                    raise RequestError("unexpected_tool")
                if delta.get("content"):
                    if self.completed:
                        raise RequestError("invalid_stream")
                    self.parts[(0, 0)] = self.parts.get((0, 0), "") + str(delta["content"])
                if choice.get("finish_reason") is not None:
                    self.finish_reason = choice["finish_reason"]
                    if self.finish_reason != "stop":
                        raise RequestError(incomplete_code(self.finish_reason))
                    self.completed = True

    def finish(self) -> dict:
        if self.rejection:
            raise RequestError(self.rejection, evidence=self.evidence())
        if not self.completed or (self.mode == "chat" and not self.saw_done):
            raise RequestError("truncated_stream", evidence=self.evidence())
        answer = "".join(value for _, value in sorted(self.parts.items()))
        return {"answer": answer, "usage": normalize_usage(self.usage),
                "response_id": self.response.get("id"), "provider": self.response.get("provider"),
                "stream_events": self.events}

    def evidence(self) -> dict:
        return {"usage": normalize_usage(self.usage), "response_id": self.response.get("id"),
                "finish_reason": self.finish_reason, "protocol_completed": self.completed,
                "saw_done_marker": self.saw_done, "stream_events": self.events,
                **({"upstream": self.error_detail} if self.error_detail else {})}

def normalize_usage(usage: dict) -> dict:
    """Accounting metadata must not invalidate an otherwise complete answer."""
    usage = usage or {}
    warnings = []
    if not isinstance(usage, dict):
        usage = {}
        warnings.append("invalid_usage")
    elif usage.get("_invalid_usage"):
        warnings.append("invalid_usage")
        usage = {}  # An earlier partial usage object is not a final bill.
    details = usage.get("output_tokens_details") or usage.get("completion_tokens_details") or {}
    if not isinstance(details, dict):
        details = {}
        warnings.append("invalid_usage_details")
    result = {
        "input_tokens": usage.get("input_tokens", usage.get("prompt_tokens")),
        "output_tokens": usage.get("output_tokens", usage.get("completion_tokens")),
        "thinking_tokens": details.get("thinking_tokens", details.get("reasoning_tokens")),
        "cost": usage.get("cost"),
    }
    for name, value in result.items():
        if value is None:
            continue
        if isinstance(value, str):
            try:
                value = float(value) if name == "cost" else int(value)
            except ValueError:
                value = None
        if (type(value) not in (int, float) or type(value) is float and not math.isfinite(value) or value < 0
                or name != "cost" and type(value) is not int):
            value = None
            warnings.append("invalid_" + name)
        result[name] = value
    if warnings:
        result["warnings"] = warnings
    return result

def parse_stream(decoded: str, mode: str, guard: SecretGuard) -> dict:
    """Frame once; GPT uses the last response segment, never a prior fallback."""
    parser = StreamParser(mode)
    data_lines, events = [], []
    response_id = None
    try:
        for line in decoded.replace("\r\n", "\n").replace("\r", "\n").split("\n"):
            if not line:
                if not data_lines:
                    continue
                event = "\n".join(data_lines)
                data_lines.clear()
                if len(event.encode("utf-8")) > MAX_SSE_EVENT_BYTES:
                    raise RequestError("response_too_large")
                guard.check(event)
                try:
                    value = strict_json_loads(event) if event != "[DONE]" else None
                except AppError:
                    value = None  # A malformed selected segment still fails in StreamParser.
                if value is not None:
                    guard.check(value)
                response = value.get("response") if isinstance(value, dict) else None
                identity = response.get("id") if isinstance(response, dict) else None
                if mode == "gpt" and isinstance(identity, str) and identity and identity != response_id:
                    if response_id is not None or value.get("type") == "response.created":
                        events.clear()
                    response_id = identity
                events.append(event)
            elif line.startswith("data:"):
                data_lines.append(line[5:].removeprefix(" "))
        if data_lines:
            raise RequestError("truncated_stream")
        if not events and decoded.strip():
            guard.check(decoded)
            raise RequestError("unexpected_response", evidence={
                "local": {"source": "protocol", "type": "UnexpectedResponse"},
                "upstream": http_error_detail(decoded, guard)})
        for event in events:
            parser.feed(event)
        return parser.finish()
    except RequestError as exc:
        if not exc.evidence and exc.code != "credential_echo":
            exc.evidence = parser.evidence()
        raise
    except (AppError, KeyError, TypeError, ValueError, AttributeError) as exc:
        raise RequestError("invalid_stream") from exc
