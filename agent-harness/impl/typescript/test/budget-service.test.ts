/** Budget service (design §7.4.4): reservations, unknown usage, hierarchy, concurrency, and unmetered safeguards. Mirrors test_budget_service.py. */
import fs from "node:fs";
import path from "node:path";
import { describe, expect, it } from "vitest";
import { BudgetController, ConcurrencyLimiter, DeadlineExceeded, QueueFull, available, checkAllocation, checkReserve, reserveCost, safeguardProblems, unmetered } from "../src/budgets.js";
import { GatewayError } from "../src/gateway.js";
import { RecoveryManager } from "../src/recovery.js";
import { LocalExecutor, LocalProcessBackend } from "../src/sandbox.js";
import { FIN, call, eventsOf, makeTask, mk, oa, sleep, solution, tmpdir } from "./helpers.js";
import { loadTask } from "../src/eval.js";

const VEC = path.resolve(import.meta.dirname, "../../../spec/vectors"), vec = JSON.parse(fs.readFileSync(path.join(VEC, "budget-vectors.json"), "utf8"));
async function make(budgets: any = { turns: 10, tokens: 1000 }) { const h = mk([oa("d")], { budgets }), { run } = await h.createRun("u", "go", { budgets }); return { h, run, bc: new BudgetController(run) }; }
const st = (run: any) => run.proj("budgets");
const exec = (limits: any = {}) => { const d = tmpdir(); fs.mkdirSync(path.join(d, "w")); fs.mkdirSync(path.join(d, "s")); return new LocalExecutor({ "/workspace": path.join(d, "w"), "/scratch": path.join(d, "s") }, undefined, limits); };

describe("reserve, commit, release", () => {
  it("a reservation holds headroom and commit replaces the estimate with actual usage", async () => {
    const { h, run, bc } = await make(); const [rid, ex] = await bc.reserve({ turns: 1, tokens: 300 }); expect(ex).toBeNull();
    let t = (await st(run)).agent.tokens; expect([t.reserved, t.used]).toEqual([300, 0]);
    await bc.commit(rid!, { turns: 1, tokens: 120 }); const s = (await st(run)).agent; expect([s.tokens.used, s.tokens.reserved, s.turns.used]).toEqual([120, 0, 1]);
    const [rid2] = await bc.reserve({ tokens: 200 }); await bc.release(rid2!, "call did not run"); expect((await st(run)).agent.tokens.reserved).toBe(0); expect(Object.keys((await st(run)).reservations)).toEqual([]);
    expect(h.store.events(run.runId).map((e: any) => e.type).filter((t: string) => t.startsWith("budget.") && t !== "budget.updated")).toEqual(["budget.reserved", "budget.committed", "budget.reserved", "budget.released"]);
  });
  it("a reservation that would exceed the limit is refused and names the dimension", async () => {
    const { h, run, bc } = await make(); const [r1] = await bc.reserve({ tokens: 700 }), [rid, ex] = await bc.reserve({ tokens: 400 });
    expect([rid, ex]).toEqual([null, "tokens"]); const ev = eventsOf(h, run, "budget.exhausted"); expect([ev[0].payload.dimension, ev[0].payload.reserved]).toEqual(["tokens", 700]);
    await bc.release(r1!, "x"); expect((await bc.reserve({ tokens: 400 }))[0]).toBeTruthy();
  });
  it("concurrent callers cannot reserve the same remainder", async () => {
    const { run } = await make({ turns: 100, tokens: 1000 }); const results = await Promise.all(Array.from({ length: 20 }, () => new BudgetController(run).reserve({ tokens: 130 })));
    expect([results.filter(([r]) => r).length, results.filter(([, ex]) => ex === "tokens").length]).toEqual([7, 13]); const t = (await st(run)).agent.tokens; expect(t.reserved).toBe(910); expect(t.reserved + t.used).toBeLessThanOrEqual(t.limit);
  });
  it("usage that is unknown is charged at the estimate, flagged, and replaced when known", async () => {
    const { h, run, bc } = await make(); const [rid] = await bc.reserve({ turns: 1, tokens: 400 }); await bc.commit(rid!, { turns: 1, tokens: 400 }, { unreconciled: true });
    let s = await st(run); expect([s.agent.tokens.used, s.agent.tokens.unreconciled, rid! in s.unreconciled]).toEqual([400, 400, true]); expect(eventsOf(h, run, "budget.unreconciled")[0].payload.reservation_id).toBe(rid);
    await bc.commit(rid!, { tokens: 90 }, { reconciles: true }); s = await st(run); expect([s.agent.tokens.unreconciled, Object.keys(s.unreconciled).length]).toEqual([0, 0]);
  });
  it("the model call reserves the worst case, commits actual usage, and charges a turn even when the call fails", async () => {
    const b = { turns: 10, tokens: 100000 }, h = mk([oa("d")], { budgets: b }), run = await h.runToEnd("u", "go");
    const ev = h.store.events(run.runId).filter((e: any) => e.type === "budget.reserved" || e.type === "budget.committed");
    expect([ev[0].type, ev[1].type]).toEqual(["budget.reserved", "budget.committed"]); expect(ev[0].payload.deltas.tokens).toBeGreaterThanOrEqual(4096); expect(ev[1].payload.actual.tokens).toBeLessThan(ev[0].payload.deltas.tokens); expect((await st(run)).agent.tokens.reserved).toBe(0);
    for (const [code, flagged] of [["gateway.connection", true], ["gateway.http_400", false]] as const) {
      const h2 = mk([oa("d")], { budgets: b }); (h2 as any).gateway.call = async () => { throw new GatewayError(code, "x"); }; const run2 = await h2.runToEnd("u", "go"), c = eventsOf(h2, run2, "budget.committed")[0].payload;
      expect([c.unreconciled, c.actual.turns, "tokens" in c.actual]).toEqual([flagged, 1, flagged]); expect((await st(run2)).agent.turns.used).toBe(1);
    }
  });
});
describe("hierarchy", () => {
  it("an allocation is guaranteed to its agent and never exceeds what the workflow has left", async () => {
    const { run, bc } = await make({ turns: 100, tokens: 1000 });
    expect(await bc.allocate("research", { tokens: 600 })).toBeNull(); expect(await bc.allocate("verify", { tokens: 500 })).toBe("tokens"); expect(await bc.allocate("verify", { tokens: 300 })).toBeNull();
    expect((await bc.reserve({ tokens: 200 }))[1]).toBe("tokens"); expect((await bc.reserve({ tokens: 100 }))[0]).toBeTruthy();
    expect((await bc.reserve({ tokens: 650 }, "research"))[1]).toBe("tokens"); const [r] = await bc.reserve({ tokens: 600 }, "research"); expect(r).toBeTruthy();
    await bc.commit(r!, { tokens: 250 }); expect((await st(run)).allocations.research.dims.tokens.used).toBe(250);
    await bc.endAllocation("research"); expect((await bc.reserve({ tokens: 200 }))[0]).toBeTruthy();
  });
  it("total consumption of all agents never exceeds the global limit", async () => {
    const { run, bc } = await make({ turns: 100, tokens: 1000 }); await bc.allocate("a", { tokens: 500 }); await bc.allocate("b", { tokens: 500 });
    for (const agent of ["a", "b", "a", "b", "a", "b"]) { const [r] = await bc.reserve({ tokens: 200 }, agent); if (r) await bc.commit(r, { tokens: 200 }); }
    const t = (await st(run)).agent.tokens; expect([t.used <= t.limit, t.used]).toEqual([true, 800]);
  });
  it("pure accounting matches the shared vectors", () => {
    for (const c of vec.available) expect(available(c.state, "agent", c.dim, c.agent_id), c.name).toEqual(c.expect);
    for (const c of vec.check_reserve) expect(checkReserve(c.state, "agent", c.deltas, c.agent_id), c.name).toEqual(c.expect);
    for (const c of vec.check_allocation) expect(checkAllocation(c.state, "agent", c.limits), c.name).toEqual(c.expect);
    for (const c of vec.reserve_cost) expect(reserveCost(c.in, c.out, c.price, c.margin), JSON.stringify(c)).toBe(c.expect);
  });
});
describe("concurrency and unmetered dimensions", () => {
  it("the limiter bounds simultaneous work, queues the rest, and pushes back when full", async () => {
    const lim = new ConcurrencyLimiter(2, 1); let release!: () => void; const gate = new Promise<void>((r) => (release = r)), running: number[] = [];
    const tasks = [0, 1, 2].map((i) => lim.slot(async () => { running.push(i); await gate; })); await sleep(50);
    expect([lim.inFlight, lim.waiting, [...running].sort()]).toEqual([2, 1, [0, 1]]); await expect(lim.slot(async () => 1)).rejects.toBeInstanceOf(QueueFull);
    release(); await Promise.all(tasks); expect([lim.inFlight, lim.waiting, lim.peak, lim.rejected]).toEqual([0, 0, 2, 1]);
  });
  it("permits are released on failure and deadline, and the pool keeps working", async () => {
    const lim = new ConcurrencyLimiter(1, 5); await expect(lim.slot(async () => { throw new Error("tool failed"); })).rejects.toThrow("tool failed"); expect(lim.inFlight).toBe(0);
    let release!: () => void; const hold = new Promise<void>((r) => (release = r)), t = lim.slot(async () => hold); await sleep(20);
    await expect(lim.slot(async () => 1, 0.05)).rejects.toBeInstanceOf(DeadlineExceeded); expect(lim.waiting).toBe(0); release(); await t; expect(lim.inFlight).toBe(0);
    expect(await lim.slot(async () => "ok")).toBe("ok"); expect(() => new ConcurrencyLimiter(0)).toThrow();
  });
  it("an unmetered dimension is declared, never zero, and needs independent safeguards", () => {
    expect(unmetered({ turns: 5 })).toEqual(["compute"]); expect(unmetered({}, ["compute"])).toEqual([]);
    expect(safeguardProblems({ turns: 20, time_s: 300 }, 4, 100)).toEqual([]); const p = safeguardProblems({}, 0, -1); expect(p).toHaveLength(4); expect(p.some((x) => x.includes("turn")) && p.some((x) => x.includes("wall-clock"))).toBe(true);
  });
});
describe("nothing is left holding budget", () => {
  it("a run that ends holds no budget, and crash recovery settles orphaned reservations", async () => {
    const { run, bc } = await make({ turns: 10, tokens: 100000 }); await bc.reserve({ turns: 1, tokens: 500 }); await bc.reserve({ tokens: 200 });
    expect(await bc.releaseOpen("run ended")).toBe(2); expect([(await st(run)).agent.tokens.reserved, Object.keys((await st(run)).reservations).length]).toEqual([0, 0]);
    const h2 = mk([oa("d")], { budgets: { turns: 10, tokens: 100000 } }), { run: run2 } = await h2.createRun("u", "go", { budgets: { turns: 10, tokens: 100000 } }), bc2 = new BudgetController(run2);
    const [rA] = await bc2.reserve({ turns: 1, tokens: 100 }), [rS] = await bc2.reserve({ turns: 1, tokens: 300 });
    await run2.emit("model.requested", { request_ref: "x", model_ref: "m", provider: "p", est_input_tokens: 1, turn: 1 }); const [rL] = await bc2.reserve({ tokens: 50 });
    const how = Object.fromEntries(await new RecoveryManager(h2).settleReservations(run2, h2.store.events(run2.runId)));
    expect([how[rA!], how[rS!], how[rL!]]).toEqual(["charged_estimate", "charged_estimate", "released"]); const s = await st(run2); expect([s.agent.tokens.reserved, s.agent.tokens.unreconciled, Object.keys(s.reservations).length]).toEqual([0, 400, 0]);
  });
});
describe("independent safeguards while compute is unmetered", () => {
  it("a runaway process is killed by its CPU limit before the wall-clock deadline", async () => {
    const t0 = Date.now(), r = await exec({ cpu_s: 1 }).execRun(["python3", "-c", "while True: pass"], { timeoutS: 20 }); expect([r.timed_out, r.exit_code !== 0, Date.now() - t0 < 10000]).toEqual([false, true, true]);
    const ok = await exec({ cpu_s: 5 }).execRun(["python3", "-c", "print(sum(range(10**6)))"], { timeoutS: 20 }); expect([ok.exit_code, ok.stdout]).toEqual([0, "499999500000\n"]);
  });
  it("a process that exceeds its memory limit is stopped", async () => {
    const r = await exec({ memory_mb: 400 }).execRun(["python3", "-c", "x = bytearray(1500 * 1024 * 1024); print('allocated')"], { timeoutS: 20 }); expect(r.exit_code).not.toBe(0); expect(r.stdout).not.toContain("allocated"); expect(r.stderr).toContain("MemoryError");
    expect((await exec().execRun(["python3", "-c", "x = bytearray(100 * 1024 * 1024); print('allocated')"], { timeoutS: 20 })).stdout.trim()).toBe("allocated");
  });
  it("the local backend applies default limits that the configuration can change", () => {
    expect(new LocalProcessBackend().limits).toEqual({ cpu_s: 3600, memory_mb: 8192 }); expect(new LocalProcessBackend(undefined, { cpu_s: 60 }).limits).toEqual({ cpu_s: 60, memory_mb: 8192 });
  });
  it("ending a run kills what it left running so nothing is orphaned", async () => {
    const ex = exec(), r = ex.execStart(["sleep", "30"], { timeoutS: 60 }); await sleep(200); expect(ex.live.size).toBe(1); const pg = [...ex.live][0], t0 = Date.now();
    expect(ex.killAll()).toBe(1); const out = await ex.execWait(r.exec_id); expect([out.exit_code === null || out.exit_code === 137 || out.exit_code === -9 || out.signal !== undefined || out.exit_code !== 0, Date.now() - t0 < 5000, ex.live.size]).toEqual([true, true, 0]);
    expect(() => process.kill(pg, 0)).toThrow();
    const be = new LocalProcessBackend(path.join(tmpdir(), "b")), hd = be.create({ role: "work" }), r2 = hd.executor.execStart(["sleep", "30"], { timeoutS: 60 }); await sleep(200); be.destroy(hd); expect((await hd.executor.execWait(r2.exec_id)).exit_code).not.toBe(0);
  });
  it("tool calls pass through the bounded pool and a full queue is a retryable error", async () => {
    const script = () => [call("fs.glob", { pattern: "/workspace/*" }), oa("done")];
    const h = mk(script(), { budgets: { turns: 10, tokens: 100000, workers: 1, queue: 0 } }); expect([h.runtime.limiter.workers, h.runtime.limiter.queue]).toEqual([1, 0]); await h.runToEnd("u", "go"); expect([h.runtime.limiter.peak, h.runtime.limiter.inFlight]).toEqual([1, 0]);
    const h2 = mk(script(), { budgets: { turns: 10, tokens: 100000 } }); h2.runtime.limiter = { slot: () => { throw new QueueFull("1 running and 0 queued"); } }; const run2 = await h2.runToEnd("u", "go"), f = eventsOf(h2, run2, "tool.call.failed")[0].payload.error; expect([f.code, f.retryable]).toEqual(["tool.queue_full", true]);
  });
  it("a submission is reserved and recorded before scoring, and a crash in between still counts it", async () => {
    const h = mk([call(...solution(0.8)[0]), call(...solution(0.8)[1]), FIN()]), task = loadTask(makeTask(h.tmp, "t1")), { run, loop } = await h.createEvalRun("u", task); await h.rm.start(run); await loop.runLoop();
    const seq = h.store.events(run.runId).map((e: any) => e.type).filter((t: string) => ["budget.reserved", "submission.created", "budget.committed", "judge.started"].includes(t)), first = seq.indexOf("submission.created");
    expect([seq.slice(0, first).includes("budget.reserved"), seq[first + 1], seq.indexOf("judge.started") > first + 1]).toEqual([true, "budget.committed", true]);
    const h2 = mk([oa("d")], { budgets: { turns: 10, tokens: 100000, submissions: 3 } }), { run: run2 } = await h2.createRun("u", "go", { budgets: { turns: 10, tokens: 100000, submissions: 3 } }), bc = new BudgetController(run2);
    const [r1] = await bc.reserve({ submissions: 1 }); await run2.emit("submission.created", { submission_id: "S1", snapshot_id: null, note: null }); const [r2] = await bc.reserve({ submissions: 1 });
    const how = Object.fromEntries(await new RecoveryManager(h2).settleReservations(run2, h2.store.events(run2.runId))); expect([how[r1!], how[r2!], (await st(run2)).agent.submissions.used]).toEqual(["submission_committed", "released", 1]);
  });
  it("preflight requires the safeguards that bound an unmetered dimension, and the run records what is unmetered", async () => {
    const failed = (h: any) => new Set(h.preflight().failed.map((c: any) => c.id)); expect(failed(mk([oa("d")])).has("PF-B01")).toBe(false);
    const h0 = mk([oa("d")]); h0.config.budgets = { tokens: 1000 }; expect(failed(h0).has("PF-B01")).toBe(true); const msg = h0.preflight().checks.find((c: any) => c.id === "PF-B01").message; expect([msg.includes("turn"), msg.includes("wall-clock")]).toEqual([true, true]);
    const h = mk([oa("d")]), run = await h.runToEnd("u", "go"); expect(eventsOf(h, run, "run.created")[0].payload.provenance.unmetered).toEqual(["compute"]);
  });
});
