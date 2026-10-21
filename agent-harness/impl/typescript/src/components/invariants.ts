/** Checks of the paper's theorems, applied step by step to a running World (arXiv:2608.25512, §4.3). Mirrors invariants.py: executable readings, not proofs. */
import { Component, World, stable } from "./core.js";
export class InvariantViolation extends Error {}
const fail = (m: string): never => { throw new InvariantViolation(m); };

/** Definition 63, clauses 1-4, plus dom(sigma_n) within p_n (Definition 48). */
export function wellFormed(w: World): void {
  const provs = new Map<string, string>();
  for (const [n, f] of w.fibers) {
    if (f.parent !== "root" && !w.fibers.has(f.parent)) fail(`Def63(1): parent of ${n} is '${f.parent}'`);
    for (const k of f.comp.p) { if (provs.has(k)) fail(`Def63(2): ${k} is provided by both ${provs.get(k)} and ${n}`); provs.set(k, n); }
    if (!Object.keys(f.sigma).every((k) => f.comp.p.has(k))) fail(`Def48: sigma_${n} binds keys outside p_${n}`);
    if (f.installed()) {
      const req = Object.keys(f.comp.requires).sort();
      if (f.omega === null || JSON.stringify(Object.keys(f.omega).sort()) !== JSON.stringify(req) || Object.values(f.omega).some((m) => !w.fibers.has(m))) fail(`Def63(3): committed view of ${n} is not total on d_${n} or names an absent fiber`);
      for (const [k, m] of Object.entries(f.omega!)) if (!w.fibers.get(m)!.installed()) fail(`Def63(4): ${n} resolves ${k} to ${m}, which is not installed`);
    }
  }
}
interface Snap { theta: string; retired: boolean; omega: any; sigma: string; parent: string; comp: Component }
const snap = (w: World): Map<string, Snap> => new Map([...w.fibers].map(([n, f]) => [n, { theta: f.theta, retired: f.retired, omega: f.omega ? stable(f.omega) : null, sigma: stable(f.sigma), parent: f.parent, comp: f.comp }]));
const sorted = (a: any[]) => a.sort((x, y) => (JSON.stringify(x) < JSON.stringify(y) ? -1 : JSON.stringify(x) > JSON.stringify(y) ? 1 : 0));
const sj = (v: any): string => stable(v);   // the signature value for a table; both implementations sort keys
export function signature(w: World, ignoreVestigial = true): any[] {
  const out: any[] = [];
  for (const [n, f] of w.fibers) {
    if (ignoreVestigial && f.retired && f.theta === "Inactive" && !Object.keys(f.sigma).length && ![...w.fibers.values()].some((g) => g.parent === n)) continue;
    out.push([n, f.theta, f.retired, sj(f.sigma)]);
  }
  return sorted(out);
}
function vestigialClosure(w: World): Set<string> {
  const v = new Set<string>(); let changed = true;
  while (changed) { changed = false; for (const [n, f] of w.fibers) if (!v.has(n) && f.retired && f.theta === "Inactive" && !Object.keys(f.sigma).length && [...w.fibers].every(([m, g]) => g.parent !== n || v.has(m))) { v.add(n); changed = true; } }
  return v;
}
export function signatureByComponent(w: World): any[] {
  const v = vestigialClosure(w);
  return sorted([...w.fibers].filter(([n]) => !v.has(n)).map(([, f]) => [f.comp.id, f.parent !== "root" ? w.fibers.get(f.parent)!.comp.id : "root", f.theta, f.retired, sj(f.sigma)]));
}
export class Monitor {
  t = 0; S = new Map<string, number>(); V = new Map<string, number>(); tgt = new Map<string, string>(); episodes = new Map<string, [number, number | null][]>();
  consumes: [string, number, string][] = []; ops: any[][] = []; prev: Map<string, Snap>;
  constructor(public w: World, public K: number) { for (const n of w.fibers.keys()) this.tgt.set(n, stable(w.target(n))); this.prev = snap(w); }
  insert(comp: Component, name?: string): string { const n = this.w.insert(comp, "root", name); this.ops.push(["insert", comp.id, n]); this.after("insert", n); return n; }
  retire(n: string) { this.w.retire(n); this.ops.push(["retire", n]); this.after("retire", n); }
  sweep(): string[] {
    const removed: string[] = []; let progress = true;
    while (progress) { progress = false; for (const n of [...this.w.fibers.keys()]) if (this.w.removable(n)) { const root = this.w.fibers.get(n)!.parent === "root"; this.w.remove(n); if (root) this.ops.push(["remove", n]); this.after("remove", n); removed.push(n); progress = true; } }
    return removed;
  }
  step(rule: string, n: string) {
    const w = this.w;
    if (rule === "iter" || rule === "finish") { if (stable(w.target(n)) !== stable(w.fibers.get(n)!.omega)) fail(`Thm71: an iteration of ${n} ran against a resolution that is no longer its target`); }
    if (rule === "unload" && w.relied(n)) fail(`guard: ${n} unloaded while relied upon`);
    w.step(rule, n); this.S.set(n, (this.S.get(n) ?? 0) + 1); this.after(rule, n);
  }
  private after(rule: string, n: string) {
    const w = this.w, prev = this.prev; this.t += 1; const cur = snap(w);
    wellFormed(w);                                                                                           // Theorem 64 (Preservation)
    const actingD = new Set(w.fibers.has(n) ? Object.keys(w.fibers.get(n)!.comp.requires) : prev.has(n) ? Object.keys(prev.get(n)!.comp.requires) : []);
    for (const [m, a] of prev) {
      if (!cur.has(m)) { if (rule !== "remove" || m !== n) fail(`Lemma59: ${m} disappeared at ${rule}(${n})`); continue; }
      const b = cur.get(m)!;
      if (a.parent !== b.parent || a.comp !== b.comp) fail(`Lemma59(5): pi/d/p/e of ${m} changed`);
      if (a.retired && !b.retired) fail(`Lemma59(5): tau of ${m} went back to false`);
      if (!a.retired && b.retired && !((rule === "retire" && m === n) || rule === "unload")) fail(`Lemma59(5): tau of ${m} written by ${rule}(${n})`);
      const ia = a.theta !== "Inactive", ib = b.theta !== "Inactive";
      if (!ia && ib && !(rule === "begin" && m === n)) fail(`Lemma59(4): ${m} became installed by ${rule}(${n})`);
      if (ia && !ib && !(rule === "unload" && m === n)) fail(`Lemma59(4): ${m} stopped being installed by ${rule}(${n})`);
      if (ia && ib && a.omega !== b.omega) fail(`Lemma59(2): committed view of ${m} moved inside an episode`);
      if (a.sigma !== b.sigma) {                                                                              // Lemma 59(1): tables move only inside a state map
        if (["insert", "retire", "remove", "begin", "leave", "divert"].includes(rule)) fail(`Table1: ${rule} moved the table of ${m}`);
        if (m !== n) {
          const sa = JSON.parse(a.sigma), sb = JSON.parse(b.sigma);
          if (JSON.stringify(Object.keys(sa).sort()) !== JSON.stringify(Object.keys(sb).sort())) fail(`Lemma59(1): the domain of sigma_${m} moved at ${rule}(${n})`);
          for (const k of Object.keys(sa)) if (stable(sa[k]) !== stable(sb[k]) && !actingD.has(k)) fail(`Lemma59(1): ${rule}(${n}) changed ${k} in sigma_${m}, outside d_${n}`);
        }
      }
      if (rule === "unload" && m === n && b.sigma !== "{}") fail(`Cor69: sigma_${n} is not empty after unload`);   // terminal recovery
    }
    for (const [m, g] of w.fibers) if (g.installed() && g.omega) for (const [k, p] of Object.entries(g.omega)) if (!(k in w.fibers.get(p)!.sigma)) fail(`Thm70(3): ${k} left the table of ${p} while ${m} still resolves it there`);
    for (const [m, b] of cur) {
      const ia = prev.has(m) && prev.get(m)!.theta !== "Inactive", ib = b.theta !== "Inactive";
      if (!ia && ib) { if (!this.episodes.has(m)) this.episodes.set(m, []); this.episodes.get(m)!.push([this.t, null]); if (b.omega) for (const p of Object.values(JSON.parse(b.omega) as Record<string, string>)) this.consumes.push([m, this.t, p]); }
      if (ia && !ib) this.episodes.get(m)!.at(-1)![1] = this.t;
    }
    for (const m of w.fibers.keys()) { const d = stable(w.target(m)); if (this.tgt.has(m) && this.tgt.get(m) !== d) this.V.set(m, (this.V.get(m) ?? 0) + 1); this.tgt.set(m, d); }
    if (!w.quiet() && !w.enabled().length) fail("Thm73(1): not quiescent, yet no lifecycle rule applies");
    this.prev = cur;
  }
  finish() {
    for (const [m, b2, p] of this.consumes) {                                                                // Theorem 70(2)
      const epC = this.episodes.get(m)!.find((e) => e[0] === b2)!, epP = (this.episodes.get(p) ?? []).filter((e) => e[0] < b2 && (e[1] === null || e[1] >= b2));
      if (!epP.length) fail(`Thm70(2): ${p} had no open episode when ${m} began`);
      if (epP[0][1] !== null && epC[1] === null) fail(`Thm70(2): ${p} closed while ${m} was still installed`);
      if (epP[0][1] !== null && !(epC[1]! < epP[0][1])) fail(`Thm70(2): ${p} closed no later than its consumer ${m}`);
    }
    for (const [n, s] of this.S) { const bound = (this.K + 3) * ((this.V.get(n) ?? 0) + 1); if (s > bound) fail(`Thm73(2): S(${n})=${s} exceeds (K+3)(V+1)=${bound}`); }
  }
}
/** Theorem 80(1): the same orchestration steps, in the same order, all taken before any lifecycle step; then settle. */
export function replayStatic(ops: any[][], comps: Map<string, Component>, newWorld: () => World): World {
  const w = newWorld();
  for (const op of ops) { if (op[0] === "insert") w.insert(comps.get(op[1])!, "root", op[2]); else if (op[0] === "retire") w.retire(op[1]); else w.remove(op[1]); }
  w.settle(); return w;
}
