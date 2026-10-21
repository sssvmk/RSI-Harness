/** Crash recovery (design §5.9, §19.5): kill at event boundaries, restart on the same files, verify invariants. */
import fs from "node:fs";
import path from "node:path";
import { DatabaseSync } from "node:sqlite";
import { describe, expect, it } from "vitest";
import { Harness } from "../src/app.js";
import { loadTask } from "../src/eval.js";
import { CallableVerifier } from "../src/verifier.js";
import { FakeClock } from "../src/util.js";
import { foldAll } from "../src/projections.js";
import { FIN, call, eventsOf, makeTask, mk, oa, sleep, solution, tmpdir, waitFor } from "./helpers.js";

const PASS = () => new CallableVerifier(async () => ({ passed: true, detail: "ok", evidence: {} }));
const script = (tmp: string) => [call("fs.write", { path: "/workspace/a.txt", content: "A" }), call("shell.run", { cmd: `echo x >> ${tmp}/execs.log` }), FIN()];   // the host-side log survives teardown, so executions can be counted
const executions = (tmp: string) => { const p = path.join(tmp, "execs.log"); return fs.existsSync(p) ? fs.readFileSync(p, "utf8").split("\n").filter(Boolean).length : 0; };

/** A 'machine' that can be killed (writer goes inert right after a durable commit) and rebooted on the same disk. */
class World {
  h!: Harness & any; crashed = false; task!: Promise<any>;
  constructor(public tmp: string, public script: any[], public over: any = {}, public clock = new FakeClock()) { this.boot(script); }
  boot(s: any[]) { this.crashed = false; this.h = mk(s, { tmp: this.tmp, clock: this.clock, ...this.over }); }
  arm(pred: (ev: any) => boolean) {
    const orig = this.h.writer.submit.bind(this.h.writer);
    this.h.writer.submit = async (d: any) => { const ev = await orig(d); if (pred(ev)) { this.crashed = true; this.h.writer.close(); return new Promise(() => {}); } return ev; };   // acked to nobody: the process died right after the commit
  }
  async runUntilCrash(fn: () => Promise<any>, ms = 6000) { const t = fn().catch(() => {}); this.task = t; const end = Date.now() + ms; while (!this.crashed && Date.now() < end) { const done = await Promise.race([t.then(() => true), sleep(10).then(() => false)]); if (done) break; } return this.h.store.listRuns()[0].run_id as string; }
  kill(runId: string) { const responded = this.h.store.events(runId, { types: ["model.responded"] }).length; this.h.close(); return responded; }
  reboot(responded: number) { this.boot(this.script.slice(responded)); }
}
const after = (type: string, nth = 1) => { let n = 0; return (e: any) => e.type === type && ++n === nth; };
const invariants = (h: any, rid: string) => { expect(h.store.verify(rid).ok).toBe(true); const done = eventsOf(h, { runId: rid }, "tool.call.completed").map((e: any) => e.payload.tool_call_id); expect(new Set(done).size).toBe(done.length); };
const go = (w: World, owner = "u") => w.h.runToEnd(owner, "go", { verifier: PASS() });

describe("targeted recovery scenarios", () => {
  it("a non-idempotent call in flight is never retried: recovery.uncertain and AWAITING_HUMAN", async () => {
    const tmp = tmpdir(), w = new World(tmp, script(tmp)); w.arm((e) => e.type === "tool.call.started" && e.payload.tool === "shell.run");
    const rid = await w.runUntilCrash(() => go(w)); const n = w.kill(rid); w.reboot(n);
    const { plan, final } = await w.h.resumeRun(rid, { verifier: PASS() }); const run = w.h.rm.get(rid);
    expect(final).toBe("AWAITING_HUMAN"); expect(plan.halted).toBe("recovery_uncertain"); expect(eventsOf(w.h, run, "recovery.uncertain")[0].payload.tool).toBe("shell.run"); expect(executions(tmp)).toBe(0);
    expect(eventsOf(w.h, run, "tool.call.started")).toHaveLength(2); invariants(w.h, rid);
  });
  it("an idempotent call in flight is re-run and the run completes", async () => {
    const tmp = tmpdir(), w = new World(tmp, script(tmp)); w.arm((e) => e.type === "tool.call.started" && e.payload.tool === "fs.write");
    const rid = await w.runUntilCrash(() => go(w)); const n = w.kill(rid); w.reboot(n); const { final } = await w.h.resumeRun(rid, { verifier: PASS() }), run = w.h.rm.get(rid);
    expect(final).toBe("COMPLETED"); expect(eventsOf(w.h, run, "tool.call.started").filter((e) => e.payload.tool === "fs.write").map((e) => e.payload.attempt)).toEqual([1, 1]);
    expect(eventsOf(w.h, run, "tool.call.completed")).toHaveLength(3); expect(eventsOf(w.h, run, "recovery.uncertain")).toHaveLength(0); invariants(w.h, rid);
  });
  it("recovery does not repeat an action whose effect is already in the world", async () => {
    const tmp = tmpdir(), w = new World(tmp, script(tmp)); w.arm((e) => e.type === "tool.call.started" && e.payload.tool === "fs.write");
    const rid = await w.runUntilCrash(() => go(w)); const n = w.kill(rid); w.reboot(n);
    const sbx = path.join(tmp, "sbx"), ws = fs.readdirSync(sbx).map((d) => path.join(sbx, d, "workspace")).find((d) => fs.existsSync(d))!; fs.writeFileSync(path.join(ws, "a.txt"), "A");   // the action took effect before the crash
    const { plan, final } = await w.h.resumeRun(rid, { verifier: PASS() }), run = w.h.rm.get(rid);
    expect(final).toBe("COMPLETED"); expect(eventsOf(w.h, run, "tool.call.started").filter((e) => e.payload.tool === "fs.write")).toHaveLength(1);   // not run a second time
    expect(eventsOf(w.h, run, "tool.call.completed").some((e) => JSON.stringify(e.payload).includes("after recovery"))).toBe(true); expect(plan.notes.some((x: string) => x.includes("from state"))).toBe(true); invariants(w.h, rid);
  });
  it("recovery halts rather than guess when the resource's state cannot be read", async () => {
    const tmp = tmpdir(), w = new World(tmp, script(tmp)); w.arm((e) => e.type === "tool.call.started" && e.payload.tool === "fs.write");
    const rid = await w.runUntilCrash(() => go(w)); const n = w.kill(rid); w.reboot(n); w.h.runtime.gate.adapters.workspace_file.readState = async () => null;
    const { plan, final } = await w.h.resumeRun(rid, { verifier: PASS() }), run = w.h.rm.get(rid);
    expect([final, plan.halted]).toEqual(["AWAITING_HUMAN", "recovery_uncertain"]); expect(eventsOf(w.h, run, "recovery.uncertain")[0].payload.reason).toContain("not known whether"); expect(eventsOf(w.h, run, "tool.call.started").filter((e) => e.payload.tool === "fs.write")).toHaveLength(1);
  });
  it("a model call in flight is re-issued and counts against the budget again", async () => {
    const tmp = tmpdir(), w = new World(tmp, script(tmp)); w.arm(after("model.requested", 2)); const rid = await w.runUntilCrash(() => go(w));
    const before = foldAll("budgets", w.h.store.events(rid)).agent.turns.used;   // fold from the log: the dead writer cannot serve cached projections
    const n = w.kill(rid); w.reboot(n); const { plan, final } = await w.h.resumeRun(rid, { verifier: PASS() });
    expect(plan.reissue_model).toBe(true); expect(final).toBe("COMPLETED"); expect([before, (await w.h.rm.get(rid).proj("budgets")).agent.turns.used]).toEqual([1, 4]); invariants(w.h, rid);
  });
  it("a corrupted log is detected at recovery", async () => {
    const tmp = tmpdir(), w = new World(tmp, script(tmp)); w.arm(after("tool.call.completed", 1)); const rid = await w.runUntilCrash(() => go(w)); const n = w.kill(rid);
    const c = new DatabaseSync(path.join(tmp, "log.db")); c.exec("DROP TRIGGER events_no_update"); c.exec(`UPDATE events SET payload='{"x":1}' WHERE run_id='${rid}' AND seq=7`); c.close();
    w.reboot(n); const { plan, final } = await w.h.resumeRun(rid, { verifier: PASS() }); expect([plan.ok, plan.halted, final]).toEqual([false, "integrity_failure", "FAILED_INTEGRITY"]); expect(eventsOf(w.h, w.h.rm.get(rid), "integrity.alert")[0].payload.bad_seq).toBe(7);
  });
  it.each([["harness_failure", 100, 100_000], ["harness_failure", 900, 300_000], ["operator", 100, 0]])("downtime (%s, %is) excludes %ims", async (cause, s, expected) => {
    const tmp = tmpdir(), w = new World(tmp, script(tmp)); w.arm(after("tool.call.completed", 1)); const rid = await w.runUntilCrash(() => go(w)); w.kill(rid); w.clock.advance(s * 1000); w.reboot(1);
    await w.h.resumeRun(rid, { verifier: PASS(), downtimeCause: cause }); const rec = eventsOf(w.h, w.h.rm.get(rid), "run.recovered")[0].payload;
    expect(rec.downtime_ms).toBeGreaterThanOrEqual(s * 1000); expect(rec.excluded_ms).toBe(expected); expect((await w.h.rm.get(rid).proj("run_state")).excluded_ms).toBe(expected);
  });
  it("an open approval survives a restart and the command executes exactly once", async () => {
    const rules = { schema_version: 1, rules: [{ id: "R", action: "require_approval", match: { tool: ["shell.run"] } }] }, tmp = tmpdir(), w = new World(tmp, script(tmp), { approvals: { approvers: ["boss"] }, ruleset: rules });
    w.arm(after("approval.requested")); const rid = await w.runUntilCrash(() => go(w)); const n = w.kill(rid); w.reboot(n);
    const resumed = w.h.resumeRun(rid, { verifier: PASS() }), run = w.h.rm.get(rid); await waitFor(() => w.h.store.runRow(rid).state === "AWAITING_APPROVAL");
    const open = Object.keys(await run.proj("approvals_open")); expect(open).toHaveLength(1); expect(eventsOf(w.h, run, "approval.requested")).toHaveLength(1);
    await w.h.approvals.decide(run, open[0], "boss", ["approver"], true); const { final } = await resumed; expect(final).toBe("COMPLETED");
    expect(eventsOf(w.h, run, "tool.call.started").filter((e) => e.payload.tool === "shell.run")).toHaveLength(1); expect(executions(tmp)).toBe(1);
  });
  it("a crash in the middle of termination finishes teardown exactly once", async () => {
    const tmp = tmpdir(), w = new World(tmp, Array(5).fill(call("fs.glob", { pattern: "/workspace/*" })), { budgets: { turns: 1 } }); w.arm(after("teardown.started"));
    const rid = await w.runUntilCrash(() => w.h.runToEnd("u", "go")); expect(w.h.store.runRow(rid).state).toBe("TERMINATING"); const n = w.kill(rid); w.reboot(n);
    const { final } = await w.h.resumeRun(rid), run = w.h.rm.get(rid); expect(final).toBe("TERMINATED"); expect(await run.proj("grants_active")).toEqual({}); expect(await run.proj("sandboxes")).toEqual({}); expect(eventsOf(w.h, run, "run.terminated")).toHaveLength(1); invariants(w.h, rid);
  });
  it.each(["submission.created", "snapshot.created", "judge.started", "judge.completed", "feedback.delivered"])("a crash after %s inside the submit sequence recovers without a second submission", async (crashAt) => {
    const tmp = tmpdir(), s = [call(...solution(0.7)[0]), call(...solution(0.7)[1]), FIN()], w = new World(tmp, s), task = loadTask(makeTask(tmp, "t")); w.arm(after(crashAt));
    const rid = await w.runUntilCrash(async () => { const { run, loop } = await w.h.createEvalRun("u", task); await w.h.rm.start(run); await loop.runLoop(); }); const n = w.kill(rid); w.reboot(n);
    const { plan, final } = await w.h.resumeRun(rid, { task }), run = w.h.rm.get(rid); expect(final, JSON.stringify(plan)).toBe("COMPLETED");
    expect(eventsOf(w.h, run, "submission.created")).toHaveLength(1); expect(eventsOf(w.h, run, "judge.completed")).toHaveLength(1); expect(eventsOf(w.h, run, "feedback.delivered")).toHaveLength(1);
    const res = eventsOf(w.h, run, "tool.call.completed").filter((e) => e.payload.tool === "submit"); expect(res).toHaveLength(1); expect(res[0].payload.message.parts[0].parts[0].text).toBe("Score: 0.7");
    expect(await run.proj("sandboxes")).toEqual({}); expect(await run.proj("grants_active")).toEqual({}); invariants(w.h, rid);
  });
});
describe("chaos sweep", () => {
  it.each(Array.from({ length: 21 }, (_, i) => 1 + i * 3))("kill after event #%i, reboot, resume: invariants hold and the outcome is legal", async (k) => {
    const tmp = tmpdir(), w = new World(tmp, script(tmp)); let c = 0; w.arm(() => ++c === k);
    const rid = await w.runUntilCrash(() => go(w)); if (!w.crashed) return;    // crash point was after the run finished
    if (["COMPLETED", "TERMINATED"].includes(w.h.store.runRow(rid).state)) { w.kill(rid); return; }
    const hasBudgets = w.h.store.lastSeq(rid) > 1, b0 = hasBudgets ? foldAll("budgets", w.h.store.events(rid)).agent : null;
    const n = w.kill(rid); w.reboot(n); const { final } = await w.h.resumeRun(rid, { verifier: PASS() }), run = w.h.rm.get(rid);
    expect(["COMPLETED", "AWAITING_HUMAN", "TERMINATED"]).toContain(final);
    if (final === "TERMINATED") expect(eventsOf(w.h, run, "run.terminated")[0].payload.detail).toContain("crashed during");   // only legal before the agent loop existed
    if (final === "AWAITING_HUMAN") expect(eventsOf(w.h, run, "recovery.uncertain")[0].payload.tool).toBe("shell.run");     // only legal for an unresolved non-idempotent call
    const b = (await run.proj("budgets")).agent; if (b0?.turns) { expect(b.turns.used).toBeGreaterThanOrEqual(b0.turns.used); expect(b.tokens.used).toBeGreaterThanOrEqual(b0.tokens.used); }   // counters never reset
    expect(executions(tmp)).toBeLessThanOrEqual(1); invariants(w.h, rid);
  });
});
void oa;
