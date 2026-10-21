"""Run provenance manifest (design §13.9, PRD §3.6). One manifest per run, captured at execution time. Every field is `recorded`, `unavailable`, or `not_applicable`; an unavailable value is null with a reason and is
never invented. `run.created` carries the start fields, `run.finished` the final fields and `manifest_hash` (SHA-256 of the canonical manifest). A complete manifest improves auditability; it does not make a model deterministic."""
from __future__ import annotations
import jsonschema
from .canonical import canonicalize, sha256_hex
from .tools.registry import load_schema

SCHEMA_VERSION = "1.0"
START = ("models", "harness_version", "config_commit", "profile", "limits", "definitions", "tier_policy", "image_digests", "build_hashes", "task", "inputs", "budgets", "test_visibility", "parent_run_id", "project")
FINAL = ("termination_status", "tool_call_count", "outcome", "usage", "model_calls")
REQUIRED = {"core": {"models", "harness_version", "config_commit", "profile", "limits", "budgets"},
            "standard": {"models", "harness_version", "config_commit", "profile", "limits", "budgets", "definitions", "tier_policy"},
            "high": {"models", "harness_version", "config_commit", "profile", "limits", "budgets", "definitions", "tier_policy", "image_digests", "build_hashes"}}


class ManifestError(ValueError):
    pass


def rec(value, reason: str | None = None) -> dict:
    return {"status": "recorded", "value": value, **({"reason": reason} if reason else {})}


def na(reason: str) -> dict:
    return {"status": "not_applicable", "value": None, "reason": reason}


def unavailable(reason: str) -> dict:
    return {"status": "unavailable", "value": None, "reason": reason}


def validate(m: dict, profile: str) -> list[str]:
    """Schema, every start field present, and no required field unavailable (a required field that could be recorded and was not refuses the run; one that does not apply is fine)."""
    try: jsonschema.validate(m, load_schema("run.manifest.v1.json"))
    except jsonschema.ValidationError as e: return [f"manifest invalid: {e.message}"]
    f, errs = m["fields"], []
    for k in START:
        if k not in f: errs.append(f"field {k} is missing")
    for k in sorted(REQUIRED.get(profile, REQUIRED["core"])):
        if k in f and f[k]["status"] == "unavailable": errs.append(f"required field {k} is unavailable ({f[k].get('reason', '')})")      # not_applicable is a legitimate absence; unavailable is a gap
    return errs


def pending_final() -> dict:
    return {k: unavailable("the run has not finished") for k in FINAL}


def manifest_hash(m: dict) -> str:
    return sha256_hex(canonicalize(m))


def merged(start: dict, final: dict) -> dict:
    return {**start, "fields": {**start["fields"], **final}}


async def final_fields(svc, run, models: dict, state: str | None = None) -> dict:
    """Final fields, computed from the log at the moment the run finishes."""
    from .budgets import cost_of
    evs = svc.store.events(run.run_id)
    st = state or await run.state(); term = next((e["payload"] for e in reversed(evs) if e["type"] == "run.terminated"), None)
    calls = sum(1 for e in evs if e["type"] == "tool.call.requested")
    outs = [e["payload"] for e in evs if e["type"] == "outcome.evaluated"]
    resp, ref, cost, priced, i_tok, o_tok = [], None, 0.0, False, 0, 0
    for e in evs:                                                  # each response is priced by the model that was most recently requested
        if e["type"] == "model.requested": ref = e["payload"]["model_ref"]
        elif e["type"] == "model.responded":
            u = e["payload"]["usage"]; i_tok += u["input_tokens"]; o_tok += u["output_tokens"]; price = (models.get(ref) or {}).get("price")
            if price: priced = True; cost += cost_of(u, price)
    tv = next((e["payload"] for e in reversed(evs) if e["type"] == "test_visibility.verified"), None)      # the declaration, plus what the environment was verified to do
    out = {"termination_status": rec({"state": st, "reason": (term or {}).get("reason", "completed" if st == "COMPLETED" else None)}),
            "tool_call_count": rec(calls),
            "outcome": rec({"verdict": outs[-1]["verdict"] if outs else "not_evaluated", "evidence_references": [outs[-1]["contract_hash"]] if outs else []}),
            "usage": rec({"input_tokens": i_tok, "output_tokens": o_tok, "estimated_cost": round(cost, 8) if priced else None, "currency": "USD" if priced else None}, None if priced else "no price is configured for the model")}
    out["model_calls"] = rec([{"call_id": e["payload"]["request_ref"], "seq": e["seq"], "turn": e["payload"].get("turn"), "model_ref": e["payload"]["model_ref"]} for e in evs if e["type"] == "model.requested"])      # each model call links to this run
    if tv is not None: out["test_visibility"] = rec(tv)
    return out
