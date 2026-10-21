/** The plugin interface (design §18.11). Mirrors test_plugins.py; the subprocess test points this host at the Python plugin script, so the protocol is checked across languages. */
import fs from "node:fs";
import path from "node:path";
import { describe, expect, it } from "vitest";
import * as P from "../src/plugins.js";
import { KINDS, PluginHost } from "../src/plugins.js";
import { Component, Provide } from "../src/components/core.js";
import { Fake, OUT, manifest } from "./plugin-fakes.js";
import { eventsOf, mk, oa, tmpdir } from "./helpers.js";

const PROC_SCRIPT = path.resolve(import.meta.dirname, "../../python/tests/plugin_proc.py");
const PROC = (...extra: string[]) => manifest("retrieval", "proc-retrieval", "1.0.0", "subprocess", { entry: { command: ["python3", PROC_SCRIPT, ...extra] } });
const host = (profile = "high", maxFailures = 3) => new PluginHost(profile, Object.keys(KINDS).map((k) => `${k}-fake`), maxFailures);
const rejects = async (p: Promise<any>, cls: any, re: RegExp) => { try { await p; } catch (e: any) { expect(e).toBeInstanceOf(cls); expect(e.message).toMatch(re); return; } throw new Error("did not throw"); };
const sample = (k: string, m: string) => KINDS[k].methods[m].sample;
async function withTimeout(kind: string, method: string, s: number, body: () => Promise<void>) { const spec = KINDS[kind].methods[method], old = spec.timeout_s; spec.timeout_s = s; try { await body(); } finally { spec.timeout_s = old; } }

describe("the contracts", () => {
  it("every kind defines a complete contract with a sample that satisfies its own input schema", () => {
    expect(new Set(Object.keys(KINDS))).toEqual(new Set(["proposer", "retrieval", "user_model", "computer_use", "research", "isolation", "assurance", "evaluator"]));
    for (const [k, v] of Object.entries<any>(KINDS)) {
      expect(["describe", "health"].every((m) => m in v.methods) && v.min_profile in P.RANK && v.trust in P.TRUST, k).toBe(true);
      for (const [name, spec] of Object.entries<any>(v.methods)) { expect(Object.keys(spec).sort(), `${k}.${name}`).toEqual(["effect", "errors", "input", "output", "purpose", "returns_doc", "sample", "timeout_s"]); expect(P.schemaErrors(spec.input, spec.sample), `${k}.${name}`).toEqual([]); }
      expect(new Set(Object.keys(OUT[k]))).toEqual(new Set(Object.keys(v.methods).filter((m) => m !== "describe" && m !== "health")));   // the test fakes cover every method
    }
  });
  it("a conforming plugin of every kind loads and each method returns what the contract says", async () => {
    const h = host();
    for (const kind of Object.keys(KINDS)) { const handle = await h.load(manifest(kind), new Fake(manifest(kind))); for (const [name, spec] of Object.entries<any>(KINDS[kind].methods)) { const r = await handle.call(name, spec.sample); expect([P.schemaErrors(spec.output, r.value), r.plugin_id, r.method]).toEqual([[], `${kind}-fake`, name]); } }
    expect([...h.handles.values()].map((x) => x.kind).sort()).toEqual(Object.keys(KINDS).sort()); await h.close(); expect([h.handles.size, h.world.fibers.size]).toEqual([0, 0]);
  });
  it("what a model or an index produced is untrusted and what the operator's infrastructure reported is not", async () => {
    const h = host(), r = await (await h.load(manifest("retrieval"), new Fake(manifest("retrieval")))).call("query", sample("retrieval", "query")), iso = await (await h.load(manifest("isolation"), new Fake(manifest("isolation")))).call("verify", sample("isolation", "verify"));
    expect([r.trust, iso.trust]).toEqual(["untrusted", "tool_trusted"]);
  });
});
describe("what is refused before a plugin is registered", () => {
  it.each([["unknown kind", { kind: "telepathy" }, /unknown plugin kind/], ["bad version", { version: "1.0" }, /pattern/], ["bad id", { id: "Bad Id" }, /pattern/], ["incompatible interface", { interface_version: "2.0.0" }, /not compatible/],
    ["grants beyond the kind", { grants: ["host"] }, /not allowed/], ["in_process without a factory", { entry: { module: "x" } }, /needs entry.module and entry.factory/], ["subprocess without a command", { trust: "subprocess", entry: { module: "x", factory: "y" } }, /needs entry.command/]])(
    "an invalid manifest is refused and nothing is registered: %s", async (_n: any, change: any, re: any) => {
      const h = host(); await rejects(h.load({ ...manifest("retrieval"), ...change }, new Fake(manifest("retrieval"))), P.ManifestInvalid, re); expect([h.handles.size, h.world.fibers.size, h.history.at(-1).event]).toEqual([0, 0, "plugin.rejected"]);
    });
  it("only one plugin of a kind is active and the same id cannot load twice", async () => {
    const h = new PluginHost("core"); await h.load(manifest("retrieval", "a"), new Fake(manifest("retrieval", "a")));
    await rejects(h.load(manifest("retrieval", "b"), new Fake(manifest("retrieval", "b"))), P.ManifestInvalid, /already loaded/); await rejects(h.load(manifest("retrieval", "a"), new Fake(manifest("retrieval", "a"))), P.ManifestInvalid, /already loaded/);
  });
  it("the deployment profile decides which kinds and which trust levels are allowed", async () => {
    const core = new PluginHost("core"), f = (k: string) => new Fake(manifest(k));
    await rejects(core.load(manifest("research"), f("research")), P.PolicyDenied, /needs a high/); await rejects(core.load(manifest("computer_use"), f("computer_use")), P.PolicyDenied, /needs a standard/); await core.load(manifest("retrieval"), f("retrieval")); await core.close(); await core.load(manifest("proposer"), f("proposer")); await core.close(); await core.load(manifest("isolation"), f("isolation"));   // retrieval, a proposer and a dev-tier isolation backend are all fine in a core deployment
    const std = new PluginHost("standard"); await rejects(std.load(manifest("retrieval"), f("retrieval")), P.PolicyDenied, /out of process/); await std.load(PROC()); expect(std.has("retrieval")).toBe(true); await std.close();
    const trusted = new PluginHost("standard", ["retrieval-fake"]); await trusted.load(manifest("retrieval"), f("retrieval")); expect(trusted.has("retrieval")).toBe(true);
  });
  it("the code a manifest names must be the code that is loaded", async () => {
    const f = path.join(tmpdir(), "plug.js"); fs.writeFileSync(f, "x = 1\n"); const good = P.fileSha256(f), h = host(), bad = manifest("retrieval", undefined, "1.0.0", "in_process", { entry: { module: "plugin_fakes", factory: "make", code_sha256: "0".repeat(64) } });
    await rejects(h.load(bad, new Fake(bad), f), P.ManifestInvalid, /does not match the hash/); const ok = manifest("retrieval", undefined, "1.0.0", "in_process", { entry: { module: "plugin_fakes", factory: "make", code_sha256: good } }); expect((await h.load(ok, new Fake(ok), f)).codeSha256).toBe(good);
  });
});
describe("conformance", () => {
  it.each([["describe disagrees with the manifest", { describe: { id: "other", kind: "retrieval", version: "1.0.0" } }, /describe says/], ["health is not ok", { health: { ok: false } }, /health is not ok/], ["a method returns the wrong shape", { query: { passages: "none" } }, /query: output/],
    ["a method is missing a required field", { index: { indexed: 1 } }, /index: output/], ["a method raises", { forget: new Error("down") }, /forget: Error/]])("a plugin that does not satisfy the contract is not loaded: %s", async (_n: any, over: any, re: any) => {
    const h = host(), m = manifest("retrieval"); await rejects(h.load(m, new Fake(m, over)), P.ConformanceFailed, re); expect([h.handles.size, h.world.fibers.size, h.history.at(-1).event]).toEqual([0, 0, "plugin.rejected"]);
  });
  it("a plugin that is missing a method or hangs is not loaded", async () => {
    const h = host(), partial = { describe: () => ({ id: "retrieval-fake", kind: "retrieval", version: "1.0.0" }), health: () => ({ ok: true }) };
    await rejects(h.load(manifest("retrieval"), partial), P.ConformanceFailed, /no method/);
    await withTimeout("retrieval", "index", 0.2, async () => { await rejects(h.load(manifest("retrieval"), new Fake(manifest("retrieval"), { index: () => new Promise((r) => setTimeout(r, 2000)) })), P.ConformanceFailed, /index: timed out/); });
  });
});
describe("calls go through the handle", () => {
  it("input is validated before the plugin is called and output after", async () => {
    const h = host(), m = manifest("retrieval"), fake = new Fake(m), handle = await h.load(m, fake); fake.calls.length = 0;
    await rejects(handle.call("query", { user: "u", query: "q" }), P.ContractViolation, /input/); await rejects(handle.call("delete_everything", {}), P.ContractViolation, /no method/); expect(fake.calls).toEqual([]);   // the plugin never saw a bad request
    fake.over.query = { passages: [{ id: 1 }] }; await rejects(handle.call("query", sample("retrieval", "query")), P.ContractViolation, /output/);   // and a bad answer is not passed on
  });
  it("a slow call times out and repeated failures quarantine the plugin", async () => {
    await withTimeout("retrieval", "query", 0.2, async () => {
      const h = host("high", 3), m = manifest("retrieval"), fake = new Fake(m, { query: () => new Promise((r) => setTimeout(r, 2000)) }), handle = await h.load(m, new Fake(m));
      handle.impl = new P.InProcessPlugin(fake);   // the plugin turns slow after it was verified
      for (let i = 0; i < 3; i++) await rejects(handle.call("query", sample("retrieval", "query")), P.PluginTimeout, /exceeded/);
      expect([handle.state, h.has("retrieval")]).toEqual(["quarantined", false]); await rejects(handle.call("query", sample("retrieval", "query")), P.PluginUnavailable, /quarantined/); await rejects(Promise.resolve().then(() => h.get("retrieval")), P.PluginUnavailable, /no active retrieval/);
    });
  });
  it("a success resets the failure count so scattered errors do not quarantine a flaky plugin", async () => {
    const h = host("high", 3), m = manifest("retrieval"), fake = new Fake(m), handle = await h.load(m, fake);
    for (let i = 0; i < 5; i++) { fake.over.query = new Error("blip"); for (let j = 0; j < 2; j++) await rejects(handle.call("query", sample("retrieval", "query")), P.PluginFailed, /query/); delete fake.over.query; await handle.call("query", sample("retrieval", "query")); }
    expect(handle.state).toBe("active");
  });
});
describe("in a run: events, budget, pins, preflight", () => {
  async function withPlugin(over: any = {}, cfg: any = {}, budgets?: any) { const m = manifest("retrieval", undefined, "1.0.0", "in_process", over), h = mk([oa("d")], { plugins: { load: [m], required: ["retrieval"], ...cfg }, ...(budgets ? { budgets } : {}) }), { run, loop } = await h.createRun("u", "go", budgets ? { budgets } : {}); return { h, run, loop, handle: h.plugins.get("retrieval") as P.PluginHandle }; }
  it("every call in a run is audited without its payload and charged to the tool budget", async () => {
    const { h, run, handle } = await withPlugin(), before = (await run.proj("budgets")).agent.tools?.used ?? 0; await handle.call("query", { user: "u", query: "secret question", k: 1, filters: {} }, run);
    const ev = eventsOf(h, run, "plugin.called")[0].payload; expect([ev.plugin_id, ev.kind, ev.method, ev.ok, ev.trust, JSON.stringify(ev).includes("secret question")]).toEqual(["retrieval-fake", "retrieval", "query", true, "untrusted", false]);
    const st = (await run.proj("budgets")).agent; if (st.tools) expect([st.tools.used, st.tools.reserved]).toEqual([before + 1, 0]); await rejects(handle.call("query", { user: "u" }, run), P.ContractViolation, /input/);
  });
  it("a plugin call that would exceed the budget does not run", async () => {
    const b = { turns: 10, tokens: 100000, tools: 1 }, { run, handle } = await withPlugin({}, {}, b), fake = (handle.impl as P.InProcessPlugin).obj as Fake; fake.calls.length = 0;
    await handle.call("query", sample("retrieval", "query"), run); await rejects(handle.call("query", sample("retrieval", "query"), run), P.PluginBudgetExhausted, /budget/); expect(fake.calls.filter(([n]) => n === "query")).toHaveLength(1);   // the second call never reached the plugin
  });
  it("failures and quarantine are recorded in the run", async () => {
    const { h, run, handle } = await withPlugin(); ((handle.impl as P.InProcessPlugin).obj as Fake).over.query = new Error("down");
    for (let i = 0; i < 3; i++) await rejects(handle.call("query", sample("retrieval", "query"), run), P.PluginFailed, /query/);
    expect(eventsOf(h, run, "plugin.failed").map((e: any) => e.payload.consecutive_failures)).toEqual([1, 2, 3]); expect(eventsOf(h, run, "plugin.quarantined")[0].payload.after_failures).toBe(3);
  });
  it("a run pins the plugins it uses and a plugin swapped during the run stops it", async () => {
    const { h, run, loop } = await withPlugin(); expect([run.pins.resolve("plugin.retrieval-fake").manifest.version, run.pins.byId.get("plugin.retrieval-fake").version]).toEqual(["1.0.0", 1]);
    await h.plugins.replace(manifest("retrieval", undefined, "2.0.0"), new Fake(manifest("retrieval", undefined, "2.0.0")));   // someone swaps the plugin after the run was pinned
    await h.rm.start(run); await loop.runLoop(); const mm = eventsOf(h, run, "definition.mismatch")[0].payload;
    expect([mm.id, mm.reason.includes("changed after the run was pinned"), eventsOf(h, run, "model.requested").length]).toEqual(["plugin.retrieval-fake", true, 0]);
  });
  it("a plugin that was removed during the run stops it", async () => {
    const { h, run, loop } = await withPlugin(); await h.plugins.unload("retrieval-fake"); await h.rm.start(run); await loop.runLoop();
    expect([eventsOf(h, run, "definition.mismatch")[0].payload.reason.includes("no longer loaded"), eventsOf(h, run, "model.requested").length]).toEqual([true, 0]);
  });
  it("preflight requires the plugin kinds a deployment says it needs", async () => {
    const h = mk([oa("d")], { plugins: { required: ["retrieval", "proposer"] } }); await h.loadPlugins(); const pf = h.preflight(), c = pf.checks.find((x: any) => x.id === "PF-P01");
    expect([c.ok, c.message.includes("no active retrieval plugin"), pf.failed.some((x: any) => x.id === "PF-P01")]).toEqual([false, true, true]);
    const h2 = mk([oa("d")], { plugins: { load: [manifest("retrieval")], required: ["retrieval"] } }); await h2.loadPlugins(); expect(h2.preflight().failed.some((x: any) => x.id === "PF-P01")).toBe(false); expect(mk([oa("d")]).preflight().failed.some((x: any) => x.id === "PF-P01")).toBe(false);   // nothing required, nothing to check
  });
  it("a plugin that fails verification stops startup instead of being skipped", async () => {
    const bad = manifest("retrieval"); bad.interface_version = "9.0.0"; await rejects(mk([oa("d")], { plugins: { load: [bad] } }).createRun("u", "go"), P.ManifestInvalid, /not compatible/);
  });
});
describe("the component runtime: features follow plugins", () => {
  it("a feature that needs a plugin activates when it loads and deactivates when it is removed", async () => {
    const h = host(), feat = new Component("feature.search", () => [new Provide("feature.search", {})], { requires: { "plugin.retrieval": "^1.0.0" }, provides: { "feature.search": "1.0.0" } }), n = h.world.insert(feat); h.world.settle();
    expect(h.world.fibers.get(n)!.state).not.toBe("ACTIVE");   // nothing provides retrieval yet
    await h.load(manifest("retrieval", undefined, "1.4.0"), new Fake(manifest("retrieval", undefined, "1.4.0"))); expect(h.world.fibers.get(n)!.state).toBe("ACTIVE");
    await h.unload("retrieval-fake"); expect([h.world.fibers.get(n)!.state === "ACTIVE", h.has("retrieval")]).toEqual([false, false]);
    await h.load(manifest("retrieval", undefined, "2.0.0"), new Fake(manifest("retrieval", undefined, "2.0.0"))); expect(h.world.fibers.get(n)!.state).not.toBe("ACTIVE");   // a 2.x plugin does not satisfy ^1.0.0
  });
  it("replace swaps the plugin of a kind", async () => {
    const h = host(); await h.load(manifest("retrieval", undefined, "1.0.0"), new Fake(manifest("retrieval", undefined, "1.0.0"))); await h.replace(manifest("retrieval", undefined, "1.1.0"), new Fake(manifest("retrieval", undefined, "1.1.0")));
    expect([h.get("retrieval").manifest.version, h.handles.size, h.definitions()["plugin.retrieval-fake"].manifest.version]).toEqual(["1.1.0", 1, "1.1.0"]);
  });
});
describe("out of process (the Python plugin script, over the JSON-lines protocol)", () => {
  it("speaks the same contract and is killed when unloaded or when it hangs", async () => {
    const h = new PluginHost("standard"), handle = await h.load(PROC()), r = await handle.call("query", sample("retrieval", "query"));
    expect([r.value.passages[0].text, r.trust]).toEqual(["from a process", "untrusted"]); const proc = (handle.impl as P.SubprocessPlugin).proc!, pid = proc.pid!; await h.unload("proc-retrieval"); await new Promise((r) => setTimeout(r, 100)); expect(() => process.kill(pid, 0)).toThrow();   // unloading kills it
    await withTimeout("retrieval", "query", 0.5, async () => { await rejects(new PluginHost("standard").load(PROC("--slow")), P.ConformanceFailed, /query: timed out/); });   // a hanging process fails conformance
  });
  it("a subprocess that reports an error or dies is a plugin failure, not a crash", async () => {
    const h = new PluginHost("standard"), handle = await h.load(PROC()); await rejects(handle.impl.rpc("boom", {}), P.PluginFailed, /boom/);
    const p = (handle.impl as P.SubprocessPlugin).proc!; p.kill("SIGKILL"); await new Promise((r) => p.once("close", r)); expect((await handle.call("query", sample("retrieval", "query"))).value.passages.length).toBeGreaterThan(0);   // the next call starts a fresh process
    await h.close();
  });
});

describe("the evaluator kind (design §13.4)", () => {
  const EV = (type: string, kw: any = {}) => manifest("evaluator", "ev1", "1.0.0", "in_process", { evaluator: { type, ...kw } }), AGENT = { model_ref: "agent-model", credential_ref: "agent-key", context: "fresh" };
  it.each([["no evaluator block", null, /must declare evaluator.type/], ["a model judge without a model", { type: "llm_judge", credential_ref: "k" }, /must declare the model_ref and credential_ref/], ["a typed AI without a credential", { type: "typesafe_ai", model_ref: "m" }, /must declare the model_ref and credential_ref/],
    ["a code evaluator that names a model", { type: "code", model_ref: "m" }, /uses no model/], ["an unknown type", { type: "oracle" }, /oracle|allowed values/]])("an evaluator's manifest must declare its type and what a model-based one uses: %s", async (_n: any, ev: any, re: any) => {
    const m: any = manifest("evaluator"); delete m.evaluator; if (ev) m.evaluator = ev; await rejects(host().load(m, new Fake(m)), P.ManifestInvalid, re);
    const notev = manifest("retrieval", undefined, "1.0.0", "in_process", { evaluator: { type: "code" } }); await rejects(host().load(notev, new Fake(notev)), P.ManifestInvalid, /only an evaluator plugin/);
  });
  it("independence is derived from the agent under test and never declared", () => {
    const code = P.independenceLevel(EV("code"), AGENT); expect([code.level, code.shared]).toEqual(["deterministic", []]);
    const fresh = () => P.independenceLevel(EV("llm_judge", { model_ref: "judge-model", credential_ref: "judge-key", context: "fresh" }), AGENT); expect([fresh().level, fresh().shared]).toEqual(["independent_model", []]);
    for (const [ev, shared] of [[{ model_ref: "agent-model", credential_ref: "judge-key", context: "fresh" }, ["model"]], [{ model_ref: "judge-model", credential_ref: "agent-key", context: "fresh" }, ["credential"]], [{ model_ref: "judge-model", credential_ref: "judge-key", context: "shared" }, ["context"]], [{ model_ref: "agent-model", credential_ref: "agent-key", context: "shared" }, ["model", "credential", "context"]]] as [any, string[]][]) {
      const r = P.independenceLevel(EV("typesafe_ai", ev), AGENT); expect([r.level, r.shared, r.type]).toEqual(["same_model_low_independence", shared, "typesafe_ai"]);
    }
    expect(P.independenceLevel(EV("llm_judge", { model_ref: "m", credential_ref: "k" }), AGENT).shared).toEqual(["context"]);   // an unstated context is treated as shared, not assumed fresh
  });
  it("what may gate depends on the independence level and the profile", () => {
    const D = "deterministic", I = "independent_model", L = "same_model_low_independence", g = (l: string, p: string, m = false, b = false) => P.mayGate(l, p, m, b);
    expect([D, I, L].map((l) => g(l, "core")[0])).toEqual([true, true, true]); expect([D, I, L].map((l) => g(l, "standard")[0])).toEqual([true, true, false]); expect([D, I, L].map((l) => g(l, "high")[0])).toEqual([true, true, false]);
    expect([g(I, "high", true)[0], g(I, "high", true)[1].includes("second independent evaluator"), g(I, "high", true, true)[0], g(D, "high", true)[0], g(I, "standard", true)[0], g(L, "standard")[1].includes("advisory")]).toEqual([false, true, true, true, true, true]);
  });
  it("an evaluator of each type loads and its verdict is one of four", async () => {
    for (const [t, kw] of [["code", {}], ["llm_judge", { model_ref: "m", credential_ref: "k", context: "fresh" }], ["typesafe_ai", { model_ref: "m", credential_ref: "k", context: "fresh" }]] as [string, any][]) {
      const m = EV(t, kw), r = await (await new PluginHost("core").load(m, new Fake(m))).call("evaluate", sample("evaluator", "evaluate")); expect([["PASS", "FAIL", "PARTIAL", "UNCERTAIN"].includes(r.value.verdict), r.trust], t).toEqual([true, "untrusted"]);
    }
    const bad = EV("code"); await rejects(new PluginHost("core").load(bad, new Fake(bad, { evaluate: { verdict: "MAYBE", reason_codes: [], evidence_refs: [] } })), P.ConformanceFailed, /evaluate: output/);   // a verdict outside the four is refused
    await rejects(new PluginHost("core").load(bad, new Fake(bad, { evaluate: { verdict: "PASS" } })), P.ConformanceFailed, /evaluate: output/);   // and so is one without reasons and evidence
  });
});

describe("exfiltration monitoring: partial in the core, payload inspection only when demonstrated (design §10.6)", () => {
  const GOOD = { inspection_point: "egress proxy (endpoint instrumentation before encryption)", policy_applied: true, audit_records: true, coverage_gaps: ["QUIC"], bypass_conditions: ["a process that opens a raw socket"], verified: true };
  it("the core claims partial conformance and payload inspection only on demonstration", () => {
    expect([P.exfiltrationClaim(null).claim, P.exfiltrationClaim({}).claim, P.exfiltrationClaim({}).reasons[0].includes("no payload inspection point"), P.exfiltrationClaim({ tls_interception: true }).claim]).toEqual(["partial", "partial", true, "partial"]);   // supporting interception is not enough
    const ok = P.exfiltrationClaim({ egress_inspection: GOOD }); expect([ok.claim, ok.reasons, ok.coverage_gaps, ok.bypass_conditions]).toEqual(["payload_inspection", [], ["QUIC"], ["a process that opens a raw socket"]]);
    for (const [change, needle] of [[{ inspection_point: "" }, "no inspection point"], [{ policy_applied: false }, "policy is not applied"], [{ audit_records: false }, "not recorded for audit"], [{ coverage_gaps: null }, "coverage gaps are not documented"], [{ bypass_conditions: "none" }, "bypass conditions are not documented"], [{ verified: false }, "not been verified"]] as [any, string][]) {
      const r = P.exfiltrationClaim({ egress_inspection: { ...GOOD, ...change } }); expect([r.claim, r.reasons.some((x) => x.includes(needle))], needle).toEqual(["partial", true]);
    }
    expect([P.exfiltrationClaim({ egress_inspection: "yes" }).claim, P.exfiltrationClaim({ egress_inspection: { ...GOOD, coverage_gaps: [] } }).claim]).toEqual(["partial", "payload_inspection"]);   // an empty list is documented; a missing one is not
  });
});

describe("conformance runs on the live plugin, so it must not touch real data", () => {
  it("uses reserved ids for anything that changes state and never a real one", async () => {
    const m = manifest("retrieval"), f = new Fake(m); await host().load(m, f);
    expect([f.calls.filter(([n]) => n === "index").map(([, p]) => p), f.calls.filter(([n]) => n === "forget").map(([, p]) => p)]).toEqual([[{ documents: [{ id: "__conformance_doc__", text: "conformance check" }] }], [{ ids: ["__conformance_doc__"] }]]);
    const m2 = manifest("user_model"), f2 = new Fake(m2); await host().load(m2, f2); expect([...new Set(f2.calls.filter(([, p]) => p.user !== undefined).map(([, p]) => p.user))]).toEqual(["__conformance_user__"]);   // no real user is observed, predicted or forgotten
  });
  it("uses the ids the plugin created and never expects a particular one", async () => {
    const needs = (created: string, field: string, out: any) => (p: any) => { if (p[field] !== created) throw new P.PluginRejection("not_found", String(p[field])); return out; };
    const cases: [string, any, string, string][] = [["computer_use", { open: { session: "abc-123" }, observe: needs("abc-123", "session", { observation: {} }), act: needs("abc-123", "session", { observation: {} }), close: needs("abc-123", "session", { closed: true }) }, "session", "abc-123"],
      ["research", { fork: { fork_id: "fk-7" }, step: needs("fk-7", "fork_id", { state: {} }), stop: needs("fk-7", "fork_id", { stopped: true }) }, "fork_id", "fk-7"],
      ["isolation", { provision: { sandbox_id: "box-9", effective: {} }, verify: needs("box-9", "sandbox_id", { ok: true, observed: {} }), destroy: needs("box-9", "sandbox_id", { destroyed: true }) }, "sandbox_id", "box-9"]];
    for (const [kind, over, field, id] of cases) { const m = manifest(kind), f = new Fake(m, over), h = host(); await h.load(m, f); expect(h.has(kind), kind).toBe(true); expect([...new Set(f.calls.filter(([, p]) => p[field] !== undefined).map(([, p]) => p[field]))], kind).toEqual([id]); }   // every later call used that id
    await rejects(host().load(manifest("computer_use"), new Fake(manifest("computer_use"), { open: { session: "abc-123" }, observe: needs("something-else", "session", { observation: {} }) })), P.ConformanceFailed, /observe/);   // and a plugin that breaks its own ids does not load
  });
});
