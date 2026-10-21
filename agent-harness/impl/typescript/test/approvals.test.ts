import { describe, expect, it } from "vitest";
import { ApprovalError } from "../src/approvals.js";
import { FakeClock } from "../src/util.js";
import { Run } from "../src/run.js";
import { call, eventsOf, mk, oa, stateOf, waitFor } from "./helpers.js";

const SHELL = { schema_version: 1, rules: [{ id: "R-shell", action: "require_approval", match: { tool: ["shell.run"] }, approval: { ttl_s: 3600, max_count: 1, allow_standing: true } }] };
async function start(h: any) { const { run, loop } = await h.createRun("owner", "go"); await h.rm.start(run); return { run, loop, task: loop.runLoop() }; }
const pending = (h: any, run: any) => eventsOf(h, run, "approval.requested").at(-1)!.payload.approval_id;

describe("approvals", () => {
  it("require_approval pauses the run, nothing executes before the decision, grant proceeds", async () => {
    const h = mk([call("shell.run", { cmd: "echo hi" }), oa("done")], { profile: "standard", ruleset: SHELL }); const { run, task } = await start(h);
    await waitFor(() => stateOf(h, run) === "AWAITING_APPROVAL"); expect(eventsOf(h, run, "tool.call.started")).toHaveLength(0);
    expect(await h.approvals.decide(run, pending(h, run), "boss", ["approver"], true)).toBe("granted"); await waitFor(() => eventsOf(h, run, "tool.call.completed").length > 0);
    const types = h.store.events(run.runId).map((e) => e.type); expect(types.indexOf("policy.decided")).toBeLessThan(types.indexOf("approval.requested")); expect(types.indexOf("approval.decided")).toBeLessThan(types.indexOf("tool.call.started")); void task;
  });
  it("denied approval becomes an observation", async () => {
    const h = mk([call("shell.run", { cmd: "rm -rf /" }), oa("done")], { profile: "standard", ruleset: SHELL }); const { run } = await start(h);
    await waitFor(() => stateOf(h, run) === "AWAITING_APPROVAL"); await h.approvals.decide(run, pending(h, run), "boss", ["approver"], false); await waitFor(() => eventsOf(h, run, "tool.call.failed").length > 0);
    expect(eventsOf(h, run, "tool.call.failed")[0].payload.error.code).toBe("approval.denied"); expect(eventsOf(h, run, "tool.call.started")).toHaveLength(0);
  });
  it("only approvers, and never the run's owner; replayed decisions have no second effect", async () => {
    const h = mk([call("shell.run", { cmd: "x" }), oa("d")], { profile: "standard", ruleset: SHELL }); const { run } = await start(h); await waitFor(() => stateOf(h, run) === "AWAITING_APPROVAL"); const aid = pending(h, run);
    await expect(h.approvals.decide(run, aid, "mallory", ["viewer"], true)).rejects.toMatchObject({ code: "approvals.forbidden" }); await expect(h.approvals.decide(run, aid, "owner", ["approver"], true)).rejects.toMatchObject({ code: "approvals.self_approval" });
    expect(Object.keys(await run.proj("approvals_open"))).toEqual([aid]); await h.approvals.decide(run, aid, "boss", ["approver"], true);
    await expect(h.approvals.decide(run, aid, "boss", ["approver"], false)).rejects.toBeInstanceOf(ApprovalError); expect(eventsOf(h, run, "approval.decided")).toHaveLength(1);
  });
  it("expiry is recorded and an expired approval never executes the tool", async () => {
    const clock = new FakeClock(), rules = { schema_version: 1, rules: [{ id: "R", action: "require_approval", match: { tool: ["shell.run"] }, approval: { ttl_s: 60 } }] };
    const h = mk([call("shell.run", { cmd: "x" }), oa("d")], { profile: "standard", ruleset: rules, clock }); const { run } = await start(h); await waitFor(() => stateOf(h, run) === "AWAITING_APPROVAL");
    expect(await h.approvals.expireDue(run)).toBe(0); clock.advance(61_000); expect(await h.approvals.expireDue(run)).toBe(1);
    expect(eventsOf(h, run, "approval.decided")[0].payload).toMatchObject({ status: "expired", decided_by: "system:expiry" }); await waitFor(() => eventsOf(h, run, "tool.call.failed").length > 0);
    expect(eventsOf(h, run, "tool.call.failed")[0].payload.error.code).toBe("approval.expired"); expect(eventsOf(h, run, "tool.call.started")).toHaveLength(0);
  });
  it("standing approvals are scoped, counted atomically and capped", async () => {
    const h = mk([call("shell.run", { cmd: "echo a" }), call("shell.run", { cmd: "echo b" }), call("shell.run", { cmd: "echo c" }), oa("d")], { profile: "standard", ruleset: SHELL });
    await expect(h.approvals.createStanding("x", ["viewer"], "shell.run", null, 60, 2)).rejects.toBeInstanceOf(ApprovalError); await h.approvals.createStanding("boss", ["approver"], "shell.run", '*"echo*', 3600, 2);
    const { run } = await start(h); await waitFor(() => eventsOf(h, run, "tool.call.completed").length === 2); await waitFor(() => stateOf(h, run) === "AWAITING_APPROVAL");
    expect(h.store.get("SELECT used FROM standing_approvals").used).toBe(2);
  });
  it("allow_standing:false forces a per-instance approval; High never honours standing for irreversible actions", async () => {
    const rules = { schema_version: 1, rules: [{ id: "R", action: "require_approval", match: { tool: ["shell.run"] }, approval: { allow_standing: false } }] };
    const h = mk([call("shell.run", { cmd: "echo a" }), oa("d")], { profile: "standard", ruleset: rules }); await h.approvals.createStanding("boss", ["approver"], "shell.run", null, 3600, 5);
    const { run } = await start(h); await waitFor(() => stateOf(h, run) === "AWAITING_APPROVAL"); expect(eventsOf(h, run, "tool.call.started")).toHaveLength(0);
    const hh = mk([], { profile: "high" }), hr = new Run(hh.svc, "01HIGH0000000000000000000A", "high", "owner"); const sid = await hh.approvals.createStanding("boss", ["approver"], "shell.run", null, 3600, 5);
    expect(await hh.approvals.tryStanding(hr, "shell.run", { cmd: "x" })).toBe(sid);
    await hh.writer.execute("INSERT INTO runs(run_id,created_at,owner,profile,config_commit,state) VALUES(?,?,?,?,?,?)", [hr.runId, "t", "owner", "high", "c", "RUNNING"]);
    expect((await hh.approvals.request(hr, "C1", "shell.run", { cmd: "x" }, "R", null, "irreversible")).status).toBe("pending");
    expect((await hh.approvals.request(hr, "C2", "shell.run", { cmd: "x" }, "R", null, "reversible_write")).status).toBe("granted");
  });
});
