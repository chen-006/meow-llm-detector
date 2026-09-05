from __future__ import annotations

import re
from typing import Any

from .errors import AppError
from .utils import integer

NORMALIZER_IDS = (
    "exact_trimmed_casefold", "exact_trimmed", "integer",
    "b80_exact_3", "behavior_label", "fixed_enum", "whitespace_collapse",
)
MAX_ANSWER_LENGTH = 65536


def validate_normalizer(config: dict[str, Any]) -> None:
    if not isinstance(config, dict) or config.get("id") not in NORMALIZER_IDS:
        raise AppError("unsupported_normalizer", field="normalizer")
    parameters = config.get("parameters", {})
    if not isinstance(parameters, dict):
        raise AppError("invalid_normalizer", field="parameters")
    integer(parameters.get("max_length", 128), "max_length", 1, 4096)
    allowed = {"max_length", "values"} if config["id"] == "fixed_enum" else {"max_length"}
    if set(parameters) - allowed:
        raise AppError("invalid_normalizer", field="parameters")
    if config["id"] == "fixed_enum":
        values = parameters.get("values")
        if not isinstance(values, dict) or not 1 <= len(values) <= 256:
            raise AppError("invalid_normalizer", field="values")
        normalized = set()
        for key, value in values.items():
            if not isinstance(key, str) or not isinstance(value, str) or not key.strip() or not value:
                raise AppError("invalid_normalizer", field="values")
            if len(key) > 4096 or len(value) > 4096 or key.strip().casefold() in normalized:
                raise AppError("invalid_normalizer", field="values")
            normalized.add(key.strip().casefold())


def normalize_answer(value: str, config: dict[str, Any] | None = None) -> str:
    config = config or {"id": "exact_trimmed_casefold", "parameters": {}}
    normalizer_id = config["id"]
    parameters = config.get("parameters", {})
    if not isinstance(value, str) or len(value) > MAX_ANSWER_LENGTH:
        return "__INVALID_OUTPUT__"
    text = value.strip()
    if not text:
        return "__INVALID_OUTPUT__"
    if normalizer_id == "exact_trimmed_casefold":
        text = text.casefold()
    elif normalizer_id in {"integer", "b80_exact_3"}:
        if len(text) > 128 or not re.fullmatch(r"[+-]?\d+", text):
            return "__INVALID_OUTPUT__"
        text = str(int(text))
        if normalizer_id == "b80_exact_3":
            text = "exact_3" if text == "3" else "other_integer"
    elif normalizer_id == "behavior_label":
        text = text.strip('`"\'.,:;!?()[]{}').casefold()
        text = re.sub(r"\s+", " ", text)
        if not re.fullmatch(r"[a-z][a-z .'-]*", text):
            return "__INVALID_OUTPUT__"
    elif normalizer_id == "fixed_enum":
        mapping = {key.strip().casefold(): item for key, item in parameters["values"].items()}
        text = mapping.get(text.casefold(), "__OTHER__")
    elif normalizer_id == "whitespace_collapse":
        text = " ".join(text.split())
    elif normalizer_id != "exact_trimmed":
        raise AppError("unsupported_normalizer")
    if not text or len(text) > parameters.get("max_length", 128):
        return "__INVALID_OUTPUT__"
    return text


def builtin_probability_normalizer(probe_id: str) -> dict[str, Any]:
    name = {"b80_letter_count": "b80_exact_3", "rand_country": "behavior_label",
            "rand_bird": "behavior_label"}.get(probe_id, "exact_trimmed_casefold")
    return {"id": name, "parameters": {}}
