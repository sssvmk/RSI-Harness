"""Stuck-loop detection (design §7.4.3)."""
from __future__ import annotations
from .canonical import canonicalize, sha256_hex


def call_signature(tool: str, args, result_digest: str) -> str:
    return sha256_hex(tool.encode() + canonicalize(args) + result_digest.encode())


def turn_signature(call_sigs: list[str]) -> str | None:
    return sha256_hex("".join(call_sigs)) if call_sigs else None


def stuck_action(loop_state: dict, threshold: int = 4, extra: int = 2) -> str | None:
    """Decide from the `loop` projection (already including the latest turn.completed)."""
    n = loop_state["repeat_count"]
    if n >= threshold + extra and loop_state["stuck_warned"]:
        return "terminate"
    if n >= threshold and not loop_state["stuck_warned"]:
        return "guidance"
    return None
