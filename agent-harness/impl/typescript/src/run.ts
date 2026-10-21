/** Run model: services, run handle, state machine (§6), RunManager lifecycle. */
import { Pins } from "./definitions.js";
import { finalFields, manifestHash, merged } from "./manifest.js";
import { BudgetController, normalizeBudgets } from "./budgets.js";
import { TERMINAL_STATES } from "./projections.js";
import { EventStore, EventWriter, ProjectionManager } from "./eventlog.js";
import { BlobStore } from "./blobs.js";
import { classify, loadRules } from "./failure.js";
import { Clock, ulid } from "./util.js";

export const STATES = new Set(["CREATED", "PROVISIONING", "READY", "RUNNING", "AWAITING_APPROVAL", "SCORING", "AWAITING_HUMAN", "PAUSED", "COMPLETING", "TERMINATING", "COMPLETED", "TERMINATED", "FAILED_INTEGRITY"]);
const NON_TERMINAL = new Set([...STATES].filter((s) => !TERMINAL_STATES.has(s)));
const EXPLICIT = new Set(["CREATED>PROVISIONING", "PROVISIONING>READY", "READY>RUNNING", "RUNNING>AWAITING_APPROVAL", "AWAITING_APPROVAL>RUNNING", "RUNNING>SCORING", "SCORING>RUNNING", "RUNNING>COMPLETING", "COMPLETING>COMPLETED", "COMPLETING>RUNNING",
  "RUNNING>AWAITING_HUMAN", "AWAITING_APPROVAL>AWAITING_HUMAN", "SCORING>AWAITING_HUMAN", "PAUSED>RUNNING", "TERMINATING>TERMINATED"]);
export const TERMINATION_REASONS = new Set(["budget_exhausted", "stuck_loop", "policy_violation", "operator_kill", "fatal_error", "verification_failed_limit", "timeout", "integrity_failure", "recovery_uncertain_abandoned"]);
export class TransitionError extends Error {}
export function transitionAllowed(frm: string, to: string): boolean {
  if (EXPLICIT.has(`${frm}>${to}`)) return true;
  if (NON_TERMINAL.has(frm) && to === "PAUSED") return true;
  if (NON_TERMINAL.has(frm) && frm !== "TERMINATING" && to === "TERMINATING") return true;
  return to === "FAILED_INTEGRITY" && NON_TERMINAL.has(frm);
}
export class Services {
  projections: ProjectionManager; handles: any = {}; plugins: any = null;   // the PluginHost of the deployment (design §18.11)
  constructor(public store: EventStore, public writer: EventWriter, public blobs: BlobStore, public clock: Clock, public config: any) { this.projections = new ProjectionManager(store, writer); }
}
export class Deferred<T> { promise: Promise<T>; resolve!: (v: T) => void; reject!: (e: any) => void; constructor() { this.promise = new Promise((res, rej) => { this.resolve = res; this.reject = rej; }); this.promise.catch(() => {}); } }

export class Run {
  waiters = new Map<string, Deferred<string>>(); private resumeGate: Deferred<void> | null = null;
  private _pins: Pins | null = null;
  constructor(public svc: Services, public runId: string, public profile: string, public owner: string) {}
  /** The definitions this run is pinned to (design §13.9): loaded from its manifest, so a restarted process sees exactly what the run began with. */
  get pins(): Pins { return (this._pins ??= Pins.load(this.svc, this.runId)); }
  waiter(aid: string) { let d = this.waiters.get(aid); if (!d) { d = new Deferred<string>(); this.waiters.set(aid, d); } return d; }
  resolveWaiter(aid: string, status: string) { this.waiters.get(aid)?.resolve(status); }
  cancelWaiters() { for (const d of this.waiters.values()) d.reject(new Error("cancelled")); }
  emit(type: string, payload: any, actor?: any, causationSeq?: number | null, correlationId?: string | null) {
    return this.svc.writer.submit({ run_id: this.runId, type, payload, actor: actor ?? { kind: "control_plane", id: "harness" }, causation_seq: causationSeq ?? null, correlation_id: correlationId ?? null });
  }
  proj(name: string): Promise<any> { return this.svc.projections.get(this.runId, name); }
  async state(): Promise<string> { return (await this.proj("run_state")).state; }
  async waitResumed() { if (this.resumeGate) await this.resumeGate.promise; }
  async transition(to: string, reason: string | null = null, causationSeq: number | null = null) {
    const rs = await this.proj("run_state"), frm = rs.state;
    if (frm === to) return null;
    if (!transitionAllowed(frm, to)) throw new TransitionError(`illegal transition ${frm} -> ${to}`);
    if (frm === "COMPLETING" && to === "COMPLETED" && (rs.verified_seq === null || rs.entered_completing_seq === null || rs.verified_seq < rs.entered_completing_seq))
      throw new TransitionError("COMPLETED requires recorded verification evidence (H-3.3-evidence-based-completion)");
    if (to === "PAUSED") this.resumeGate = new Deferred<void>();
    const ev = await this.emit("run.state_changed", { from: frm, to, reason }, undefined, causationSeq);
    if (frm === "PAUSED") { this.resumeGate?.resolve(); this.resumeGate = null; }
    return ev;
  }
}
export class RunManager {
  runs = new Map<string, Run>(); rules: any;
  constructor(public svc: Services, public sandboxes: any, rules?: any) { this.rules = rules ?? loadRules(); }
  get(runId: string): Run {
    let r = this.runs.get(runId);
    if (!r) { const row = this.svc.store.runRow(runId); if (!row) throw new Error(`unknown run ${runId}`); r = new Run(this.svc, runId, row.profile, row.owner); this.runs.set(runId, r); }
    return r;
  }
  async createRun(o: { owner: string; profile: string; instruction: string; agent: any; taskId?: string | null; configCommit?: string; budgets?: any; evaluatorBudgets?: any; provenance?: any; preflight?: any; egress?: any[]; manifest?: any }) {
    const runId = ulid(), run = new Run(this.svc, runId, o.profile, o.owner); this.runs.set(runId, run);
    await run.emit("run.created", { owner: o.owner, profile: o.profile, task_id: o.taskId ?? null, config_commit: o.configCommit ?? "local", agent: o.agent,
      budgets: normalizeBudgets(o.budgets ?? this.svc.config.budgets), evaluator_budgets: normalizeBudgets(o.evaluatorBudgets ?? {}), provenance: o.provenance ?? {}, ...(o.manifest ? { manifest: { ...o.manifest, run_id: runId } } : {}) }, { kind: "user", id: o.owner });
    if (o.preflight && !o.preflight.ok) { await this.terminate(run, "fatal_error", "preflight failed: " + o.preflight.failed.map((c: any) => c.id).join(", ")); return run; }
    await run.transition("PROVISIONING", "run.created accepted");
    try { await this.sandboxes.create(run, "work", o.egress); } catch (e: any) { await this.terminate(run, "fatal_error", `provisioning failed: ${e.message}`); return run; }
    await run.transition("READY", "executor.ready");
    await run.emit("user.message", { message: { role: "user", parts: [{ type: "text", text: o.instruction }] }, trust: "user", search_text: o.instruction }, { kind: "user", id: o.owner });
    return run;
  }
  start(run: Run) { return run.transition("RUNNING", "start"); }
  async pause(run: Run, who: string) { await run.emit("operator.action", { action: "pause", who }, { kind: "user", id: who }); await run.transition("PAUSED", "operator pause"); }
  async resume(run: Run, who: string) { await run.emit("operator.action", { action: "resume", who }, { kind: "user", id: who }); await run.transition("RUNNING", "operator resume"); }
  kill(run: Run, who: string) { return this.terminate(run, "operator_kill", `killed by ${who}`); }
  async terminate(run: Run, reason: string, detail = "") {
    if (!TERMINATION_REASONS.has(reason.split(":")[0])) throw new Error(`unknown termination reason ${reason}`);
    const state = await run.state();
    if (TERMINAL_STATES.has(state)) return;
    if (state !== "TERMINATING") await run.transition("TERMINATING", reason);   // recorded before any action takes effect (§3.3 rule 5)
    run.cancelWaiters();
    await this.sandboxes.teardown(run);
    await new BudgetController(run).releaseOpen("run ended");   // a run that ends holds no budget (§7.4.4)
    const m = /^([a-z_]+\.[a-z_0-9]+):/.exec(detail ?? ""), evs = this.svc.store.events(run.runId, { types: ["tool.call.failed"] });   // e.g. "gateway.http_500: ..." carries its own error code
    const lastErr = m ? m[1] : evs.length ? evs[evs.length - 1].payload.error?.code ?? null : null, cls = classify(reason, lastErr, this.rules, evs.map((e: any) => e.payload.error?.code).filter(Boolean));
    if ((await run.proj("run_state")).terminal_reason === null) { await run.emit("run.terminated", { reason, detail }); if (cls) await run.emit("failure.classified", cls); }
    await this.finish(run, "TERMINATED");
    await run.transition("TERMINATED", reason);
  }
  /** `run.finished` carries the manifest's final fields and its hash (design §13.9). A run created without a manifest has nothing to finish. */
  async finish(run: Run, state: string) {
    const start = this.svc.store.events(run.runId, { types: ["run.created"] })[0]?.payload?.manifest; if (!start) return;
    const models = Object.fromEntries((this.svc.config?.models ?? []).map((m: any) => [m.ref, m])), fin = await finalFields(this.svc, run, models, state);
    await run.emit("run.finished", { fields: fin, manifest_hash: manifestHash(merged(start, fin)) });
  }
  async complete(run: Run, evidenceRef: string | null) { await run.emit("run.completed", { evidence_ref: evidenceRef }); await this.finish(run, "COMPLETED"); await run.transition("COMPLETED", "verified"); await this.sandboxes.teardown(run); }
}
