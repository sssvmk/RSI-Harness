/** Budget accounting and enforcement (design §7.4); state lives in the `budgets` projection. */
import { activeTimeMs } from "./projections.js";
export const DIMENSIONS = ["turns", "tokens", "time_ms", "cost", "tools", "concurrency", "compute", "submissions"];
export const TERMINATING = ["turns", "tokens", "time_ms", "cost", "tools"];   // hard stops; the rest are guards on actions (§6.2)
export const RESERVABLE = ["turns", "tokens", "cost", "tools", "submissions"];   // cumulative dimensions reserved before use (time is read from the clock; concurrency and compute are guards)
export const UNMETERED_DEFAULT = ["compute"];                                   // no platform accounting exists for compute here: declared unmetered, never counted as zero
export const REQUIRED: Record<string, string[]> = { core: ["turns", "tokens", "time_ms"], standard: ["turns", "tokens", "time_ms", "cost", "tools"], high: ["turns", "tokens", "time_ms", "cost", "tools", "concurrency", "compute", "submissions"] };
export function normalizeBudgets(cfg: Record<string, any> | undefined, warn = 0.8) {
  const c = cfg ?? {}, wf = c.warn_fraction ?? warn, per: Record<string, number> = c.warn_fractions ?? {};
  for (const [name, f] of [["warn_fraction", wf], ...Object.entries(per).map(([k, v]) => [`warn_fractions.${k}`, v] as [string, any])] as [string, any][]) if (typeof f !== "number" || !(f > 0 && f <= 1)) throw new Error(`budgets.${name} must be a number above 0 and at most 1 (got ${JSON.stringify(f)})`);
  const out: any = {};
  for (const [k, v] of Object.entries<any>(c)) { const [dim, val] = k === "time_s" ? ["time_ms", v * 1000] : [k, v]; if (DIMENSIONS.includes(dim) && v !== null && v !== undefined) out[dim] = { limit: val, warn_at: val * (per[k] ?? wf) }; }
  return out;
}
export const costOf = (u: any, price?: any) => (price ? ((u.input_tokens ?? 0) * (price.in ?? 0) + (u.output_tokens ?? 0) * (price.out ?? 0)) / 1_000_000 : 0.0);
class Accounting {
  constructor(public run: any, public set = "agent") {}
  private async state() { return (await this.run.proj("budgets"))[this.set]; }
  async used(dim: string): Promise<number> {
    if (dim === "time_ms") return activeTimeMs(await this.run.proj("run_state"), this.run.svc.clock.nowMs());
    const st = await this.state(); return st[dim] ? st[dim].used : 0;
  }
  async precheck(deltas: Record<string, number>): Promise<string | null> {
    const st = await this.state();
    for (const [dim, d] of Object.entries<any>(st)) {
      if (!TERMINATING.includes(dim)) continue;
      const delta = deltas[dim] ?? 0, used = await this.used(dim);
      if (used >= d.limit || used + delta > d.limit) {
        if (!d.exhausted) await this.run.emit("budget.exhausted", { dimension: dim, used, limit: d.limit, budget_set: this.set });
        return dim;
      }
      if (!d.warned && used + delta >= d.warn_at) {
        await this.run.emit("budget.warning", { dimension: dim, used, limit: d.limit, budget_set: this.set });
        await this.run.emit("guidance.injected", { kind: "budget", text: `Budget warning: ${dim} has reached ${used + delta} of ${d.limit}. Wrap up and finish soon.` });
      }
    }
    return null;
  }
  async add(deltas: Record<string, number>): Promise<string | null> {
    const st = await this.state(); let exhausted: string | null = null;
    for (const [dim, delta] of Object.entries(deltas)) {
      if (!st[dim] || dim === "time_ms") continue;
      const used = st[dim].used + delta, lim = st[dim].limit;
      await this.run.emit("budget.updated", { dimension: dim, used, limit: lim, budget_set: this.set });
      if (used > lim || (used >= lim && (dim === "tokens" || dim === "cost"))) {
        if (!st[dim].exhausted) await this.run.emit("budget.exhausted", { dimension: dim, used, limit: lim, budget_set: this.set });
        exhausted = exhausted ?? dim;
      }
    }
    return exhausted;
  }
  async snapshotTime() { const st = await this.state(); if (st.time_ms) await this.run.emit("budget.updated", { dimension: "time_ms", used: await this.used("time_ms"), limit: st.time_ms.limit, budget_set: this.set }); }
}

// ------------------------------------------------------------------------------------------------------------------ pure accounting (shared vectors: spec/vectors/budget-vectors.json)
export const unspent = (a: any): number => Math.max(0, a.limit - a.used - a.reserved);
/** (global, local) headroom for `dim`. The global figure leaves other open allocations their guaranteed unspent capacity; local is the agent's own allocation, or null. */
export function available(state: any, bset: string, dim: string, agentId: string | null = null): [number, number | null] {
  const d = state[bset][dim]; let others = 0;
  for (const [aid, al] of Object.entries<any>(state.allocations)) if (!al.ended && al.set === bset && aid !== agentId && dim in al.dims) others += unspent(al.dims[dim]);
  const glob = d.limit - d.used - d.reserved - others, al = agentId ? state.allocations[agentId] : undefined;
  let local: number | null = null; if (al && !al.ended && dim in al.dims) { const a = al.dims[dim]; local = a.limit - a.used - a.reserved; }
  return [glob, local];
}
/** The first dimension that cannot cover `deltas`, else null. */
export function checkReserve(state: any, bset: string, deltas: Record<string, number>, agentId: string | null = null): string | null {
  for (const [dim, amt] of Object.entries(deltas)) { if (!RESERVABLE.includes(dim) || !(dim in state[bset])) continue; const [g, l] = available(state, bset, dim, agentId); if (amt > g || (l !== null && amt > l)) return dim; }
  return null;
}
export function checkAllocation(state: any, bset: string, limits: Record<string, number>): string | null {
  for (const [dim, lim] of Object.entries(limits)) { if (!(dim in state[bset])) return dim; if (lim > available(state, bset, dim)[0]) return dim; }
  return null;
}
export const reserveCost = (usageIn: number, maxOut: number, price: any, margin: number): number => costOf({ input_tokens: usageIn, output_tokens: maxOut }, price) * (1 + margin);
export const unmetered = (_cfg?: any, metered: string[] = []): string[] => UNMETERED_DEFAULT.filter((d) => !metered.includes(d));
export function safeguardProblems(cfg: any, workers: number, queue: number): string[] {
  const c = cfg ?? {}, out: string[] = [];
  if (!c.turns) out.push("an unmetered dimension needs a turn (iteration) limit");
  if (!(c.time_s || c.time_ms)) out.push("an unmetered dimension needs a wall-clock limit");
  if (!(Number.isInteger(workers) && workers > 0)) out.push("the worker pool must be bounded");
  if (!(Number.isInteger(queue) && queue >= 0)) out.push("the queue must be bounded");
  return out;
}
export class QueueFull extends Error {}
export class DeadlineExceeded extends Error {}
/** B_concurrency: at most `workers` operations run at once and at most `queue` wait behind them; permits are released on failure, and a waiter gives up at its deadline. */
export class ConcurrencyLimiter {
  inFlight = 0; waiting = 0; peak = 0; rejected = 0; private waiters: (() => void)[] = [];
  constructor(public workers = 4, public queue = 100) { if (workers < 1 || queue < 0) throw new Error("workers must be at least 1 and queue at least 0"); }
  private acquire(timeoutS?: number): Promise<void> {
    if (this.inFlight < this.workers && this.waiters.length === 0) { this.inFlight++; return Promise.resolve(); }
    if (this.waiting >= this.queue) { this.rejected++; return Promise.reject(new QueueFull(`${this.workers} running and ${this.waiting} queued`)); }
    this.waiting++;
    return new Promise<void>((resolve, reject) => {
      let timer: NodeJS.Timeout | undefined;
      const go = () => { if (timer) clearTimeout(timer); this.waiting--; this.inFlight++; resolve(); };
      this.waiters.push(go);
      if (timeoutS !== undefined) timer = setTimeout(() => { const i = this.waiters.indexOf(go); if (i >= 0) { this.waiters.splice(i, 1); this.waiting--; reject(new DeadlineExceeded(`no worker free within ${timeoutS}s`)); } }, timeoutS * 1000);
    });
  }
  async slot<T>(fn: () => Promise<T>, timeoutS?: number): Promise<T> {
    await this.acquire(timeoutS); this.peak = Math.max(this.peak, this.inFlight);
    try { return await fn(); } finally { this.inFlight--; const next = this.waiters.shift(); if (next) next(); }
  }
}
// ------------------------------------------------------------------------------------------------------------------ the service
/** A per-run mutex held by the harness instance (not the process), so a crashed instance can never leave a lock behind for the one that replaces it. */
async function withLock<T>(svc: any, key: string, fn: () => Promise<T>): Promise<T> {
  const tails: Map<string, Promise<void>> = (svc._budgetLocks ??= new Map());
  const prev = tails.get(key) ?? Promise.resolve(); let release!: () => void; const mine = new Promise<void>((r) => (release = r));
  tails.set(key, prev.then(() => mine)); await prev;
  try { return await fn(); } finally { release(); }
}
let rseq = 0;
/** BudgetController with reservations (design §7.4.4): reserve before a call, then commit the actual usage or release. */
export class BudgetController extends Accounting {
  private async full() { return this.run.proj("budgets"); }
  /** [reservation_id, null] when made, else [null, the dimension that cannot cover it]. Atomic: the check and the append happen under one lock. */
  async reserve(deltas: Record<string, number>, agentId: string | null = null): Promise<[string | null, string | null]> {
    return withLock(this.run.svc, this.run.runId, async () => {
      const full = await this.full(), st = full[this.set];
      if (st.time_ms) { const used = await this.used("time_ms"); if (used >= st.time_ms.limit) { if (!st.time_ms.exhausted) await this.run.emit("budget.exhausted", { dimension: "time_ms", used, limit: st.time_ms.limit, budget_set: this.set }); return [null, "time_ms"] as [null, string]; } }
      const bad = checkReserve(full, this.set, deltas, agentId);
      if (bad) { if (!st[bad].exhausted) await this.run.emit("budget.exhausted", { dimension: bad, used: st[bad].used, reserved: st[bad].reserved, limit: st[bad].limit, needed: deltas[bad], budget_set: this.set }); return [null, bad] as [null, string]; }
      for (const [dim, d] of Object.entries<any>(st)) if (RESERVABLE.includes(dim) && !d.warned && dim in deltas && d.used + deltas[dim] >= d.warn_at) {
        await this.run.emit("budget.warning", { dimension: dim, used: d.used, limit: d.limit, budget_set: this.set });
        await this.run.emit("guidance.injected", { kind: "budget", text: `Budget warning: ${dim} has reached ${d.used + deltas[dim]} of ${d.limit}. Wrap up and finish soon.` });
      }
      const rid = `${Date.now().toString(16)}${(rseq++).toString(16)}${Math.random().toString(16).slice(2, 8)}`.slice(0, 16);
      await this.run.emit("budget.reserved", { reservation_id: rid, deltas: Object.fromEntries(Object.entries(deltas).filter(([k]) => RESERVABLE.includes(k) && k in st)), agent_id: agentId, budget_set: this.set });
      return [rid, null] as [string, null];
    });
  }
  /** Replace the estimate with what was used (the rest of the reservation is released). `unreconciled` charges the estimate and flags it until the real usage is known. */
  async commit(rid: string, actual: Record<string, number>, o: { unreconciled?: boolean; reconciles?: boolean } = {}): Promise<string | null> {
    const full = await this.full(), r = full.reservations[rid], agent = r ? r.agent_id : null;
    await this.run.emit("budget.committed", { reservation_id: rid, actual: Object.fromEntries(Object.entries(actual).filter(([k]) => RESERVABLE.includes(k))), agent_id: agent, unreconciled: o.unreconciled ?? false, reconciles: o.reconciles ?? false, budget_set: this.set });
    if (o.unreconciled) await this.run.emit("budget.unreconciled", { reservation_id: rid, estimate: actual, budget_set: this.set });
    return this.add(actual);
  }
  async release(rid: string, reason: string) { await this.run.emit("budget.released", { reservation_id: rid, reason, budget_set: this.set }); }
  async allocate(agentId: string, limits: Record<string, number>): Promise<string | null> {
    return withLock(this.run.svc, this.run.runId, async () => {
      const full = await this.full(); if (full.allocations[agentId] && !full.allocations[agentId].ended) throw new Error(`${agentId} already has an allocation`);
      const bad = checkAllocation(full, this.set, limits); if (bad) return bad;
      await this.run.emit("budget.allocated", { agent_id: agentId, limits, budget_set: this.set }); return null;
    });
  }
  async endAllocation(agentId: string) { await this.run.emit("budget.allocation_ended", { agent_id: agentId, budget_set: this.set }); }
  /** Give back every reservation still open (a run that ends must not hold budget). */
  async releaseOpen(reason: string): Promise<number> { let n = 0; for (const [rid, r] of Object.entries<any>((await this.full()).reservations)) if (r.set === this.set) { await this.release(rid, reason); n++; } return n; }
}
