"""RecoveryManager (design §5.9): verify, rebuild, reconcile in-flight operations, never reset counters."""
from __future__ import annotations
from dataclasses import dataclass, field
from .clock import parse_ts
from .eventlog.projections import ACTIVE_STATES, NAMES, TERMINAL_STATES
from .gateway.canonical import tool_calls_of
from .tools.runtime import ToolContext, ToolResult


@dataclass
class RecoveryPlan:
    run_id: str
    ok: bool = True
    halted: str | None = None            # reason if the run cannot continue automatically
    pending_calls: list[dict] = field(default_factory=list)
    reissue_model: bool = False
    rescore: list[str] = field(default_factory=list)
    notes: list[str] = field(default_factory=list)
    settled_reservations: list = field(default_factory=list)   # (reservation_id, how) for each budget reservation a crash left open


class RecoveryManager:
    def __init__(self, h):
        self.h = h

    async def recover(self, run_id: str, downtime_cause: str = "harness_failure", full_verify: bool = False) -> RecoveryPlan:
        h = self.h
        run = h.rm.get(run_id)
        plan = RecoveryPlan(run_id)
        rs0 = await run.proj("run_state")
        if rs0["state"] in TERMINAL_STATES:
            plan.halted = "terminal"
            return plan
        # 1. verify the tail of the chain
        n = None if full_verify else h.config["recovery"]["verify_last_n"]
        v = h.store.verify(run_id, n)
        if not v["ok"]:
            await run.emit("integrity.alert", {"reason": v["reason"], "bad_seq": v["bad_seq"]})
            await run.transition("FAILED_INTEGRITY", "integrity_failure")
            plan.ok, plan.halted = False, "integrity_failure"
            return plan
        # 2. rebuild projections from the log
        for name in NAMES:
            await h.svc.projections.rebuild(run_id, name)
        rs = await run.proj("run_state")
        # 4. clock: downtime accounting (counters themselves are folds over events and never reset)
        last = h.store.get_event(run_id, h.store.last_seq(run_id))
        downtime = max(0, h.clock.now_ms() - parse_ts(last["ts"]))
        allowance = h.config["recovery"]["max_excluded_downtime_s"] * 1000 - rs["excluded_ms"]
        excluded = min(downtime, max(0, allowance)) if (downtime_cause == "harness_failure" and rs["state"] in ACTIVE_STATES) else 0
        # 3. reconcile in-flight operations
        evs = h.store.events(run_id)
        last_req = max((e["seq"] for e in evs if e["type"] == "model.requested"), default=0)
        last_resp = max((e["seq"] for e in evs if e["type"] in ("model.responded", "model.failed")), default=0)
        plan.reissue_model = last_req > last_resp
        plan.settled_reservations = await self._settle_reservations(run, evs)
        await self._sandboxes(run, plan)
        await h.approvals.rearm(run)
        conv = await run.proj("conversation")
        tc = await run.proj("tool_calls")
        for e in conv["entries"]:
            if e["type"] != "model.responded":
                continue
            msg = e["message"] if e["message"] is not None else __import__("json").loads(h.blobs.get(e["message_ref"]))
            for call in tool_calls_of(msg):
                st = tc.get(call["id"], {}).get("status")
                if st in ("completed", "failed"):
                    continue
                plan.pending_calls.append(call)
        await run.emit("run.recovered", {"resumed_from_seq": h.store.last_seq(run_id), "downtime_ms": downtime, "downtime_cause": downtime_cause,
                                         "excluded_ms": excluded})
        # a started call on a resource the harness can read is resolved from that resource's authoritative state, not from the log alone and not by blindly repeating it (§5.13)
        for call in list(plan.pending_calls):
            info, tool = tc.get(call["id"]), h.registry.get(call["name"])
            if not (info and info["status"] == "started" and tool and tool.manifest.get("world")): continue
            loop = h.loops.get(run_id)
            if loop is None: continue
            action = h.runtime.world_action(run, call["id"], call["name"], call["arguments"], tool.manifest)
            est = await h.runtime.gate.establish_outcome(lambda t, p, cid=call["id"]: run.emit(t, p, correlation_id=cid), action, None, loop._tctx())
            if est["outcome"] == "applied":
                await h.runtime._complete(run, call["id"], call["name"], ToolResult("(verified from the resource's state after recovery: the action had already taken effect, so it was not repeated)"), "tool_trusted", None)
                plan.pending_calls.remove(call); plan.notes.append(f"reconciled {call['name']} from state"); plan.reconciled_from_state = getattr(plan, "reconciled_from_state", 0) + 1
            elif est["outcome"] == "unknown":
                await run.emit("recovery.uncertain", {"tool_call_id": call["id"], "tool": call["name"], "reason": "the resource's state could not be read, so it is not known whether the action took effect"})
                if (await run.state()) in ("RUNNING", "AWAITING_APPROVAL", "SCORING"): await run.transition("AWAITING_HUMAN", "recovery.uncertain")
                plan.halted = "recovery_uncertain"; return plan
        # started non_idempotent calls: reconcile hook, else uncertain -> AWAITING_HUMAN, never retried
        for call in list(plan.pending_calls):
            info = tc.get(call["id"])
            if not info or info["status"] != "started" or info["idempotency"] != "non_idempotent":
                continue
            tool = h.registry.get(call["name"])
            outcome = None
            if tool and tool.reconcile:
                loop = h.loops.get(run_id)
                if loop is not None:
                    outcome = await tool.reconcile(call["arguments"], loop._tctx())
            if isinstance(outcome, str):
                outcome = ToolResult(outcome)
            if isinstance(outcome, ToolResult):
                await h.runtime._complete(run, call["id"], call["name"], outcome, "tool_trusted", None)
                plan.pending_calls.remove(call)
                plan.notes.append(f"reconciled {call['name']}")
            else:
                await run.emit("recovery.uncertain", {"tool_call_id": call["id"], "tool": call["name"],
                                                      "reason": "non-idempotent call was started and its outcome is unknown"})
                if (await run.state()) in ("RUNNING", "AWAITING_APPROVAL", "SCORING"):
                    await run.transition("AWAITING_HUMAN", "recovery.uncertain")
                plan.halted = "recovery_uncertain"
                return plan
        return plan

    async def _settle_reservations(self, run, evs: list) -> list:
        """Reservations left open by a crash (§7.4.4). A call that was sent and not answered may have been charged, so its estimate is committed and flagged; one that was answered
        but not committed is committed at its actual usage; one that never started is released."""
        from .budgets import BudgetController, cost_of
        bc, h, out = BudgetController(run), self.h, []
        for rid, r in list((await run.proj("budgets"))["reservations"].items()):
            if r["set"] != "agent": continue
            rseq = next((e["seq"] for e in evs if e["type"] == "budget.reserved" and e["payload"]["reservation_id"] == rid), 0)
            if "submissions" in r["deltas"]:                       # the submission was recorded before the job was launched, so the count must survive the crash
                made = any(e["type"] == "submission.created" and e["seq"] > rseq for e in evs)
                if made: await bc.commit(rid, {"submissions": 1}); out.append((rid, "submission_committed"))
                else: await bc.release(rid, "crash before the submission"); out.append((rid, "released"))
                continue
            req = next((e for e in evs if e["type"] == "model.requested" and e["seq"] > rseq), None)
            resp = next((e for e in evs if e["type"] in ("model.responded", "model.failed") and req and e["seq"] > req["seq"]), None)
            if req is None: await bc.release(rid, "crash before the call"); out.append((rid, "released"))
            elif resp is None: await bc.commit(rid, {k: v for k, v in r["deltas"].items()}, unreconciled=True); out.append((rid, "charged_estimate"))
            elif resp["type"] == "model.responded":
                u = resp["payload"]["usage"]; price = (h.models.get(req["payload"]["model_ref"]) or {}).get("price")
                await bc.commit(rid, {"turns": 1, "tokens": u["input_tokens"] + u["output_tokens"], "cost": cost_of(u, price)}); out.append((rid, "committed_actual"))
            else: await bc.commit(rid, {"turns": 1}); out.append((rid, "turn_only"))
        return out

    async def _sandboxes(self, run, plan: RecoveryPlan) -> None:
        """Reattach by label if the sandbox still exists, otherwise recreate from the last snapshot."""
        h = self.h
        for sid, info in (await run.proj("sandboxes")).items():
            if sid in h.sandboxes.handles:
                continue
            found = h.backend.find_by_labels(run.run_id, info["role"])
            if found is not None and found.sandbox_id == sid:
                h.sandboxes.handles[sid] = found       # reattached by label
                plan.notes.append(f"reattached {info['role']} sandbox")
                continue
            await run.emit("sandbox.destroyed", {"sandbox_id": sid, "reason": "lost in crash"})
            for gid, g in (await run.proj("grants_active")).items():
                if (g.get("spec") or {}).get("sandbox_id") == sid:
                    await run.emit("grant.revoked", {"grant_id": gid, "kind": g["kind"]})
            if info["role"] == "work":
                new = await h.sandboxes.create(run, "work", h.config.get("egress", {}).get("grants"))
                row = h.store.conn().execute("SELECT manifest_hash FROM snapshots WHERE run_id=? ORDER BY taken_at_seq DESC LIMIT 1", (run.run_id,)).fetchone()
                if row:
                    from .sandbox.snapshots import restore_snapshot
                    restore_snapshot(row["manifest_hash"], h.blobs, new)
                    plan.notes.append("work sandbox recreated from last snapshot")
                else:
                    plan.notes.append("work sandbox recreated empty (no snapshot existed)")
