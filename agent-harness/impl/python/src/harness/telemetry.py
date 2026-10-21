"""Operator telemetry (design §14.4, §15.2) and trajectory metrics (§13.11). Everything here is a pure function of a run's event log, so it needs no dashboard, collector, or background service, and the same events
always give the same numbers. Collection is declared, not inferred: a category the build does not collect is reported `unavailable`, never concluded from the absence of events."""
from __future__ import annotations
import re
from .selection import active_alerts, selection_stats

ANALYZER_VERSION = 1
ALERTS_COLLECTED = True                                   # the tool-selection monitor (§9.13) raises `alert.raised` and `alert.resolved`
CONTENT_KEYS = {"message", "message_ref", "args", "args_ref", "search_text", "result", "summary", "text", "instruction", "content", "note", "detail", "reason_text", "feedback"}
ERROR_TYPES = {"tool.call.failed", "model.failed", "definition.mismatch", "integrity.alert", "failure.classified", "failure.observed", "recovery.uncertain", "alert.raised"}
SECRET_RE = re.compile(r"(sk-[A-Za-z0-9_\-]{12,}|AKIA[0-9A-Z]{12,}|Bearer\s+[A-Za-z0-9._\-]{12,}|(?:password|secret|token|api[_-]?key)\s*[=:]\s*\S+)", re.I)


def redact(s: str) -> str:
    return SECRET_RE.sub("[REDACTED]", s)


def _pct(xs: list, q: float):
    """Nearest-rank percentile."""
    if not xs: return None
    xs = sorted(xs); return xs[max(0, -(-int(round(q * 1000)) * len(xs) // 1000) - 1)]


def _hist(xs: list) -> dict:
    if not xs: return {"count": 0, "min": None, "p50": None, "p95": None, "max": None, "mean": None}
    return {"count": len(xs), "min": min(xs), "p50": _pct(xs, 0.5), "p95": _pct(xs, 0.95), "max": max(xs), "mean": sum(xs) / len(xs)}


def _is_error(e: dict) -> bool:
    t, p = e["type"], e["payload"]
    if t == "stuck_loop.detected": return p.get("action") in ("escalate", "terminate")
    if t == "run.terminated": return p.get("reason") != "completed"
    if t == "alert.raised": return p.get("severity") in ("critical", "security")
    return t in ERROR_TYPES and t != "alert.raised"


def run_metrics(events: list[dict]) -> dict:
    """Counters and histograms for one run: turns, tool failures, model latency, tokens, cost, retries, approval events, budget terminations by dimension, stuck-loop terminations,
    and evaluation-integrity events."""
    c = {"turns": 0, "tool_calls": 0, "tool_failures": 0, "model_calls": 0, "model_failures": 0, "tool_retries": 0, "model_retries": 0, "input_tokens": 0, "output_tokens": 0, "policy_denials": 0,
         "approvals": {"requested": 0, "granted": 0, "denied": 0, "expired": 0, "automatic": 0}, "budget_terminations": {}, "stuck_loop": {"warnings": 0, "escalations": 0, "terminations": 0},
         "eval_integrity_events": {"total": 0, "by_code": {}}, "log_integrity_alerts": 0, "operator_actions": 0, "alerts": 0}
    lat, cost = [], None
    for e in events:
        t, p = e["type"], e["payload"]
        if t == "turn.completed": c["turns"] += 1
        elif t == "tool.call.requested": c["tool_calls"] += 1
        elif t == "tool.call.failed": c["tool_failures"] += 1
        elif t == "tool.call.started" and p.get("attempt", 1) > 1: c["tool_retries"] += 1
        elif t == "model.requested": c["model_calls"] += 1
        elif t == "model.failed": c["model_failures"] += 1
        elif t == "model.responded":
            u = p.get("usage") or {}; c["input_tokens"] += u.get("input_tokens", 0); c["output_tokens"] += u.get("output_tokens", 0); c["model_retries"] += max(0, p.get("attempt", 1) - 1)
            if p.get("latency_ms") is not None: lat.append(p["latency_ms"])
        elif t == "policy.decided" and p.get("decision") == "deny": c["policy_denials"] += 1
        elif t == "approval.requested": c["approvals"]["requested"] += 1
        elif t == "approval.decided":
            st = p.get("status"); c["approvals"]["granted" if st == "granted" else "denied" if st == "denied" else "expired" if st == "expired" else "granted"] += 1
            if str(p.get("decided_by", "")).startswith("standing:"): c["approvals"]["automatic"] += 1
        elif t == "run.terminated" and str(p.get("reason", "")).startswith("budget_exhausted:"): d = p["reason"].split(":", 1)[1]; c["budget_terminations"][d] = c["budget_terminations"].get(d, 0) + 1
        elif t == "stuck_loop.detected": c["stuck_loop"][{"guidance": "warnings", "escalate": "escalations", "terminate": "terminations"}.get(p.get("action"), "warnings")] += 1
        elif t == "failure.observed" and p.get("class") == "eval_integrity": c["eval_integrity_events"]["total"] += 1; c["eval_integrity_events"]["by_code"][p["code"]] = c["eval_integrity_events"]["by_code"].get(p["code"], 0) + 1
        elif t == "integrity.alert": c["log_integrity_alerts"] += 1
        elif t == "operator.action": c["operator_actions"] += 1
        elif t == "alert.raised": c["alerts"] += 1
        elif t == "budget.updated" and p.get("dimension") == "cost" and p.get("budget_set", "agent") == "agent": cost = p["used"]
    c["retries"] = {"tool": c["tool_retries"], "model": c["model_retries"], "total": c["tool_retries"] + c["model_retries"]}
    c["tokens"] = {"input": c["input_tokens"], "output": c["output_tokens"], "total": c["input_tokens"] + c["output_tokens"]}
    c["cost"] = cost                                          # null when no cost dimension was metered: unavailable, not zero
    c["selection_error_rate"] = selection_stats(events)["error_rate"]                  # observed: unknown tool, invalid arguments, policy denial; null when nothing was selected
    return {"counters": c, "histograms": {"model_latency_ms": _hist(lat)}}


def trajectory_metrics(events: list[dict]) -> dict:
    """The deterministic trajectory metrics of a run (design §13.11): steps, tool errors, retries, budget use, policy denials, approvals, safety events, and stuck loops. A model-based assessment is optional and labelled."""
    m = run_metrics(events)["counters"]; used: dict = {}
    for e in events:
        if e["type"] == "budget.updated" and e["payload"].get("budget_set", "agent") == "agent": used[e["payload"]["dimension"]] = {"used": e["payload"]["used"], "limit": e["payload"]["limit"]}
    safety = {"policy_denials": m["policy_denials"], "quarantined": sum(1 for e in events if e["type"] == "memory.quarantined"), "integrity_alerts": m["log_integrity_alerts"], "definition_mismatches": sum(1 for e in events if e["type"] == "definition.mismatch")}
    safety["total"] = sum(safety.values())
    return {"analyzer_version": ANALYZER_VERSION, "steps": m["turns"], "tool_calls": m["tool_calls"], "tool_errors": m["tool_failures"], "retries": m["retries"]["total"], "budget_use": dict(sorted(used.items())),
            "policy_denials": m["policy_denials"], "approvals": m["approvals"]["requested"], "safety_events": safety, "stuck_loops": m["stuck_loop"]["warnings"] + m["stuck_loop"]["escalations"] + m["stuck_loop"]["terminations"]}


def usage(events: list[dict]) -> dict:
    """Tokens and cost, in total and by model. Cost is reported only when a cost dimension was metered or the run recorded an estimate."""
    by, ref, cost = {}, None, None
    for e in events:
        t, p = e["type"], e["payload"]
        if t == "model.requested": ref = p.get("model_ref")
        elif t == "model.responded":
            u = p.get("usage") or {}; b = by.setdefault(ref, {"calls": 0, "input_tokens": 0, "output_tokens": 0}); b["calls"] += 1; b["input_tokens"] += u.get("input_tokens", 0); b["output_tokens"] += u.get("output_tokens", 0)
        elif t == "run.finished":
            est = ((p.get("fields") or {}).get("usage") or {}).get("value") or {}
            if est.get("estimated_cost") is not None: cost = {"estimated_cost": est["estimated_cost"], "currency": est.get("currency")}
        elif t == "budget.updated" and p.get("dimension") == "cost" and cost is None: pass
    tot = {"input_tokens": sum(b["input_tokens"] for b in by.values()), "output_tokens": sum(b["output_tokens"] for b in by.values())}
    return {"tokens": {**tot, "total": tot["input_tokens"] + tot["output_tokens"]}, "by_model": dict(sorted(by.items(), key=lambda kv: str(kv[0]))), "cost": cost}


def categories(events: list[dict], run_id: str) -> dict:
    """`inspect --telemetry`: each category is `available` or `unavailable`. Availability follows what this build collects and what the run's own manifest says, never the mere absence of events."""
    created = next((e for e in events if e["type"] == "run.created"), None); man = ((created or {}).get("payload") or {}).get("manifest")
    fin = next((e for e in reversed(events) if e["type"] == "run.finished"), None); m = run_metrics(events)["counters"]
    sc = [e for e in events if e["type"] == "run.state_changed"]; term = next((e["payload"] for e in reversed(events) if e["type"] == "run.terminated"), None)
    av = lambda value: {"status": "available", "value": value}; un = lambda why: {"status": "unavailable", "reason": why}
    cost_est = (((fin or {}).get("payload") or {}).get("fields") or {}).get("usage", {}).get("value") or {}
    errs = {}
    for e in events:
        if e["type"] == "tool.call.failed": k = (e["payload"].get("error") or {}).get("code", "unknown"); errs[k] = errs.get(k, 0) + 1
    models = (man or {}).get("fields", {}).get("models", {})
    return {"run_id": run_id, "categories": {
        "status": av({"state": sc[-1]["payload"]["to"] if sc else None, "terminal_reason": (term or {}).get("reason")}),
        "model_and_version": av(models["value"]) if models.get("status") == "recorded" else un("this run has no provenance manifest"),
        "tool_calls": av({"requested": m["tool_calls"], "failed": m["tool_failures"], "retries": m["retries"]["tool"]}),
        "errors_and_termination": av({"tool_errors_by_code": dict(sorted(errs.items())), "model_failures": m["model_failures"], "termination": term}),
        "usage_and_cost": av({"tokens": m["tokens"], "cost": cost_est.get("estimated_cost")}) if cost_est.get("estimated_cost") is not None or m["cost"] is not None else {**av({"tokens": m["tokens"], "cost": None}), "note": "cost is unavailable: no price or cost budget was configured"},
        "interventions": av({"approvals": m["approvals"], "operator_actions": m["operator_actions"]}),
        "alerts": av({"raised": m["alerts"], "active": [{"rule": a["rule"], "severity": a["severity"], "seq": a["seq"]} for a in active_alerts(events)]}) if ALERTS_COLLECTED else un("alerting is not implemented in this build")}}


def event_rows(events: list[dict], run_id: str, role: str = "operator", type_filter: str | None = None, errors_only: bool = False) -> list[dict]:
    """Events for the operator. An `operator` sees metadata and the codes and counts that explain a run, with message and tool content gated; a `reviewer` or `admin` sees content. Everything is redacted, and every row carries its run id."""
    rows = []
    for e in events:
        if type_filter and not (e["type"] == type_filter or e["type"].startswith(type_filter.rstrip("*"))) : continue
        if errors_only and not _is_error(e): continue
        rows.append({"run_id": run_id, "seq": e["seq"], "ts": e["ts"], "type": e["type"], "actor": e["actor"]["kind"], "payload": _view(e["payload"], role)})
    return rows


def _view(v, role: str):
    if isinstance(v, dict): return {k: ("[gated: reviewer or admin role required]" if k in CONTENT_KEYS and role == "operator" else _view(x, role)) for k, x in v.items()}
    if isinstance(v, list): return [_view(x, role) for x in v]
    return redact(v) if isinstance(v, str) else v
