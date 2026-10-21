/** Outcome contract and verdicts (design §13.11). Mirrors test_outcome.py and checks the shared vectors. */
import fs from "node:fs";
import path from "node:path";
import { describe, expect, it } from "vitest";
import { VERDICTS, VERIFIERS, ContractError, contractHash, decimal, deriveVerdict, evaluateContract, outcomeMetrics, registerVerifier, validateContract } from "../src/outcome.js";
import { loadTask, validateTask } from "../src/eval.js";
import { trajectoryMetrics } from "../src/telemetry.js";
import { FIN, call, eventsOf, makeTask, mk, solution, tmpdir } from "./helpers.js";

const V = JSON.parse(fs.readFileSync(path.resolve(import.meta.dirname, "../../../spec/vectors/outcome-vectors.json"), "utf8"));
const ctxOf = (c: any) => { const files = c.files; return { ...Object.fromEntries(Object.entries(c).filter(([k]) => k !== "files")), fs_read: files ? (p: string) => { if (!(p in files)) throw new Error(p); return files[p]; } : null } as any; };
const CONTRACT = (...c: any[]) => ({ acceptance_criteria: c, success_policy: "all_required_criteria_pass", inconclusive_policy: "do_not_report_success" });
const SCRIPT = () => [call(...solution(0.8)[0]), call(...solution(0.8)[1]), FIN(), FIN(), FIN()];
async function evalRun(taskOver: any = {}) { const h = mk(SCRIPT()), task = loadTask(makeTask(h.tmp, "t1", taskOver)), { run, loop } = await h.createEvalRun("u", task); await h.rm.start(run); await loop.runLoop(); return { h, run }; }

describe("verdicts and contracts", () => {
  it("verdict precedence matches the shared vectors, and every verdict is reachable", () => {
    for (const c of V.derive) expect(deriveVerdict(c.results), c.name).toBe(c.expect); expect(new Set(V.derive.map((c: any) => c.expect))).toEqual(new Set(VERDICTS));
  });
  it("evidence that is missing, stale, conflicting, or from a failing adapter can never make a success", () => {
    for (const status of ["unavailable", "stale", "conflict", "error", "anything else"]) expect(deriveVerdict([{ required: true, status: "pass" }, { required: true, status }])).toBe("INCONCLUSIVE");
    const con = CONTRACT({ id: "r", required: true, verification: "authoritative_lookup" });
    registerVerifier("authoritative_lookup", () => { throw new Error("system down"); });
    try { expect(evaluateContract(con, {}).verdict).toBe("INCONCLUSIVE"); registerVerifier("authoritative_lookup", () => ({ status: "pass", ref: "record:42" })); const r = evaluateContract(con, {}); expect([r.verdict, r.criteria[0].evidence_ref]).toEqual(["SUCCESS", "record:42"]); }
    finally { delete VERIFIERS.authoritative_lookup; }
  });
  it("contracts are evaluated identically to the vectors", () => {
    for (const c of V.evaluate) { const r = evaluateContract(c.contract, ctxOf(c.ctx)); expect([r.verdict, r.criteria.map((x) => x.status), r.criteria.map((x) => x.evidence_ref), r.contract_hash], c.name).toEqual([c.verdict, c.statuses, c.refs, c.contract_hash]); }
  });
  it("invalid contracts are rejected before anything runs", () => {
    for (const c of V.invalid) { expect(validateContract(c.contract).length > 0, c.name).toBe(true); expect(() => evaluateContract(c.contract, {}), c.name).toThrow(ContractError); }
  });
  it("money is compared as exact decimals, never floats, and non-finite values prove nothing", () => {
    const mkc = (a: string, e: string) => CONTRACT({ id: "amt", required: true, verification: "exact_decimal_comparison", params: { actual: a, expected: e } });
    expect(evaluateContract(mkc("0.1000000000000000055511151231257827", "0.1"), {}).verdict).toBe("FAILURE"); expect(evaluateContract(mkc("0.10", "0.1"), {}).verdict).toBe("SUCCESS"); expect(evaluateContract(mkc("not a number", "1"), {}).verdict).toBe("INCONCLUSIVE");
    expect([decimal("NaN"), decimal("inf"), decimal(null), decimal(true)]).toEqual([null, null, null, null]); expect(decimal("1e3")).toEqual({ m: 1n, e: -3 });
  });
  it("the contract hash follows the contract", () => {
    const a = V.evaluate[1].contract, b = JSON.parse(JSON.stringify(a)); expect(contractHash(a)).toBe(contractHash(b)); b.acceptance_criteria[0].params.value = 0.9; expect(contractHash(a)).not.toBe(contractHash(b));
  });
  it("outcome metrics use every evaluated task unless an exclusion is documented", () => {
    const m = outcomeMetrics(V.metrics.records); expect(m).toEqual(V.metrics.all); expect([m.overall.n, m.overall.success_rate, m.overall.audited, m.overall.evaluation_error_rate]).toEqual([5, 0.4, 2, 0.5]);
    const ex = outcomeMetrics(V.metrics.records, "tasks without an authoritative system", (r) => r.category === "db"); expect(ex).toEqual(V.metrics.excluding);
    expect(() => outcomeMetrics(V.metrics.records, null, (r) => r.category === "db")).toThrow(/documented exclusion policy/);
  });
});
describe("in a real evaluation run", () => {
  it("every evaluation run records a verdict even without an explicit contract", async () => {
    const { h, run } = await evalRun(), ev = eventsOf(h, run, "outcome.evaluated")[0].payload; expect([ev.verdict, ev.criteria[0].id, ev.contract_hash.length, await run.state()]).toEqual(["SUCCESS", "valid_submission", 64, "COMPLETED"]);
  });
  it("a contract the work does not meet fails the run with a failure verdict", async () => {
    const { h, run } = await evalRun({ over: { outcome: CONTRACT({ id: "score", required: true, verification: "test_result", params: { metric: "value", op: ">=", value: 0.95 } }) } });
    expect(eventsOf(h, run, "outcome.evaluated")[0].payload.verdict).toBe("FAILURE"); expect(await run.state()).not.toBe("COMPLETED");
  });
  it("a criterion with no adapter is inconclusive, so the run cannot claim success", async () => {
    const { h, run } = await evalRun({ over: { outcome: CONTRACT({ id: "score", required: true, verification: "test_result", params: { metric: "value", op: ">=", value: 0.5 } }, { id: "record_updated", required: true, verification: "authoritative_lookup" }) } });
    const ev = eventsOf(h, run, "outcome.evaluated")[0].payload; expect([ev.verdict, ev.criteria.map((c: any) => c.status), await run.state() !== "COMPLETED"]).toEqual(["INCONCLUSIVE", ["pass", "unavailable"], true]);
  });
  it("an artifact criterion checks the final workspace", async () => {
    const ok = await evalRun({ over: { outcome: CONTRACT({ id: "script", required: true, verification: "artifact_check", params: { path: "/workspace/run.sh" } }) } }); expect(eventsOf(ok.h, ok.run, "outcome.evaluated")[0].payload.verdict).toBe("SUCCESS");
    const no = await evalRun({ over: { outcome: CONTRACT({ id: "report", required: true, verification: "artifact_check", params: { path: "/workspace/report.md" } }) } }); expect(eventsOf(no.h, no.run, "outcome.evaluated")[0].payload.verdict).toBe("FAILURE");
  });
  it("task validation rejects a bad contract", () => {
    const t = tmpdir(), dup = CONTRACT({ id: "a", required: true, verification: "test_result" }, { id: "a", required: true, verification: "test_result" });
    expect(validateTask(makeTask(t, "bad", { over: { outcome: dup } })).some((e) => e.includes("used twice"))).toBe(true); expect(validateTask(makeTask(t, "good", { over: { outcome: CONTRACT({ id: "a", required: true, verification: "test_result" }) } }))).toEqual([]);
  });
});
describe("trajectory metrics", () => {
  it("every evaluation run records its trajectory metrics apart from the outcome", async () => {
    const { h, run } = await evalRun(), tr = eventsOf(h, run, "trajectory.analyzed")[0], out = eventsOf(h, run, "outcome.evaluated")[0], m = tr.payload.metrics;
    expect([tr.seq < out.seq, m.analyzer_version, m.steps >= 2, m.tool_calls >= 2, m.tool_errors, "verdict" in m]).toEqual([true, 1, true, true, 0, false]);   // the transcript's record comes first, and is separate
    expect(m).toEqual(trajectoryMetrics(h.store.events(run.runId).filter((e: any) => e.seq < tr.seq)));   // a pure function of the log up to that point
    const ev = JSON.parse(h.blobs.get(eventsOf(h, run, "verification.completed")[0].payload.evidence_ref).toString("utf8")); expect([ev.evidence.trajectory, ev.evidence.outcome.verdict]).toEqual([m, "SUCCESS"]);   // both appear in the evaluation report
  });
});
