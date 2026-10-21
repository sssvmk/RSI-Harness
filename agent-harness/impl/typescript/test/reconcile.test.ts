/** Lifecycle reconciliation and the pre-action gate (design §5.13). Mirrors test_reconcile.py against the same vectors. */
import fs from "node:fs";
import path from "node:path";
import { describe, expect, it } from "vitest";
import * as R from "../src/reconcile.js";
import { CallableVerifier } from "../src/verifier.js";
import { call, eventsOf, mk, oa } from "./helpers.js";

const V = JSON.parse(fs.readFileSync(path.resolve(import.meta.dirname, "../../../spec/vectors/reconcile-vectors.json"), "utf8"));
const PASS = () => new CallableVerifier(async () => ({ passed: true, detail: "ok", evidence: {} })), WRITE = (c = "A", p = "/workspace/a.txt") => call("fs.write", { path: p, content: c }), FINISH = () => call("finish", { summary: "done" });
const proto = R.WorkspaceFileAdapter.prototype as any, wf = (h: any) => h.runtime.gate.adapters.workspace_file, subj = "workspace_file:/workspace/a.txt";
const throwsRe = (fn: () => any, re: RegExp) => { try { fn(); } catch (e: any) { expect(String(e.message)).toMatch(re); return; } throw new Error("did not throw"); };

/** A scripted adapter: reads, the precondition result and the effect check are all scripted. Mirrors tests/reconcile_kit.py. */
class Scripted implements R.WorldAdapter {
  reads: any[]; n = 0; constructor(reads: any[], public pre = true, public eff = true) { this.reads = [...reads]; }
  async readState() { this.n++; if (!this.reads.length) return null; const r = this.reads.shift(); if (r === "raise") throw new Error("read failed"); return r; }
  async checkPreconditions() { return this.pre; } async effectsHold() { return this.eff; }
}
async function runScenario(sc: any) {
  const ev: any[] = [], emit = async (t: string, p: any) => { ev.push([t, p]); }, ad = sc.adapter ? new Scripted(sc.adapter.reads, sc.adapter.pre ?? true, sc.adapter.eff ?? true) : null;
  const gate = new R.Gate(ad ? { fake: ad } : {}, sc.max_age_s ?? 900), act = sc.action, out: any = {};
  if (["pre", "pre_post", "guarded"].includes(sc.call)) {
    const pre = await gate.pre(emit, act, null, sc.now_s ?? 1000); out.pre = { status: pre.status, reason: pre.reason, actual: pre.actual };
    if (pre.status === "ok" && sc.call === "pre_post") { const post: any = await gate.post(emit, act, pre.actual, "r"); out.post = { status: post.status, reason: post.reason ?? null, observed: post.observed }; }
    if (pre.status === "ok" && sc.call === "guarded") { let did = 0; try { await gate.guarded(act, pre.actual, async () => { did++; return "done"; }); out.guarded = { conflict: false, executed: did }; } catch (e) { if (!(e instanceof R.VersionConflict)) throw e; out.guarded = { conflict: true, executed: did }; } }
  } else if (sc.call === "establish") out.establish = await gate.establishOutcome(emit, act, sc.before);
  out.events = ev; out.reads = ad ? ad.n : 0; return JSON.parse(JSON.stringify(out));
}
async function runWith(script: any[], patch?: (h: any) => void, manifest?: (t: any) => void, over: any = {}) {
  const h = mk(script, over); if (manifest) manifest(h.registry.get("fs.write")); if (patch) patch(h); return { h, run: await h.runToEnd("u", "go", { verifier: PASS() }) };
}

describe("the pure parts, against the shared vectors", () => {
  it("every state-event pair matches, and unknown never becomes consistent without evidence", () => {
    for (const c of V.transitions) {
      let nxt: any; try { nxt = R.nextState(c.state, c.event, c.authoritative); } catch { nxt = "ILLEGAL"; }
      expect([nxt, R.applyEvent(c.state, c.event, c.authoritative)], JSON.stringify(c)).toEqual([c.next, c.apply]);
    }
    expect([R.nextState("unknown", "verified", false), R.nextState("unknown", "verified", true)]).toEqual(["unknown", "consistent"]);
    for (const s of ["unknown", "recovery_required", "reconciling", "blocked"]) expect(R.applyEvent(s, "verified", false)).toBe(s);   // no evidence, no change
    expect([R.applyEvent("drift_detected", "verified", true), R.applyEvent("drift_detected", "verified", false), R.applyEvent("consistent", "start_reconcile"), R.applyEvent("blocked", "repair_failed")]).toEqual(["consistent", "reconciling", "consistent", "blocked"]);
  });
  it("risk tiers follow the impact of the action and add requirements as they rise", () => {
    expect(Object.fromEntries(R.RISKS.map((r) => [r, R.requirements(r)]))).toEqual(V.requirements);
    for (const c of V.risk_of) expect(R.riskOf(c.effect_class, c.declared, c.critical), JSON.stringify(c)).toBe(c.risk);
    for (let i = 0; i + 1 < R.RISKS.length; i++) for (const [k, v] of Object.entries<boolean>(R.requirements(R.RISKS[i]))) expect(R.requirements(R.RISKS[i + 1])[k as keyof ReturnType<typeof R.requirements>] || !v).toBe(true);   // each tier needs at least what the one below needs
    throwsRe(() => R.requirements("catastrophic"), /unknown risk/);
  });
  it("approval freshness matches", () => { for (const c of V.approval) expect(R.approvalCurrent(c.approval, c.now_s, c.max_age_s), JSON.stringify(c)).toBe(c.current); });
  it("the gate scenarios match exactly, including the events emitted", async () => { for (const c of V.scenarios) expect(await runScenario(c), c.name).toEqual(c.expect); });
});
/** Patch an adapter method for the duration of `body`, and always put the original back. */
async function patched<T>(method: string, fn: (...a: any[]) => any, body: () => Promise<T>): Promise<T> {
  const orig = Object.getOwnPropertyDescriptor(proto, method)!; Object.defineProperty(proto, method, { value: fn, writable: true, configurable: true });
  try { return await body(); } finally { Object.defineProperty(proto, method, orig); }
}
const slowHandler = (write: boolean, calls: number[]) => async (a: any, t: any) => { calls.push(1); if (write) t.sandbox.executor.fsWrite(a.path, a.content); await new Promise((r) => setTimeout(r, 2000)); return "wrote"; };
const timeoutTo = (t: any) => { t.manifest.timeout_s = 0.2; };
describe("through the real tool pipeline", () => {
  it("a verified write is checked before and read back after and leaves the subject consistent", async () => {
    const { h, run } = await runWith([WRITE(), oa("d")]), ev = h.store.events(run.runId).filter((e: any) => e.type.startsWith("reconcile.")).map((e: any) => [e.type, e.payload.result ?? null]);
    expect(ev).toEqual([["reconcile.checked", "ok_to_execute"], ["reconcile.checked", "verified"], ["reconcile.state_changed", null]]);
    expect(Object.fromEntries(Object.entries<any>((await run.proj("reconcile")).subjects).map(([k, v]) => [k, v.state]))).toEqual({ [subj]: "consistent" });
    const c = eventsOf(h, run, "reconcile.checked")[0].payload; expect([c.risk, c.action_id]).toEqual(["medium", "ID1"]);
  });
  it("a tool that says it succeeded is not believed when the state disagrees", async () => {
    const { h, run } = await patched("effectsHold", async () => false, () => runWith([WRITE(), FINISH(), FINISH(), FINISH()]));
    expect(eventsOf(h, run, "tool.call.failed")[0].payload.error.code).toBe("reconcile.effect_mismatch"); expect((await run.proj("reconcile")).subjects[subj].state).toBe("drift_detected");
    const v = eventsOf(h, run, "verification.completed")[0].payload; expect([v.passed, v.detail.includes("required effects remain unverified"), v.detail.includes("drift_detected"), await run.state()]).toEqual([false, true, true, "TERMINATED"]);   // not complete, though the verifier would pass
  });
  it("a run completes once the unverified effect is verified by a later action", async () => {
    let n = 0; const { h, run } = await patched("effectsHold", async () => ++n > 1, () => runWith([WRITE(), WRITE(), FINISH()]));   // the first read-back disagrees, the second agrees
    expect([await run.state(), (await run.proj("reconcile")).subjects[subj].state]).toEqual(["COMPLETED", "consistent"]);
    expect(eventsOf(h, run, "reconcile.checked").map((e: any) => e.payload.result).filter((r: string) => ["effect_mismatch", "verified"].includes(r))).toEqual(["effect_mismatch", "verified"]);
  });
  it("an unreadable result is recorded unknown and blocks completion until verified", async () => {
    const orig = proto.readState; let n = 0;
    const { h, run } = await patched("readState", async function (this: any, r: string, c: any) { return ++n === 3 ? null : orig.call(this, r, c); }, () => runWith([WRITE(), FINISH(), WRITE(), FINISH()]));   // pre, the guard, then the read-back fails
    expect(eventsOf(h, run, "tool.call.completed")[0].payload.message.parts[0].parts[0].text).toContain("recorded as unverified");
    expect(eventsOf(h, run, "verification.completed")[0].payload.detail).toContain(`required effects remain unverified: ${subj} (unknown)`); expect([await run.state(), (await run.proj("reconcile")).subjects[subj].state]).toEqual(["COMPLETED", "consistent"]);   // the second, verified write resolves it
  });
  it("a resource that changes between the check and the action blocks the action", async () => {
    const orig = proto.readState; let n = 0;
    const { h, run } = await patched("readState", async function (this: any, r: string, c: any) { const x = await orig.call(this, r, c); return ++n === 2 && x ? { version: "someone-else-wrote-it", value: "x" } : x; }, () => runWith([WRITE("B"), oa("d")]));   // the guard's re-read sees a different version
    expect([eventsOf(h, run, "tool.call.failed")[0].payload.error.code, eventsOf(h, run, "action.blocked")[0].payload.reason, eventsOf(h, run, "tool.call.completed").length, (await run.proj("reconcile")).subjects[subj].state]).toEqual(["action.blocked", "stale_state", 0, "drift_detected"]);
  });
  it("an action that cannot be verified does not run", async () => {
    const { h, run } = await patched("readState", async () => null, () => runWith([WRITE(), oa("d")])), f = eventsOf(h, run, "tool.call.failed")[0].payload.error;
    expect([f.code, f.message.includes("world_state_unavailable"), eventsOf(h, run, "tool.call.started").length]).toEqual(["action.blocked", true, 0]); const b = eventsOf(h, run, "action.blocked")[0].payload; expect([b.reason, b.risk]).toEqual(["world_state_unavailable", "medium"]);
  });
  it("the risk follows the impact and a policy can mark a tool critical", async () => {
    const h = mk([oa("d")], { reconcile: { critical_tools: ["fs.write"] } }), { run } = await h.createRun("u", "go"), m = h.registry.get("fs.write")!.manifest;
    expect(h.runtime.worldAction(run, "c", "fs.write", { path: "/workspace/x", content: "y" }, m).risk).toBe("critical");
    const h2 = mk([oa("d")]), { run: r2 } = await h2.createRun("u", "go"); expect(h2.runtime.worldAction(r2, "c", "fs.write", { path: "/workspace/x", content: "y" }, h2.registry.get("fs.write")!.manifest).risk).toBe("medium");   // from its effect class
    expect(h2.runtime.worldAction(r2, "c", "fs.read", { path: "/x" }, h2.registry.get("fs.read")!.manifest)).toBeNull();   // read-only tools carry no world block
    const h3 = mk([WRITE(), oa("d")], { reconcile: { critical_tools: ["fs.write"] } }), r3 = await h3.runToEnd("u", "go", { verifier: PASS() });
    expect([eventsOf(h3, r3, "action.blocked")[0].payload.reason, eventsOf(h3, r3, "tool.call.failed")[0].payload.error.category]).toEqual(["human_intervention_required", "security"]);
  });
});
describe("a timeout is not a failure", () => {
  it("a timed-out action that took effect is verified from state and never repeated", async () => {
    const calls: number[] = [], { h, run } = await runWith([WRITE(), oa("d")], (h) => { h.registry.get("fs.write").handler = slowHandler(true, calls); }, timeoutTo), text = eventsOf(h, run, "tool.call.completed")[0].payload.message.parts[0].parts[0].text;
    expect([calls.length, text.includes("had already taken effect"), text.includes("was not repeated"), eventsOf(h, run, "tool.call.failed").length, eventsOf(h, run, "tool.call.started").length]).toEqual([1, true, true, 0, 1]);   // one execution, not two
    expect(eventsOf(h, run, "reconcile.checked").map((e: any) => e.payload.result)).toContain("applied_before_retry");
  });
  it("a timed-out action that did not take effect is retried", async () => {
    const calls: number[] = [], handler = async (a: any, t: any) => { calls.push(1); if (calls.length === 1) await new Promise((r) => setTimeout(r, 2000)); t.sandbox.executor.fsWrite(a.path, a.content); return "wrote"; };   // the first attempt hangs without writing
    const { h, run } = await runWith([WRITE(), oa("d")], (h) => { h.registry.get("fs.write").handler = handler; }, timeoutTo);
    expect([calls.length, eventsOf(h, run, "tool.call.started").map((e: any) => e.payload.attempt)]).toEqual([2, [1, 2]]); expect(eventsOf(h, run, "reconcile.checked").map((e: any) => e.payload.result)).toContain("not_applied");
  });
  it("a timed-out action whose state cannot be read is not retried and stays unknown", async () => {
    const calls: number[] = [], orig = proto.readState; let n = 0;
    const { h, run } = await patched("readState", async function (this: any, r: string, c: any) { return ++n >= 3 ? null : orig.call(this, r, c); }, () => runWith([WRITE(), oa("d")], (h) => { h.registry.get("fs.write").handler = slowHandler(true, calls); }, timeoutTo));   // unreadable once the action has timed out
    const f = eventsOf(h, run, "tool.call.failed")[0].payload.error; expect([f.code, f.retryable, calls.length, (await run.proj("reconcile")).subjects[subj].state]).toEqual(["reconcile.unknown", false, 1, "unknown"]);
  });
});
