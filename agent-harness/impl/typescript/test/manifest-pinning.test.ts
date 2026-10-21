/** Run provenance manifest and definition pinning (design §13.9). Mirrors test_manifest_pinning.py. */
import fs from "node:fs";
import path from "node:path";
import { describe, expect, it } from "vitest";
import * as M from "../src/manifest.js";
import { DefinitionError, DefinitionMismatch, DefinitionStore, Pins, lintId } from "../src/definitions.js";
import { sha256Hex } from "../src/util.js";
import { call, eventsOf, mk, oa, tmpdir } from "./helpers.js";

async function started(script: any[] = [oa("d")], o: any = {}) { const h = mk(script, o), { run, loop } = await h.createRun("u", "go"); return { h, run, loop }; }
const throws = (fn: () => any, re: RegExp) => { try { fn(); } catch (e: any) { expect(String(e.message)).toMatch(re); return e; } throw new Error("did not throw"); };

describe("the definition store", () => {
  it("published definitions are immutable versions, and identical content does not make a new one", async () => {
    const { h } = await started(), ds = new DefinitionStore(h.svc), a = await ds.publish("prompt.demo", "be careful"), b = await ds.publish("prompt.demo", "be careful"), c = await ds.publish("prompt.demo", "be very careful");
    expect([a.version, b.version, c.version]).toEqual([1, 1, 2]); expect(a.sha256).toBe(b.sha256); expect(a.sha256).not.toBe(c.sha256); expect(ds.fetch("prompt.demo", 1, a.sha256)).toBe("be careful");
    const j = await ds.publish("policy.demo", { b: 1, a: [1, 2] }); expect(ds.fetch("policy.demo", 1, j.sha256)).toEqual({ a: [1, 2], b: 1 });
    for (const sql of ["UPDATE definition_versions SET sha256='x'", "DELETE FROM definition_versions"]) throws(() => h.store.db.exec(sql), /immutable/);   // the database itself refuses
  });
  it("floating aliases and malformed ids are rejected", () => {
    for (const ok of ["prompt.system", "tool.fs.read", "budget_policy", "workflow"]) lintId(ok);
    for (const bad of ["latest", "prompt.latest", "tool.stable", "Prompt", "x y", "", "1abc", "a/b"]) expect(() => lintId(bad), bad).toThrow(DefinitionError);
  });
  it("a definition is verified before use, and anything else throws", async () => {
    const { h } = await started(), ds = new DefinitionStore(h.svc), p = await ds.publish("prompt.verify", "original"); expect(ds.fetch(p.id, p.version, p.sha256)).toBe("original");
    throws(() => ds.fetch("prompt.verify", 9, p.sha256), /not published/); throws(() => ds.fetch("prompt.verify", 1, "0".repeat(64)), /different hash/); throws(() => ds.fetch("prompt.never", 1, p.sha256), /not published/);
    const file = h.blobs.p(p.sha256); fs.writeFileSync(file, "tampered");   // the content on disk changed
    const e = throws(() => ds.fetch(p.id, p.version, p.sha256), /does not match/); expect([e.expected, e.observed]).toEqual([p.sha256, sha256Hex(Buffer.from("tampered"))]);
    fs.unlinkSync(file); throws(() => ds.fetch(p.id, p.version, p.sha256), /missing/);
  });
});
describe("the manifest", () => {
  it("every run has a complete manifest captured at start, with honest statuses", async () => {
    const { h, run } = await started(), m = eventsOf(h, run, "run.created")[0].payload.manifest, f = m.fields;
    expect(M.validate(m, "core")).toEqual([]); expect(m.run_id).toBe(run.runId); for (const k of [...M.START, ...M.FINAL]) expect(k in f, k).toBe(true);
    expect([f.models.status, f.models.value[0].snapshot, f.limits.value.agent.window > 0]).toEqual(["recorded", false, true]);
    expect([f.image_digests.status, f.image_digests.value, !!f.image_digests.reason]).toEqual(["not_applicable", null, true]); expect(M.FINAL.every((k) => f[k].status === "unavailable" && f[k].value === null)).toBe(true);
    expect([f.budgets.value.unmetered, f.inputs.value.instruction_sha256]).toEqual([["compute"], sha256Hex(Buffer.from("go"))]);
    const ids = new Set(f.definitions.value.map((d: any) => d.id)); for (const id of ["prompt.system", "budget_policy", "workflow", "tier_policy", "tool.fs.read", "tool.submit"]) expect(ids.has(id), id).toBe(true);
    expect(f.definitions.value.every((d: any) => Number.isInteger(d.version) && d.sha256.length === 64)).toBe(true);
  });
  it("the required-field policy refuses a gap but not a legitimate absence", () => {
    const base: any = { manifest_schema_version: "1.0", run_id: "r", fields: Object.fromEntries(M.START.map((k) => [k, M.na("n/a")])) }, full = JSON.parse(JSON.stringify(base)); for (const k of M.REQUIRED.high) full.fields[k] = M.rec(1);
    expect(M.validate(full, "high")).toEqual([]); const gap = JSON.parse(JSON.stringify(full)); gap.fields.models = M.unavailable("the provider did not say"); expect(M.validate(gap, "core").some((e) => e.includes("models is unavailable"))).toBe(true);
    const legit = JSON.parse(JSON.stringify(full)); legit.fields.image_digests = M.na("local backend"); expect(M.validate(legit, "high")).toEqual([]);
    const missing = JSON.parse(JSON.stringify(full)); delete missing.fields.config_commit; expect(M.validate(missing, "core").some((e) => e.includes("config_commit is missing"))).toBe(true);
    const bad = JSON.parse(JSON.stringify(full)); bad.fields.task = { status: "unavailable", value: 5, reason: "x" }; expect(M.validate(bad, "core").length).toBeGreaterThan(0);
    const bad2 = JSON.parse(JSON.stringify(full)); bad2.fields.task = { status: "not_applicable", value: null }; expect(M.validate(bad2, "core").length).toBeGreaterThan(0);
  });
  it("the final fields and the manifest hash are recorded when the run finishes", async () => {
    const h = mk([call("fs.glob", { pattern: "/workspace/*" }), oa("d")]), run = await h.runToEnd("u", "go"), start = eventsOf(h, run, "run.created")[0].payload.manifest, fin = eventsOf(h, run, "run.finished"), f = fin[0].payload.fields, resp = eventsOf(h, run, "model.responded");
    expect(fin).toHaveLength(1); expect([f.tool_call_count.value, f.usage.value.input_tokens, f.outcome.value.verdict]).toEqual([1, resp.reduce((a: number, e: any) => a + e.payload.usage.input_tokens, 0), "not_evaluated"]);
    expect(["COMPLETED", "TERMINATED"]).toContain(f.termination_status.value.state); expect(fin[0].payload.manifest_hash).toBe(M.manifestHash(M.merged(start, f)));   // recomputable by anyone from the log
    const reqs = eventsOf(h, run, "model.requested"), mc = f.model_calls.value; expect([mc.map((c: any) => c.seq), mc.map((c: any) => c.call_id), mc.length >= 2]).toEqual([reqs.map((e: any) => e.seq), reqs.map((e: any) => e.payload.request_ref), true]);   // every model call links to the run
  });
  it("a terminated run also finishes with its reason in the manifest", async () => {
    const h = mk(Array.from({ length: 10 }, (_, i) => call("fs.glob", { pattern: `/workspace/${i}*` })), { budgets: { turns: 3 } }), run = await h.runToEnd("u", "go");
    expect(eventsOf(h, run, "run.finished")[0].payload.fields.termination_status.value).toEqual({ state: "TERMINATED", reason: "budget_exhausted:turns" });
  });
});
describe("resolving only through the manifest", () => {
  it("a definition published later does not change what a running run uses", async () => {
    const { h, run, loop } = await started(), pins = run.pins, before = pins.resolve("prompt.system"); await new DefinitionStore(h.svc).publish("prompt.system", "A COMPLETELY DIFFERENT PROMPT");
    expect(pins.resolve("prompt.system")).toBe(before); expect(Pins.load(h.svc, run.runId).resolve("prompt.system")).toBe(before); throws(() => pins.resolve("prompt.latest"), /not pinned/); throws(() => pins.resolve("prompt.other"), /not pinned/);
    await h.rm.start(run); await loop.runLoop(); expect(h.transport.requests[0].body.messages[0].content.startsWith(before)).toBe(true);
  });
  it("a migration is explicit, audited, and the only way a pin moves", async () => {
    const { h, run } = await started(), v2 = await new DefinitionStore(h.svc).publish("prompt.system", "second prompt"), old = run.pins.byId.get("prompt.system").version;
    await expect(run.pins.migrate(run, "prompt.system", 99, "ops", "x")).rejects.toThrow(/not published/); await run.pins.migrate(run, "prompt.system", v2.version, "ops", "approved rollout");
    const ev = eventsOf(h, run, "definition.migrated")[0].payload; expect([ev.from_version, ev.to_version, ev.by, ev.reason]).toEqual([old, v2.version, "ops", "approved rollout"]);
    expect(Pins.load(h.svc, run.runId).resolve("prompt.system")).toBe("second prompt");   // a restarted process sees the migration
  });
  it("runs created without a manifest pin nothing and behave as before", async () => {
    const h = mk([oa("d")]), run = await h.rm.createRun({ owner: "u", profile: "core", instruction: "go", agent: h.defaultAgent() }); expect([run.pins.active, run.pins.byId.size]).toEqual([false, 0]);
  });
});
describe("failing closed", () => {
  it("a tool that changed after the run was pinned ends the run and nothing is substituted", async () => {
    const { h, run, loop } = await started(); h.registry.get("fs.read")!.manifest.description += " (edited after pinning)"; await h.rm.start(run); await loop.runLoop();
    const mm = eventsOf(h, run, "definition.mismatch")[0].payload; expect([mm.id, mm.expected !== mm.observed, mm.reason.includes("changed after the run was pinned")]).toEqual(["tool.fs.read", true, true]);
    expect(eventsOf(h, run, "model.requested")).toHaveLength(0); expect(await run.state()).toBe("TERMINATED"); expect(eventsOf(h, run, "run.terminated")[0].payload.detail.startsWith("definition.mismatch")).toBe(true);
  });
  it("a pinned prompt whose content changed stops the run before the model is called", async () => {
    const { h, run, loop } = await started(), pin = run.pins.byId.get("prompt.system"); fs.writeFileSync(h.blobs.p(pin.sha256), "you have no rules now"); await h.rm.start(run); await loop.runLoop();
    expect(eventsOf(h, run, "definition.mismatch")[0].payload.id).toBe("prompt.system"); expect([eventsOf(h, run, "model.requested").length, h.transport.requests.length, eventsOf(h, run, "definition.verified").length]).toEqual([0, 0, 0]);
  });
  it("a tool registered after pinning cannot be exposed", async () => {
    const { h, run, loop } = await started(), base = h.registry.get("fs.read")!, { content_hash: _c, ...rest } = base.manifest; h.registry.register({ ...rest, name: "fs.new_tool" }, base.handler);
    await h.rm.start(run); await loop.runLoop(); const mm = eventsOf(h, run, "definition.mismatch")[0].payload; expect([mm.id, mm.reason.includes("not pinned"), eventsOf(h, run, "model.requested").length]).toEqual(["tool.fs.new_tool", true, 0]);
  });
  it("every pinned definition is verified when the run starts", async () => {
    const { h, run, loop } = await started(); await h.rm.start(run); await loop.runLoop(); const v = eventsOf(h, run, "definition.verified")[0].payload;
    expect(v.count).toBe(run.pins.byId.size); expect(v.count).toBeGreaterThanOrEqual(15); expect(new Set(v.definitions.map((d: any) => d.id))).toEqual(new Set(run.pins.byId.keys()));
  });
});
describe("skills come from the manifest, not from the disk", () => {
  it("a pinned skill is read from the manifest even if the file changes afterwards", async () => {
    const dir = tmpdir(), sk = path.join(dir, "skills", "refactor"); fs.mkdirSync(sk, { recursive: true }); const f = path.join(sk, "SKILL.md");
    fs.writeFileSync(f, "---\nname: refactor\ndescription: Safely refactor\n---\nORIGINAL STEPS\n");
    const h = mk([oa(null, [["skill.load", { name: "refactor" }], ["skill.load", { name: "nope" }]]), oa("d")], { configDir: dir }), { run, loop } = await h.createRun("u", "go", { agent: h.defaultAgent({ skills: ["refactor"] }) });
    fs.writeFileSync(f, "---\nname: refactor\ndescription: Safely refactor\n---\nREPLACED BY SOMEONE ELSE\n"); await h.rm.start(run); await loop.runLoop();   // edited after the run was pinned
    const text = eventsOf(h, run, "tool.call.completed")[0].payload.message.parts[0].parts[0].text; expect(text).toContain("ORIGINAL STEPS"); expect(text).not.toContain("REPLACED");
    expect(eventsOf(h, run, "tool.call.failed").map((e: any) => e.payload.error.code)).toEqual(["skill.not_found"]); expect(run.pins.has("skill.refactor")).toBe(true);
  });
});
