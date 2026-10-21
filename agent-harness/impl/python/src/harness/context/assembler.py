"""ContextAssembler (§7.3): pure function from projections + config to a model request, with cache-stable segments."""
from __future__ import annotations
from typing import Callable
from ..canonical import canonicalize, sha256_hex
from ..gateway.canonical import message_text
from ..tokens import estimate_request


def wrap_untrusted(text: str, source: str) -> str:
    return f'<<untrusted source="{source}">>\n{text}\n<</untrusted>>'


def cleared_stub(call_id: str | None, seq: int) -> str:
    return f"[result cleared: call {call_id}; retrieve with history.get({seq})]"


def _resolve_message(entry: dict, resolve: Callable[[str], bytes]) -> dict:
    import json
    if entry.get("message") is not None:
        return entry["message"]
    return json.loads(resolve(entry["message_ref"]))


def entry_to_message(entry: dict, resolve) -> dict:
    t = entry["type"]
    untrusted = entry["trust"] == "untrusted"
    if t == "summary":
        text = entry["summary"] if entry.get("summary") is not None else resolve(entry["summary_ref"]).decode()
        body = f"[summary of events seq {entry['from_seq']}-{entry['to_seq']}]\n{text}"
        return {"role": "user", "parts": [{"type": "text", "text": wrap_untrusted(body, "summary") if untrusted else body}]}
    msg = _resolve_message(entry, resolve)
    if entry["cleared"] and msg["role"] == "tool":
        parts = [{**p, "parts": [{"type": "text", "text": cleared_stub(p["call_id"], entry["seq"])}]} if p["type"] == "tool_result" else p for p in msg["parts"]]
        return {"role": "tool", "parts": parts}
    if t == "recalled":
        text = message_text(msg)
        return {"role": "user", "parts": [{"type": "text", "text": wrap_untrusted(text, "recalled") if untrusted else text}]}
    if msg["role"] == "tool" and untrusted:
        src = (msg.get("provenance") or {}).get("source") or entry.get("tool") or "tool"
        parts = []
        for p in msg["parts"]:
            if p["type"] == "tool_result":
                inner = [{"type": "text", "text": wrap_untrusted(x["text"], src)} if x["type"] == "text" else x for x in p["parts"]]
                parts.append({**p, "parts": inner})
            else:
                parts.append(p)
        return {"role": "tool", "parts": parts}
    return {"role": msg["role"], "parts": msg["parts"]}


def build_context(*, system_prompt: str, profile: str, tools: list[dict], skills: list[tuple[str, str]], conversation: dict,
                  working_state: dict[str, str], guidance: list[dict], resolve, divisor: float = 3.0, window: int = 200000) -> dict:
    """Pure. Returns {messages, tools, segment_hashes, est_tokens, id_map}."""
    s0 = f"{system_prompt}\n\n[profile: {profile}]"
    tool_defs = [{"name": t["name"], "description": t["description"], "input_schema": t["input_schema"]} for t in sorted(tools, key=lambda x: x["name"])]
    s2 = "Available skills (load with skill.load):\n" + "\n".join(f"- {n}: {d}" for n, d in sorted(skills)) if skills else ""
    body = [entry_to_message(e, resolve) for e in conversation["entries"]]
    s4 = ("[working state]\n" + "\n\n".join(f"## {n}\n{c}" for n, c in sorted(working_state.items()))) if working_state else ""
    s5 = ("[harness guidance]\n" + "\n".join(f"- {g['text']}" for g in guidance)) if guidance else ""
    system_text = s0 + (("\n\n" + s2) if s2 else "")
    messages = [{"role": "system", "parts": [{"type": "text", "text": system_text}]}] + body
    if s4:
        messages.append({"role": "user", "parts": [{"type": "text", "text": s4}]})
    if s5:
        messages.append({"role": "user", "parts": [{"type": "text", "text": s5}]})
    h = lambda o: sha256_hex(canonicalize(o))
    id_map: dict = {}
    for e in conversation["entries"]:
        id_map.update(e.get("id_map") or {})
    return {"messages": messages, "tools": tool_defs, "id_map": id_map,
            "segment_hashes": {"S0": h(s0), "S1": h(tool_defs), "S2": h(s2), "S3": h(body), "S4": h(s4), "S5": h(s5)},
            "est_tokens": estimate_request(messages, tool_defs, divisor), "window": window}


def prefix_hash(ctx: dict) -> str:
    sh = ctx["segment_hashes"]
    return sha256_hex(canonicalize([sh["S0"], sh["S1"], sh["S2"]]))
