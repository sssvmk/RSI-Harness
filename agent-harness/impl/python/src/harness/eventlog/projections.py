"""Projections (design §5.6): pure, versioned folds over events, cached with as_of_seq."""
from __future__ import annotations
import copy
from typing import Any
from ..clock import parse_ts

TRUST_RANK = {"untrusted": 0, "tool_trusted": 1, "user": 2, "developer": 3, "system": 4}
ACTIVE_STATES = {"RUNNING", "AWAITING_APPROVAL", "SCORING", "COMPLETING"}
TERMINAL_STATES = {"COMPLETED", "TERMINATED", "FAILED_INTEGRITY"}
VERSIONS = {"run_state": 1, "budgets": 2, "reconcile": 1, "conversation": 1, "approvals_open": 1, "tool_calls": 1,
            "submissions": 1, "grants_active": 1, "sandboxes": 1, "ephemeral": 1, "working_state": 1, "loop": 1}
NAMES = list(VERSIONS)


def min_trust(trusts: list[str]) -> str:
    return min(trusts, key=lambda t: TRUST_RANK.get(t, 0)) if trusts else "user"


def initial(name: str) -> Any:
    return {
        "run_state": {"state": "CREATED", "terminal_reason": None, "owner": None, "profile": None, "active_ms": 0,
                      "active_since_ms": None, "excluded_ms": 0, "entered_completing_seq": None, "verified_seq": None,
                      "verification_failures": 0},
        "budgets": {"agent": {}, "evaluator": {}, "reservations": {}, "allocations": {}, "unreconciled": {}},
        "reconcile": {"subjects": {}},
        "conversation": {"entries": []},
        "approvals_open": {},
        "tool_calls": {},
        "submissions": {},
        "grants_active": {},
        "sandboxes": {},
        "ephemeral": {"guidance": []},
        "working_state": {},
        "loop": {"turn": 0, "idle_streak": 0, "last_signature": None, "repeat_count": 0, "stuck_warned": False, "exposed_tools": [], "completion_pending": None},
    }[name]


def _fold_run_state(s, ev):
    t, p, ts = ev["type"], ev["payload"], parse_ts(ev["ts"])
    if t == "run.created":
        s["owner"], s["profile"] = p["owner"], p["profile"]
    elif t == "run.state_changed":
        was_active = s["state"] in ACTIVE_STATES
        now_active = p["to"] in ACTIVE_STATES
        if was_active and not now_active and s["active_since_ms"] is not None:
            s["active_ms"] += ts - s["active_since_ms"]
            s["active_since_ms"] = None
        elif not was_active and now_active:
            s["active_since_ms"] = ts
        s["state"] = p["to"]
        if p["to"] == "COMPLETING":
            s["entered_completing_seq"] = ev["seq"]
        if p["to"] in TERMINAL_STATES and p.get("reason"):
            s["terminal_reason"] = s["terminal_reason"] or p["reason"]
    elif t == "run.terminated":
        s["terminal_reason"] = p["reason"]
    elif t == "run.recovered":
        s["excluded_ms"] += p.get("excluded_ms", 0)
    elif t == "verification.completed":
        if p.get("passed"):
            s["verified_seq"] = ev["seq"]
        else:
            s["verification_failures"] += 1
    return s


def active_time_ms(rs: dict, now_ms: int) -> int:
    total = rs["active_ms"] + (now_ms - rs["active_since_ms"] if rs["active_since_ms"] is not None else 0)
    return max(0, total - rs["excluded_ms"])


def _fold_budgets(s, ev):
    """Budget state (design §7.4): per dimension `used` (set by absolute budget.updated events), `reserved` (open reservations) and `unreconciled` (charged at an estimate, awaiting actual usage);
    plus the open reservations, the allocations (guaranteed capacity for an agent or task), and the unreconciled charges."""
    t, p = ev["type"], ev["payload"]
    if t == "run.created":
        for bset, key in (("agent", "budgets"), ("evaluator", "evaluator_budgets")):
            for dim, cfg in (p.get(key) or {}).items():
                lim = cfg["limit"]
                s[bset][dim] = {"limit": lim, "used": 0, "reserved": 0, "unreconciled": 0, "warn_at": cfg.get("warn_at", lim * 0.8), "warned": False, "exhausted": False}
    elif t in ("budget.updated", "budget.warning", "budget.exhausted"):
        d = s[p.get("budget_set", "agent")].get(p["dimension"])
        if d is None:
            return s
        if t == "budget.updated":
            d["used"] = p["used"]
        elif t == "budget.warning":
            d["warned"] = True
        else:
            d["exhausted"] = True
    elif t == "budget.reserved":
        bset, agent = p.get("budget_set", "agent"), p.get("agent_id")
        s["reservations"][p["reservation_id"]] = {"set": bset, "deltas": dict(p["deltas"]), "agent_id": agent}
        for dim, amt in p["deltas"].items():
            if dim in s[bset]: s[bset][dim]["reserved"] += amt
            if agent and agent in s["allocations"] and dim in s["allocations"][agent]["dims"]: s["allocations"][agent]["dims"][dim]["reserved"] += amt
    elif t in ("budget.committed", "budget.released"):
        r = s["reservations"].pop(p["reservation_id"], None)
        if r is not None:
            bset, agent = r["set"], r["agent_id"]
            for dim, amt in r["deltas"].items():
                if dim in s[bset]: s[bset][dim]["reserved"] -= amt
                if agent and agent in s["allocations"] and dim in s["allocations"][agent]["dims"]: s["allocations"][agent]["dims"][dim]["reserved"] -= amt
            if t == "budget.committed":
                for dim, amt in (p.get("actual") or {}).items():
                    if agent and agent in s["allocations"] and dim in s["allocations"][agent]["dims"]: s["allocations"][agent]["dims"][dim]["used"] += amt
                if p.get("unreconciled"):
                    est = {dim: amt for dim, amt in (p.get("actual") or {}).items() if dim in s[bset]}
                    s["unreconciled"][p["reservation_id"]] = {"set": bset, "deltas": est}
                    for dim, amt in est.items(): s[bset][dim]["unreconciled"] += amt
        if t == "budget.committed" and p.get("reconciles"):
            u = s["unreconciled"].pop(p["reservation_id"], None)
            if u:
                for dim, amt in u["deltas"].items(): s[u["set"]][dim]["unreconciled"] -= amt
    elif t == "budget.allocated":
        s["allocations"][p["agent_id"]] = {"set": p.get("budget_set", "agent"), "ended": False, "dims": {d: {"limit": lim, "used": 0, "reserved": 0} for d, lim in p["limits"].items()}}
    elif t == "budget.allocation_ended":
        if p["agent_id"] in s["allocations"]: s["allocations"][p["agent_id"]]["ended"] = True
    return s


def _fold_reconcile(s, ev):
    """Reconciliation state per subject (design §5.13), from `reconcile.state_changed`. Explicit, logged transitions; `unknown` never becomes `consistent` without authoritative evidence."""
    from ..reconcile import apply_event
    if ev["type"] == "reconcile.state_changed":
        p = ev["payload"]; prev = s["subjects"].get(p["subject"], {}).get("state")
        s["subjects"][p["subject"]] = {"state": apply_event(prev, p["event"], bool(p.get("authoritative"))), "action_id": p.get("action_id"), "seq": ev["seq"]}
    return s


def _msg_entry(ev, default_trust):
    p = ev["payload"]
    msg = p.get("message")
    call_ids = p.get("call_ids")
    if call_ids is None:
        call_ids = [x["id"] for x in (msg or {}).get("parts", []) if x.get("type") == "tool_call"]
    return {"seq": ev["seq"], "type": ev["type"], "trust": p.get("trust", default_trust), "message": msg,
            "message_ref": p.get("message_ref"), "cleared": False, "call_ids": call_ids,
            "call_id": p.get("tool_call_id"), "tool": p.get("tool"), "id_map": p.get("id_map", {})}


def _fold_conversation(s, ev):
    t, p = ev["type"], ev["payload"]
    es = s["entries"]
    if t == "user.message":
        es.append(_msg_entry(ev, "user"))
    elif t == "model.responded":
        es.append(_msg_entry(ev, "tool_trusted"))
    elif t in ("tool.call.completed", "tool.call.failed"):
        if "message" in p or "message_ref" in p:
            es.append(_msg_entry(ev, "untrusted"))
    elif t == "context.hydrated":
        if "message" in p:
            e = _msg_entry(ev, "untrusted"); e["type"] = "recalled"; e["hit_seqs"] = p.get("hit_seqs", [])
            es.append(e)
    elif t == "context.compacted":
        fr, to = p["from_seq"], p["to_seq"]
        removed = [e for e in es if fr <= e["seq"] <= to]
        if removed:
            idx = es.index(removed[0])
            summ = {"seq": ev["seq"], "type": "summary", "trust": min_trust([e["trust"] for e in removed]),
                    "summary": p.get("summary"), "summary_ref": p.get("summary_ref"), "from_seq": fr, "to_seq": to,
                    "cleared": False, "call_ids": [], "call_id": None, "tool": None, "message": None, "message_ref": None, "id_map": {}}
            # keep provider id handles of removed assistant entries so later requests can still map ids
            for e in removed:
                summ["id_map"].update(e.get("id_map") or {})
            es[:] = [e for e in es if not (fr <= e["seq"] <= to)]
            es.insert(idx, summ)
        cl = set(p.get("cleared_seqs", []))
        for e in es:
            if e["seq"] in cl:
                e["cleared"] = True
    return s


def _fold_approvals(s, ev):
    t, p = ev["type"], ev["payload"]
    if t == "approval.requested":
        s[p["approval_id"]] = {"tool_call_id": p.get("tool_call_id"), "scope": p.get("scope"), "tool": p.get("tool"),
                               "rule_id": p.get("rule_id"), "seq": ev["seq"], "expires_at": p.get("expires_at")}
    elif t == "approval.decided":
        s.pop(p["approval_id"], None)
    return s


def _fold_tool_calls(s, ev):
    t, p = ev["type"], ev["payload"]
    cid = p.get("tool_call_id")
    if t == "tool.call.requested":
        s[cid] = {"tool": p["tool"], "status": "requested", "seq": ev["seq"], "effect_class": p.get("effect_class"),
                  "idempotency": p.get("idempotency"), "attempt": 0, "args": p.get("args"), "args_ref": p.get("args_ref")}
    elif t == "tool.call.started" and cid in s:
        s[cid]["status"], s[cid]["attempt"], s[cid]["started_seq"] = "started", p.get("attempt", 1), ev["seq"]
    elif t == "tool.call.completed" and cid in s:
        s[cid]["status"] = "completed"
    elif t == "tool.call.failed" and cid in s:
        s[cid]["status"] = "failed"
    return s


def _fold_submissions(s, ev):
    t, p = ev["type"], ev["payload"]
    sid = p.get("submission_id")
    if t == "submission.created":
        s[sid] = {"seq": len(s) + 1, "created_seq": ev["seq"], "snapshot_id": p.get("snapshot_id"), "status": "created",
                  "valid": None, "primary": None, "report_ref": None}
    elif t == "snapshot.created" and sid in s:
        s[sid]["snapshot_id"], s[sid]["status"] = p["snapshot_id"], "snapshotted"
    elif t == "judge.started" and sid in s:
        s[sid]["status"] = "judging"
    elif t == "judge.completed" and sid in s:
        s[sid].update({"status": "scored", "valid": p.get("valid"), "primary": p.get("primary"), "report_ref": p.get("score_report_ref")})
    elif t == "feedback.delivered" and sid in s:
        s[sid]["status"] = "feedback"
    return s


def _fold_grants(s, ev):
    t, p = ev["type"], ev["payload"]
    if t == "grant.issued":
        s[p["grant_id"]] = {"kind": p["kind"], "spec": p.get("spec"), "issued_seq": ev["seq"]}
    elif t == "grant.revoked":
        s.pop(p["grant_id"], None)
    return s


def _fold_sandboxes(s, ev):
    t, p = ev["type"], ev["payload"]
    if t == "sandbox.created":
        s[p["sandbox_id"]] = {"role": p.get("role"), "backend": p.get("backend"), "tier": p.get("tier"), "seq": ev["seq"]}
    elif t == "sandbox.destroyed":
        s.pop(p["sandbox_id"], None)
    return s


def _fold_ephemeral(s, ev):
    t, p = ev["type"], ev["payload"]
    if t == "guidance.injected":
        s["guidance"].append({"kind": p.get("kind"), "text": p.get("text"), "seq": ev["seq"]})
    elif t == "model.requested":
        s["guidance"] = []
    return s


def _fold_working_state(s, ev):
    if ev["type"] == "state.written":
        p = ev["payload"]
        s[p["name"]] = {"hash": p["content_hash"], "seq": ev["seq"]}
    return s


def _fold_loop(s, ev):
    t, p = ev["type"], ev["payload"]
    if t == "turn.completed":
        s["turn"] = p["turn"]
        sig = p.get("signature")
        if p.get("idle"):
            s["idle_streak"] += 1
        else:
            s["idle_streak"] = 0
            if sig is not None:
                if sig == s["last_signature"]:
                    s["repeat_count"] += 1
                else:
                    s["last_signature"], s["repeat_count"], s["stuck_warned"] = sig, 1, False
    elif t == "stuck_loop.detected" and p.get("action") == "guidance":
        s["stuck_warned"] = True
    elif t == "completion.proposed":
        s["completion_pending"] = p["summary"]
    elif t == "verification.completed":
        s["completion_pending"] = None
    elif t == "tools.exposed":
        for n in p["names"]:
            if n not in s["exposed_tools"]:
                s["exposed_tools"].append(n)
    return s


FOLDS = {"run_state": _fold_run_state, "budgets": _fold_budgets, "reconcile": _fold_reconcile, "conversation": _fold_conversation,
         "approvals_open": _fold_approvals, "tool_calls": _fold_tool_calls, "submissions": _fold_submissions,
         "grants_active": _fold_grants, "sandboxes": _fold_sandboxes, "ephemeral": _fold_ephemeral,
         "working_state": _fold_working_state, "loop": _fold_loop}


def fold(name: str, state: Any, ev: dict) -> Any:
    return FOLDS[name](state, ev)


def fold_all(name: str, events: list[dict], state: Any = None) -> Any:
    state = copy.deepcopy(initial(name)) if state is None else state
    for ev in events:
        state = fold(name, state, ev)
    return state


class ProjectionManager:
    """Loads a cached projection, folds forward from as_of_seq, and refreshes the cache."""

    def __init__(self, store, writer):
        self.store, self.writer = store, writer

    async def get(self, run_id: str, name: str, persist: bool = True) -> Any:
        cached = self.store.get_projection(run_id, name)
        state, as_of = None, 0
        if cached and cached[1].get("version") == VERSIONS[name]:
            as_of, state = cached[0], cached[1]["state"]
        evs = self.store.events(run_id, after_seq=as_of)
        if as_of > self.store.last_seq(run_id):  # cache ahead of log: discard
            state, as_of, evs = None, 0, self.store.events(run_id)
        state = fold_all(name, evs, state)
        if evs and persist:
            await self.writer.put_projection(run_id, name, evs[-1]["seq"], {"version": VERSIONS[name], "state": state})
        return state

    async def rebuild(self, run_id: str, name: str) -> Any:
        state = fold_all(name, self.store.events(run_id))
        last = self.store.last_seq(run_id)
        await self.writer.put_projection(run_id, name, last, {"version": VERSIONS[name], "state": state})
        return state

    def get_sync(self, run_id: str, name: str) -> Any:
        return fold_all(name, self.store.events(run_id))
