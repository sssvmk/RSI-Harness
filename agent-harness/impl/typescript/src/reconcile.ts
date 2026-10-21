/** Lifecycle reconciliation and the pre-action gate (design §5.13). Mirrors reconcile.py: a runtime service in the tool-call pipeline, authorization and consistency checked separately, and a timeout read from
 *  authoritative state before any retry. */
import crypto from "node:crypto";

export const STATES = ["consistent", "drift_detected", "reconciling", "blocked", "unknown", "recovery_required"];
export const UNVERIFIED = ["drift_detected", "reconciling", "unknown", "recovery_required"];   // effects that are not established: a run is not complete while any remain
export const EVENTS = ["verified", "mismatch", "start_reconcile", "denied", "unreadable", "repair_failed"];
export const RISKS = ["low", "medium", "high", "critical"];
export const DEFAULT_RISK: Record<string, string> = { read_only: "low", reversible_write: "medium", irreversible: "high", external_facing: "high" };
const T = (s: string | null, e: string) => `${s}\u0000${e}`;
export const TABLE = new Map<string, string>(Object.entries({
  [T(null, "verified")]: "consistent", [T(null, "mismatch")]: "drift_detected", [T(null, "unreadable")]: "unknown", [T(null, "denied")]: "blocked",
  [T("consistent", "verified")]: "consistent", [T("consistent", "mismatch")]: "drift_detected", [T("consistent", "unreadable")]: "unknown", [T("consistent", "denied")]: "blocked",
  [T("drift_detected", "start_reconcile")]: "reconciling", [T("drift_detected", "denied")]: "blocked", [T("drift_detected", "unreadable")]: "unknown", [T("drift_detected", "mismatch")]: "drift_detected",
  [T("reconciling", "verified")]: "consistent", [T("reconciling", "mismatch")]: "drift_detected", [T("reconciling", "unreadable")]: "unknown", [T("reconciling", "repair_failed")]: "recovery_required",
  [T("blocked", "verified")]: "consistent", [T("blocked", "mismatch")]: "drift_detected", [T("blocked", "unreadable")]: "unknown", [T("blocked", "denied")]: "blocked",
  [T("unknown", "verified")]: "consistent", [T("unknown", "mismatch")]: "drift_detected", [T("unknown", "unreadable")]: "unknown", [T("unknown", "start_reconcile")]: "reconciling", [T("unknown", "denied")]: "blocked",
  [T("recovery_required", "start_reconcile")]: "reconciling", [T("recovery_required", "verified")]: "consistent", [T("recovery_required", "mismatch")]: "recovery_required", [T("recovery_required", "unreadable")]: "recovery_required" }));
export class ReconcileError extends Error {}
/** The next state. An `unknown` (or `recovery_required`, `reconciling`, `blocked`) subject becomes `consistent` only on `verified` evidence from an authoritative read; without it the state stays where it is. */
export function nextState(state: string | null, event: string, authoritative = false): string | null {
  const nxt = TABLE.get(T(state, event)); if (nxt === undefined) throw new ReconcileError(`illegal transition: ${state} on ${event}`);
  if (event === "verified" && ["unknown", "recovery_required", "reconciling", "blocked"].includes(state ?? "") && !authoritative) return state;
  return nxt;
}
/** Apply an event the way the log does: a verified read of a drifting subject passes through `reconciling`; an event that does not apply to the state changes nothing. */
export function applyEvent(state: string | null, event: string, authoritative = false): string | null {
  if (TABLE.has(T(state, event))) return nextState(state, event, authoritative);
  if (event === "verified" && TABLE.has(T(state, "start_reconcile"))) return nextState(nextState(state, "start_reconcile"), "verified", authoritative);
  return state;
}
export function requirements(risk: string) {
  const i = RISKS.indexOf(risk); if (i < 0) throw new ReconcileError(`unknown risk '${risk}'`);
  return { world_read: i >= 1, preconditions: i >= 1, version_check: i >= 1, readback: i >= 1, fresh_state: i >= 2, current_approval: i >= 2, idempotency_key: i >= 2, fail_closed: i >= 2, human: i >= 3 };
}
export const riskOf = (effectClass: string | null | undefined, declared?: string | null, critical = false): string => (critical ? "critical" : declared ?? DEFAULT_RISK[effectClass ?? "read_only"] ?? "medium");
export class VersionConflict extends Error {}
export interface WorldAdapter {
  readState(resource: string, ctx?: any): Promise<{ version: string; value: any } | null>;
  checkPreconditions?(action: any, actual: any): Promise<boolean>;
  effectsHold?(action: any, before: any, after: any, result: any): Promise<boolean>;
}
/** An approval is current if it was granted, has not been revoked, and is not older than the freshness window [proposed P81]. */
export const approvalCurrent = (a: any, nowS: number, maxAgeS = 900): boolean => !!a && a.status === "granted" && !a.revoked && nowS - (a.decided_at_s ?? nowS) <= maxAgeS;
type Emit = (type: string, payload: any) => Promise<any>;
export class Gate {
  private locks = new Map<string, Promise<void>>();
  constructor(public adapters: Record<string, WorldAdapter> = {}, public approvalMaxAgeS = 900) {}
  private async withLock<T>(key: string, fn: () => Promise<T>): Promise<T> {
    const prev = this.locks.get(key) ?? Promise.resolve(); let release!: () => void; const mine = new Promise<void>((r) => (release = r)); this.locks.set(key, prev.then(() => mine)); await prev;
    try { return await fn(); } finally { release(); }
  }
  async pre(emit: Emit, action: any, ctx: any = null, nowS = 0) {
    const req = requirements(action.risk), aid = action.id;
    const block = async (reason: string, ev = "denied") => {
      await emit("action.blocked", { action_id: aid, reason, risk: action.risk }); await emit("reconcile.checked", { action_id: aid, risk: action.risk, result: "blocked", reason }); await this.state(emit, action, ev, false);
      return { status: "blocked", reason, actual: null as any };
    };
    if (!(action.authorized ?? true)) return block("authorization_denied");
    if (!req.world_read) { await emit("reconcile.checked", { action_id: aid, risk: action.risk, result: "internal_only" }); return { status: "ok", reason: null, actual: null as any }; }
    const ad = this.adapters[action.adapter]; if (!ad) return block("world_state_unavailable", "unreadable");
    let actual: any; try { actual = await ad.readState(action.resource, ctx); } catch { actual = null; }
    if (actual === null || actual === undefined) return block("world_state_unavailable", "unreadable");
    if (req.preconditions && ad.checkPreconditions && !(await ad.checkPreconditions(action, actual))) return block("precondition_failed", "mismatch");
    if (req.version_check && action.expected_version !== null && action.expected_version !== undefined && action.expected_version !== actual.version) return block("stale_state", "mismatch");
    if (req.current_approval && action.approval_required && !approvalCurrent(action.approval, nowS, this.approvalMaxAgeS)) return block("approval_stale_or_revoked");
    if (req.human && !action.human_approved) return block("human_intervention_required");
    await emit("reconcile.checked", { action_id: aid, risk: action.risk, result: "ok_to_execute" }); return { status: "ok", reason: null, actual };
  }
  /** Run `do` with concurrency protection: under the resource's lock the state is read again and must still be the state that was checked. */
  async guarded<T>(action: any, actual: any, doIt: () => Promise<T>, ctx: any = null): Promise<T> {
    if (!actual) return doIt(); const ad = this.adapters[action.adapter];
    return this.withLock(`${action.adapter}\u0000${action.resource}`, async () => { const cur = await ad.readState(action.resource, ctx); if (!cur || cur.version !== actual.version) throw new VersionConflict(action.resource); return doIt(); });
  }
  /** Read the state back and compare it with the expected effect; the tool's acknowledgment alone is not trusted. */
  async post(emit: Emit, action: any, actual: any, result: any, ctx: any = null) {
    const req = requirements(action.risk), aid = action.id; if (!req.readback) return { status: "not_checked", observed: null as any };
    const ad = this.adapters[action.adapter]; let observed: any; try { observed = ad ? await ad.readState(action.resource, ctx) : null; } catch { observed = null; }
    if (!observed) { await emit("reconcile.checked", { action_id: aid, risk: action.risk, result: "outcome_unverified" }); await this.state(emit, action, "unreadable", false); return { status: "pending_reconciliation", reason: "outcome_unverified", observed: null as any }; }
    if (ad.effectsHold && !(await ad.effectsHold(action, actual ?? {}, observed, result))) { await emit("reconcile.checked", { action_id: aid, risk: action.risk, result: "effect_mismatch" }); await this.state(emit, action, "mismatch", true); return { status: "reconciliation_required", reason: "effect_mismatch", observed }; }
    await emit("reconcile.checked", { action_id: aid, risk: action.risk, result: "verified" }); await this.state(emit, action, "verified", true); return { status: "verified_success", observed };
  }
  /** After a timeout or interruption: did the operation complete? Read authoritative state, never assume. `unknown` means do not retry. */
  async establishOutcome(emit: Emit, action: any, before: any, ctx: any = null) {
    const ad = this.adapters[action.adapter]; let now: any; try { now = ad ? await ad.readState(action.resource, ctx) : null; } catch { now = null; }
    if (!now) { await this.state(emit, action, "unreadable", false); return { outcome: "unknown", observed: null as any }; }
    const applied = ad.effectsHold ? await ad.effectsHold(action, before ?? {}, now, null) : true;
    await emit("reconcile.checked", { action_id: action.id, risk: action.risk, result: applied ? "applied_before_retry" : "not_applied", lifecycle: "retry" });
    await this.state(emit, action, applied ? "verified" : "mismatch", true); return { outcome: applied ? "applied" : "not_applied", observed: now };
  }
  async state(emit: Emit, action: any, event: string, authoritative: boolean) { await emit("reconcile.state_changed", { subject: `${action.adapter}:${action.resource}`, action_id: action.id, event, authoritative }); }
}
/** Files in the agent's workspace. The version of a file is the hash of its content, or `absent`. */
export class WorkspaceFileAdapter implements WorldAdapter {
  async readState(resource: string, ctx: any = null) {
    try { const content = ctx.sandbox.executor.fsRead(resource).content as string; return { version: crypto.createHash("sha256").update(content, "utf8").digest("hex"), value: content }; }
    catch (e: any) { if (e?.code === "fs.path_not_found") return { version: "absent", value: null }; return null; }   // a file that does not exist is a readable state; anything else cannot be read
  }
  async checkPreconditions(action: any, actual: any) { const p: string[] = action.preconditions ?? []; return p.every((c) => c !== "exists" || actual.version !== "absent") && p.every((c) => c !== "absent" || actual.version === "absent"); }
  async effectsHold(action: any, _b: any, after: any) { const want = action.args?.content; return (action.expected_effects ?? []).every((e: string) => e !== "content_equals" || after.value === want); }
}
