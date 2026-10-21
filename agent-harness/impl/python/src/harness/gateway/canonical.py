"""Canonical request/response helpers (design §8.1, Appendix A.6)."""
from __future__ import annotations


def text(t: str) -> dict:
    return {"type": "text", "text": t}


def tool_call(id: str, name: str, arguments: dict) -> dict:
    return {"type": "tool_call", "id": id, "name": name, "arguments": arguments}


def tool_result(call_id: str, parts: list[dict], is_error: bool = False) -> dict:
    return {"type": "tool_result", "call_id": call_id, "parts": parts, "is_error": is_error}


def message(role: str, parts: list[dict], **extra) -> dict:
    return {"role": role, "parts": parts, **extra}


def message_text(msg: dict) -> str:
    out = []
    for p in msg["parts"]:
        if p["type"] == "text":
            out.append(p["text"])
        elif p["type"] == "tool_result":
            out.append(message_text({"parts": p["parts"]}))
    return "\n".join(out)


def tool_calls_of(msg: dict) -> list[dict]:
    return [p for p in msg["parts"] if p["type"] == "tool_call"]


def validate_request(req: dict) -> None:
    for k in ("model_ref", "messages", "params", "metadata"):
        if k not in req:
            raise ValueError(f"request missing {k}")
    if "max_output_tokens" not in req["params"]:
        raise ValueError("params.max_output_tokens is required")
    if "run_id" not in req["metadata"]:
        raise ValueError("metadata.run_id is required")
    for m in req["messages"]:
        for p in m["parts"]:
            if p["type"] == "tool_call" and not all(k in p for k in ("id", "name", "arguments")):
                raise ValueError("tool_call part requires id, name, arguments")
            if p["type"] == "tool_result" and not all(k in p for k in ("call_id", "parts", "is_error")):
                raise ValueError("tool_result part requires call_id, parts, is_error")
