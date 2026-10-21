/** PRD §3.9 Component Composability, tested against the TypeScript runtime (mirrors tests/test_components.py). */
import fs from "node:fs";
import path from "node:path";
import { describe, expect, it } from "vitest";
import { Component, ConfinementViolation, External, Instantiate, Operate, PreconditionFailed, Provide, World, notify, versionSatisfies } from "../src/components/core.js";
import { STATED_EQUIVALENCE, chainKey, counterKey, settingKey, tableKey, workspaceKey } from "../src/components/keys.js";
import { ComponentRegistry, Reconciler, RegistrationError, RunPins } from "../src/components/loader.js";
import { Scope, mergeMeta } from "../src/components/scope.js";
import * as scenario from "../src/components/scenario.js";

const VEC = path.resolve(import.meta.dirname, "../../../spec/vectors");
const K = (...names: string[]) => Object.fromEntries(names.map((n) => [n, tableKey()]));
const provider = (id = "web", key = "ns/routes", version = "1.2.0", value: any = {}) => new Component(id, function* () { yield new Provide(key, value); }, { provides: { [key]: version } });
const consumer = (id = "api", key = "ns/routes", rng = "^1.0.0", n = 2) => new Component(id, function* () { for (let i = 0; i < n; i++) yield new Operate(key, "add", [`${id}.${i}`, i]); }, { requires: { [key]: rng } });
const theta = (w: World) => Object.fromEntries([...w.fibers].map(([n, f]) => [n, f.theta]));

describe("revertible effects, locality, stated equivalence", () => {
  it("effects are undone last-in-first-out and return the context (and a FIFO revert is caught)", () => {
    const build = (w: World) => [w.insert(new Component("host", function* () { yield new Provide("ns/mode", "initial"); }, { provides: { "ns/mode": "1.0.0" } })),
      w.insert(new Component("tweak", function* () { yield new Operate("ns/mode", "set", "A"); yield new Operate("ns/mode", "set", "B"); }, { requires: { "ns/mode": "*" } }))];
    const w = new World({ "ns/mode": settingKey() }), [hp, hc] = build(w); w.settle(); expect(w.fibers.get(hp)!.sigma["ns/mode"]).toBe("B");
    w.retire(hc); w.settle(); expect(w.fibers.get(hp)!.sigma["ns/mode"]).toBe("initial");
    const proto = World.prototype as any, orig = proto.unload;
    proto.unload = function (f: any) { f.acc.reverse(); return orig.call(this, f); };
    try { const w2 = new World({ "ns/mode": settingKey() }), [hp2, hc2] = build(w2); w2.settle(); w2.retire(hc2); w2.settle(); expect(w2.fibers.get(hp2)!.sigma["ns/mode"]).not.toBe("initial"); }
    finally { proto.unload = orig; }
  });
  it("has no separate uninstall path: retirement alone reverts everything, including instantiated children", () => {
    for (const a of ["uninstall", "dispose", "deactivate", "cleanup"]) expect(a in Component.prototype).toBe(false);
    const w = new World(K("ns/a", "ns/b", "ns/c")); const n = w.insert(new Component("host", function* () { yield new Provide("ns/a", {}); yield new Provide("ns/b", {}); yield new Instantiate(provider("kid", "ns/c", "1.0.0")); yield new Operate("ns/a", "add", ["x", 1]); }, { provides: { "ns/a": "1.0.0", "ns/b": "1.0.0" } }));
    w.settle(); expect([...w.fibers.values()].some((f) => f.comp.id === "kid")).toBe(true); w.retire(n); w.settle(); w.sweep(); expect(w.fibers.size).toBe(0);
  });
  it("states the equivalence per resource class and tests against it", () => {
    const w2 = new World({ "ns/ws": workspaceKey() }); const keep = { content: "k", mode: 0o600, mtime: 5, inode: 3 };
    w2.insert(new Component("fs", function* () { yield new Provide("ns/ws", { keep }); }, { provides: { "ns/ws": "1.0.0" } }));
    const u2 = w2.insert(new Component("edit", function* () { yield new Operate("ns/ws", "write", ["keep", "changed", 0o644]); }, { requires: { "ns/ws": "*" } })); w2.settle();
    const before = { "ns/ws": { keep: { ...keep } } }; w2.retire(u2); w2.settle(); const after = w2.projection(new Set(["ns/ws"]));
    expect(JSON.stringify(after)).not.toBe(JSON.stringify(before)); expect(w2.equivalent(after, before, new Set(["ns/ws"]))).toBe(true);
    for (const kt of [tableKey(), chainKey(), counterKey(), workspaceKey()]) expect(kt.resourceClass in STATED_EQUIVALENCE).toBe(true);
  });
});
describe("declared dependencies", () => {
  it("are namespaced, versioned, disjoint unless isolated, and acyclic", () => {
    const r = new ComponentRegistry(); r.register(provider("web")); expect(() => r.register(provider("web2"))).toThrow(/overlap/);
    const r2 = new ComponentRegistry(); r2.register(new Component("a", function* () {}, { provides: { "ns/k": "1.0.0" }, isolated: true })); r2.register(new Component("b", function* () {}, { provides: { "ns/k": "1.0.0" }, isolated: true }));
    expect(() => new ComponentRegistry().register(new Component("x", function* () {}, { provides: { plainkey: "1.0.0" } }))).toThrow(/namespaced/);
    expect(() => new ComponentRegistry().register(new Component("x", function* () {}, { requires: { "ns/k": "banana" } }))).toThrow(/range/);
    const cyc = new ComponentRegistry(); cyc.register(new Component("a", function* () {}, { provides: { "ns/a": "1.0.0" }, requires: { "ns/b": "*" } }));
    expect(() => cyc.register(new Component("b", function* () {}, { provides: { "ns/b": "1.0.0" }, requires: { "ns/a": "*" } }))).toThrow(/cycle/); expect(RegistrationError).toBeDefined();
    expect([versionSatisfies("1.4.2", "^1.2.0"), versionSatisfies("2.0.0", "^1.2.0"), versionSatisfies("1.2.9", "~1.2.0"), versionSatisfies("1.3.0", "~1.2.0")]).toEqual([true, false, true, false]);
    expect([versionSatisfies("1.5.0", ">=1.2.0,<2.0.0"), versionSatisfies("0.9.0", ">=1.0.0"), versionSatisfies("0.2.5", "^0.2.0"), versionSatisfies("0.3.0", "^0.2.0")]).toEqual([true, false, true, false]);
    const w = new World(K("ns/routes")), a = w.insert(consumer("api", "ns/routes", "^2.0.0")), p = w.insert(provider("web", "ns/routes", "1.2.0")); w.settle();
    expect([w.fibers.get(a)!.theta, w.fibers.get(p)!.theta]).toEqual(["Inactive", "Active"]); expect(r.checkDependencies(["web"])).toEqual([]);
  });
});
describe("reactive activation", () => {
  it("classifies changes, activates and deactivates without errors or restarts", () => {
    expect([notify(false, true), notify(true, false), notify(true, true), notify(false, false)]).toEqual(["activating", "deactivating", "neutral", "neutral"]);
    const w = new World(K("ns/routes")), a = w.insert(consumer()); w.settle(); expect(w.fibers.get(a)!.theta).toBe("Inactive"); expect(w.violations).toHaveLength(0);
    const p = w.insert(provider()); w.settle(); expect(w.fibers.get(a)!.theta).toBe("Active"); expect(w.trace).toContainEqual(["notify", a, "activating"]);
    w.retire(p); w.settle(); expect(w.fibers.get(a)!.theta).toBe("Inactive"); expect(w.trace).toContainEqual(["notify", a, "deactivating"]); expect(w.violations).toHaveLength(0);
    w.sweep(); w.insert(provider("web3")); w.settle(); expect(w.fibers.get(a)!.theta).toBe("Active");
  });
  it("withdraws a binding only after the deactivations it causes have finished (teardown can still read it)", () => {
    const seen: boolean[] = [], w = new World(K("ns/routes")), p = w.insert(provider());
    const a = w.insert(new Component("api", function* () { yield new External("conn", () => null, () => seen.push(w.fibers.get(p)!.sigma["ns/routes"] !== undefined)); }, { requires: { "ns/routes": "*" } })); w.settle(); w.retire(p);
    const en = w.enabled().map(([r, n]) => `${r}:${n}`); expect(en).toContain(`leave:${p}`); expect(en).not.toContain(`unload:${p}`);
    w.settle(); expect(seen).toEqual([true]); expect([w.fibers.get(p)!.theta, w.fibers.get(a)!.theta]).toEqual(["Inactive", "Inactive"]);
    expect(JSON.stringify(w.trace.findIndex((t) => t[0] === "unload" && t[1] === a) < w.trace.findIndex((t) => t[0] === "unload" && t[1] === p))).toBe("true");
  });
  it("does not move a binding while an activation that reads it runs", () => {
    const w = new World(K("ns/routes")), p = w.insert(provider()); let a = w.insert(consumer("api", "ns/routes", "^1.0.0", 3)); w.settle(); w.retire(a); w.settle(); w.sweep(); a = w.insert(consumer("api", "ns/routes", "^1.0.0", 3));
    w.step("begin", a); w.step("iter", a); w.retire(p);
    expect(new Set(w.enabled().map(([r, n]) => `${r}:${n}`))).toEqual(new Set([`leave:${p}`, `iter:${a}`]));
    w.step("leave", p); expect(Object.fromEntries(w.enabled().map(([r, n]) => [n, r]))[a]).toBe("divert");
    expect(w.fibers.get(p)!.sigma["ns/routes"]).toEqual({ "api.0": 0 }); expect(w.enabled().map(([r, n]) => `${r}:${n}`)).not.toContain(`unload:${p}`);
    w.step("divert", a); w.step("unload", a); expect(w.enabled().map(([r, n]) => `${r}:${n}`)).toContain(`unload:${p}`); w.settle();
    expect([w.fibers.get(a)!.theta, w.fibers.get(p)!.theta]).toEqual(["Inactive", "Inactive"]); expect([...w.fibers.values()].every((f) => !Object.keys(f.sigma).length)).toBe(true);
  });
});
describe("confinement, isolation and interception", () => {
  it("refuses, records once and leaves state unchanged on undeclared access; a component that handles the refusal continues", () => {
    const w = new World(K("ns/mine0", "ns/mine1", "ns/mine2", "ns/other"));
    const bad = [
      (_c: any) => (function* (): any { yield new Provide("ns/other", {}); })(),                                           // provides an undeclared key
      (_c: any) => (function* (): any { yield new Provide("ns/mine1", {}); yield new Operate("ns/other", "add", ["x", 1]); })(), // operates on one
      (c: any) => (function* (): any { c.get("ns/other"); yield* []; })(),                                                  // reads one
    ];
    bad.forEach((eff, i) => {
      const n = w.insert(new Component(`bad${i}`, eff, { provides: { [`ns/mine${i}`]: "1.0.0" } })); w.settle(); const f = w.fibers.get(n)!;
      expect([f.failed, f.theta, Object.keys(f.sigma).length]).toEqual([true, "Inactive", 0]); expect(w.violations.at(-1).code).toBe("component.confinement_violation");
    });
    expect(w.violations).toHaveLength(3); expect(w.quiet()).toBe(true);                                                     // each refusal is recorded exactly once
    const w2 = new World(K("ns/mine2", "ns/other")), n = w2.insert(new Component("ok", function* () { try { yield new Provide("ns/other", {}); } catch (e) { if (!(e instanceof ConfinementViolation)) throw e; } yield new Provide("ns/mine2", {}); }, { provides: { "ns/mine2": "1.0.0" } })); w2.settle();
    expect([w2.fibers.get(n)!.theta, w2.violations.length]).toEqual(["Active", 1]);                                          // recorded, but a component that handles the refusal carries on
    expect([...new Component("c", function* () {}, { requires: { "ns/a": "*" }, provides: { "ns/b": "1.0.0" } }).declared].sort()).toEqual(["ns/a", "ns/b"]);   // the declared set is reviewable at load time
  });
  it("isolation and interception change resolution without reloading or rewiring", () => {
    const w = new World({}), db = (m: any) => `db[${m.tenant ?? "default"}]`;
    for (const realm of ["ns/db", "ns/db@tenant-a"]) w.insert(new Component(`p-${realm}`, function* () { yield new Provide(realm, db); }, { provides: { [realm]: "1.0.0" }, isolated: true }));
    w.settle(); const trace = JSON.stringify(w.trace), states = JSON.stringify(theta(w)); const root = new Scope(w), a = root.isolate("ns/db", "ns/db@tenant-a");
    expect([root.get("ns/db", { tenant: "x" }), a.get("ns/db", { tenant: "x" })]).toEqual(["db[x]", "db[x]"]);
    const log: any[] = [], guarded = a.intercept("ns/db", { on_access: [(k: string, r: string) => log.push([k, r])], tenant: "forced" });
    expect(guarded.get("ns/db", { tenant: "x" })).toBe("db[forced]"); expect(log).toEqual([["ns/db", "ns/db@tenant-a"]]);
    expect(() => guarded.intercept("ns/db", { on_access: [() => { throw new Error("rate limit"); }] }).get("ns/db")).toThrow("rate limit");
    expect(JSON.stringify(w.trace)).toBe(trace); expect(JSON.stringify(theta(w))).toBe(states);
    expect(mergeMeta({ on_access: [1] }, { on_access: [1, 2], x: 1 })).toEqual({ on_access: [1, 2], x: 1 }); expect(() => root.isolate("ns/db", "ns/missing").get("ns/db")).toThrow(PreconditionFailed);
  });
});
describe("lifecycle orchestration, reconciliation, live replacement", () => {
  it("the orchestrator only inserts and retires; removal needs an inactive, reverted, childless fiber; retirement cascades to children", () => {
    const w = new World(K("ns/routes")), n = w.insert(consumer()); expect(() => w.step("iter", n)).toThrow(PreconditionFailed); w.insert(provider()); w.settle(); expect(w.fibers.get(n)!.theta).toBe("Active");
    expect(() => w.remove(n)).toThrow(PreconditionFailed); w.retire(n); expect(() => w.remove(n)).toThrow(PreconditionFailed); w.settle(); expect(w.removable(n)).toBe(true);
    const w3 = new World(K("ns/kid")), par = w3.insert(new Component("par", function* () { yield new Instantiate(provider("kid", "ns/kid", "1.0.0")); })); w3.settle(); const kid = [...w3.fibers].find(([, f]) => f.parent === par)![0];
    w3.retire(par); w3.settle(); expect(w3.fibers.get(kid)!.retired).toBe(true); expect(w3.removable(par)).toBe(false); w3.sweep(); expect(w3.fibers.size).toBe(0);
  });
  it("reconciles a declarative desired set, replaces live, and pins runs unless they opted in", () => {
    const w = new World(K("ns/routes")), rec = new Reconciler(w);
    const v1 = { web: provider("web", "ns/routes", "1.0.0"), api: new Component("api", consumer().effects, { requires: { "ns/routes": "^1.0.0" }, config: { workers: 2 } }) };
    expect(rec.plan(v1)).toEqual([["insert", "api"], ["insert", "web"]]); rec.apply(v1);
    expect(Object.fromEntries([...w.fibers.values()].map((f) => [f.comp.id, f.theta]))).toEqual({ web: "Active", api: "Active" }); expect(rec.plan(v1)).toEqual([]);
    const pins = new RunPins(rec); pins.open("run-pinned"); pins.open("run-live", true);
    const v2 = { web: provider("web", "ns/routes", "1.1.0"), api: v1.api }; expect(rec.plan(v2)).toEqual([["update", "web"]]); const apiFiber = rec.byId.get("api")![0]; rec.apply(v2);
    expect([...w.fibers.values()].filter((f) => f.comp.id === "web").map((f) => f.comp.provides["ns/routes"])).toEqual(["1.1.0"]); expect([...w.fibers.values()].every((f) => f.theta === "Active")).toBe(true);
    expect(pins.configFor("run-pinned")).not.toEqual(pins.configFor("run-live")); expect(pins.configFor("run-live")).toEqual(pins.snapshot()); expect(rec.byId.get("api")![0]).toBe(apiFiber);
    rec.apply({ web: v2.web }); expect(rec.plan({ web: v2.web })).toEqual([]); expect([...w.fibers.values()].map((f) => f.comp.id)).toEqual(["web"]);
  });
});
describe("compensation and ordered revert", () => {
  it("compensates in reverse, withholds until approved, and escalates a failed compensation", () => {
    const log: string[] = [], w = new World(), n = w.insert(new Component("x", function* () { yield new External("a", () => log.push("do a"), () => log.push("undo a")); yield new External("b", () => log.push("do b"), () => log.push("undo b")); })); w.settle(); w.retire(n); w.settle();
    expect(log).toEqual(["do a", "do b", "undo b", "undo a"]);
    const sent: string[] = [], mail = () => new Component("mail", function* () { const out: any = yield new External("email", () => sent.push("sent")); if (!("withheld" in out)) throw new Error("expected withheld"); });
    const w2 = new World(), n2 = w2.insert(mail()); w2.settle(); expect([sent.length, w2.pendingExternal.size]).toEqual([0, 1]); w2.retire(n2); w2.settle(); expect(sent.length).toBe(0); expect(w2.fibers.get(n2)!.theta).toBe("Inactive");
    const w3 = new World(); w3.insert(mail()); w3.settle(); w3.approve([...w3.pendingExternal.keys()][0]); expect(sent).toEqual(["sent"]);
    const w4 = new World(), n4 = w4.insert(new Component("f", function* () { yield new External("x", () => null, () => { throw new Error("compensation failed"); }); })); w4.settle(); w4.retire(n4); w4.settle();
    expect(w4.escalations[0].fiber).toBe(n4); expect(w4.fibers.get(n4)!.theta).toBe("Unloading"); w4.resolveEscalation(n4); w4.settle(); expect(w4.fibers.get(n4)!.theta).toBe("Inactive");
  });
  it("an out-of-order revert needs independence; otherwise the dependents go first or the result is re-gated", () => {
    const w = new World({ "ns/log": tableKey(), "ns/chain": chainKey() });
    const host = new Component("host", function* () { yield new Provide("ns/log", {}); yield new Provide("ns/chain", []); }, { provides: { "ns/log": "1.0.0", "ns/chain": "1.0.0" } });
    const mk = (id: string, key: string, op: string, arg: any) => new Component(id, function* () { yield new Operate(key, op, arg); }, { requires: { [key]: "*" } });
    const ids = [host, mk("t1", "ns/log", "add", ["t1", 1]), mk("t2", "ns/log", "add", ["t2", 2]), mk("m1", "ns/chain", "push", "m1"), mk("m2", "ns/chain", "push", "m2")].map((c) => w.insert(c)); w.settle();
    expect(w.planRevert(ids[1])).toEqual({ fiber: ids[1], independent: true, dependents: [] }); const p = w.planRevert(ids[3]); expect([p.independent, p.dependents]).toEqual([false, [ids[4]]]);
    expect(w.revert(ids[3]).status).toBe("revert.blocked"); expect(w.fibers.get(ids[3])!.theta).toBe("Active");
    expect(w.revert(ids[3], true).status).toBe("reverted"); expect(w.projection(new Set(["ns/chain"]))["ns/chain"]).toEqual([]);
    expect(w.revert(ids[1]).status).toBe("reverted"); expect(Object.keys(w.projection(new Set(["ns/log"]))["ns/log"])).toEqual(["t2"]);
  });
});
describe("composability verification", () => {
  it("400 random interleavings preserve the guard, reach quiescence, revert consumers fully and tear everything down", () => { for (let s = 0; s < 400; s++) scenario.runScenario(s); });
  it("is confluent across lifecycle schedules", () => { for (let s = 0; s < 120; s++) { const sigs = Array.from({ length: 5 }, (_, t) => JSON.stringify(scenario.confluenceSignature(s, t))); expect(new Set(sigs).size, `seed ${s}`).toBe(1); } });
  it("the property tests can fail: a missing guard and a leaked inverse are both caught", () => {
    const fails = (s: number) => { try { scenario.runScenario(s); return false; } catch { return true; } }, seeds = Array.from({ length: 60 }, (_, s) => s);
    expect(seeds.some(fails)).toBe(false);
    const proto = World.prototype as any, relied = proto.relied; proto.relied = () => false;                                  // mutation 1: unload without waiting for consumers
    try { expect(seeds.some(fails)).toBe(true); } finally { proto.relied = relied; }
    const exec = proto.exec;                                                                                                   // mutation 2: an Operate whose inverse forgets to undo
    proto.exec = function (f: any, st: any) { const out = exec.call(this, f, st); if (st instanceof Operate) f.acc[f.acc.length - 1] = ["state", () => {}]; return out; };
    try { expect(seeds.some(fails)).toBe(true); } finally { proto.exec = exec; }
    expect(seeds.some(fails)).toBe(false);
  });
  it("reproduces the golden scenarios of the Python implementation exactly (trace hashes and settled states)", () => {
    const v = JSON.parse(fs.readFileSync(path.join(VEC, "components-scenarios.json"), "utf8"));
    for (const rec of v.scenarios) { const got = scenario.runScenario(rec.seed); expect(got.trace_hash, `seed ${rec.seed}`).toBe(rec.trace_hash); expect(got.mid, `seed ${rec.seed}`).toEqual(rec.mid); expect(got.steps).toBe(rec.steps); }
  });
});
