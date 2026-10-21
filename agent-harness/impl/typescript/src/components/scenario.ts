/** Deterministic random scenarios, identical to the Python generator: the property tests and the cross-implementation golden vector. */
import { Xoshiro256SS } from "../stats.js";
import { Component, Instantiate, Operate, Provide, World } from "./core.js";
import { Monitor, replayStatic, signature, signatureByComponent } from "./invariants.js";
import { tableKey } from "./keys.js";
/** Python's json.dumps(obj, sort_keys=True): ', ' and ': ' separators, keys sorted. */
export function pyJson(v: any): string {
  if (v === null || v === undefined) return "null"; if (typeof v === "string") return JSON.stringify(v); if (typeof v === "number" || typeof v === "boolean") return String(v);
  if (Array.isArray(v)) return "[" + v.map(pyJson).join(", ") + "]";
  return "{" + Object.keys(v).sort().map((k) => JSON.stringify(k) + ": " + pyJson(v[k])).join(", ") + "}";
}
export function buildComponents(rng: Xoshiro256SS, n: number, nKeys: number): Component[] {
  const owner = new Map<number, number>();
  for (let k = 0; k < nKeys; k++) { const o = rng.index(n + 1); if (o < n) owner.set(k, o); }
  const comps: Component[] = [];
  for (let i = 0; i < n; i++) {
    const prov = [...owner].filter(([, o]) => o === i).map(([k]) => k).sort((a, b) => a - b);
    const req = [...owner].filter(([, o]) => o < i).map(([k]) => k).sort((a, b) => a - b).filter(() => rng.index(2) === 1);
    comps.push(new Component(`c${i}`, function* () { for (const k of prov) yield new Provide(`ns/k${k}`, {}); for (const k of req) yield new Operate(`ns/k${k}`, "add", [`c${i}.k${k}`, i]); },
      { requires: Object.fromEntries(req.map((k) => [`ns/k${k}`, "*"])), provides: Object.fromEntries(prov.map((k) => [`ns/k${k}`, "1.0.0"])) }));
  }
  return comps;
}
export const newWorld = (nKeys: number) => new World(Object.fromEntries(Array.from({ length: nKeys }, (_, k) => [`ns/k${k}`, tableKey()])));
export function guardInvariant(w: World): boolean {
  for (const m of w.fibers.values()) if (m.installed() && m.omega) for (const p of Object.values(m.omega)) if (w.fibers.has(p) && w.fibers.get(p)!.theta === "Inactive") return false;
  return true;
}
const keep = (names: Map<number, string>, w: World) => new Map([...names].filter(([, nm]) => w.fibers.has(nm)));
const settleRandom = (w: World, mon: Monitor, rng: Xoshiro256SS) => { for (;;) { const en = w.enabled(); if (!en.length) return; const [r, nm] = en[rng.index(en.length)]; mon.step(r, nm); } };
export function runScenario(seed: number, check = true) {
  const rng = new Xoshiro256SS(BigInt(seed)), n = 3 + rng.index(4), nKeys = 2 + rng.index(3), comps = buildComponents(rng, n, nKeys);
  const K = Math.max(...comps.map((c) => Object.keys(c.provides).length + Object.keys(c.requires).length + 1)), w = newWorld(nKeys), mon = new Monitor(w, K);
  let names = new Map<number, string>();
  const steps = 12 + rng.index(20);
  for (let s = 0; s < steps; s++) {
    const op = rng.index(6), free = Array.from({ length: n }, (_, i) => i).filter((i) => !names.has(i)), live = [...names.keys()].sort((a, b) => a - b).filter((i) => !w.fibers.get(names.get(i)!)!.retired);
    if (op <= 1 && free.length) { const i = free[rng.index(free.length)]; names.set(i, mon.insert(comps[i])); }
    else if (op === 2 && live.length) mon.retire(names.get(live[rng.index(live.length)])!);
    else if (op === 3) { mon.sweep(); names = keep(names, w); }
    else { const en = w.enabled(); if (en.length) { const [r, nm] = en[rng.index(en.length)]; mon.step(r, nm); } }
  }
  settleRandom(w, mon, rng); names = keep(names, w);
  if (!(w.quiet() && guardInvariant(w))) throw new Error(`not quiescent (seed ${seed})`);
  const mid = snap(w);
  if (check) { const ref = replayStatic(mon.ops, new Map(comps.map((c) => [c.id, c])), () => newWorld(nKeys)); if (JSON.stringify(signature(w, false)) !== JSON.stringify(signature(ref, false))) throw new Error(`dynamic run and static assembly disagree (seed ${seed})`); }
  for (const i of [...names.keys()].sort((a, b) => a - b)) if (Object.keys(comps[i].requires).length) mon.retire(names.get(i)!);
  settleRandom(w, mon, rng);
  const residual: Record<string, any> = {}; for (const f of w.fibers.values()) if (!Object.keys(f.comp.requires).length) for (const [k, v] of Object.entries(f.sigma)) if (v && Object.keys(v).length) residual[k] = v;
  if (check && Object.keys(residual).length) throw new Error(`residual entries after reverting all consumers (seed ${seed})`);
  for (const i of [...names.keys()].sort((a, b) => a - b)) mon.retire(names.get(i)!);
  settleRandom(w, mon, rng); mon.sweep();
  if (check) { if (w.fibers.size) throw new Error(`fibers left after retiring everything (seed ${seed})`); mon.finish(); }
  return { seed, trace_hash: w.traceHash(), mid, steps: w.trace.length, violations: w.violations.length };
}
function snap(w: World) { return [...w.fibers].map(([n, f]) => [n, f.theta, f.retired, pyJson(f.sigma)] as [string, string, boolean, string]).sort((a, b) => (a[0] < b[0] ? -1 : a[0] > b[0] ? 1 : 0)); }
export function buildTreeComponents(rng: Xoshiro256SS, n: number, nKeys: number): Component[] {
  const owner = new Map<number, number>();
  for (let k = 0; k < nKeys; k++) { const o = rng.index(n + 1); if (o < n) owner.set(k, o); }
  const comps: Component[] = [];
  for (let i = 0; i < n; i++) {
    const prov = [...owner].filter(([, o]) => o === i).map(([k]) => k).sort((a, b) => a - b);
    const req = [...owner].filter(([, o]) => o < i).map(([k]) => k).sort((a, b) => a - b).filter(() => rng.index(2) === 1);
    const creq = [...owner].filter(([, o]) => o <= i).map(([k]) => k).sort((a, b) => a - b).filter(() => rng.index(2) === 1);
    const child = new Component(`c${i}.child`, function* () { for (const k of creq) yield new Operate(`ns/k${k}`, "add", [`c${i}.child.k${k}`, i]); }, { requires: Object.fromEntries(creq.map((k) => [`ns/k${k}`, "*"])) });
    comps.push(new Component(`c${i}`, function* () { for (const k of prov) yield new Provide(`ns/k${k}`, {}); for (const k of req) yield new Operate(`ns/k${k}`, "add", [`c${i}.k${k}`, i]); yield new Instantiate(child); },
      { requires: Object.fromEntries(req.map((k) => [`ns/k${k}`, "*"])), provides: Object.fromEntries(prov.map((k) => [`ns/k${k}`, "1.0.0"])) }));
  }
  return comps;
}
export function runTreeScenario(seed: number) {
  const rng = new Xoshiro256SS(BigInt(seed)), n = 3 + rng.index(3), nKeys = 2 + rng.index(3), comps = buildTreeComponents(rng, n, nKeys);
  const K = Math.max(...comps.map((c) => Object.keys(c.provides).length + Object.keys(c.requires).length + 2)) + nKeys + 1, w = newWorld(nKeys), mon = new Monitor(w, K);
  let names = new Map<number, string>(), counter = 0; const steps = 10 + rng.index(16);
  for (let s = 0; s < steps; s++) {
    const op = rng.index(6), free = Array.from({ length: n }, (_, i) => i).filter((i) => !names.has(i)), live = [...names.keys()].sort((a, b) => a - b).filter((i) => !w.fibers.get(names.get(i)!)!.retired);
    if (op <= 1 && free.length) { const i = free[rng.index(free.length)]; counter++; names.set(i, mon.insert(comps[i], `o${counter}`)); }
    else if (op === 2 && live.length) mon.retire(names.get(live[rng.index(live.length)])!);
    else if (op === 3) { mon.sweep(); names = keep(names, w); }
    else { const en = w.enabled(); if (en.length) { const [r, nm] = en[rng.index(en.length)]; mon.step(r, nm); } }
  }
  settleRandom(w, mon, rng);
  if (!w.quiet()) throw new Error(`not quiescent (seed ${seed})`);
  const ref = replayStatic(mon.ops, new Map(comps.map((c) => [c.id, c])), () => newWorld(nKeys));
  if (JSON.stringify(signatureByComponent(w)) !== JSON.stringify(signatureByComponent(ref))) throw new Error(`dynamic and static assembly disagree (seed ${seed})`);
  for (const i of [...names.keys()].sort((a, b) => a - b)) if (w.fibers.has(names.get(i)!)) mon.retire(names.get(i)!);
  settleRandom(w, mon, rng); mon.sweep();
  if (w.fibers.size) throw new Error(`fibers left after retiring everything (seed ${seed})`);
  mon.finish(); return { seed, trace_hash: w.traceHash(), steps: w.trace.length };
}
export function runReplacementScenario(seed: number) {
  const rng = new Xoshiro256SS(BigInt(seed)), n = 3 + rng.index(4), nKeys = 2 + rng.index(3), comps = buildComponents(rng, n, nKeys), w = newWorld(nKeys), mon = new Monitor(w, Math.max(...comps.map((c) => Object.keys(c.provides).length + Object.keys(c.requires).length + 1)));
  let live = new Map<number, string>(), counter = 0;
  const partial = (k: number) => { for (let i = 0; i < k; i++) { const en = w.enabled(); if (en.length) { const [r, nm] = en[rng.index(en.length)]; mon.step(r, nm); } } };
  const order = Array.from({ length: n }, (_, i) => i);
  for (let j = n - 1; j > 0; j--) { const k = rng.index(j + 1); [order[j], order[k]] = [order[k], order[j]]; }
  for (const i of order) { counter++; live.set(i, mon.insert(comps[i], `o${counter}`)); partial(rng.index(4)); }
  settleRandom(w, mon, rng);
  for (let rep = 2 + rng.index(3); rep > 0; rep--) {
    const cands = [...live.keys()].sort((a, b) => a - b).filter((i) => Object.keys(comps[i].provides).length); if (!cands.length) break;
    const i = cands[rng.index(cands.length)]; mon.retire(live.get(i)!); partial(rng.index(5)); settleRandom(w, mon, rng); mon.sweep();
    live = new Map([...live].filter(([, b]) => w.fibers.has(b))); counter++; live.set(i, mon.insert(comps[i], `o${counter}`)); partial(rng.index(4));
  }
  settleRandom(w, mon, rng);
  if (!w.quiet()) throw new Error(`not quiescent (seed ${seed})`);
  const ref = replayStatic(mon.ops, new Map(comps.map((c) => [c.id, c])), () => newWorld(nKeys));
  if (JSON.stringify(signature(w)) !== JSON.stringify(signature(ref))) throw new Error(`after provider replacement the dynamic state differs from the static assembly (seed ${seed})`);
  mon.finish(); return { seed, trace_hash: w.traceHash(), steps: w.trace.length };
}
export function confluenceSignature(seed: number, scheduleSeed: number) {
  const rng = new Xoshiro256SS(BigInt(seed)), n = 3 + rng.index(4), nKeys = 2 + rng.index(3), comps = buildComponents(rng, n, nKeys), w = newWorld(nKeys), sched = new Xoshiro256SS(BigInt(scheduleSeed));
  const names = new Map<number, string>(), order = Array.from({ length: n }, (_, i) => i);
  for (let j = n - 1; j > 0; j--) { const k = rng.index(j + 1); [order[j], order[k]] = [order[k], order[j]]; }
  const retireSet = order.filter(() => rng.index(3) === 0);
  const some = () => { for (let t = sched.index(4); t > 0; t--) { const en = w.enabled(); if (en.length) { const [r, nm] = en[sched.index(en.length)]; w.step(r, nm); } } };
  for (const i of order) { names.set(i, w.insert(comps[i])); some(); }
  for (const i of retireSet) { w.retire(names.get(i)!); some(); }
  w.settle((m) => sched.index(m));
  return [...w.fibers.values()].map((f) => [f.comp.id, f.theta, f.retired, pyJson(f.sigma)] as [string, string, boolean, string]).sort((a, b) => (a[0] < b[0] ? -1 : a[0] > b[0] ? 1 : 0));
}
