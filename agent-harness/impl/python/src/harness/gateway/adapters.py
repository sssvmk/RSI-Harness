"""Provider adapters (design §8.2). to_wire(request) / from_wire(response) with gateway-owned tool-call ids.

Tool names contain dots (fs.read); OpenAI/Anthropic reject dots, and manifest names never contain '-',
so the wire name is name.replace('.', '-') (reversible).
"""
from __future__ import annotations
import json
from typing import Callable
from ..ids import ulid
from .canonical import tool_calls_of

GEMINI_SCHEMA_KEYS = {"type", "format", "description", "nullable", "enum", "properties", "required", "items",
                      "minItems", "maxItems", "minimum", "maximum"}


def wire_name(n: str) -> str:
    return n.replace(".", "-")


def canon_name(n: str) -> str:
    return n.replace("-", ".")


def _text_of(parts: list[dict]) -> str:
    return "\n".join(p["text"] for p in parts if p["type"] == "text")


def downgrade_schema(schema, path="$", log=None):
    """Reduce JSON Schema to the Gemini-supported subset and record every dropped keyword."""
    log = [] if log is None else log
    if isinstance(schema, list):
        return [downgrade_schema(s, path, log)[0] for s in schema], log
    if not isinstance(schema, dict):
        return schema, log
    out = {}
    for k, v in schema.items():
        if k not in GEMINI_SCHEMA_KEYS:
            log.append({"path": path, "keyword": k})
            continue
        if k == "properties":
            out[k] = {pk: downgrade_schema(pv, f"{path}.properties.{pk}", log)[0] for pk, pv in v.items()}
        elif k == "items":
            out[k] = downgrade_schema(v, f"{path}.items", log)[0]
        elif k == "type" and isinstance(v, list):
            non_null = [t for t in v if t != "null"]
            out["type"] = non_null[0] if non_null else "string"
            if "null" in v:
                out["nullable"] = True
        else:
            out[k] = v
    return out, log


class Adapter:
    provider = ""

    def __init__(self, id_factory: Callable[[], str] = ulid):
        self.new_id = id_factory

    def _handle(self, id_map: dict, cid: str) -> str:
        return id_map.get(cid, cid)


class OpenAIAdapter(Adapter):
    provider = "openai"

    def to_wire(self, req: dict, model_id: str, id_map: dict | None = None) -> dict:
        id_map = id_map or {}
        msgs = []
        for m in req["messages"]:
            if m["role"] == "system":
                msgs.append({"role": "system", "content": _text_of(m["parts"])})
            elif m["role"] == "user":
                msgs.append({"role": "user", "content": _text_of(m["parts"])})
            elif m["role"] == "assistant":
                calls = tool_calls_of(m)
                w = {"role": "assistant", "content": _text_of(m["parts"]) or None}
                if calls:
                    w["tool_calls"] = [{"id": self._handle(id_map, c["id"]), "type": "function",
                                        "function": {"name": wire_name(c["name"]), "arguments": json.dumps(c["arguments"], separators=(",", ":"))}}
                                       for c in calls]
                msgs.append(w)
            elif m["role"] == "tool":
                for p in m["parts"]:
                    if p["type"] == "tool_result":
                        msgs.append({"role": "tool", "tool_call_id": self._handle(id_map, p["call_id"]), "content": _text_of(p["parts"])})
        out = {"model": model_id, "messages": msgs, "max_completion_tokens": req["params"]["max_output_tokens"]}
        if req.get("tools"):
            out["tools"] = [{"type": "function", "function": {"name": wire_name(t["name"]), "description": t["description"],
                                                               "parameters": t["input_schema"]}} for t in req["tools"]]
        if "temperature" in req["params"]:
            out["temperature"] = req["params"]["temperature"]
        if req["params"].get("stop"):
            out["stop"] = req["params"]["stop"]
        return out

    def from_wire(self, wire: dict) -> dict:
        ch = wire["choices"][0]
        m = ch["message"]
        parts, id_map = [], {}
        if m.get("content"):
            parts.append({"type": "text", "text": m["content"]})
        for tc in m.get("tool_calls") or []:
            cid = self.new_id()
            id_map[cid] = tc["id"]
            parts.append({"type": "tool_call", "id": cid, "name": canon_name(tc["function"]["name"]),
                          "arguments": json.loads(tc["function"]["arguments"] or "{}")})
        fr = {"stop": "stop", "tool_calls": "tool_calls", "length": "length", "content_filter": "content_filter"}.get(ch.get("finish_reason"), "error")
        if any(p["type"] == "tool_call" for p in parts) and fr == "stop":
            fr = "tool_calls"
        u = wire.get("usage", {})
        usage = {"input_tokens": u.get("prompt_tokens", 0), "output_tokens": u.get("completion_tokens", 0)}
        cached = (u.get("prompt_tokens_details") or {}).get("cached_tokens")
        if cached is not None:
            usage["cached_input_tokens"] = cached
        return {"message": {"role": "assistant", "parts": parts}, "finish_reason": fr, "usage": usage, "provider_meta": {"id_map": id_map}}


class AnthropicAdapter(Adapter):
    provider = "anthropic"

    def to_wire(self, req: dict, model_id: str, id_map: dict | None = None) -> dict:
        id_map = id_map or {}
        system, msgs = [], []

        def push(role, blocks):
            if msgs and msgs[-1]["role"] == role:
                msgs[-1]["content"].extend(blocks)  # Anthropic requires alternating roles
            else:
                msgs.append({"role": role, "content": list(blocks)})

        for m in req["messages"]:
            if m["role"] == "system":
                system.append(_text_of(m["parts"]))
            elif m["role"] == "user":
                push("user", [{"type": "text", "text": _text_of(m["parts"])}])
            elif m["role"] == "assistant":
                blocks = [{"type": "text", "text": p["text"]} for p in m["parts"] if p["type"] == "text" and p["text"]]
                blocks += [{"type": "tool_use", "id": self._handle(id_map, c["id"]), "name": wire_name(c["name"]), "input": c["arguments"]}
                           for c in tool_calls_of(m)]
                push("assistant", blocks)
            elif m["role"] == "tool":
                blocks = [{"type": "tool_result", "tool_use_id": self._handle(id_map, p["call_id"]), "content": _text_of(p["parts"]),
                           **({"is_error": True} if p["is_error"] else {})} for p in m["parts"] if p["type"] == "tool_result"]
                if msgs and msgs[-1]["role"] == "user":  # tool_result blocks must come first in the user turn
                    msgs[-1]["content"][0:0] = blocks
                else:
                    push("user", blocks)
        out = {"model": model_id, "max_tokens": req["params"]["max_output_tokens"], "messages": msgs}
        if system:
            out["system"] = "\n\n".join(system)
        if req.get("tools"):
            out["tools"] = [{"name": wire_name(t["name"]), "description": t["description"], "input_schema": t["input_schema"]} for t in req["tools"]]
        if "temperature" in req["params"]:
            out["temperature"] = req["params"]["temperature"]
        if req["params"].get("stop"):
            out["stop_sequences"] = req["params"]["stop"]
        return out

    def from_wire(self, wire: dict) -> dict:
        parts, id_map = [], {}
        for b in wire["content"]:
            if b["type"] == "text":
                parts.append({"type": "text", "text": b["text"]})
            elif b["type"] == "tool_use":
                cid = self.new_id()
                id_map[cid] = b["id"]
                parts.append({"type": "tool_call", "id": cid, "name": canon_name(b["name"]), "arguments": b["input"]})
        fr = {"end_turn": "stop", "stop_sequence": "stop", "tool_use": "tool_calls", "max_tokens": "length",
              "refusal": "content_filter"}.get(wire.get("stop_reason"), "error")
        u = wire.get("usage", {})
        usage = {"input_tokens": u.get("input_tokens", 0), "output_tokens": u.get("output_tokens", 0)}
        if "cache_read_input_tokens" in u:
            usage["cached_input_tokens"] = u["cache_read_input_tokens"]
        return {"message": {"role": "assistant", "parts": parts}, "finish_reason": fr, "usage": usage, "provider_meta": {"id_map": id_map}}


class GeminiAdapter(Adapter):
    provider = "gemini"

    def to_wire(self, req: dict, model_id: str, id_map: dict | None = None) -> dict:
        names: dict[str, str] = {}
        order: dict[str, int] = {}
        system, contents = [], []
        downgrades: list = []
        for m in req["messages"]:
            if m["role"] == "system":
                system.append(_text_of(m["parts"]))
            elif m["role"] == "user":
                contents.append({"role": "user", "parts": [{"text": _text_of(m["parts"])}]})
            elif m["role"] == "assistant":
                parts = [{"text": p["text"]} for p in m["parts"] if p["type"] == "text" and p["text"]]
                for i, c in enumerate(tool_calls_of(m)):
                    names[c["id"]], order[c["id"]] = c["name"], i
                    parts.append({"functionCall": {"name": c["name"], "args": c["arguments"]}})
                contents.append({"role": "model", "parts": parts})
            elif m["role"] == "tool":
                # functionResponse parts match by name and order: emit in the order of the preceding calls
                rs = sorted((p for p in m["parts"] if p["type"] == "tool_result"), key=lambda p: order.get(p["call_id"], 0))
                parts = [{"functionResponse": {"name": names.get(p["call_id"], "unknown"),
                                               "response": {"error": _text_of(p["parts"])} if p["is_error"] else {"result": _text_of(p["parts"])}}}
                         for p in rs]
                if contents and contents[-1]["role"] == "user" and contents[-1]["parts"] and "functionResponse" in contents[-1]["parts"][0]:
                    contents[-1]["parts"].extend(parts)
                else:
                    contents.append({"role": "user", "parts": parts})
        gen = {"maxOutputTokens": req["params"]["max_output_tokens"]}
        if "temperature" in req["params"]:
            gen["temperature"] = req["params"]["temperature"]
        if req["params"].get("stop"):
            gen["stopSequences"] = req["params"]["stop"]
        out = {"contents": contents, "generationConfig": gen}
        if system:
            out["systemInstruction"] = {"parts": [{"text": "\n\n".join(system)}]}
        if req.get("tools"):
            decls = []
            for t in req["tools"]:
                params, log = downgrade_schema(t["input_schema"], f"tool:{t['name']}")
                downgrades += log
                decls.append({"name": t["name"], "description": t["description"], "parameters": params})
            out["tools"] = [{"functionDeclarations": decls}]
        self.last_downgrades = downgrades
        return out

    def from_wire(self, wire: dict) -> dict:
        cand = wire["candidates"][0]
        parts, id_map, handles = [], {}, []
        for i, p in enumerate((cand.get("content") or {}).get("parts", [])):
            if "text" in p:
                parts.append({"type": "text", "text": p["text"]})
            elif "functionCall" in p:
                cid = self.new_id()
                id_map[cid] = {"name": p["functionCall"]["name"], "index": len(handles)}
                handles.append(cid)
                parts.append({"type": "tool_call", "id": cid, "name": p["functionCall"]["name"], "arguments": p["functionCall"].get("args", {})})
        raw = cand.get("finishReason", "STOP")
        fr = {"STOP": "stop", "MAX_TOKENS": "length", "SAFETY": "content_filter", "RECITATION": "content_filter",
              "BLOCKLIST": "content_filter", "PROHIBITED_CONTENT": "content_filter", "SPII": "content_filter"}.get(raw, "error")
        if handles and fr == "stop":
            fr = "tool_calls"  # Gemini reports STOP even when it requested function calls
        u = wire.get("usageMetadata", {})
        usage = {"input_tokens": u.get("promptTokenCount", 0),
                 "output_tokens": u.get("candidatesTokenCount", 0) + u.get("thoughtsTokenCount", 0)}
        if "cachedContentTokenCount" in u:
            usage["cached_input_tokens"] = u["cachedContentTokenCount"]
        return {"message": {"role": "assistant", "parts": parts}, "finish_reason": fr, "usage": usage, "provider_meta": {"id_map": id_map}}


ADAPTERS = {"openai": OpenAIAdapter, "anthropic": AnthropicAdapter, "gemini": GeminiAdapter}
