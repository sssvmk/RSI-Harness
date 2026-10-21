/** RecoveryManager (design §5.9): verify, rebuild, reconcile in-flight operations, never reset counters. */
import { BudgetController, costOf } from "./budgets.js";
import { NAMES, ACTIVE_STATES, TERMINAL_STATES } from "./projections.js";
import { parseTs } from "./util.js";
import { ToolResult } from "./runtime.js";
import { restoreSnapshot } from "./sandbox.js";
export interface RecoveryPlan { run_id: string; ok: boolean; halted: string | null; pending_calls: any[]; reissue_model: boolean; rescore: string[]; notes: string[]; settled_reservations?: [string, string][] }
export class RecoveryManager {
  constructor(public h: any) {}
  /** Reservations left open by a crash (§7.4.4). A call that was sent and not answered may have been charged, so its estimate is committed and flagged; one that was answered but not committed
   *  is committed at its actual usage; one that never started is released. A submission reservation is committed if its submission exists. */
  async settleReservations(run: any, evs: any[]): Promise<[string, string][]> {
    const bc = new BudgetController(run), h = this.h, out: [string, string][] = [];
    for (const [rid, r] of Object.entries<any>((await run.proj("budgets")).reservations)) {
      if (r.set !== "agent") continue;
      const rseq = evs.find((e: any) => e.type === "budget.reserved" && e.payload.reservation_id === rid)?.seq ?? 0;
      if ("submissions" in r.deltas) {
        if (evs.some((e: any) => e.type === "submission.created" && e.seq > rseq)) { await bc.commit(rid, { submissions: 1 }); out.push([rid, "submission_committed"]); }
        else { await bc.release(rid, "crash before the submission"); out.push([rid, "released"]); }
        continue;
      }
      const req = evs.find((e: any) => e.type === "model.requested" && e.seq > rseq), resp = req ? evs.find((e: any) => (e.type === "model.responded" || e.type === "model.failed") && e.seq > req.seq) : undefined;
      if (!req) { await bc.release(rid, "crash before the call"); out.push([rid, "released"]); }
      else if (!resp) { await bc.commit(rid, { ...r.deltas }, { unreconciled: true }); out.push([rid, "charged_estimate"]); }
      else if (resp.type === "model.responded") { const u = resp.payload.usage, price = h.models?.[req.payload.model_ref]?.price; await bc.commit(rid, { turns: 1, tokens: u.input_tokens + u.output_tokens, cost: costOf(u, price) }); out.push([rid, "committed_actual"]); }
      else { await bc.commit(rid, { turns: 1 }); out.push([rid, "turn_only"]); }
    }
    return out;
  }
  async recover(runId: string, downtimeCause = "harness_failure", fullVerify = false): Promise<RecoveryPlan> {
    const h = this.h, run = h.rm.get(runId), plan: RecoveryPlan = { run_id: runId, ok: true, halted: null, pending_calls: [], reissue_model: false, rescore: [], notes: [] };
    const rs0 = await run.proj("run_state");
    if (TERMINAL_STATES.has(rs0.state)) { plan.halted = "terminal"; return plan; }
    const v = h.store.verify(runId, fullVerify ? undefined : h.config.recovery.verify_last_n);
    if (!v.ok) { await run.emit("integrity.alert", { reason: v.reason, bad_seq: v.bad_seq }); await run.transition("FAILED_INTEGRITY", "integrity_failure"); plan.ok = false; plan.halted = "integrity_failure"; return plan; }
    for (const n of NAMES) await h.svc.projections.rebuild(runId, n);
    const rs = await run.proj("run_state"), last = h.store.getEvent(runId, h.store.lastSeq(runId));
    const downtime = Math.max(0, h.clock.nowMs() - parseTs(last.ts)), allowance = h.config.recovery.max_excluded_downtime_s * 1000 - rs.excluded_ms;
    const excluded = downtimeCause === "harness_failure" && ACTIVE_STATES.has(rs.state) ? Math.min(downtime, Math.max(0, allowance)) : 0;
    const evs = h.store.events(runId);
    const lastReq = Math.max(0, ...evs.filter((e: any) => e.type === "model.requested").map((e: any) => e.seq));
    const lastResp = Math.max(0, ...evs.filter((e: any) => e.type === "model.responded" || e.type === "model.failed").map((e: any) => e.seq));
    plan.reissue_model = lastReq > lastResp;
    plan.settled_reservations = await this.settleReservations(run, evs);
    await this.sandboxes(run, plan);
    await h.approvals.rearm(run);
    const conv = await run.proj("conversation"), tc = await run.proj("tool_calls");
    for (const e of conv.entries) {
      if (e.type !== "model.responded") continue;
      const msg = e.message ?? JSON.parse(h.blobs.get(e.message_ref).toString("utf8"));
      for (const call of msg.parts.filter((p: any) => p.type === "tool_call")) { const st = tc[call.id]?.status; if (st !== "completed" && st !== "failed") plan.pending_calls.push(call); }
    }
    await run.emit("run.recovered", { resumed_from_seq: h.store.lastSeq(runId), downtime_ms: downtime, downtime_cause: downtimeCause, excluded_ms: excluded });
    // a started call on a resource the harness can read is resolved from that resource's authoritative state, not from the log alone and not by blindly repeating it (§5.13)
    for (const call of [...plan.pending_calls]) {
      const info = tc[call.id], t = h.registry.get(call.name); if (!(info && info.status === "started" && t?.manifest.world)) continue;
      const loop = h.loops.get(runId); if (!loop) continue;
      const action = h.runtime.worldAction(run, call.id, call.name, call.arguments, t.manifest), est = await h.runtime.gate.establishOutcome((tp: string, p: any) => run.emit(tp, p, null, null, call.id), action, null, loop.tctx());
      if (est.outcome === "applied") { await h.runtime.complete(run, call.id, call.name, new ToolResult("(verified from the resource's state after recovery: the action had already taken effect, so it was not repeated)"), "tool_trusted", null); plan.pending_calls.splice(plan.pending_calls.indexOf(call), 1); plan.notes.push(`reconciled ${call.name} from state`); }
      else if (est.outcome === "unknown") {
        await run.emit("recovery.uncertain", { tool_call_id: call.id, tool: call.name, reason: "the resource's state could not be read, so it is not known whether the action took effect" });
        if (["RUNNING", "AWAITING_APPROVAL", "SCORING"].includes(await run.state())) await run.transition("AWAITING_HUMAN", "recovery.uncertain");
        plan.halted = "recovery_uncertain"; return plan;
      }
    }
    for (const call of [...plan.pending_calls]) {
      const info = tc[call.id];
      if (!info || info.status !== "started" || info.idempotency !== "non_idempotent") continue;
      const tool = h.registry.get(call.name); let outcome: any = null;
      if (tool?.reconcile) { const loop = h.loops.get(runId); if (loop) outcome = await tool.reconcile(call.arguments, loop.tctx()); }
      if (typeof outcome === "string") outcome = new ToolResult(outcome);
      if (outcome instanceof ToolResult) { await h.runtime.complete(run, call.id, call.name, outcome, "tool_trusted", null); plan.pending_calls.splice(plan.pending_calls.indexOf(call), 1); plan.notes.push(`reconciled ${call.name}`); }
      else {
        await run.emit("recovery.uncertain", { tool_call_id: call.id, tool: call.name, reason: "non-idempotent call was started and its outcome is unknown" });
        if (["RUNNING", "AWAITING_APPROVAL", "SCORING"].includes(await run.state())) await run.transition("AWAITING_HUMAN", "recovery.uncertain");
        plan.halted = "recovery_uncertain"; return plan;
      }
    }
    return plan;
  }
  /** Reattach by label if the sandbox still exists, otherwise recreate from the last snapshot. */
  private async sandboxes(run: any, plan: RecoveryPlan) {
    const h = this.h;
    for (const [sid, info] of Object.entries<any>(await run.proj("sandboxes"))) {
      if (h.sandboxes.handles.has(sid)) continue;
      const found = h.backend.findByLabels(run.runId, info.role);
      if (found && found.sandbox_id === sid) { h.sandboxes.handles.set(sid, found); plan.notes.push(`reattached ${info.role} sandbox`); continue; }
      await run.emit("sandbox.destroyed", { sandbox_id: sid, reason: "lost in crash" });
      for (const [gid, g] of Object.entries<any>(await run.proj("grants_active"))) if (g.spec?.sandbox_id === sid) await run.emit("grant.revoked", { grant_id: gid, kind: g.kind });
      if (info.role === "work") {
        const nw = await h.sandboxes.create(run, "work", h.config.egress?.grants), row = h.store.get("SELECT manifest_hash FROM snapshots WHERE run_id=? ORDER BY taken_at_seq DESC LIMIT 1", run.runId);
        if (row) { restoreSnapshot(row.manifest_hash, h.blobs, nw); plan.notes.push("work sandbox recreated from last snapshot"); } else plan.notes.push("work sandbox recreated empty (no snapshot existed)");
      }
    }
  }
}
