/** Operator telemetry (§14.4, §15.2), trajectory metrics (§13.11), and selection monitoring (§9.13). Mirrors test_telemetry.py against the same vectors. */
import fs from "node:fs";
import path from "node:path";
import { describe, expect, it } from "vitest";
import * as T from "../src/telemetry.js";
import * as S from "../src/selection.js";
import { telemetryCommand } from "../src/cliTelemetry.js";
import { call, eventsOf, mk, oa } from "./helpers.js";

const V = JSON.parse(fs.readFileSync(path.resolve(import.meta.dirname, "../../../spec/vectors/telemetry-vectors.json"), "utf8")), RID = V.events[0].run_id;
const BAD = { window: 10, min_sample: 5, warn: 0.2, critical: 0.5 };

describe("pure functions, against the shared vectors", () => {
  it("metrics, trajectory, usage and categories match", () => {
    const E = V.events; expect(T.runMetrics(E)).toEqual(V.run_metrics); expect(T.trajectoryMetrics(E)).toEqual(V.trajectory); expect(T.usage(E)).toEqual(V.usage); expect(T.categories(E, RID)).toEqual(V.categories);
    expect(S.selectionStats(E)).toEqual(V.selection_stats); expect(S.activeAlerts(E)).toEqual(V.active_alerts);
    expect(T.eventRows(E, RID, "operator")).toEqual(V.rows.operator); expect(T.eventRows(E, RID, "reviewer")).toEqual(V.rows.reviewer); expect(T.eventRows(E, RID, "operator", null, true)).toEqual(V.rows.errors); expect(T.eventRows(E, RID, "operator", "tool.call.failed")).toEqual(V.rows.type_tool);
    expect(T.categories(E.filter((e: any) => e.type !== "run.created"), RID)).toEqual(V.rows.no_manifest);
  });
  it("the required metrics are all reported with the right numbers", () => {
    const c = T.runMetrics(V.events).counters; expect([c.turns, c.tool_failures, c.tokens.total, c.retries, c.cost]).toEqual([2, 3, 540, { tool: 1, model: 3, total: 4 }, 0.42]);
    expect(c.approvals).toEqual({ requested: 3, granted: 2, denied: 1, expired: 0, automatic: 1 }); expect(c.budget_terminations).toEqual({ tokens: 1 }); expect(c.stuck_loop).toEqual({ warnings: 1, escalations: 0, terminations: 1 });
    expect([c.eval_integrity_events.total, c.selection_error_rate, c.log_integrity_alerts]).toEqual([1, 0.6, 1]); const h = T.runMetrics(V.events).histograms.model_latency_ms; expect([h.count, h.min, h.p50, h.p95, h.max]).toEqual([3, 120, 300, 900, 900]);
    expect([T.runMetrics([]).counters.cost, T.runMetrics([]).histograms.model_latency_ms.p50]).toEqual([null, null]);   // nothing measured is not zero
    expect([0.5, 0.95, 1.0, 0.1].map((q) => T.pct([1, 2, 3, 4, 5, 6, 7, 8, 9, 10], q))).toEqual([5, 10, 10, 1]); expect(T.pct([7], 0.95)).toBe(7);   // nearest-rank
  });
  it("operators see metadata, reviewers see content, and secrets are redacted for everyone", () => {
    const op = JSON.stringify(T.eventRows(V.events, RID, "operator")), rv = JSON.stringify(T.eventRows(V.events, RID, "reviewer"));
    for (const x of [op, rv]) { expect(x).not.toContain("hunter2"); expect(x).not.toContain("sk-abcdefghijklmnop12345"); } expect(rv).toContain("[REDACTED]"); expect(op).toContain("gated"); expect(rv).not.toContain("gated"); expect(op).not.toContain('"/workspace/a"'); expect(rv).toContain('"/workspace/a"');
    expect(T.eventRows(V.events, RID).every((r) => r.run_id === RID)).toBe(true);
  });
  it("a category is unavailable when it is not collected, never inferred from missing events", () => {
    const c = T.categories(V.events.filter((e: any) => e.type !== "run.created"), RID).categories as any; expect(c.model_and_version).toEqual({ status: "unavailable", reason: "this run has no provenance manifest" });
    expect((T.categories(V.events.slice(0, 3), RID).categories.usage_and_cost as any).note.startsWith("cost is unavailable")).toBe(true);
    T.flags.ALERTS_COLLECTED = false; try { expect(T.categories(V.events, RID).categories.alerts).toEqual({ status: "unavailable", reason: "alerting is not implemented in this build" }); } finally { T.flags.ALERTS_COLLECTED = true; }
  });
});
describe("selection monitoring", () => {
  it("alert rules follow the shared vectors, and a sustained breach is one alert", () => {
    for (const c of V.monitor) {
      const m = new S.SelectionMonitor(c.cfg), got: any[] = [];
      c.errors.forEach((x: number, i: number) => { const r = m.observe(!!x, { tool: "t" }); if (r) got.push({ i, event: r.event, severity: r.payload.severity ?? null, previous: r.payload.previous_severity ?? null, value: r.payload.value, window: r.payload.window }); });
      expect(got, c.name).toEqual(c.transitions);
    }
  });
  it("alert rules are validated and invalid ones are refused", () => {
    for (const c of V.rules.valid) expect(S.rules(c.in)).toEqual(c.out); expect(S.rules({})).toEqual({ window: 200, min_sample: 30, warn: 0.05, critical: 0.1 }); for (const bad of V.rules.invalid) expect(() => S.rules(bad), JSON.stringify(bad)).toThrow();
  });
  it("the offline benchmark separates the kinds of selection error", () => {
    const r = S.evaluateSelection(V.benchmark.benchmark, V.benchmark.decisions); expect(r).toEqual(V.benchmark.result);
    expect([r.wrong_tool_rate, r.invalid_call_rate, r.unnecessary_call_rate, r.unsafe_selection_rate, r.miss_rate, r.recovery_rate, r.error_rate]).toEqual([0.6, 0.2, 0.2, 0.2, 1 / 3, 1 / 3, 0.6]);
  });
});
describe("in real runs", () => {
  const calls = (n: number, tool = "nope.tool") => oa(null, Array.from({ length: n }, () => [tool, {}] as [string, any]));
  it("a run of bad selections raises one alert and resolves it once selection recovers", async () => {
    const script = [calls(5), ...Array.from({ length: 2 }, () => oa(null, Array.from({ length: 5 }, () => ["fs.glob", { pattern: "/workspace/*" }] as [string, any]))), oa("d")], h = mk(script, { alerts: { selection: BAD } }), run = await h.runToEnd("u", "go");
    const raised = eventsOf(h, run, "alert.raised"), resolved = eventsOf(h, run, "alert.resolved");
    expect(raised.map((a: any) => [a.payload.rule, a.payload.severity])).toEqual([["selection.error_rate", "critical"]]); expect(resolved).toHaveLength(1); expect(raised[0].seq).toBeLessThan(resolved[0].seq);
    expect([raised[0].payload.context, raised[0].payload.threshold, JSON.stringify(raised[0].payload).includes("args")]).toEqual([{ tool: "nope.tool" }, 0.5, false]); expect(S.activeAlerts(h.store.events(run.runId))).toEqual([]);
  });
  it("an unauthorized selection that reaches execution is blocked and is an immediate alert", async () => {
    const h = mk([oa("d")]), { run, loop } = await h.createRun("u", "go"), tool = h.registry.get("fs.glob")!, ctx = loop.tctx();
    const r = await h.runtime.runWithRetries(run, "c1", "fs.glob", { pattern: "/workspace/*" }, tool, tool.manifest, ctx, 1, "deny");   // forced: it cannot happen by construction
    const a = eventsOf(h, run, "alert.raised")[0].payload; expect([a.rule, a.severity, a.context.tool, a.context.decision]).toEqual(["selection.unsafe_execution", "security", "fs.glob", "deny"]); expect(eventsOf(h, run, "tool.call.started")).toHaveLength(0); expect(r.ok).toBe(false);
    await h.runtime.runWithRetries(run, "c2", "fs.glob", { pattern: "/workspace/*" }, tool, tool.manifest, ctx, 2, "require_approval", false);
    expect(eventsOf(h, run, "alert.raised").map((e: any) => e.payload.context.decision)).toEqual(["deny", "unapproved"]); expect(eventsOf(h, run, "tool.call.started")).toHaveLength(0);
  });
  it("the monitor is rebuilt from the log after a restart", async () => {
    const h = mk([calls(4), oa("d")], { alerts: { selection: BAD } }), run = await h.runToEnd("u", "go"), m = S.SelectionMonitor.fromEvents(h.store.events(run.runId), BAD); expect([m.window, m.active]).toEqual([[1, 1, 1, 1], null]);
  });
});
describe("the CLI", () => {
  const cli = (cmd: string, ...argv: string[]) => { const out: string[] = [], err: string[] = [], code = telemetryCommand(cmd, argv, (s) => out.push(s), (s) => err.push(s)); return { code, out: out.join("\n"), err: err.join("\n") }; };
  async function cliRun() { const h = mk([call("fs.write", { path: "/workspace/n.txt", content: "password=hunter2 is the secret" }), oa(null, [["nope.tool", {}]]), oa("done")]), run = await h.runToEnd("u", "my key is sk-abcdefghijklmnop12345"); return { h, run, db: h.config.storage.event_log.path as string }; }
  it("lists events with content gated, redacted, and the reviewer's access logged", async () => {
    const { h, run, db } = await cliRun(), a = cli("events", "--db", db, "--run", run.runId, "--json"), rows = a.out.split("\n").map((l) => JSON.parse(l));
    expect([a.code, rows.length > 0, rows.every((r: any) => r.run_id === run.runId)]).toEqual([0, true, true]); for (const x of ["hunter2", "sk-abcdefghijklmnop12345"]) expect(a.out).not.toContain(x); expect(a.out).toContain("gated");
    for (const r of rows.filter((r: any) => r.type === "tool.call.requested")) expect(r.payload.args).toBe("[gated: reviewer or admin role required]");   // a resource identifier (a reconcile subject) is metadata; the arguments and content are not
    const b = cli("events", "--db", db, "--run", run.runId, "--role", "reviewer", "--who", "rita", "--type", "tool.call.requested", "--json"); expect(b.out).not.toContain("hunter2"); expect(b.out).toContain("n.txt"); expect(b.out).toContain("[REDACTED]");
    const acc = h.store.db.prepare("SELECT who, role, run_id, what FROM content_access").all() as any[]; expect(acc.map((r) => [r.who, r.role, r.run_id])).toEqual([["rita", "reviewer", run.runId]]); expect(acc[0].what).toContain("tool.call.requested");
    const c = cli("events", "--db", db, "--run", run.runId, "--errors", "--json"), types = new Set(c.out.split("\n").map((l) => JSON.parse(l).type)); expect(types.has("tool.call.failed")).toBe(true); for (const t of types) expect(["tool.call.failed", "run.terminated", "failure.classified"]).toContain(t);
    expect([cli("events", "--db", db, "--run", "NOSUCHRUN").code, cli("events", "--db", db, "--run", "NOSUCHRUN").err]).toEqual([2, "no such run"]);
  });
  it("reports usage and metrics for a run", async () => {
    const { run, db } = await cliRun(), u = JSON.parse(cli("usage", "--db", db, "--run", run.runId, "--json").out); expect([u.run_id, u.tokens.total > 0, Object.keys(u.by_model).length > 0]).toEqual([run.runId, true, true]);
    const m = JSON.parse(cli("metrics", "--db", db, "--run", run.runId, "--json").out); expect([m.counters.tool_failures, m.counters.selection_error_rate, m.run_id]).toEqual([1, 0.5, run.runId]);
  });
  it("lists alerts by run and across runs", async () => {
    const h = mk([calls5(), oa("d")], { alerts: { selection: BAD } }), run = await h.runToEnd("u", "go"), db = h.config.storage.event_log.path as string, a = cli("alerts", "--db", db, "--run", run.runId);
    for (const x of ["selection.error_rate", "critical", "active", run.runId]) expect(a.out).toContain(x); expect(cli("alerts", "--db", db).out).toContain("selection.error_rate");
    const h2 = mk([oa("d")]), r2 = await h2.runToEnd("u", "go"); expect(cli("alerts", "--db", h2.config.storage.event_log.path, "--run", r2.runId).out).toContain("no alerts recorded");
  });
});
const calls5 = () => oa(null, Array.from({ length: 5 }, () => ["nope.tool", {}] as [string, any]));
