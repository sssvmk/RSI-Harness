/** Approvals (design §11.4): pending -> granted|denied|expired|cancelled; standing approvals with atomic use counts. */
import { canonicalize, formatTs, parseTs, ulid } from "./util.js";
import { globMatch } from "./policy.js";
export class ApprovalError extends Error { constructor(public code: string, msg: string) { super(msg); } }
export class ApprovalService {
  cfg: any; timers = new Map<string, NodeJS.Timeout>();
  constructor(public svc: any, public redactor: any = null) { this.cfg = svc.config.approvals ?? {}; }
  async createStanding(who: string, roles: string[], tool: string, argsPattern: string | null, ttlS: number, maxCount: number, runId: string | null = null) {
    if (!roles.includes("approver")) throw new ApprovalError("approvals.forbidden", "approver role required");
    const id = ulid();
    await this.svc.writer.execute("INSERT INTO standing_approvals(id,run_id,tool,args_pattern,expires_at,max_count,used,granted_by) VALUES(?,?,?,?,?,?,0,?)", [id, runId, tool, argsPattern, formatTs(this.svc.clock.nowMs() + ttlS * 1000), maxCount, who]);
    return id;
  }
  async tryStanding(run: any, tool: string, args: any): Promise<string | null> {
    const rows = this.svc.store.all("SELECT * FROM standing_approvals WHERE (run_id IS NULL OR run_id=?) AND expires_at>? AND used<max_count", run.runId, formatTs(this.svc.clock.nowMs()));
    const argsJson = canonicalize(args);
    for (const r of rows) {
      if (!globMatch(r.tool, tool)) continue;
      if (r.args_pattern && !globMatch(r.args_pattern, argsJson)) continue;
      const n = await this.svc.writer.execute("UPDATE standing_approvals SET used=used+1 WHERE id=? AND used<max_count", [r.id]);
      if (n === 1) return r.id;   // the atomic increment won
    }
    return null;
  }
  async request(run: any, toolCallId: string, tool: string, args: any, ruleId: string | null, cfg0: any, effectClass: string | null, causationSeq: number | null = null) {
    const cfg = cfg0 ?? {}, perInstance = cfg.allow_standing === false || (run.profile === "high" && effectClass === "irreversible");
    if (!perInstance) {
      const sid = await this.tryStanding(run, tool, args);
      if (sid) {
        const aid = ulid();
        await run.emit("approval.requested", { approval_id: aid, tool_call_id: toolCallId, tool, rule_id: ruleId, scope: { standing: sid } }, null, causationSeq);
        await run.emit("approval.decided", { approval_id: aid, status: "granted", decided_by: `standing:${sid}` });
        return { approval_id: aid, status: "granted", standing: sid };
      }
    }
    const aid = ulid(), ttl = cfg.ttl_s ?? this.cfg.expiry_s ?? 3600, now = this.svc.clock.nowMs(), scope = cfg.scope ?? { by: ["tool", "args_pattern"] };
    const shown = this.redactor ? this.redactor.redactObj(args) : args;
    await this.svc.writer.execute("INSERT INTO approvals(approval_id,run_id,tool_call_id,rule_id,status,requested_at,expires_at,scope) VALUES(?,?,?,?,'pending',?,?,?)", [aid, run.runId, toolCallId, ruleId, formatTs(now), formatTs(now + ttl * 1000), JSON.stringify(scope)]);
    await run.emit("approval.requested", { approval_id: aid, tool_call_id: toolCallId, tool, rule_id: ruleId, scope, args: shown, expires_at: formatTs(now + ttl * 1000) }, null, causationSeq);
    run.waiter(aid);
    this.arm(run, aid, ttl * 1000);
    return { approval_id: aid, status: "pending" };
  }
  private arm(run: any, aid: string, ms: number) {
    const t = setTimeout(() => { void this.finalize(run, aid, "expired", "system:expiry"); }, Math.max(0, ms));   // real timer; durable expires_at is the source of truth
    t.unref?.(); this.timers.set(aid, t);
  }
  async expireDue(run: any): Promise<number> {
    let n = 0;
    for (const r of this.svc.store.all("SELECT * FROM approvals WHERE run_id=? AND status='pending'", run.runId)) if (parseTs(r.expires_at) <= this.svc.clock.nowMs()) n += (await this.finalize(run, r.approval_id, "expired", "system:expiry")) ? 1 : 0;
    return n;
  }
  async rearm(run: any) {
    for (const r of this.svc.store.all("SELECT * FROM approvals WHERE run_id=? AND status='pending'", run.runId)) { run.waiter(r.approval_id); this.arm(run, r.approval_id, Math.max(0, parseTs(r.expires_at) - this.svc.clock.nowMs())); }
    await this.expireDue(run);
  }
  clearTimers() { for (const t of this.timers.values()) clearTimeout(t); this.timers.clear(); }
  async finalize(run: any, aid: string, status: string, by: string): Promise<boolean> {
    const n = await this.svc.writer.execute("UPDATE approvals SET status=?, decided_by=?, decided_at=? WHERE approval_id=? AND status='pending'", [status, by, formatTs(this.svc.clock.nowMs()), aid]);
    if (n !== 1) return false;   // already decided: replays have no further effect (exactly-once)
    await run.emit("approval.decided", { approval_id: aid, status, decided_by: by });
    const t = this.timers.get(aid); if (t) { clearTimeout(t); this.timers.delete(aid); }
    run.resolveWaiter(aid, status);
    return true;
  }
  async decide(run: any, aid: string, who: string, roles: string[], grant: boolean) {
    if (!roles.includes("approver")) throw new ApprovalError("approvals.forbidden", "approver role required");
    if (who === run.owner && !this.cfg.allow_owner_approval) throw new ApprovalError("approvals.self_approval", "the run owner cannot approve their own run");
    const row = this.svc.store.get("SELECT * FROM approvals WHERE approval_id=?", aid);
    if (!row || row.run_id !== run.runId) throw new ApprovalError("approvals.not_found", aid);
    const status = grant ? "granted" : "denied";
    if (!(await this.finalize(run, aid, status, who))) throw new ApprovalError("approvals.already_decided", `approval already ${this.svc.store.get("SELECT status FROM approvals WHERE approval_id=?", aid).status}`);
    return status;
  }
  async wait(run: any, aid: string): Promise<string> {
    const row = this.svc.store.get("SELECT status FROM approvals WHERE approval_id=?", aid);
    if (row && row.status !== "pending") return row.status;
    try { return await run.waiter(aid).promise; }
    catch { await this.finalize(run, aid, "cancelled", "system:cancel"); return "cancelled"; }
  }
}
