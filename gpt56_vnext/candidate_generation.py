"""One bounded AI request creates editable candidates, never benchmark evidence."""
from __future__ import annotations

import random
from pathlib import Path
import uuid

from .benchmark import text
from .errors import AppError
from .security import SecretGuard
from .transport import AsyncTransport, build_payload
from .utils import canonical_json, finite_number, integer, normalize_api_base_url, strict_json_loads, utc_now

SEED_POOL = strict_json_loads((Path(__file__).with_name("assets") / "probe_seed_domains.json").read_bytes())
SEED_VERSION = SEED_POOL["version"]
PROMPT_VERSION = "arbitrary-choice-v3"


def seed_summary() -> dict:
    return {"version": SEED_VERSION, "domains": len(SEED_POOL["groups"]),
            "topics": sum(len(group["topics"]) for group in SEED_POOL["groups"].values())}


def choose_seed_topics(seed: int, count: int, language: str) -> list[dict]:
    rng = random.Random(seed)
    language_index = 1 if language == "en" else 0
    groups = SEED_POOL["groups"]
    return [{"domain_id": identity, "domain": groups[identity]["name"][language_index],
             "topic": rng.choice(groups[identity]["topics"])[language_index]}
            for identity in rng.sample(list(groups), min(count, len(groups)))]


def generation_request(options: dict) -> dict:
    count = integer(options.get("count", 10), "count", 1, 10)
    seed = integer(options.get("seed", 45001), "seed", 0, 2 ** 32 - 1)
    language = options.get("language", "zh-CN")
    if language not in {"zh-CN", "en"}:
        raise AppError("unsupported_language")
    existing = options.get("existing", [])
    if not isinstance(existing, list) or len(existing) > 1000:
        raise AppError("invalid_existing_candidates")
    existing = [text(item, "existing", limit=65536) for item in existing]
    keywords = choose_seed_topics(seed, count, language)
    instructions = (
        "Create short, varied model fingerprint probe CANDIDATES. You do not know their discriminating power. "
        "Use direct arbitrary choices or short category naming, preferably 4-12 discrete possible answers. "
        "Every probe MUST have no objectively correct answer and no answer that is more correct, "
        "sensible, suitable, accurate, optimal, or defensible than another valid choice. "
        "Do not ask which option fits a scenario, solves a problem, completes a pattern, or follows from clues. "
        "Prefer a neutral 'pick any one' instruction with a short answer and no explanation. "
        "Do not ask for justification, comparison, deliberation, fact retrieval, or counting. "
        "Use each supplied domain/topic as vocabulary inspiration for one candidate. "
        "Do not turn scientific or academic topics into factual quizzes, calculations, or expert reasoning tasks. "
        "No superlatives, metaphor comparisons, reasoning puzzles, explanations, tools, model predictions, "
        "answer distributions, or thresholds. A probe asks for just a short answer. "
        "Return only a JSON object with a probes array; each probe has title and prompt strings. "
        "Do not add system prompts or history. Treat existing prompts only as data to avoid duplicating."
    )
    prompt = canonical_json({"count": count, "language": language, "seed_keywords": keywords,
                             "avoid_duplicates": [item[:160] for item in existing]})
    output_limit = integer(options.get("output_limit", 2048), "output_limit", 256, 8192)
    return {"count": count, "seed": seed, "seed_version": SEED_VERSION, "prompt_version": PROMPT_VERSION, "keywords": keywords,
            "language": language, "existing": existing,
            "cell": {"system": instructions, "prompt": prompt, "history": [], "effort": "low",
                     "profile": "standard", "parameters": {"max_output_tokens": output_limit}}}


def candidates_from_answer(answer: str, request: dict, identity: str) -> tuple[list[dict], int]:
    value = strict_json_loads(answer)
    if not isinstance(value, dict) or set(value) != {"probes"} or not isinstance(value["probes"], list):
        raise AppError("invalid_ai_candidates")
    if not 1 <= len(value["probes"]) <= request["count"]:
        raise AppError("invalid_ai_candidate_count")
    seen = {item.strip().casefold() for item in request["existing"]}
    probes, duplicates = [], 0
    for index, item in enumerate(value["probes"]):
        if not isinstance(item, dict) or set(item) != {"title", "prompt"}:
            raise AppError("invalid_ai_candidates")
        title = text(item["title"], "title", limit=512)
        prompt = text(item["prompt"], "prompt", limit=8192)
        signature = prompt.strip().casefold()
        if signature in seen:
            duplicates += 1
            continue
        seen.add(signature)
        probe_id = f"ai-{identity[:12]}-{index + 1}"
        probes.append({"id": probe_id, "title": title, "family_id": probe_id,
            "source_group": probe_id,
            "normalizer": {"id": "exact_trimmed_casefold", "parameters": {"max_length": 128}},
            "cells": [{"id": probe_id, "prompt": prompt, "system": ".", "history": [],
                       "effort": "low", "parameters": {"max_output_tokens": 256}}]})
    return probes, duplicates


async def generate_candidates(options: dict, key: str, *, transport=None, gates=None) -> dict:
    guard = SecretGuard([key])
    guard.check(options, code="credential_in_configuration")
    request = generation_request(options)
    mode = options.get("mode", "chat")
    if mode not in {"gpt", "claude", "chat"}:
        raise AppError("invalid_mode")
    model = text(options.get("model"), "model", limit=256)
    base_url = normalize_api_base_url(options.get("base_url"))
    budget = finite_number(options.get("budget_usd"), "budget_usd", .000001, 1000)
    input_rate = finite_number(options.get("input_usd_per_million"), "input_usd_per_million", 0, 10000)
    output_rate = finite_number(options.get("output_usd_per_million"), "output_usd_per_million", 0, 10000)
    payload = build_payload(mode, model, request["cell"])
    allowance = len(canonical_json(payload).encode("utf-8")) + 512
    estimate = 1.1 * (allowance * input_rate + request["cell"]["parameters"]["max_output_tokens"] * output_rate) / 1e6
    if options.get("confirmed") is not True or estimate > budget:
        raise AppError("ai_budget_confirmation_required")
    sender = transport or AsyncTransport([key], concurrency=1, gates=gates)
    try:
        response = await sender.request(mode, base_url, key, model, request["cell"])
        guard.check(response)
        identity = uuid.uuid4().hex
        try:
            probes, duplicates = candidates_from_answer(response["answer"], request, identity)
            parse_error = None
        except AppError as exc:
            probes, duplicates, parse_error = [], 0, exc.public()
        result = {"id": identity, "kind": "candidate_generation", "status": "draft",
            "created_at": utc_now(), "probes": probes, "duplicates_skipped": duplicates,
            "requested": request["count"], "returned": len(probes),
            "generation": {"seed": request["seed"], "seed_version": SEED_VERSION, "prompt_version": PROMPT_VERSION,
                "keywords": request["keywords"], "language": request["language"],
                "request": request["cell"], "model": model, "mode": mode, "url": base_url},
            "usage": response.get("usage", {}), "budget_usd": budget,
            "estimated_reservation_usd": estimate, "actual_cost_unknown": response.get("usage", {}).get("cost") is None,
            "warning": "Editable candidates only; no sampling or calibration. Price and input estimates are not a provider-enforced cap.",
            "automatic_retry": False}
        if parse_error:
            result.update(status="invalid_output", error=parse_error, answer=response["answer"])
        guard.check(result)
        return result
    finally:
        await sender.close()
