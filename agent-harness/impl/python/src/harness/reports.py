"""Run comparison and aggregation (PRD §3.6 Harness as a Behavioral Component, Run Provenance Manifest): performance = F(model, harness, environment, task, grader).
Runs are comparable in aggregate metrics only when they share the same configuration commit, models, task and grader; the report refuses to mix them unless asked, and says so."""
from __future__ import annotations
from .canonical import canonicalize, sha256_hex

KEY_FIELDS = ("config_commit", "models", "task_id", "task_version", "grader_hash", "harness_version", "profile")


class NotComparable(ValueError):
    def __init__(self, groups: list[dict]):
        super().__init__(f"{len(groups)} incomparable groups: " + "; ".join(f"{g['runs']} differ on {g['key_diff']}" for g in groups[1:]))
        self.groups = groups


def comparability_key(created: dict) -> dict:
    """The fields of a run.created payload that decide whether two runs measure the same thing. A missing field is recorded as null, so unknown never matches known."""
    prov = created.get("provenance") or {}
    models = prov.get("models") or [m for m in ((created.get("agent") or {}).get("model_ref"),) if m]
    return {"config_commit": created.get("config_commit"), "models": sorted(models) if isinstance(models, list) else models, "task_id": created.get("task_id"),
            "task_version": prov.get("task_version"), "grader_hash": prov.get("grader_hash"), "harness_version": prov.get("harness_version"), "profile": created.get("profile")}


def differences(a: dict, b: dict) -> list[dict]:
    ka, kb = comparability_key(a), comparability_key(b)
    return [{"field": f, "a": ka[f], "b": kb[f]} for f in KEY_FIELDS if canonicalize(ka[f]) != canonicalize(kb[f])]


def compare_runs(a: dict, b: dict) -> dict:
    d = differences(a, b)
    return {"comparable": not d, "differences": d}


def aggregate(runs: list[dict], allow_mixed: bool = False) -> dict:
    """runs: [{run_id, created, value}]. Returns per-group statistics. Mixed groups are refused unless `allow_mixed`, and then the result is stamped non-comparable."""
    groups: dict[str, dict] = {}
    for r in runs:
        k = comparability_key(r["created"]); fp = sha256_hex(canonicalize(k))[:16]
        g = groups.setdefault(fp, {"key": k, "fingerprint": fp, "runs": [], "values": []})
        g["runs"].append(r["run_id"]); g["values"].append(r["value"])
    out = []
    for g in groups.values():
        n, vals, t = len(g["values"]), g["values"], 0.0
        for v in vals: t += v                                          # plain left-to-right addition (Python's sum() is compensated, which other languages would not reproduce)
        out.append({"key": g["key"], "fingerprint": g["fingerprint"], "runs": g["runs"], "n": n, "mean": t / n if n else None})
    out.sort(key=lambda g: g["fingerprint"])
    if len(out) > 1:
        for g in out[1:]: g["key_diff"] = [f for f in KEY_FIELDS if canonicalize(g["key"][f]) != canonicalize(out[0]["key"][f])]
        if not allow_mixed: raise NotComparable(out)
    return {"comparable": len(out) <= 1, "groups": out, "non_comparable": len(out) > 1}
