/** Checks derived from the complete paper (arXiv:2608.25512): the theorems of §4.3, the Cordis implementation (§5) and the discussion (§6). Mirrors test_components_paper.py. */
import fs from "node:fs";
import path from "node:path";
import { describe, expect, it } from "vitest";
import { Component, ConfinementViolation, InactiveAccess, Operate, PreconditionFailed, Provide, World } from "../src/components/core.js";
import { InvariantViolation, wellFormed } from "../src/components/invariants.js";
import { Broker, brokerKey, tableKey } from "../src/components/keys.js";
import { Entry, Loader, classify, detectStale, getDependencies, hotReload } from "../src/components/loader.js";
import { Scope } from "../src/components/scope.js";
import * as scenario from "../src/components/scenario.js";

const VEC = path.resolve(import.meta.dirname, "../../../spec/vectors");
const K = (...names: string[]) => Object.fromEntries(names.map((n) => [n, tableKey()]));
const fails = (fn: (s: number) => any, seeds: number[]) => seeds.filter((s) => { try { fn(s); return false; } catch (e) { return e instanceof Error; } });
const range = (n: number) => Array.from({ length: n }, (_, i) => i);
const stateOf = (w: World) => Object.fromEntries([...w.fibers].map(([n, f]) => [n, f.state]));

describe("the theorems, over random runs", () => {
  it("flat systems satisfy Theorems 64, 68/Cor 69, 70, 71, 73 and 80", () => { for (const s of range(300)) scenario.runScenario(s); });
  it("systems that instantiate children satisfy them up to renaming and vestigial entries", () => { for (const s of range(300)) scenario.runTreeScenario(s); });
  it("replacing providers under running consumers still reaches the static assembly", () => { for (const s of range(200)) scenario.runReplacementScenario(s); });
  it("every theorem check can fail (mutation checks)", () => {
    const seeds = range(120), proto = World.prototype as any;
    expect(fails(scenario.runScenario, seeds)).toEqual([]); expect(fails(scenario.runTreeScenario, seeds)).toEqual([]);
    const mutate = (attr: string, repl: any, fn: (s: number) => any = scenario.runScenario) => { const orig = proto[attr]; proto[attr] = repl; try { return fails(fn, seeds).length > 0; } finally { proto[attr] = orig; } };
    expect(mutate("relied", () => false)).toBe(true);                                                           // no guard: Definition 63(4) / Theorem 70
    const enabled = proto.enabled;
    expect(mutate("enabled", function (this: any) { return enabled.call(this).filter(([r]: any) => r !== "leave"); })).toBe(true);   // a fiber can never leave: Theorem 73(1)
    expect(mutate("enabled", function (this: any) { return enabled.call(this).map(([r, n]: any) => [r === "divert" ? "iter" : r, n]); })).toBe(true);   // an iteration against a stale resolution: Theorem 71
    expect(mutate("unload", function (this: any, f: any) { f.acc.length = 0; f.theta = "Inactive"; f.omega = null; f.gen = null; this.log("unload", f.name); })).toBe(true);   // accumulator never applied: Corollary 69
    const target = proto.target;
    expect(mutate("target", function (this: any, n: string) { const f = this.fibers.get(n); if (f.wasActive && f.theta === "Inactive") return null; if (f.theta === "Active") f.wasActive = true; return target.call(this, n); }, scenario.runReplacementScenario)).toBe(true);   // history-dependent: Theorem 80
  });
  it("well-formedness detects each clause of Definition 63", () => {
    const w = new World(K("ns/k")), p = w.insert(new Component("p", function* () { yield new Provide("ns/k", {}); }, { provides: { "ns/k": "1.0.0" } })), c = w.insert(new Component("c", function* () { yield new Operate("ns/k", "add", ["x", 1]); }, { requires: { "ns/k": "*" } })); w.settle(); wellFormed(w);
    w.fibers.get(c)!.parent = "ghost"; expect(() => wellFormed(w)).toThrow(/Def63\(1\)/);
    w.fibers.get(c)!.parent = "root"; w.fibers.get(p)!.theta = "Inactive"; expect(() => wellFormed(w)).toThrow(/Def63\(4\)/);
    w.fibers.get(p)!.theta = "Active"; w.fibers.get(c)!.omega = {}; expect(() => wellFormed(w)).toThrow(/Def63\(3\)/); expect(InvariantViolation).toBeDefined();
  });
});
describe("runtime additions from the full paper", () => {
  it("totality on provision (Def. 76) is detected and optionally enforced; state names include FAILED", () => {
    const half = () => new Component("half", function* () { yield new Provide("ns/a", {}); }, { provides: { "ns/a": "1.0.0", "ns/b": "1.0.0" } });
    const w = new World(K("ns/a", "ns/b")), n = w.insert(half()); w.settle(); expect([w.fibers.get(n)!.state, w.notTotal]).toEqual(["ACTIVE", [n]]);
    const w2 = new World(K("ns/a", "ns/b"), { requireTotal: true }), n2 = w2.insert(half()); w2.settle(); expect([w2.fibers.get(n2)!.state, Object.keys(w2.fibers.get(n2)!.sigma).length, w2.violations.at(-1).code]).toEqual(["FAILED", 0, "component.not_total"]);
    const bad = new World(), b = bad.insert(new Component("b", () => { throw new Error("boom"); })); bad.settle(); expect([bad.fibers.get(b)!.state, bad.quiet()]).toEqual(["FAILED", true]);   // raised before yielding
    const w3 = new World(K("ns/r")), p = w3.insert(new Component("p", function* () { yield new Provide("ns/r", {}); }, { provides: { "ns/r": "1.0.0" } })), seen = [w3.fibers.get(p)!.state];
    while (w3.enabled().length) { w3.step(...w3.enabled()[0]); seen.push(w3.fibers.get(p)!.state); }
    expect(seen[0]).toBe("INACTIVE"); expect(seen).toContain("LOADING"); expect(seen.at(-1)).toBe("ACTIVE");
  });
  it("access errors distinguish an undeclared key from a declared-but-not-loaded one", () => {
    expect(new ConfinementViolation("x").kind).toBe("UNDECLARED_ACCESS"); expect(new InactiveAccess("x").code).toBe("component.inactive_access"); expect(new InactiveAccess("x")).toBeInstanceOf(PreconditionFailed);
    const seen: string[] = [], w = new World(K("ns/dep", "ns/other"));
    w.insert(new Component("x", (c: any) => (function* (): any { try { c.get("ns/other"); } catch (e: any) { seen.push(e.kind ?? e.code); } yield* []; })(), { requires: { "ns/dep": "*" }, provides: { "ns/own": "1.0.0" } })); w.settle();
    w.insert(new Component("dep", function* () { yield new Provide("ns/dep", {}); }, { provides: { "ns/dep": "1.0.0" } })); w.settle(); expect(seen).toEqual(["UNDECLARED_ACCESS"]);
  });
  it("a fiber can perform effects after activation and they are reverted with the rest", () => {
    const w = new World(K("ns/t")), host = w.insert(new Component("host", function* () { yield new Provide("ns/t", {}); }, { provides: { "ns/t": "1.0.0" } })), user = w.insert(new Component("user", function* () { yield new Operate("ns/t", "add", ["a", 1]); }, { requires: { "ns/t": "*" } })); w.settle();
    w.perform(user, new Operate("ns/t", "add", ["late", 2])); expect(Object.keys(w.fibers.get(host)!.sigma["ns/t"]).sort()).toEqual(["a", "late"]);
    w.retire(user); w.settle(); expect(w.fibers.get(host)!.sigma["ns/t"]).toEqual({}); expect(() => w.perform(user, new Operate("ns/t", "add", ["again", 3]))).toThrow(PreconditionFailed);
  });
  it("capabilities are reviewed at load time; dependency cycles are reported from the declarations", () => {
    const approved: string[] = [], w = new World(K("ns/a", "ns/b", "ns/secret"), { review: (c) => { approved.push(c.id); return !Object.keys(c.requires).some((k) => k.startsWith("ns/secret")); } });
    w.insert(new Component("community", function* () {}, { requires: { "ns/a": "*" } })); expect(() => w.insert(new Component("snoop", function* () {}, { requires: { "ns/secret": "*" } }))).toThrow(/review denied/);
    expect(approved).toEqual(["community", "snoop"]); expect(w.trace).toContainEqual(["review_denied", "snoop"]);
    const w2 = new World(K("ns/a", "ns/b")); w2.insert(new Component("a", function* () {}, { requires: { "ns/b": "*" }, provides: { "ns/a": "1.0.0" } })); w2.insert(new Component("b", function* () {}, { requires: { "ns/a": "*" }, provides: { "ns/b": "1.0.0" } })); w2.settle();
    expect(Object.values(stateOf(w2))).toEqual(["INACTIVE", "INACTIVE"]); expect(new Set(w2.dependencyCycles()[0])).toEqual(new Set(["f1", "f2"]));
  });
});
describe("service multiplexing and access control (paper §6)", () => {
  it("a service broker absorbs provider changes so consumers never reload", () => {
    const w = new World({ "ns/svc": brokerKey() }), broker = new Broker("weighted"), host = w.insert(new Component("broker", function* () { yield new Provide("ns/svc", {}); }, { provides: { "ns/svc": "1.0.0" } }));
    const reg = (pid: string, wt: number) => new Component(`impl-${pid}`, function* () { yield new Operate("ns/svc", "register", [pid, { weight: wt }]); }, { requires: { "ns/svc": "*" } });
    const consumer = w.insert(new Component("client", function* () {}, { requires: { "ns/svc": "*" } })), v1 = w.insert(reg("v1", 1)); w.settle(); const table = () => w.fibers.get(host)!.sigma["ns/svc"];
    expect(new Set(range(5).map(() => broker.pick(table())))).toEqual(new Set(["v1"]));
    const v2 = w.insert(reg("v2", 0)); w.settle(); w.perform(v2, new Operate("ns/svc", "set_weight", ["v2", 1])); w.perform(v1, new Operate("ns/svc", "set_weight", ["v1", 3]));
    const picks = range(8).map(() => broker.pick(table())); expect([picks.filter((p) => p === "v1").length, picks.filter((p) => p === "v2").length]).toEqual([6, 2]);
    w.perform(v1, new Operate("ns/svc", "set_weight", ["v1", 0])); w.perform(v2, new Operate("ns/svc", "set_weight", ["v2", 1])); w.retire(v1); w.settle();
    expect(Object.keys(table())).toEqual(["v2"]); expect(broker.pick(table(), "v2")).toBe("v2");
    expect(w.trace.filter((t) => t[0] === "begin" && t[1] === consumer)).toHaveLength(1); expect(w.fibers.get(consumer)!.state).toBe("ACTIVE");                  // never reloaded
    expect(new Broker("round_robin").pick({ a: { weight: 1 }, b: { weight: 1 } })).toBe("a");
  });
  it("interception carries fine-grained policy without reloading anything", () => {
    const w = new World({}), fs_ = (meta: any) => ({ read: (p: string) => { if (!(meta.allow_read ?? []).some((a: string) => p.startsWith(a))) throw new Error(`${p} is not readable`); return `contents of ${p}`; }, write: (p: string) => { if (meta.readonly) throw new Error("read-only"); return `wrote ${p}`; } });
    w.insert(new Component("fs", function* () { yield new Provide("ns/fs", fs_); }, { provides: { "ns/fs": "1.0.0" } })); w.settle(); const before = [JSON.stringify(w.trace), JSON.stringify(stateOf(w))], root = new Scope(w);
    const community = root.intercept("ns/fs", { allow_read: ["/data/public"], readonly: true }), core = root.intercept("ns/fs", { allow_read: ["/"] });
    expect(community.get("ns/fs").read("/data/public/a")).toBe("contents of /data/public/a"); expect(core.get("ns/fs").write("/etc/x")).toBe("wrote /etc/x");
    expect(() => community.get("ns/fs").read("/etc/passwd")).toThrow(); expect(() => community.get("ns/fs").write("/data/public/a")).toThrow();
    const tightened = community.intercept("ns/fs", { allow_read: ["/data/public/reports"] }); expect([JSON.stringify(w.trace), JSON.stringify(stateOf(w))]).toEqual(before); expect(tightened.get("ns/fs").read("/data/public/reports/q3")).toMatch(/^contents/);
  });
});
describe("declarative loader and hot module replacement (paper §5.2)", () => {
  const svc = (config: any) => new Component("svc", function* () { yield new Provide("ns/r", { ...config }); }, { provides: { "ns/r": "1.0.0" } });
  it("dispatches on the changed field with the least disruptive operation", () => {
    const w = new World(K("ns/r")), L = new Loader(w), no = () => false, yes = () => true;
    expect(L.apply([new Entry("svc", svc, { url: "svc.js", config: { v: 1 }, onConfig: no })])).toEqual([["insert", "svc"]]); expect(w.fibers.get(L.fiberOf("svc")!)!.sigma["ns/r"]).toEqual({ v: 1 });
    const f0 = L.fiberOf("svc"), trace0 = w.trace.length;
    expect(L.apply([new Entry("svc", svc, { url: "svc.js", config: { v: 1 }, intercept: { "ns/r": { x: 1 } }, onConfig: no })])).toEqual([["intercept", "svc"]]); expect([L.fiberOf("svc"), w.trace.length]).toEqual([f0, trace0]);
    expect(L.apply([new Entry("svc", svc, { url: "svc.js", config: { v: 1 }, intercept: { "ns/r": { x: 1 } }, isolate: { "ns/r": true } })])).toEqual([["isolate", "svc"]]);
    expect(L.apply([new Entry("svc", svc, { url: "svc.js", config: { v: 2 }, intercept: { "ns/r": { x: 1 } }, isolate: { "ns/r": true }, onConfig: yes })])).toEqual([["config_in_place", "svc"]]); expect(L.fiberOf("svc")).toBe(f0);
    const keep = { config: { v: 3 }, intercept: { "ns/r": { x: 1 } }, isolate: { "ns/r": true } };
    expect(L.apply([new Entry("svc", svc, { url: "svc.js", ...keep, onConfig: no })])).toEqual([["reload", "svc"]]); expect(w.fibers.get(L.fiberOf("svc")!)!.sigma["ns/r"]).toEqual({ v: 3 });
    const f1 = L.fiberOf("svc")!; expect(L.apply([new Entry("svc", svc, { url: "svc.js", disabled: true, ...keep })])).toEqual([["disable", "svc"]]); expect(w.fibers.has(f1)).toBe(false);
    expect(L.apply([new Entry("svc", svc, { url: "svc.js", ...keep })])).toEqual([["enable", "svc"]]); expect(L.fiberOf("svc")).not.toBe(f1); expect(w.fibers.get(L.fiberOf("svc")!)!.state).toBe("ACTIVE");
    expect(L.apply([new Entry("svc", svc, { url: "svc2.js", ...keep })])).toEqual([["rebuild", "svc"]]); expect(L.apply([])).toEqual([["retire", "svc"]]); expect(w.fibers.size).toBe(0);
  });
  it("groups reconcile children by keyed diff and unload as a unit", () => {
    const w = new World(K("ns/a", "ns/b", "ns/c")), L = new Loader(w), mk = (key: string) => () => new Component(`p-${key}`, function* () { yield new Provide(key, {}); }, { provides: { [key]: "1.0.0" } });
    const ent = (...ids: string[]) => new Entry("grp", null, { children: ids.map((i) => new Entry(i, mk(`ns/${i}`), { url: `${i}.js` })) }), comps = () => [...w.fibers.values()].filter((f) => f.state === "ACTIVE").map((f) => f.comp.id).sort();
    L.apply([ent("a", "b")]); expect(comps()).toEqual(["group:grp", "p-ns/a", "p-ns/b"]); const fa = L.fiberOf("a"), ops = L.apply([ent("a", "c")]);
    expect(ops).toContainEqual(["retire", "b"]); expect(ops).toContainEqual(["insert", "c"]); expect(L.fiberOf("a")).toBe(fa); expect(comps()).toEqual(["group:grp", "p-ns/a", "p-ns/c"]); L.apply([]); expect(w.fibers.size).toBe(0);
  });
  const GRAPH: Record<string, string[]> = { "app.js": ["feature.js", "util.js"], "feature.js": ["lib.js"], "util.js": ["lib.js", "ext.js"], "lib.js": [], "ext.js": [], "a.js": ["b.js"], "b.js": ["a.js"] };
  const imports = (u: string) => new Set(GRAPH[u] ?? []);
  it("classifies modules, detects stale entries, and defaults import cycles to declined", () => {
    expect(classify(new Set(["lib.js"]), new Set(["ext.js"]), imports)[0]).toEqual(new Set(["lib.js"]));
    const [acc2, dec2] = classify(new Set(["feature.js"]), new Set(["ext.js"]), imports); expect(acc2).toEqual(new Set(["feature.js"])); expect(["lib.js", "ext.js"].every((u) => dec2.has(u))).toBe(true);
    const [, dec3] = classify(new Set(["x.js"]), new Set(), (u) => (u === "x.js" ? new Set(["a.js"]) : imports(u))); expect(dec3.has("a.js") && dec3.has("b.js")).toBe(true);
    const entries = [new Entry("app", () => new Component("app", function* () {}), { url: "app.js" }), new Entry("other", () => new Component("other", function* () {}), { url: "lib.js" })];
    expect(detectStale(entries, new Set(["feature.js"]), new Set(["ext.js"]), imports).map((e) => e.id)).toEqual(["app", "other"]); expect(detectStale([entries[1]], new Set(["feature.js"]), new Set(["ext.js"]), imports)).toEqual([]);
    expect(getDependencies("app.js", new Set(["ext.js"]), imports)).toEqual(new Set(["app.js", "feature.js", "util.js", "lib.js"]));
  });
  it("reload is transactional: a failed import restores the previous system", () => {
    const w = new World(K("ns/r")), L = new Loader(w), mk = (ver: number) => () => new Component("svc", function* () { yield new Provide("ns/r", { ver }); }, { provides: { "ns/r": "1.0.0" } }), v1 = mk(1), v2 = mk(2);
    const ent = new Entry("svc", v1, { url: "svc.js" }); L.apply([ent]); const modules: Record<string, any> = { "svc.js": v1 }, ver = () => w.fibers.get(L.fiberOf("svc")!)!.sigma["ns/r"].ver;
    expect(ver()).toBe(1); expect(hotReload(L, [ent], modules, new Set(["svc.js"]), new Set(), () => new Set(), () => v2)).toEqual(["svc"]); expect([ver(), modules["svc.js"] === v2]).toEqual([2, true]);
    const ent2 = new Entry("svc", v2, { url: "svc.js" }); L.live.get("svc")!.entry = ent2;
    expect(() => hotReload(L, [ent2], modules, new Set(["svc.js"]), new Set(), () => new Set(), () => { throw new SyntaxError("unexpected token"); })).toThrow(SyntaxError);
    expect([ver(), modules["svc.js"] === v2, w.quiet()]).toEqual([2, true, true]);
  });
});
describe("golden scenario vectors reproduce the Python implementation exactly", () => {
  for (const [file, fn] of [["components-tree-scenarios.json", scenario.runTreeScenario], ["components-replacement-scenarios.json", scenario.runReplacementScenario]] as const)
    it(file, () => { const v = JSON.parse(fs.readFileSync(path.join(VEC, file), "utf8")); for (const rec of v.scenarios) { const got = fn(rec.seed); expect([got.trace_hash, got.steps], `seed ${rec.seed}`).toEqual([rec.trace_hash, rec.steps]); } });
});
