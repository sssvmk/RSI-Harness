/** The error contract (design §18.11, docs/plugin_contract.md): what a plugin may signal, what the harness throws for each case, and how a plugin that does not follow its contract is caught. Mirrors test_plugin_errors.py. */
import fs from "node:fs";
import path from "node:path";
import { describe, expect, it } from "vitest";
import * as P from "../src/plugins.js";
import { KINDS, PluginHost, PluginRejection } from "../src/plugins.js";
import { Fake, OUT, ProbeAware, manifest } from "./plugin-fakes.js";
import { eventsOf, mk, oa } from "./helpers.js";

const FAKES = new URL("./plugin-fakes.ts", import.meta.url).pathname, SAMPLE = KINDS.retrieval.methods.query.sample, BAD_K = KINDS.retrieval.methods.query.errors.find((e: any) => e.code === "invalid_argument").probe;   // k = 0, the spec's own probe
const PROC = () => manifest("retrieval", "proc-retrieval", "1.0.0", "subprocess", { entry: { command: ["python3", path.resolve(import.meta.dirname, "../../python/tests/plugin_proc.py")] } });
const core = (maxFailures = 3, verify = false) => new PluginHost("core", [], maxFailures, verify);
const rejects = async (p: Promise<any>, cls: any, re?: RegExp) => { try { await p; } catch (e: any) { expect(e).toBeInstanceOf(cls); if (re) expect(e.message).toMatch(re); return e; } throw new Error("did not throw"); };
async function loaded(kind = "retrieval", max = 3) { const h = core(max), m = manifest(kind), fake = new ProbeAware(m); return { h, fake, handle: await h.load(m, fake) }; }

describe("the catalogue and the spec", () => {
  it("the error catalogue and every method's declared errors are complete and consistent", () => {
    const spec = JSON.parse(fs.readFileSync(path.resolve(import.meta.dirname, "../../../spec/plugins/kinds.json"), "utf8"));
    expect([Object.keys(spec.error_codes), Object.entries<any>(spec.error_codes).filter(([, v]) => v.retryable).map(([k]) => k).sort(), [...P.RETRYABLE].sort()]).toEqual([P.ERROR_CODES, ["rate_limited", "unavailable"], ["rate_limited", "unavailable"]]);
    for (const [kind, v] of Object.entries<any>(KINDS)) for (const [name, m] of Object.entries<any>(v.methods)) {
      expect(Boolean(m.purpose && m.returns_doc && Array.isArray(m.errors)), `${kind}.${name}`).toBe(true);
      for (const e of m.errors) { expect(P.ERROR_CODES.includes(e.code) && e.when && "probe" in e, `${kind}.${name}`).toBeTruthy(); if (e.probe !== null) expect(P.schemaErrors(m.input, e.probe), `${kind}.${name} probe must be a valid input`).toEqual([]); }
      expect(new Set(m.errors.map((e: any) => e.code)).size).toBe(m.errors.length);
    }
    expect([KINDS.retrieval.methods.health.errors, KINDS.retrieval.methods.describe.errors]).toEqual([[], []]);   // these never raise
  });
});
describe("what the harness throws", () => {
  it("a declared refusal is a typed rejection with its code and whether it is retryable, and it is not a fault", async () => {
    const { h, fake, handle } = await loaded();
    for (let i = 0; i < 10; i++) { const e = await rejects(handle.call("query", BAD_K), P.PluginRejected); expect([e.reason, e.retryable, e.code]).toEqual(["invalid_argument", false, "plugin.rejected"]); }
    expect([handle.state, handle.failures, h.has("retrieval")]).toEqual(["active", 0, true]);   // a correct refusal never quarantines a healthy plugin
    fake.over.query = new PluginRejection("rate_limited", "slow down"); const e = await rejects(handle.call("query", SAMPLE), P.PluginRejected); expect([e.retryable, e.reason, handle.state]).toEqual([true, "rate_limited", "active"]);
  });
  it("a backend that is down is retryable and repeated unavailability does quarantine", async () => {
    const { fake, handle } = await loaded("retrieval", 3); fake.over.query = new PluginRejection("unavailable", "index down");
    for (let i = 0; i < 3; i++) { const e = await rejects(handle.call("query", SAMPLE), P.PluginRejected); expect([e.reason, e.retryable]).toEqual(["unavailable", true]); }
    expect(handle.state).toBe("quarantined"); await rejects(handle.call("query", SAMPLE), P.PluginUnavailable, /quarantined/);
  });
  it("a code the contract does not declare for the method is a contract violation and a fault", async () => {
    const { fake, handle } = await loaded("retrieval", 2); fake.over.query = new PluginRejection("conflict", "x");   // conflict is a real code, but not declared for query
    for (let i = 0; i < 2; i++) await rejects(handle.call("query", SAMPLE), P.ContractViolation, /does not declare/); expect(handle.state).toBe("quarantined");
    const b = await loaded(); b.fake.over.query = new PluginRejection("made_up_code"); await rejects(b.handle.call("query", SAMPLE), P.ContractViolation, /does not declare/);
  });
  it("any other exception is a plugin failure, a bad shape a violation, and a slow call a timeout", async () => {
    const { fake, handle } = await loaded("retrieval", 99); fake.over.query = new TypeError("boom"); await rejects(handle.call("query", SAMPLE), P.PluginFailed, /TypeError/);
    fake.over.query = { passages: "none" }; await rejects(handle.call("query", SAMPLE), P.ContractViolation, /output/);
    const spec = KINDS.retrieval.methods.query, old = spec.timeout_s; spec.timeout_s = 0.2; try { fake.over.query = () => new Promise((r) => setTimeout(r, 2000)); await rejects(handle.call("query", SAMPLE), P.PluginTimeout); } finally { spec.timeout_s = old; }
    await rejects(handle.call("query", { user: "u" }), P.ContractViolation, /input/);   // and a bad request from the harness's own caller is refused before the plugin sees it
  });
  it("a refusal is recorded in the run with its code and still costs a call", async () => {
    const h = mk([oa("d")], { plugins: { load: [manifest("retrieval", undefined, "1.0.0", "in_process", { entry: { module: FAKES, factory: "makeProbeAware" } })] } }), { run } = await h.createRun("u", "go"), handle = h.plugins.get("retrieval");
    await rejects(handle.call("query", BAD_K, run), P.PluginRejected); const ev = eventsOf(h, run, "plugin.called").at(-1)!.payload;
    expect([ev.ok, ev.error, ev.retryable, ev.method, eventsOf(h, run, "plugin.failed").length]).toEqual([false, "invalid_argument", false, "query", 0]);
  });
  it("a contract error crosses the process boundary as its code", async () => {
    const h = new PluginHost("standard"), handle = await h.load(PROC());
    for (const [args, code] of [[{ ...SAMPLE, k: 0 }, "invalid_argument"], [{ ...SAMPLE, user: "__no_access__" }, "permission_denied"]] as [any, string][]) expect((await rejects(handle.call("query", args), P.PluginRejected)).reason).toBe(code);
    await rejects(handle.impl.rpc("boom", {}), P.PluginFailed, /boom/);   // a code that is not in the catalogue is a failure, not a contract error
    await h.close();
  });
});
describe("probes at load", () => {
  it.each(Object.keys(KINDS).sort())("a plugin that follows its contract passes every probe: %s", async (kind: string) => {
    const h = new PluginHost("high", [`${kind}-fake`], 3, true), m = manifest(kind), handle = await h.load(m, new ProbeAware(m)); expect(h.has(kind)).toBe(true);
    for (const [name, s] of Object.entries<any>(KINDS[kind].methods)) for (const e of s.errors) if (e.probe !== null) expect((await rejects(handle.call(name, e.probe), P.PluginRejected)).reason, `${kind}.${name}`).toBe(e.code);
  });
  it("a plugin that does not signal what its contract says is not loaded when error verification is on", async () => {
    const m = manifest("retrieval"), h = core(3, true); const e = await rejects(h.load(m, new Fake(m)), P.ConformanceFailed);   // answers every probe as if it were a normal call
    expect([e.message.includes("the invalid_argument probe did not raise"), e.message.includes("the permission_denied probe did not raise"), h.handles.size]).toEqual([true, true, 0]);
    const wrong = new Fake(m, { query: (p: any) => { if (p.k < 1) throw new PluginRejection("permission_denied"); return OUT.retrieval.query; } });   // right on the sample, wrong on the probe
    await rejects(core(3, true).load(m, wrong), P.ConformanceFailed, /the invalid_argument probe raised permission_denied/);
    const crash = new Fake(m, { forget: (p: any) => { if (!p.ids.length) throw new Error("down"); return { removed: p.ids.length }; } }); await rejects(core(3, true).load(m, crash), P.ConformanceFailed, /not a contract error/);
    const off = core(); await off.load(m, new Fake(m)); expect(off.has("retrieval")).toBe(true);   // off by default: the shape is checked, the errors are not
  });
  it("a subprocess plugin passes the probes too", async () => { const h = new PluginHost("standard", [], 3, true); await h.load(PROC()); expect(h.has("retrieval")).toBe(true); await h.close(); });
  it("the deployment configuration turns error verification on", async () => {
    const bad = manifest("retrieval", undefined, "1.0.0", "in_process", { entry: { module: FAKES, factory: "make" } });
    await rejects(mk([oa("d")], { plugins: { load: [bad], verify_errors: true } }).createRun("u", "go"), P.ConformanceFailed, /probe did not raise/);
    const good = manifest("retrieval", undefined, "1.0.0", "in_process", { entry: { module: FAKES, factory: "makeProbeAware" } }), h2 = mk([oa("d")], { plugins: { load: [good], verify_errors: true } }); await h2.loadPlugins(); expect(h2.plugins.has("retrieval")).toBe(true);
  });
});

describe("loading the class the configuration names", () => {
  const CLS = (cls: string, config: any = { id: "kw" }, id = "kw") => manifest("retrieval", id, "1.0.0", "in_process", { entry: { class: `${FAKES}:${cls}`, config } });
  it("loads a conforming class and calls it through the interface, with every probe passing", async () => {
    const h = new PluginHost("core", [], 3, true), handle = await h.load(CLS("DummyRetriever")); expect(h.has("retrieval")).toBe(true);
    await handle.call("index", { documents: [{ id: "d1", text: "payment calendar" }, { id: "d2", text: "hello world" }] }); const r = await handle.call("query", { user: "u", query: "hello", k: 3, filters: {} }); expect([r.trust, r.value.passages.map((x: any) => x.id)]).toEqual(["untrusted", ["d2"]]);
    expect((await handle.call("forget", { ids: ["d2", "nope"] })).value.removed).toBe(1);   // an absent id is not an error
  });
  it.each([["a class that leaves methods unimplemented", "HalfRetriever", /abstract methods left unimplemented/], ["a class that does not exist", "Nope", /not a class/], ["a name that is not a class", "OUT", /not a class/]])("%s is refused and nothing is registered", async (_n: string, cls: string, re: RegExp) => {
    const h = core(); await rejects(h.load(CLS(cls)), P.ManifestInvalid, re); expect([h.handles.size, h.world.fibers.size, h.history.at(-1).event]).toEqual([0, 0, "plugin.rejected"]);
  });
  it("a module that cannot be loaded, a class path that is not module:Class, and a class that describes itself differently are refused", async () => {
    await rejects(core().load(manifest("retrieval", "kw", "1.0.0", "in_process", { entry: { class: "/no/such/module.ts:Thing" } })), P.ManifestInvalid, /cannot load/);
    await rejects(core().load(manifest("retrieval", "kw", "1.0.0", "in_process", { entry: { class: "nocolon" } })), P.ManifestInvalid, /pattern/);
    await rejects(core().load(CLS("DummyRetriever", { id: "kw" }, "someone-else")), P.ConformanceFailed, /describe says kw\/retrieval\/1.0.0, the manifest says someone-else/);
  });
  it("the harness finds its plugins from the deployment configuration", async () => {
    const h = mk([oa("d")], { plugins: { load: [CLS("DummyRetriever")], required: ["retrieval"], verify_errors: true } }); await h.loadPlugins(); expect([h.plugins.has("retrieval"), h.preflight().failed.some((x: any) => x.id === "PF-P01")]).toEqual([true, false]);
    const { run } = await h.createRun("u", "go"); expect(run.pins.byId.get("plugin.kw").version).toBe(1);   // and the run pins the class's manifest
  });
});
