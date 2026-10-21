import fs from "node:fs";
import path from "node:path";
import { describe, expect, it } from "vitest";
import { loadTask, shapeFeedback, truncateUtf8, validateTask } from "../src/eval.js";
import { requiredTrials } from "../src/stats.js";
import { runPreflight } from "../src/preflight.js";
import { FIN, call, eventsOf, makeTask, mk, oa, solution, tmpdir } from "./helpers.js";

const textOf = (e: any) => e.payload.message.parts[0].parts[0].text;
async function evalRun(script: any[], over: any = {}, taskOver: any = {}) {
  const h = mk(script, over), task = loadTask(makeTask(h.tmp, "t1", taskOver)), { run, loop, runner } = await h.createEvalRun("u", task);
  await h.rm.start(run); await loop.runLoop(); return { h, run, runner, task };
}
describe("task validation", () => {
  it("valid task loads", () => { const d = makeTask(tmpdir()); expect(validateTask(d)).toEqual([]); expect(loadTask(d).id).toBe("t1"); });
  it.each([["instruction file", (d: string) => fs.unlinkSync(path.join(d, "instruction.md"))], ["build file", (d: string) => fs.rmSync(path.join(d, "environment"), { recursive: true })],
    ["not found under tests/", (d: string) => fs.unlinkSync(path.join(d, "tests", "grade.py"))], ["agent-reachable", (d: string) => fs.mkdirSync(path.join(d, "environment", "tests"))]])("rejects %s", (needle, mutate) => {
    const d = makeTask(tmpdir()); mutate(d); expect(validateTask(d).some((e) => e.includes(needle)), JSON.stringify(validateTask(d))).toBe(true); expect(() => loadTask(d)).toThrow();
  });
  it("users, step ids, heldout mode, schema", () => {
    const t = tmpdir();
    expect(validateTask(makeTask(t, "a", { over: { environment: { work_image: { build: "environment/Dockerfile" }, users: { agent: 1, candidate: 1, judge: 2 } } } })).some((e) => e.includes("distinct"))).toBe(true);
    const dup = Array(2).fill({ id: "x", run_as: "judge", cmd: ["true"], timeout_s: 5 }); expect(validateTask(makeTask(t, "b", { steps: dup })).some((e) => e.includes("unique"))).toBe(true);
    const c = makeTask(t, "c", { over: { confirmation: { mode: "heldout_set", trials: 1 } } }); fs.rmSync(path.join(c, "tests", "confirm"), { recursive: true }); expect(validateTask(c).some((e) => e.includes("tests/confirm"))).toBe(true);
    expect(validateTask(makeTask(t, "e", { over: { scoring: { steps: [{ id: "s", run_as: "root", cmd: ["x"], timeout_s: 1 }], report_path: "/p", primary: { metric: "m", direction: "maximize" } } } }))[0]).toContain("invalid");
    expect(validateTask(path.join(t, "missing"))[0]).toMatch(/not found$/);
  });
});
describe("feedback shaping", () => {
  it("is bounded, UTF-8 safe and never raw", () => {
    const rep = { valid: true, primary: 0.5, feedback_text: "é".repeat(100) }; expect(shapeFeedback(rep, "none", 100)).toBe("Submission received."); expect(shapeFeedback(rep, "score", 100)).toBe("Score: 0.5");
    const out = shapeFeedback(rep, "score_and_summary", 20); expect(Buffer.byteLength(out)).toBeLessThanOrEqual(20); expect(out.startsWith("Score: 0.5\n")).toBe(true); expect(out).not.toContain("\uFFFD"); expect(truncateUtf8("aé", 2)).toBe("a");
    expect(shapeFeedback({ valid: false, primary: 0 }, "score", 100)).toContain("could not be scored");
  });
  it("replication sizing", () => { expect(requiredTrials(0.01, 0.1)).toBe(3); expect(requiredTrials(0.2, 0.05)).toBe(62); expect(() => requiredTrials(1, 1, 0.5)).toThrow(); });
});
describe("submit, score, feedback", () => {
  it("full flow: ordering, states, fresh judge sandbox, chain head, evaluator kind set by the control plane", async () => {
    const { h, run } = await evalRun([call(...solution(0.8)[0]), call(...solution(0.8)[1]), FIN()]);
    expect(await run.state()).toBe("COMPLETED"); const types = h.store.events(run.runId).map((e) => e.type);
    const idx = ["submission.created", "snapshot.created", "judge.started", "judge.completed", "feedback.delivered", "verification.completed", "run.completed"].map((t) => types.indexOf(t)); expect([...idx].sort((a, b) => a - b)).toEqual(idx);
    const states = eventsOf(h, run, "run.state_changed").map((e) => `${e.payload.from}>${e.payload.to}`); expect(states).toContain("RUNNING>SCORING"); expect(states).toContain("SCORING>RUNNING");
    expect(textOf(eventsOf(h, run, "tool.call.completed").find((e) => e.payload.tool === "submit")!)).toBe("Score: 0.8");
    const created = eventsOf(h, run, "sandbox.created"); expect(created.map((c) => c.payload.role)).toEqual(["work", "judge"]); expect(new Set(created.map((c) => c.payload.sandbox_id)).size).toBe(2);
    expect(eventsOf(h, run, "sandbox.destroyed").some((e) => e.payload.sandbox_id === created[1].payload.sandbox_id)).toBe(true); expect(await run.proj("grants_active")).toEqual({});
    const snap = eventsOf(h, run, "snapshot.created")[0]; expect(snap.payload.chain_head).toBe(h.store.getEvent(run.runId, snap.seq - 1)!.hash);
    const rep = h.blobs.getJson(eventsOf(h, run, "judge.completed")[0].payload.score_report_ref); expect(rep.evaluator).toEqual({ kind: "deterministic", model_ref: null }); expect(rep.primary).toBe(0.8); expect(h.store.verify(run.runId).ok).toBe(true);
  });
  it("the agent cannot reach hidden tests through the file tools", async () => {
    const { h, run } = await evalRun([oa(null, [["fs.read", { path: "/tests/grade.py" }], ["fs.read", { path: "/workspace/tests/grade.py" }], ["fs.glob", { pattern: "/workspace/*" }], ["fs.search", { pattern: "HIDDEN-HINT|SECRETSTACK", path: "/workspace" }]]), oa("x")]);
    expect(eventsOf(h, run, "tool.call.failed").map((e) => e.payload.error.code)).toEqual(["fs.path_not_allowed", "fs.path_not_found"]);
    const texts = eventsOf(h, run, "tool.call.completed").map(textOf); expect(texts.join(" ")).not.toContain("grade.py"); expect(texts.at(-1)).toBe("(no matches)");
  });
  it("the work sandbox never mounts the tests dir (PF-13)", async () => {
    const h = mk([]), task = loadTask(makeTask(h.tmp)), { loop } = await h.createEvalRun("u", task), work = loop.sandbox();
    expect("/tests" in work.executor.roots).toBe(false); expect(fs.existsSync(path.join(work.root, "tests"))).toBe(false);
    expect(runPreflight("core", h.config, { workRoots: [work.root], hiddenTestsDirs: [task.testsDir] }).checks.find((c: any) => c.id === "PF-13").ok).toBe(true);
  });
  it("no raw grader output reaches the agent or the log", async () => {
    const { h, run } = await evalRun([call("submit"), call("submit"), oa("x")]);
    const blob = JSON.stringify(h.store.events(run.runId)) + JSON.stringify(h.transport.requests.map((r: any) => r.body)); expect(blob).not.toContain("SECRETSTACK"); expect(blob).not.toContain("HIDDEN-HINT");
    expect(textOf(eventsOf(h, run, "tool.call.completed").find((e) => e.payload.tool === "submit")!)).toBe("Submission was invalid: it could not be scored."); expect(eventsOf(h, run, "judge.completed")[0].payload.valid).toBe(false);
  });
  it("score_and_summary feedback is truncated to max_bytes", async () => {
    const { h, run } = await evalRun([call(...solution(0.5)[0]), call(...solution(0.5)[1]), oa("x")], {}, { feedback: { level: "score_and_summary", max_bytes: 40 } });
    const fb = textOf(eventsOf(h, run, "tool.call.completed").find((e) => e.payload.tool === "submit")!); expect(Buffer.byteLength(fb)).toBeLessThanOrEqual(40); expect(fb.startsWith("Score: 0.5\nHIDDEN-HINT")).toBe(true); expect(eventsOf(h, run, "feedback.delivered")[0].payload.bytes).toBeLessThanOrEqual(40);
  });
  it("the submission budget is a guard: submit returns an error observation and the run continues", async () => {
    const { h, run } = await evalRun([call(...solution(0.1)[0]), call(...solution(0.1)[1]), call(...solution(0.2)[1]), oa("x")], {}, { over: { agent: { budgets: { turns: 30, tokens: 2_000_000, time_s: 600, submissions: 1 } } } });
    expect(eventsOf(h, run, "submission.created")).toHaveLength(1); expect(eventsOf(h, run, "tool.call.failed").map((e) => e.payload.error.code)).toEqual(["submit.budget_exhausted"]);
  });
  it("a judge step timeout invalidates the submission", async () => {
    const steps = [{ id: "slow", run_as: "judge", cmd: ["bash", "-c", "sleep 30"], timeout_s: 1 }];
    const { h, run } = await evalRun([call("submit"), oa("x")], {}, { over: { scoring: { steps, report_path: "/scratch/score.json", primary: { metric: "m", direction: "maximize" } } } });
    const rep = h.blobs.getJson(eventsOf(h, run, "judge.completed")[0].payload.score_report_ref); expect(rep.valid).toBe(false); expect(rep.errors[0]).toContain("timed out");
  });
});
describe("selection and confirmation", () => {
  it("picks the best of three and confirms (mandatory for >= 2 submissions)", async () => {
    const s = [call(...solution(0.6)[0]), call(...solution(0.6)[1]), call(...solution(0.9)[0]), call(...solution(0.9)[1]), call(...solution(0.7)[0]), call(...solution(0.7)[1]), FIN()];
    const { h, run } = await evalRun(s, {}, { over: { scoring: { pass_threshold: 0.85 } } }); const sel = eventsOf(h, run, "selection.made")[0].payload, subs = await run.proj("submissions");
    expect(sel.n_submissions).toBe(3); expect(subs[sel.submission_id].primary).toBe(0.9); const conf = eventsOf(h, run, "confirmation.completed")[0].payload, r = conf.result;
    expect([conf.rule, conf.n_submissions]).toEqual(["reseed", 3]); expect(r).toMatchObject({ valid: true, trials: 3, unconfirmed_upper_bound: 0.9, submissions_used: 3 }); expect(r.lower <= r.mean && r.mean <= r.upper).toBe(true); expect(r.mean).toBeGreaterThanOrEqual(0.9); expect(r.mean).toBeLessThanOrEqual(0.907);
    expect(eventsOf(h, run, "confirmation.trial")).toHaveLength(3); expect(await run.state()).toBe("COMPLETED"); expect(h.blobs.getJson(eventsOf(h, run, "verification.completed")[0].payload.evidence_ref).detail).toContain("confirmed value");
  });
  it("a single submission is labelled an unconfirmed upper bound", async () => {
    const { h, run } = await evalRun([call(...solution(0.9)[0]), call(...solution(0.9)[1]), FIN()]); expect(eventsOf(h, run, "confirmation.completed")).toHaveLength(0);
    expect(h.blobs.getJson(eventsOf(h, run, "verification.completed")[0].payload.evidence_ref).detail).toContain("unconfirmed upper bound");
  });
  it("held-out confirmation uses a separate hidden set and can fail the gate", async () => {
    const s = [call(...solution(0.9)[0]), call(...solution(0.9)[1]), call(...solution(0.9)[1]), FIN(), FIN(), FIN()];
    const { h, run } = await evalRun(s, {}, { over: { confirmation: { mode: "heldout_set", trials: 2 }, scoring: { pass_threshold: 0.86 } } }); const conf = eventsOf(h, run, "confirmation.completed")[0].payload;
    expect(conf.rule).toBe("heldout_set"); expect(Math.abs(conf.result.mean - 0.85)).toBeLessThan(1e-9); expect(conf.result.unconfirmed_upper_bound).toBe(0.9);
    const v = eventsOf(h, run, "verification.completed")[0].payload; expect(v.passed).toBe(false); expect(v.detail).toContain("confirmed value 0.85"); expect(await run.state()).toBe("TERMINATED"); expect(eventsOf(h, run, "run.completed")).toHaveLength(0);
  });
  it("an invalid submission never wins selection", async () => {
    const { h, run } = await evalRun([call("submit"), call(...solution(0.4)[0]), call(...solution(0.4)[1]), FIN()]); const sel = eventsOf(h, run, "selection.made")[0].payload; expect((await run.proj("submissions"))[sel.submission_id].valid).toBe(true);
  });
});
