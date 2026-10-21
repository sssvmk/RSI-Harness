/** Declared dependencies, the registry, the declarative reconciler, run pinning and live replacement (PRD §3.9). */
import { Component, ComponentError, KEY_RE, World, versionSatisfies } from "./core.js";
export class RegistrationError extends ComponentError { code = "component.registration_rejected"; }
export class ComponentRegistry {
  components = new Map<string, Component>();
  register(comp: Component) {
    if (this.components.has(comp.id)) throw new RegistrationError(`duplicate component id '${comp.id}'`);
    for (const k of [...Object.keys(comp.requires), ...Object.keys(comp.provides)]) if (!KEY_RE.test(k)) throw new RegistrationError(`key '${k}' is not namespaced as '<namespace>/<name>'`);
    for (const [k, rng] of Object.entries(comp.requires)) { try { versionSatisfies("0.0.0", rng); } catch (e: any) { throw new RegistrationError(`bad version range for '${k}': ${e.message}`); } }
    for (const o of this.components.values()) { const clash = Object.keys(comp.provides).filter((k) => k in o.provides); if (clash.length && !(comp.isolated && o.isolated)) throw new RegistrationError(`provisions of '${comp.id}' and '${o.id}' overlap on [${clash}] (provisions are disjoint unless isolated)`); }
    const cyc = ComponentRegistry.cycle(new Map([...this.components, [comp.id, comp]])); if (cyc) throw new RegistrationError("dependency cycle: " + cyc.join(" -> "));
    this.components.set(comp.id, comp);
  }
  static cycle(comps: Map<string, Component>): string[] | null {
    const provider = new Map<string, string>(); for (const c of comps.values()) for (const k of Object.keys(c.provides)) provider.set(k, c.id);
    const edges = new Map<string, string[]>(); for (const c of comps.values()) edges.set(c.id, [...new Set(Object.keys(c.requires).filter((k) => provider.has(k)).map((k) => provider.get(k)!))].sort());
    const state = new Map<string, number>();
    const dfs = (n: string, path: string[]): string[] | null => {
      state.set(n, 1);
      for (const m of edges.get(n)!) { if (state.get(m) === 1) return path.includes(m) ? [...path.slice(path.indexOf(m)), m] : [n, m]; if (!state.has(m)) { const r = dfs(m, [...path, m]); if (r) return r; } }
      state.set(n, 2); return null;
    };
    for (const n of [...edges.keys()].sort()) if (!state.has(n)) { const r = dfs(n, [n]); if (r) return r; }
    return null;
  }
  checkDependencies(ids: string[]): string[] {
    const comps = ids.map((i) => this.components.get(i)!), have: Record<string, string> = {}; for (const c of comps) Object.assign(have, c.provides);
    return comps.flatMap((c) => Object.entries(c.requires).filter(([k, rng]) => !(k in have) || !versionSatisfies(have[k], rng)).map(([k]) => `${c.id}:${k}`)).sort();
  }
}
export class Reconciler {
  byId = new Map<string, [string, string]>();
  constructor(public world: World) {}
  plan(desired: Record<string, Component>): [string, string][] {
    const ops: [string, string][] = [];
    for (const sid of [...this.byId.keys()].sort()) if (!(sid in desired)) ops.push(["retire", sid]);
    for (const sid of Object.keys(desired).sort()) { if (!this.byId.has(sid)) ops.push(["insert", sid]); else if (this.byId.get(sid)![1] !== desired[sid].configHash()) ops.push(["update", sid]); }
    return ops;
  }
  private retireAndRemove(sid: string) { const name = this.byId.get(sid)![0]; this.byId.delete(sid); this.world.retire(name); this.world.settle(); this.world.sweep(); }
  apply(desired: Record<string, Component>): [string, string][] {
    const ops = this.plan(desired);
    for (const [op, sid] of ops) if (op === "retire" || op === "update") this.retireAndRemove(sid);
    for (const [op, sid] of ops) if (op === "insert" || op === "update") this.byId.set(sid, [this.world.insert(desired[sid]), desired[sid].configHash()]);
    this.world.settle(); return ops;
  }
}
export class RunPins {
  runs = new Map<string, { pinned: Record<string, string>; followLive: boolean }>();
  constructor(public rec: Reconciler) {}
  snapshot(): Record<string, string> { const o: Record<string, string> = {}; for (const sid of [...this.rec.byId.keys()].sort()) o[sid] = this.rec.byId.get(sid)![1]; return o; }
  open(runId: string, followLive = false) { this.runs.set(runId, { pinned: this.snapshot(), followLive }); }
  configFor(runId: string): Record<string, string> { const r = this.runs.get(runId)!; return r.followLive ? this.snapshot() : { ...r.pinned }; }
}

// ---------------------------------------------------------------------------------------------------------------------------- declarative entries (paper §5.2.1)
import { Instantiate } from "./core.js";
import { Scope } from "./scope.js";
/** Definition 81. `id` is the stable reconciliation key; `url` identifies the module (for HMR); `factory(config)` builds the component; `children` makes the entry a group. */
export class Entry {
  url: string; isolate: Record<string, any>; intercept: Record<string, any>;
  constructor(public id: string, public factory: ((config: any) => Component) | null = null, o: { url?: string; config?: any; disabled?: boolean; isolate?: Record<string, any>; intercept?: Record<string, any>; children?: Entry[] | null; onConfig?: ((o: any, n: any) => boolean) | null } = {}) {
    this.url = o.url ?? id; this.config = o.config ?? null; this.disabled = o.disabled ?? false; this.isolate = { ...(o.isolate ?? {}) }; this.intercept = { ...(o.intercept ?? {}) }; this.children = o.children ?? null; this.onConfig = o.onConfig ?? null;
  }
  config: any; disabled: boolean; children: Entry[] | null; onConfig: ((o: any, n: any) => boolean) | null;
}
const same = (a: any, b: any) => JSON.stringify(a) === JSON.stringify(b);
export class Loader {
  live = new Map<string, { entry: Entry; fiber: string | null; scope: Scope; parent: string | null }>(); ops: [string, string][] = []; scope: Scope;
  constructor(public w: World, scope?: Scope) { this.scope = scope ?? new Scope(w); }
  private componentFor(e: Entry): Component {
    if (e.children === null) return e.factory!(e.config);
    const loader = this;
    return new Component(`group:${e.id}`, function* (ctx: any) {
      for (const child of [...(loader.live.get(e.id)!.entry.children ?? [])]) {
        if (child.disabled) continue;
        const name: string = yield new Instantiate(loader.componentFor(child));
        loader.live.set(child.id, { entry: child, fiber: name, scope: loader.scopeFor(child), parent: ctx.name });
      }
    });
  }
  private scopeFor(e: Entry): Scope { let sc = this.scope; for (const [k, r] of Object.entries(e.isolate)) sc = sc.isolate(k, r === true ? e.id : r); for (const [k, m] of Object.entries(e.intercept)) sc = sc.intercept(k, m); return sc; }
  insert(e: Entry, parent: string | null) {
    const rec = { entry: e, fiber: null as string | null, scope: this.scopeFor(e), parent }; this.live.set(e.id, rec); if (e.disabled) return;
    const comp = this.componentFor(e); rec.fiber = parent === null ? this.w.insert(comp) : this.w.perform(parent, new Instantiate(comp));
  }
  dispose(eid: string) {
    const rec = this.live.get(eid); if (!rec) return; this.live.delete(eid);
    for (const [c, r] of [...this.live]) if (rec.fiber !== null && r.parent === rec.fiber) this.live.delete(c);
    if (rec.fiber && this.w.fibers.has(rec.fiber)) { this.w.retire(rec.fiber); this.w.settle(); this.w.sweep(); }
  }
  apply(entries: Entry[], parent: string | null = null): [string, string][] {
    const ops: [string, string][] = [], want = new Map(entries.map((e) => [e.id, e])), current = new Set([...this.live].filter(([, r]) => r.parent === parent).map(([i]) => i));
    for (const eid of [...current].filter((i) => !want.has(i)).sort()) { this.dispose(eid); ops.push(["retire", eid]); }
    for (const e of entries) {
      const rec = current.has(e.id) ? this.live.get(e.id) : undefined;
      if (!rec) { this.insert(e, parent); ops.push([e.disabled ? "declare" : "insert", e.id]); continue; }
      const old = rec.entry;
      if (old.url !== e.url) { this.dispose(e.id); this.insert(e, parent); ops.push(["rebuild", e.id]); continue; }                       // id/url: the component itself changed
      if (!same(old.isolate, e.isolate)) { rec.scope = this.scopeFor(e); ops.push(["isolate", e.id]); }
      else if (!same(old.intercept, e.intercept)) { rec.scope = this.scopeFor(e); ops.push(["intercept", e.id]); }                       // in place: no reload
      if (old.disabled !== e.disabled) {
        if (e.disabled) { this.dispose(e.id); this.live.set(e.id, { entry: e, fiber: null, scope: this.scopeFor(e), parent }); ops.push(["disable", e.id]); }
        else { this.insert(e, parent); ops.push(["enable", e.id]); }                                                                   // a fresh fiber: the entry survives, the enablement is a new identity
        continue;
      }
      if (old.children !== null && e.children !== null) { rec.entry = e; if (rec.fiber !== null && this.w.fibers.get(rec.fiber)!.theta === "Active") ops.push(...this.apply(e.children, rec.fiber)); continue; }   // keyed diff over child ids
      if (!same(old.config, e.config)) {
        if (e.onConfig !== null && e.onConfig(old.config, e.config)) ops.push(["config_in_place", e.id]);
        else { this.dispose(e.id); this.insert(e, parent); ops.push(["reload", e.id]); continue; }
      }
      rec.entry = e;
    }
    if (parent === null) this.w.settle();                                                                                              // Theorem 73: a reconciliation is complete once its operations are issued and the system quiesces
    this.ops.push(...ops); return ops;
  }
  fiberOf(eid: string): string | null { return this.live.get(eid)?.fiber ?? null; }
}
// ---------------------------------------------------------------------------------------------------------------------------- hot module replacement (paper §5.2.2, Algorithms 8-10)
type Imports = (u: string) => Set<string>;
/** Algorithm 8: accepted once one import is accepted, declined once all imports are declined; an import cycle is left undecided and declined. */
export function classify(stashed: Set<string>, externals: Set<string>, getImports: Imports): [Set<string>, Set<string>] {
  const accepted = new Set(stashed), declined = new Set(externals), pending = new Set<string>();
  for (const u of stashed) for (const i of getImports(u)) if (!accepted.has(i) && !declined.has(i)) pending.add(i);
  let progress = true;
  while (progress) {
    progress = false;
    for (const u of [...pending].sort()) {
      const imps = getImports(u);
      if ([...imps].some((i) => accepted.has(i))) { accepted.add(u); pending.delete(u); progress = true; }
      else if ([...imps].every((i) => declined.has(i))) { declined.add(u); pending.delete(u); progress = true; }
      else for (const i of imps) if (!accepted.has(i) && !declined.has(i)) pending.add(i);
    }
  }
  return [accepted, new Set([...declined, ...pending])];
}
export function getDependencies(root: string, declined: Set<string>, getImports: Imports): Set<string> {
  const deps = new Set<string>(); const traverse = (u: string) => { if (deps.has(u) || declined.has(u)) return; deps.add(u); for (const c of [...getImports(u)].sort()) traverse(c); }; traverse(root); return deps;
}
/** Algorithm 9: an entry is stale exactly when its dependency tree intersects `accepted`; that tree is then folded into `accepted`. */
export function detectStale(entries: Entry[], accepted: Set<string>, declined: Set<string>, getImports: Imports): Entry[] {
  const stale: Entry[] = [], acc = new Set(accepted);
  for (const e of entries) { const tree = getDependencies(e.url, declined, getImports); if ([...tree].some((u) => acc.has(u))) { for (const u of tree) acc.add(u); stale.push(e); } }
  return stale;
}
/** Algorithm 10, transactional: if any module fails to import the caches are restored and every stale entry is rebuilt from the backup. */
export function hotReload(loader: Loader, entries: Entry[], modules: Record<string, (c: any) => Component>, stashed: Set<string>, externals: Set<string>, getImports: Imports, importModule: (u: string) => (c: any) => Component): string[] {
  const [accepted, declined] = classify(stashed, externals, getImports), stale = detectStale(entries, accepted, declined, getImports), backup: Record<string, any> = {};
  for (const u of accepted) if (u in modules) { backup[u] = modules[u]; delete modules[u]; }                                              // invalidate
  try {
    for (const e of stale) { loader.dispose(e.id); const fresh = new Entry(e.id, importModule(e.url), { url: e.url, config: e.config, disabled: e.disabled, isolate: e.isolate, intercept: e.intercept }); loader.insert(fresh, null); modules[e.url] = fresh.factory!; }
  } catch (err) {
    Object.assign(modules, backup);                                                                                                    // restore the caches ...
    for (const e of stale) { loader.dispose(e.id); loader.insert(new Entry(e.id, backup[e.url] ?? e.factory, { url: e.url, config: e.config, disabled: e.disabled, isolate: e.isolate, intercept: e.intercept }), null); }   // ... and rebuild from the previous component
    loader.w.settle(); throw err;
  }
  loader.w.settle(); return stale.map((e) => e.id);
}
