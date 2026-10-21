/** The calculus of dynamic composition (Shi, Zhang and Cui, arXiv:2608.25512, §3-§4) as a runtime. Mirrors the Python implementation rule for rule; see
 *  impl/python/src/harness/components/core.py for the mapping from the paper's definitions and the list of deliberate deviations. */
import { createHash } from "node:crypto";
import { KeyType } from "./keys.js";

export const KEY_RE = /^[a-z0-9_.-]+\/[a-z0-9_.@:-]+$/;
export class ComponentError extends Error { code = "component.error"; recorded = false; }
/** Cordis UNDECLARED_ACCESS: the key is not in the component's declared set (Definition 55). */
export class ConfinementViolation extends ComponentError { code = "component.confinement_violation"; kind = "UNDECLARED_ACCESS"; }
export class PreconditionFailed extends ComponentError { code = "component.precondition_failed"; }
/** Cordis INACTIVE_ACCESS (Algorithm 6): the key is declared but not committed, so the dependency is not loaded. */
export class InactiveAccess extends PreconditionFailed { code = "component.inactive_access"; }

export abstract class Stage { abstract kind: string; }
export class Provide extends Stage { kind = "Provide"; constructor(public key: string, public value: any) { super(); } }
export class Operate extends Stage { kind = "Operate"; constructor(public key: string, public op: string, public arg: any = null) { super(); } }
export class Instantiate extends Stage { kind = "Instantiate"; constructor(public component: Component) { super(); } }
/** An effect that leaves the system boundary: with `compensate` it runs now and the compensation is its inverse; without, it is withheld until approved. */
export class External extends Stage { kind = "External"; constructor(public label: string, public action: () => any, public compensate: (() => void) | null = null) { super(); } }

export function parseVersion(s: string): [number, number, number] {
  const m = /^(\d+)\.(\d+)\.(\d+)$/.exec(s.trim()); if (!m) throw new Error(`bad version '${s}'`);
  return [Number(m[1]), Number(m[2]), Number(m[3])];
}
const cmp = (a: number[], b: number[]) => { for (let i = 0; i < 3; i++) if (a[i] !== b[i]) return a[i] < b[i] ? -1 : 1; return 0; };
export function versionSatisfies(version: string, rng: string): boolean {
  const v = parseVersion(version);
  for (const raw of rng.split(",")) {
    const clause = raw.trim(); if (clause === "" || clause === "*") continue; let ok: boolean;
    if (clause.startsWith("^")) { const b = parseVersion(clause.slice(1)), hi: [number, number, number] = b[0] > 0 ? [b[0] + 1, 0, 0] : b[1] > 0 ? [0, b[1] + 1, 0] : [0, 0, b[2] + 1]; ok = cmp(b, v) <= 0 && cmp(v, hi) < 0; }
    else if (clause.startsWith("~")) { const b = parseVersion(clause.slice(1)); ok = cmp(b, v) <= 0 && cmp(v, [b[0], b[1] + 1, 0]) < 0; }
    else if (clause.startsWith(">=")) ok = cmp(v, parseVersion(clause.slice(2))) >= 0;
    else if (clause.startsWith("<=")) ok = cmp(v, parseVersion(clause.slice(2))) <= 0;
    else if (clause.startsWith(">")) ok = cmp(v, parseVersion(clause.slice(1))) > 0;
    else if (clause.startsWith("<")) ok = cmp(v, parseVersion(clause.slice(1))) < 0;
    else ok = cmp(v, parseVersion(clause.replace(/^=+/, ""))) === 0;
    if (!ok) return false;
  }
  return true;
}
export const stable = (v: any): string => (v === null || typeof v !== "object" ? JSON.stringify(v) : Array.isArray(v) ? `[${v.map(stable).join(",")}]` : `{${Object.keys(v).sort().map((k) => JSON.stringify(k) + ":" + stable(v[k])).join(",")}}`);

export class Component {
  requires: Record<string, string>; provides: Record<string, string>;
  constructor(public id: string, public effects: (ctx: Ctx) => any, o: { requires?: Record<string, string>; provides?: Record<string, string>; version?: string; config?: any; isolated?: boolean } = {}) {
    this.requires = { ...(o.requires ?? {}) }; this.provides = { ...(o.provides ?? {}) }; this.version = o.version ?? "1.0.0"; this.config = o.config ?? null; this.isolated = o.isolated ?? false;
    if (Object.keys(this.requires).some((k) => k in this.provides)) throw new Error(`${id}: a component cannot both require and provide the same key`);
    parseVersion(this.version); for (const v of Object.values(this.provides)) parseVersion(v);
  }
  version: string; config: any; isolated: boolean;
  get d(): Set<string> { return new Set(Object.keys(this.requires)); }
  get p(): Set<string> { return new Set(Object.keys(this.provides)); }
  get declared(): Set<string> { return new Set([...this.d, ...this.p]); }
  configHash(): string { return createHash("sha256").update(stable({ id: this.id, version: this.version, config: this.config, requires: this.requires, provides: this.provides })).digest("hex").slice(0, 16); }
}

export class Fiber {
  sigma: Record<string, any> = {}; retired = false; failed = false; theta = "Inactive"; gen: Generator<any, any, any> | null = null;
  acc: [string, (w: World) => void][] = []; omega: Record<string, string> | null = null; started = false; pendingOut: any = null; pendingExc: Error | null = null; escalated = false;
  constructor(public name: string, public comp: Component, public parent: string) {}
  installed() { return this.theta !== "Inactive"; }
  /** The lifecycle state under the implementation's names (paper Table 2); FAILED carries the error outcome (§4.4). */
  get state(): string { if (this.failed && this.theta === "Inactive") return "FAILED"; return ({ Inactive: "INACTIVE", Reloading: "LOADING", Active: "ACTIVE", Unloading: "UNLOADING" } as any)[this.theta]; }
}
export class Ctx {
  constructor(private w: World, private f: Fiber) {}
  get name() { return this.f.name; }
  get(key: string): any { return this.w._read(this.f, key); }
}
export const notify = (before: boolean, after: boolean) => (!before && after ? "activating" : before && !after ? "deactivating" : "neutral");
const sameView = (a: Record<string, string> | null, b: Record<string, string> | null) => (a === null || b === null ? a === b : stable(a) === stable(b));

export class World {
  keyTypes: Record<string, KeyType>; fibers = new Map<string, Fiber>(); trace: any[][] = []; violations: any[] = []; escalations: any[] = []; pendingExternal = new Map<string, External>(); private n = 0;
  notTotal: string[] = []; requireTotal: boolean; review: ((c: Component) => boolean) | null;
  constructor(keyTypes: Record<string, KeyType> = {}, o: { requireTotal?: boolean; review?: (c: Component) => boolean } = {}) { this.keyTypes = { ...keyTypes }; this.requireTotal = o.requireTotal ?? false; this.review = o.review ?? null; }
  // ---- derived state (eq. 46, Def. 53, 54)
  sigma(): Map<string, [string, any]> { const out = new Map<string, [string, any]>(); for (const [n, f] of this.fibers) if (f.theta === "Active") for (const [k, v] of Object.entries(f.sigma)) out.set(k, [n, v]); return out; }
  satisfied(requires: Record<string, string>, sig?: Map<string, [string, any]>): boolean {
    sig = sig ?? this.sigma(); return Object.entries(requires).every(([k, rng]) => sig!.has(k) && versionSatisfies(this.fibers.get(sig!.get(k)![0])!.comp.provides[k], rng));
  }
  target(n: string): Record<string, string> | null {
    const f = this.fibers.get(n)!; if (f.retired || f.failed) return null; const sig = this.sigma();
    if (!this.satisfied(f.comp.requires, sig)) return null;
    const out: Record<string, string> = {}; for (const k of Object.keys(f.comp.requires).sort()) out[k] = sig.get(k)![0]; return out;
  }
  relied(n: string): boolean { for (const [m, g] of this.fibers) if (m !== n && g.installed() && g.omega !== null && Object.values(g.omega).includes(n)) return true; return false; }
  quiet(): boolean {
    for (const [n, f] of this.fibers) { const t = this.target(n);
      if (f.theta === "Inactive") { if (t !== null) return false; } else if (f.theta === "Active") { if (!sameView(t, f.omega)) return false; } else return false; }
    return true;
  }
  projection(keys?: Set<string>): Record<string, any> { const out: Record<string, any> = {}; for (const f of this.fibers.values()) for (const [k, v] of Object.entries(f.sigma)) if (!keys || keys.has(k)) out[k] = v; return out; }
  equivalent(a: Record<string, any>, b: Record<string, any>, keys?: Set<string>): boolean {
    const ka = Object.keys(a).filter((k) => !keys || keys.has(k)).sort(), kb = Object.keys(b).filter((k) => !keys || keys.has(k)).sort();
    return stable(ka) === stable(kb) && ka.every((k) => (this.keyTypes[k]?.equiv ?? ((x: any, y: any) => stable(x) === stable(y)))(a[k], b[k]));
  }
  // ---- orchestration rules
  private log(...e: any[]) { this.trace.push(e); }
  insert(comp: Component, parent = "root", name?: string): string {
    if (parent !== "root" && !this.fibers.has(parent)) throw new PreconditionFailed(`O-Insert: parent '${parent}' absent`);
    if (this.review !== null && !this.review(comp)) { this.log("review_denied", comp.id); throw new PreconditionFailed(`O-Insert: review denied for '${comp.id}'`); }   // load-time review of the declared capabilities (paper §6.3)
    for (const [m, g] of this.fibers) { const clash = [...comp.p].filter((k) => g.comp.p.has(k)); if (clash.length) throw new PreconditionFailed(`O-Insert: provision [${clash}] overlaps fiber '${m}'`); }
    this.n += 1; const n = name ?? `f${this.n}`; if (this.fibers.has(n)) throw new PreconditionFailed(`O-Insert: name '${n}' in use`);
    this.fibers.set(n, new Fiber(n, comp, parent)); this.log("insert", n, comp.id); return n;
  }
  retire(n: string) { if (!this.fibers.has(n)) throw new PreconditionFailed(`O-Retire: '${n}' absent`); const before = this.satSnapshot(); this.fibers.get(n)!.retired = true; this.log("retire", n); this.notifyChanges(before); }
  removable(n: string): boolean { const f = this.fibers.get(n); return !!f && f.retired && f.theta === "Inactive" && Object.keys(f.sigma).length === 0 && ![...this.fibers.values()].some((g) => g.parent === n); }
  remove(n: string) { if (!this.removable(n)) throw new PreconditionFailed(`O-Remove: '${n}' is not removable`); this.fibers.delete(n); this.log("remove", n); }
  sweep(): string[] { const removed: string[] = []; let progress = true; while (progress) { progress = false; for (const n of [...this.fibers.keys()]) if (this.removable(n)) { this.remove(n); removed.push(n); progress = true; } } return removed; }
  // ---- reactivity (Def. 22)
  private satSnapshot(): Map<string, boolean> { const sig = this.sigma(), out = new Map<string, boolean>(); for (const [n, f] of this.fibers) out.set(n, this.satisfied(f.comp.requires, sig)); return out; }
  private notifyChanges(before: Map<string, boolean>) { const after = this.satSnapshot(); for (const n of this.fibers.keys()) if (before.has(n)) { const v = notify(before.get(n)!, after.get(n)!); if (v !== "neutral") this.log("notify", n, v); } }
  // ---- lifecycle rules
  enabled(): [string, string][] {
    const out: [string, string][] = [];
    for (const [n, f] of this.fibers) { const t = this.target(n);
      if (f.theta === "Inactive" && t !== null) out.push(["begin", n]);
      else if (f.theta === "Reloading") out.push([sameView(t, f.omega) ? "iter" : "divert", n]);
      else if (f.theta === "Active" && !sameView(t, f.omega)) out.push(["leave", n]);
      else if (f.theta === "Unloading" && !f.escalated && !this.relied(n)) out.push(["unload", n]); }
    return out;
  }
  step(rule: string, n: string) {
    if (!this.enabled().some(([r, m]) => r === rule && m === n)) throw new PreconditionFailed(`rule ${rule} is not enabled at '${n}'`);
    const before = this.satSnapshot(), f = this.fibers.get(n)!;
    if (rule === "begin") {
      f.theta = "Reloading"; f.omega = this.target(n); f.acc = []; f.started = false; f.pendingOut = null; f.pendingExc = null; this.log("begin", n);
      try { f.gen = this.asGen(f.comp.effects(new Ctx(this, f))); }
      catch (e: any) { this.violations.push({ fiber: n, code: e.code ?? "component.error", detail: String(e.message ?? e) }); f.failed = true; this.log("error", n, e.code ?? "component.error"); this.abortToUnloading(f, "divert"); }   // raised before yielding (paper §4.4, Failure)
    }
    else if (rule === "iter") this.iter(f);
    else if (rule === "divert") this.abortToUnloading(f, "divert");
    else if (rule === "leave") { f.theta = "Unloading"; this.log("leave", n); }
    else if (rule === "unload") this.unload(f);
    this.notifyChanges(before);
  }
  private asGen(x: any): Generator<any, any, any> { if (x && typeof x.next === "function" && typeof x.throw === "function") return x; return (function* () { for (const s of x ?? []) yield s; })(); }
  private abortToUnloading(f: Fiber, why: string) { if (f.gen) { try { f.gen.return(undefined); } catch { /* closing a generator that throws is ignored */ } } f.gen = null; f.theta = "Unloading"; this.log(why, f.name); }
  private iter(f: Fiber) {
    let r: IteratorResult<any, any>;
    try {
      if (f.pendingExc !== null) { const exc = f.pendingExc; f.pendingExc = null; r = f.gen!.throw(exc); }
      else if (!f.started) { f.started = true; r = f.gen!.next(); }
      else r = f.gen!.next(f.pendingOut);
    } catch (e: any) {
      if (!e.recorded) this.violations.push({ fiber: f.name, code: e.code ?? "component.error", detail: String(e.message ?? e) });
      f.failed = true; this.log("error", f.name, e.code ?? "component.error"); this.abortToUnloading(f, "divert"); return;
    }
    if (r.done) {
      const own = Object.keys(f.sigma).sort(), declared = [...f.comp.p].sort();
      if (JSON.stringify(own) !== JSON.stringify(declared)) {                                         // Definition 76: a finished activation has installed every key of p
        this.notTotal.push(f.name); this.log("not_total", f.name);
        if (this.requireTotal) { this.violations.push({ fiber: f.name, code: "component.not_total", detail: `installed [${own}] of [${declared}]` }); f.failed = true; this.log("error", f.name, "component.not_total"); this.abortToUnloading(f, "divert"); return; }
      }
      f.theta = "Active"; f.gen = null; this.log("finish", f.name); return;
    }
    const st: Stage = r.value;
    try { f.pendingOut = this.exec(f, st); f.pendingExc = null; }
    catch (e: any) {
      if (!(e instanceof ComponentError)) throw e;
      this.violations.push({ fiber: f.name, code: e.code, detail: e.message }); this.log("violation", f.name, e.code); e.recorded = true; f.pendingExc = e; f.pendingOut = null; return;
    }
    this.log("iter", f.name, st.kind);
  }
  // ---- stages (effect functions with witnessed inverses)
  _read(f: Fiber, key: string): any {
    if (!f.comp.declared.has(key)) { this.violations.push({ fiber: f.name, code: ConfinementViolation.prototype.code ?? "component.confinement_violation", detail: `read of undeclared key '${key}'` }); const e = new ConfinementViolation(`${f.name} reads undeclared key '${key}'`); e.recorded = true; throw e; }
    if (key in f.sigma) return f.sigma[key];
    const prov = f.omega?.[key]; if (prov === undefined || !(key in this.fibers.get(prov)!.sigma)) throw new InactiveAccess(`'${key}' is declared by ${f.name} but not committed`);
    return this.fibers.get(prov)!.sigma[key];
  }
  private exec(f: Fiber, st: Stage): any {
    if (st instanceof Provide) {
      if (!f.comp.p.has(st.key)) throw new ConfinementViolation(`${f.name} provides undeclared key '${st.key}'`);
      if (st.key in f.sigma) throw new PreconditionFailed(`'${st.key}' is already provided by ${f.name}`);
      f.sigma[st.key] = st.value; const n = f.name, k = st.key; f.acc.push(["state", (w) => { delete w.fibers.get(n)!.sigma[k]; }]); return null;
    }
    if (st instanceof Operate) {
      if (!f.comp.declared.has(st.key)) throw new ConfinementViolation(`${f.name} operates on undeclared key '${st.key}'`);
      const prov = f.comp.p.has(st.key) ? f.name : f.omega?.[st.key];
      if (prov === undefined || prov === null || !(st.key in this.fibers.get(prov)!.sigma)) throw new PreconditionFailed(`'${st.key}' is not bound for ${f.name}`);
      const kt = this.keyTypes[st.key]; if (!kt || !(st.op in kt.ops)) throw new ConfinementViolation(`'${st.key}' offers no operation '${st.op}'`);
      let res: [any, (v: any) => any, any];
      try { res = kt.ops[st.op](this.fibers.get(prov)!.sigma[st.key], st.arg); } catch (e: any) { if (e.name === "KeyError") throw new PreconditionFailed(e.message); throw e; }
      const [nv, inv, outcome] = res, k = st.key; this.fibers.get(prov)!.sigma[k] = nv;
      f.acc.push(["state", (w) => { const pf = w.fibers.get(prov); if (pf && k in pf.sigma) pf.sigma[k] = inv(pf.sigma[k]); }]); return outcome;
    }
    if (st instanceof Instantiate) { const child = this.insert(st.component, f.name); f.acc.push(["state", (w) => { if (w.fibers.has(child)) w.retire(child); }]); return child; }
    if (st instanceof External) {
      if (st.compensate === null) { const eid = `${f.name}:${st.label}:${this.pendingExternal.size}`; this.pendingExternal.set(eid, st); return { withheld: eid }; }
      const out = st.action(), c = st.compensate; f.acc.push(["compensation", () => c()]); return out;
    }
    throw new ConfinementViolation(`unknown stage`);
  }
  /** An effect performed by a fiber after its iterator has finished (Cordis `ctx.effect` at any time): its inverse joins the accumulator, so it is reverted with everything else. */
  perform(n: string, stage: Stage): any {
    const f = this.fibers.get(n)!; if (f.theta !== "Reloading" && f.theta !== "Active") throw new PreconditionFailed(`'${n}' is ${f.state}; it cannot perform effects`);
    let out: any;
    try { out = this.exec(f, stage); } catch (e: any) { if (e instanceof ComponentError) { this.violations.push({ fiber: n, code: e.code, detail: e.message }); this.log("violation", n, e.code); } throw e; }
    this.log("perform", n, stage.kind); return out;
  }
  /** A cycle leaves its members permanently inactive; it is predictable from the declarations alone, so it is reported at load time (paper §6.5). */
  dependencyCycles(): string[][] {
    const prov = new Map<string, string>(); for (const [n, f] of this.fibers) for (const k of f.comp.p) prov.set(k, n);
    const edges = new Map<string, string[]>(); for (const [n, f] of this.fibers) edges.set(n, [...new Set(Object.keys(f.comp.requires).filter((k) => prov.has(k)).map((k) => prov.get(k)!))].sort());
    const out: string[][] = [], state = new Map<string, number>();
    const dfs = (n: string, path: string[]) => { state.set(n, 1); for (const m of edges.get(n)!) { if (state.get(m) === 1) out.push(path.includes(m) ? [...path.slice(path.indexOf(m)), m] : [n, m]); else if (!state.has(m)) dfs(m, [...path, m]); } state.set(n, 2); };
    for (const n of edges.keys()) if (!state.has(n)) dfs(n, [n]);
    return out;
  }
  approve(eid: string): any { const e = this.pendingExternal.get(eid)!; this.pendingExternal.delete(eid); return e.action(); }
  private unload(f: Fiber) {
    while (f.acc.length) {
      const [kind, fn] = f.acc[f.acc.length - 1];
      try { fn(this); } catch (e: any) { f.escalated = f.failed = true; this.escalations.push({ fiber: f.name, kind, detail: String(e.message ?? e) }); this.log("escalate", f.name); return; }
      f.acc.pop();
    }
    f.theta = "Inactive"; f.omega = null; f.gen = null; this.log("unload", f.name);
  }
  resolveEscalation(n: string) { const f = this.fibers.get(n)!; if (f.acc.length && f.escalated) f.acc.pop(); f.escalated = false; }
  // ---- scheduling
  settle(pick?: (n: number) => number, maxSteps = 100000): number {
    let n = 0;
    for (;;) { const en = this.enabled(); if (!en.length) return n; const [rule, name] = en[pick ? pick(en.length) : 0]; this.step(rule, name); if (++n > maxSteps) throw new Error("settle did not terminate"); }
  }
  traceHash(): string { return createHash("sha256").update(JSON.stringify(this.trace)).digest("hex"); }
  // ---- independence (Thm. 47) and ordered revert
  independent(a: Component, b: Component): boolean {
    const Sa = a.declared, Sb = b.declared;
    if ([...a.p].some((k) => Sb.has(k)) || [...b.p].some((k) => Sa.has(k))) return false;
    return [...Sa].filter((k) => Sb.has(k)).every((k) => this.keyTypes[k]?.commutative === true);
  }
  planRevert(n: string) {
    const names = [...this.fibers.keys()], idx = names.indexOf(n), later = names.slice(idx + 1).filter((m) => this.fibers.get(m)!.installed() && !this.fibers.get(m)!.retired);
    const dep = later.filter((m) => !this.independent(this.fibers.get(n)!.comp, this.fibers.get(m)!.comp));
    return { fiber: n, independent: dep.length === 0, dependents: [...dep].reverse() };
  }
  revert(n: string, cascade = false) {
    const plan = this.planRevert(n);
    if (!plan.independent && !cascade) { this.log("revert_blocked", n); return { ...plan, status: "revert.blocked" }; }
    for (const m of plan.dependents) this.retire(m);
    this.retire(n); this.settle(); this.sweep(); return { ...plan, status: "reverted" };
  }
}
