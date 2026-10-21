"""Conservative token estimator (B2, proposal P5): ceil(utf8_bytes / divisor) per text part."""
from __future__ import annotations
import math
from .canonical import canonicalize

IMAGE_TOKENS = 1500


def estimate_text(text: str, divisor: float = 3.0) -> int:
    return math.ceil(len(text.encode("utf-8")) / divisor)


def estimate_json(obj, divisor: float = 3.0) -> int:
    return math.ceil(len(canonicalize(obj)) / divisor)


def estimate_part(part: dict, divisor: float = 3.0) -> int:
    t = part["type"]
    if t == "text":
        return estimate_text(part["text"], divisor)
    if t == "image":
        return IMAGE_TOKENS
    if t == "tool_call":
        return estimate_text(part["name"], divisor) + estimate_json(part["arguments"], divisor)
    if t == "tool_result":
        return sum(estimate_part(p, divisor) for p in part["parts"])
    raise ValueError(f"unknown part type {t}")


def estimate_message(msg: dict, divisor: float = 3.0) -> int:
    return sum(estimate_part(p, divisor) for p in msg["parts"])


def estimate_request(messages: list[dict], tools: list[dict] | None = None, divisor: float = 3.0) -> int:
    n = sum(estimate_message(m, divisor) for m in messages)
    if tools:
        n += sum(estimate_json(t, divisor) for t in tools)
    return n
