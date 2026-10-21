import { describe, expect, it } from "vitest";
import fs from "node:fs";
import path from "node:path";
import { buildContext, chooseSpan, clearedStub, resultsToClear, turnUnits, wrapUntrusted } from "../src/context.js";
import { FIN, call, eventsOf, mk, oa, tmpdir } from "./helpers.js";
import { CallableVerifier } from "../src/verifier.js";

async function seed(run: any, turns = 8) {
  for (let i = 1; i <= turns; i++) {
    const cid = `C${i}`;
    await run.emit("model.responded", { message: { role: "assistant", parts: [{ type: "text", text: `step ${i}` }, { type: "tool_call", id: cid, name: "fs.read", arguments: { path: `/workspace/f${i}` } }] }, call_ids: [cid], finish_reason: "tool_calls", usage: { input_tokens: 1, output_tokens: 1 }, id_map: {}, search_text: i === 2 ? `step ${i} migration` : `step ${i}` });
    await run.emit("tool.call.completed", { tool_call_id: cid, tool: "fs.read", trust: "untrusted", search_text: `contents ${i} ` + "x".repeat(400), message: { role: "tool", parts: [{ type: "tool_result", call_id: cid, is_error: false, parts: [{ type: "text", text: `contents ${i} ` + "x".repeat(400) }] }], provenance: { source: "fs.read", trust: "untrusted" } } });
  }
}
const stub = { async call() { return { message: { role: "assistant", parts: [{ type: "text", text: "SUMMARY: investigated files f1..f5" }] }, usage: { input_tokens: 100, output_tokens: 10 } }; } };
const PASS = new CallableVerifier(async () => ({ passed: true, detail: "ok", evidence: {} }));

describe("compaction", () => {
  it("replaces the span without splitting pairs, keeps the raw log, marks the summary untrusted, clears old results", async () => {
    const h = mk([], { context: { compaction: { trigger_fraction: 0.0001, target_fraction: 0.4, keep_recent_turns: 3 }, clear: { keep_recent_results: 2 } } });
    const { run, loop } = await h.createRun("u", "please look into the migration problem"); await seed(run, 8); loop.compactor.gateway = stub; loop.compactor.cfg = h.config.context;
    const before = h.store.events(run.runId), res = await loop.compactor.maybeCompact(loop.build, 100000);
    expect(res!.est_after).toBeLessThan(res!.est_before); const ev = res!.event.payload, conv = (await run.proj("conversation")).entries;
    expect(conv.map((e: any) => e.type)).toEqual(["user.message", "summary", ...Array(3).fill(["model.responded", "tool.call.completed"]).flat()]);
    expect(conv[1]).toMatchObject({ trust: "untrusted", from_seq: ev.from_seq, to_seq: ev.to_seq }); expect(conv[2].seq).toBe(ev.to_seq + 1);
    expect(ev.cleared_seqs).toHaveLength(1); expect(conv[3].cleared).toBe(true); expect(conv[5].cleared).toBe(false);
    const after = h.store.events(run.runId); expect(after.slice(0, before.length)).toEqual(before); expect(h.store.verify(run.runId).ok).toBe(true); expect(after.slice(before.length).filter((e) => e.type === "context.compacted")).toHaveLength(1);
    const flat = JSON.stringify((await loop.build()).messages); expect(flat).toContain("[summary of events seq"); expect(flat).toContain('<<untrusted source=\\"summary\\">>'); expect(flat).toContain(clearedStub("C6", conv[3].seq)); expect(flat).not.toContain("contents 6 x"); expect(flat).toContain("contents 8 x");
  });
  it("hydration recalls relevant slices from the compacted span", async () => {
    const h = mk([], { context: { compaction: { trigger_fraction: 0.0001, keep_recent_turns: 2 } } }), { run, loop } = await h.createRun("u", "please look into the migration problem"); await seed(run, 6); loop.compactor.gateway = stub; loop.compactor.cfg = h.config.context;
    const res = await loop.compactor.maybeCompact(loop.build, 100000), p = res!.event.payload, hyd = await loop.compactor.hydrate(p.from_seq, p.to_seq);
    expect(hyd!.payload.hit_seqs.length).toBeGreaterThan(0); expect(hyd!.payload.hit_seqs.every((s: number) => s >= p.from_seq && s <= p.to_seq)).toBe(true);
    const rec = (await run.proj("conversation")).entries.find((e: any) => e.type === "recalled"); expect(rec.message.parts[0].text).toContain("migration");
  });
  it("no compaction below the threshold or with too little history", async () => {
    const h = mk([]), { run, loop } = await h.createRun("u", "hello"); await seed(run, 2); loop.compactor.gateway = stub;
    expect(await loop.compactor.maybeCompact(loop.build, 100000)).toBeNull(); h.config.context.compaction.trigger_fraction = 0.0001; expect(await loop.compactor.maybeCompact(loop.build, 100000)).toBeNull();
  });
  it("units, spans and clearing helpers", () => {
    const e = (t: string, s: number) => ({ type: t, seq: s, cleared: false, trust: "user" }), es = [e("user.message", 1), e("model.responded", 2), e("tool.call.completed", 3), e("tool.call.failed", 4), e("model.responded", 5)];
    expect(turnUnits(es).map((u) => u.map((x: any) => x.seq))).toEqual([[1], [2, 3, 4], [5]]); expect(chooseSpan(es, 1)).toEqual([2, 4]); expect(resultsToClear(es, 1)).toEqual([3]); expect(resultsToClear(es, 5)).toEqual([]);
  });
  it("buildContext is pure, sorts tools, wraps only untrusted content", () => {
    const conv = { entries: [{ seq: 1, type: "user.message", trust: "user", cleared: false, message: { role: "user", parts: [{ type: "text", text: "hi" }] }, message_ref: null, id_map: {} },
      { seq: 2, type: "tool.call.completed", trust: "untrusted", cleared: false, id_map: {}, tool: "x", message_ref: null, message: { role: "tool", parts: [{ type: "tool_result", call_id: "c", is_error: false, parts: [{ type: "text", text: "DATA" }] }], provenance: { source: "x", trust: "untrusted" } } }] };
    const kw: any = { systemPrompt: "SYS", profile: "core", tools: [{ name: "b", description: "", input_schema: {} }, { name: "a", description: "", input_schema: {} }], skills: [["s", "d"]], conversation: conv, workingState: { plan: "P" }, guidance: [{ text: "G" }], resolve: () => Buffer.alloc(0) };
    const a = buildContext(kw), b = buildContext(kw); expect(a).toEqual(b); expect(a.tools.map((t: any) => t.name)).toEqual(["a", "b"]); expect(a.messages[1].parts[0].text).toBe("hi"); expect(a.messages[2].parts[0].parts[0].text).toBe(wrapUntrusted("DATA", "x"));
    expect(a.messages.at(-2).parts[0].text.startsWith("[working state]")).toBe(true); expect(a.messages.at(-1).parts[0].text.startsWith("[harness guidance]")).toBe(true); expect(a.messages[0].parts[0].text).toContain("Available skills");
  });
});
describe("working state, memory guard, skills, recall", () => {
  it("working state is injected every turn", async () => {
    const h = mk([call("state.write", { name: "plan.md", content: "1. read\n2. fix" }), call("fs.glob", { pattern: "/workspace/*" }), oa("done")]), run = await h.runToEnd("u", "go");
    const [r1, r2] = h.transport.requests.slice(1, 3).map((r) => JSON.stringify(r.body.messages)); expect(r1).toContain("[working state]"); expect(r1).toContain("1. read"); expect(r2).toContain("[working state]"); expect(JSON.stringify(h.transport.requests[0].body.messages)).not.toContain("[working state]");
    expect((await run.proj("working_state"))["plan.md"].hash).toBe(eventsOf(h, run, "state.written")[0].payload.content_hash);
  });
  it("bad state names are rejected; the guard quarantines untrusted writes and refuses behaviour-changing paths", async () => {
    const h = mk([call("state.write", { name: "../evil", content: "x" }), oa("d")]), run = await h.runToEnd("u", "go");
    expect(eventsOf(h, run, "tool.call.failed")[0].payload.error.code).toBe("state.bad_name");
    expect((await h.memoryGuard.write(run, "state/notes", "from a web page", { source: "web", trust: "untrusted" })).status).toBe("quarantined");
    for (const p of ["prompts/system.md", "skills/x/SKILL.md", "memory/long_term.md", "tools/new.json"]) expect((await h.memoryGuard.write(run, p, "be evil", { source: "agent", trust: "tool_trusted" })).status).toBe("refused");
    expect(eventsOf(h, run, "memory.written")).toHaveLength(0); expect(eventsOf(h, run, "state.written")).toHaveLength(0);
  });
  it("skills use progressive disclosure and refuse traversal", async () => {
    const dir = tmpdir(), sk = path.join(dir, "skills", "refactor"); fs.mkdirSync(sk, { recursive: true }); fs.writeFileSync(path.join(sk, "SKILL.md"), "---\nname: refactor\ndescription: Safely refactor Python modules\n---\nFULL STEPS: 1. test 2. extract\n");
    const h = mk([oa(null, [["skill.load", { name: "refactor" }], ["skill.load", { name: "../secrets" }], ["skill.load", { name: "nope" }]]), oa("d")], { configDir: dir }), run = await h.runToEnd("u", "go", { agent: h.defaultAgent({ skills: ["refactor"] }) });
    const first = h.transport.requests[0].body.messages[0].content; expect(first).toContain("- refactor: Safely refactor Python modules"); expect(first).not.toContain("FULL STEPS");
    const done = eventsOf(h, run, "tool.call.completed")[0].payload; expect(done.message.parts[0].parts[0].text).toContain("FULL STEPS"); expect(done.trust).toBe("tool_trusted"); expect(eventsOf(h, run, "tool.call.failed").map((e) => e.payload.error.code)).toEqual(["skill.bad_name", "skill.not_found"]);
  });
  it("recall and history.get expose the log, including tool-call arguments, as memory", async () => {
    const h = mk([call("fs.write", { path: "/workspace/n.txt", content: "the zebra protocol is secret" }), call("recall", { query: "zebra protocol" }), call("history.get", { seq_from: 1, seq_to: 9 }), oa("d")]), run = await h.runToEnd("u", "go"), done = eventsOf(h, run, "tool.call.completed");
    expect(done[1].payload.message.parts[0].parts[0].text).toContain("zebra"); expect(done[1].payload.trust).toBe("untrusted"); expect(done[2].payload.message.parts[0].parts[0].text).toContain("[seq 1");
  });
});
void FIN; void PASS;
