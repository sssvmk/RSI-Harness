"""Tool-selection monitoring and alerting (design §9.13). `tool.search` and dynamic exposure are cost optimizations, not a security boundary: ToolRuntime authorizes every call whatever was exposed.

Online, the harness observes selection errors it can see without labels: a tool that does not exist or is not allowed, arguments that fail validation, and a call the policy denied. Offline, a labelled benchmark says which tools
are acceptable for each task, so that wrong-tool, miss, unnecessary, unsafe and recovery rates can be computed without counting reasonable alternatives as errors. Alert rules are configuration; a sustained breach is one alert."""
from __future__ import annotations
from collections import deque

RULE = "selection.error_rate"
UNSAFE_RULE = "selection.unsafe_execution"
DEFAULTS = {"window": 200, "min_sample": 30, "warn": 0.05, "critical": 0.10}      # [proposed P80]
SELECTION_ERRORS = ("tool.unknown", "tool.invalid_arguments")


def rules(cfg: dict | None) -> dict:
    """Validate and complete `alerts.selection`. An invalid rule is refused at startup, not ignored."""
    r = {**DEFAULTS, **((cfg or {}).get("selection") or {})}
    if not (isinstance(r["window"], int) and r["window"] >= 1): raise ValueError("alerts.selection.window must be a positive integer")
    if not (isinstance(r["min_sample"], int) and 1 <= r["min_sample"] <= r["window"]): raise ValueError("alerts.selection.min_sample must be between 1 and the window size")
    for k in ("warn", "critical"):
        if isinstance(r[k], bool) or not isinstance(r[k], (int, float)) or not (0 < r[k] <= 1): raise ValueError(f"alerts.selection.{k} must be a number above 0 and at most 1")
    if r["critical"] < r["warn"]: raise ValueError("alerts.selection.critical must not be below warn")
    return r


class SelectionMonitor:
    """A rolling window of selection outcomes for one run. `observe` returns the alert transition, if any: a raise (with severity), an escalation, or a resolve. A breach that continues is silent."""
    def __init__(self, cfg: dict | None = None):
        self.r = rules({"selection": cfg or {}})
        self.window: deque = deque(maxlen=self.r["window"]); self.active: str | None = None

    @classmethod
    def from_events(cls, events: list[dict], cfg: dict | None = None) -> "SelectionMonitor":
        """Rebuild the window after a restart from what the log says happened. A call still in flight has no outcome yet and is left out."""
        m = cls(cfg); out: dict = {}; order: list = []
        for e in events:
            t, p = e["type"], e["payload"]
            if t == "tool.call.requested": order.append(p["tool_call_id"])
            elif t == "tool.call.failed" and (p.get("error") or {}).get("code") in SELECTION_ERRORS: out[p["tool_call_id"]] = True
            elif t == "policy.decided" and p.get("decision") == "deny": out[p["tool_call_id"]] = True
            elif t == "tool.call.started": out.setdefault(p["tool_call_id"], False)
        for cid in order:
            if cid in out: m.window.append(1 if out[cid] else 0)
        for a in active_alerts(events):
            if a["rule"] == RULE: m.active = a["severity"]
        return m

    def rate(self) -> float | None:
        return sum(self.window) / len(self.window) if self.window else None

    def observe(self, error: bool, context: dict | None = None) -> dict | None:
        self.window.append(1 if error else 0)
        if len(self.window) < self.r["min_sample"]: return None                  # too few decisions to say anything: one failed selection is not an alert
        rate = sum(self.window) / len(self.window)
        sev = "critical" if rate >= self.r["critical"] else "warning" if rate >= self.r["warn"] else None
        if sev == self.active: return None
        prev, self.active = self.active, sev
        if sev is not None and prev is not None and (sev == "warning" and prev == "critical"): return None      # a breach that eases but continues is still the same alert: no new event until it resolves or worsens
        if sev is None: return {"event": "alert.resolved", "payload": {"rule": RULE, "value": rate, "window": len(self.window)}}
        thr = self.r["critical"] if sev == "critical" else self.r["warn"]
        return {"event": "alert.raised", "payload": {"rule": RULE, "severity": sev, "window": len(self.window), "value": rate, "threshold": thr, "previous_severity": prev, "context": context or {}}}


def unsafe_execution_alert(tool: str, decision: str, context: dict | None = None) -> dict:
    """A prohibited or unauthorized selection that reached execution is an immediate alert, whatever the error rate. The payload names the tool and the decision, never the arguments."""
    return {"event": "alert.raised", "payload": {"rule": UNSAFE_RULE, "severity": "security", "window": 0, "value": 1, "threshold": 0, "context": {"tool": tool, "decision": decision, **(context or {})}}}


def selection_stats(events: list[dict]) -> dict:
    """Observed selection decisions and errors for a run, from its log."""
    decisions = errors = denied = 0; by_tool: dict = {}; req: dict = {}
    for e in events:
        t, p = e["type"], e["payload"]
        if t == "tool.call.requested": decisions += 1; req[p["tool_call_id"]] = p["tool"]
        elif t == "tool.call.failed" and (p.get("error") or {}).get("code") in SELECTION_ERRORS:
            errors += 1; k = req.get(p["tool_call_id"], p.get("tool", "?")); by_tool[k] = by_tool.get(k, 0) + 1
        elif t == "policy.decided" and p.get("decision") == "deny": denied += 1; errors += 1; k = req.get(p["tool_call_id"], "?"); by_tool[k] = by_tool.get(k, 0) + 1
    return {"decisions": decisions, "errors": errors, "policy_denials": denied, "error_rate": (errors / decisions if decisions else None), "errors_by_tool": dict(sorted(by_tool.items()))}


def active_alerts(events: list[dict]) -> list[dict]:
    """Alerts raised and not yet resolved, from the log."""
    live: dict = {}
    for e in events:
        if e["type"] == "alert.raised": live[(e["payload"]["rule"], str((e["payload"].get("context") or {}).get("tool", "")))] = {"run_id": e["run_id"], "seq": e["seq"], **e["payload"]}
        elif e["type"] == "alert.resolved": live = {k: v for k, v in live.items() if k[0] != e["payload"]["rule"]}
    return list(live.values())


def evaluate_selection(benchmark: dict, decisions: list[dict]) -> dict:
    """Offline selection quality against a labelled benchmark. benchmark: {tasks: [{id, acceptable: [tool], available: [tool]}]}.
    decisions: [{task, tool, model, selector_version, valid_args, contributed, unsafe}] in order. Rates are over evaluated selection decisions, reported overall and per model, selector version, tool and task."""
    tasks = {t["id"]: t for t in benchmark["tasks"]}; n = {k: 0 for k in ("decisions", "wrong_tool", "invalid_call", "unnecessary", "unsafe", "incorrect")}
    by: dict = {"model": {}, "selector_version": {}, "tool": {}, "task": {}}; per_task: dict = {}
    for d in decisions:
        t = tasks.get(d["task"]); 
        if t is None: continue
        wrong = d["tool"] not in t["acceptable"]; inv = not d.get("valid_args", True); unnec = (not wrong) and d.get("contributed") is False; uns = bool(d.get("unsafe"))
        n["decisions"] += 1; n["wrong_tool"] += wrong; n["invalid_call"] += inv; n["unnecessary"] += unnec; n["unsafe"] += uns; n["incorrect"] += (wrong or inv or uns)     # an incorrect selection is counted once, however many ways it was wrong
        per_task.setdefault(d["task"], []).append(wrong)
        for dim in ("model", "selector_version", "tool", "task"):
            b = by[dim].setdefault(d.get(dim, d["tool"]) if dim != "task" else d["task"], {"decisions": 0, "wrong_tool": 0, "invalid_call": 0}); b["decisions"] += 1; b["wrong_tool"] += wrong; b["invalid_call"] += inv
    miss = recovered = wrong_tasks = 0
    for tid, seq in per_task.items():                                  # seq[i] is True when the i-th selection for the task was wrong
        t = tasks[tid]; chosen_ok = any(not w for w in seq)
        if not chosen_ok and any(a in t.get("available", []) for a in t["acceptable"]): miss += 1                      # a suitable tool was available and never selected
        if any(seq):
            wrong_tasks += 1
            first_wrong = seq.index(True)
            if any(not w for w in seq[first_wrong + 1:]): recovered += 1                                               # a wrong selection was followed by an acceptable one
    rate = lambda x: (x / n["decisions"] if n["decisions"] else None)
    return {"decisions": n["decisions"], "wrong_tool_rate": rate(n["wrong_tool"]), "invalid_call_rate": rate(n["invalid_call"]), "unnecessary_call_rate": rate(n["unnecessary"]), "unsafe_selection_rate": rate(n["unsafe"]),
            "miss_rate": (miss / len(per_task) if per_task else None), "recovery_rate": (recovered / wrong_tasks if wrong_tasks else None),
            "error_rate": rate(n["incorrect"]), **{f"by_{k}": {kk: {**vv, "wrong_tool_rate": vv["wrong_tool"] / vv["decisions"]} for kk, vv in sorted(v.items(), key=lambda kv: str(kv[0]))} for k, v in by.items()}}
