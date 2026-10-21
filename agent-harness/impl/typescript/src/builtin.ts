/** Built-in tools (design §7.5, §9.6) and the MemoryGuard (§11.6). */
import { DefinitionMismatch } from "./definitions.js";
import fs from "node:fs";
import path from "node:path";
import { ToolRegistry } from "./registry.js";
import { ToolContext, ToolError, ToolResult } from "./runtime.js";
const STR = (o: any = {}) => ({ type: "string", ...o }), INT = (o: any = {}) => ({ type: "integer", ...o });
const S = (props: Record<string, any>) => { const req: string[] = []; const p: any = {}; for (const [k, v] of Object.entries<any>(props)) { const { optional, ...rest } = v; p[k] = rest; if (!optional) req.push(k); } return { type: "object", properties: p, required: req, additionalProperties: false }; };
const man = (name: string, description: string, input_schema: any, location: string, idempotency: string, effect_class: string, extra: any = {}) => ({ name, version: "1.0.0", description, input_schema, location, idempotency, effect_class, ...extra });
const messageText = (m: any): string => m.parts.map((p: any) => (p.type === "text" ? p.text : p.type === "tool_result" ? messageText({ parts: p.parts }) : "")).filter(Boolean).join("\n");
export { messageText };
export const ALWAYS_LOADED = ["fs.*", "shell.run", "recall", "history.get", "state.*", "skill.load", "tool.search", "submit", "finish"];
export const STATE_DIR = "/workspace/.harness/state";
import { decideWrite, resolveWritePolicy } from "./memoryPolicy.js";

export class MemoryGuard {
  policy: any; policyError: string | null = null;
  constructor(public svc: any, policy?: any) {
    if (policy !== undefined) this.policy = resolveWritePolicy(policy);                          // an explicit policy that is invalid is a programming error
    else { try { this.policy = resolveWritePolicy(svc.config?.memory?.write_path); } catch (e: any) { this.policy = resolveWritePolicy(); this.policyError = String(e.message); } }   // a bad configuration is reported by preflight (PF-U02); the safe default applies meanwhile
  }
  async write(run: any, p: string, content: string, provenance: any, sandboxWriter?: () => Promise<void>) {
    const h = this.svc.blobs.put(content), d = decideWrite(this.policy, p, provenance, content);
    if (d.status === "refused") { await run.emit("memory.quarantined", { path: p, provenance, trust: provenance.trust, reason: d.reason }); return { status: "refused", reason: "requires self-improvement gate (§17)" }; }
    if (d.status === "quarantined") { await run.emit("memory.quarantined", { path: p, provenance, trust: provenance.trust, reason: d.reason, content_hash: h, ...(d.pattern ? { pattern: d.pattern } : {}) }); return { status: "quarantined", content_hash: h, reason: d.reason }; }
    if (sandboxWriter) await sandboxWriter();
    await run.emit("memory.written", { path: p, provenance, trust: provenance.trust, content_hash: h });
    return { status: "written", content_hash: h };
  }
}
const H = {
  async fsRead(a: any, t: ToolContext) { return t.sandbox.executor.fsRead(a.path, a.start_line, a.end_line).content; },
  async fsWrite(a: any, t: ToolContext) { const r = t.sandbox.executor.fsWrite(a.path, a.content); return `wrote ${r.bytes} bytes to ${a.path}`; },
  async fsEdit(a: any, t: ToolContext) { t.sandbox.executor.fsEdit(a.path, a.old, a.new); return `edited ${a.path}`; },
  async fsSearch(a: any, t: ToolContext) { const r = t.sandbox.executor.fsGrep(a.pattern, a.path ?? "/workspace"); return r.matches.map((m: any) => `${m.path}:${m.line}: ${m.text}`).join("\n") || "(no matches)"; },
  async fsGlob(a: any, t: ToolContext) { return t.sandbox.executor.fsGlob(a.pattern).paths.join("\n") || "(no matches)"; },
  async shellRun(a: any, t: ToolContext) {
    const ex = t.sandbox.executor; let sid = t.state.session_id;
    if (!ex.sessions.has(sid)) { sid = ex.sessionOpen({ cwd: t.state.cwd ?? "/workspace", env: t.state.env, user: "agent" }).session_id; t.state.session_id = sid; }
    const r = await ex.sessionRun(sid, a.cmd, a.timeout_s ?? 120); t.state.cwd = r.cwd;
    let out = r.stdout + (r.stderr ? "\n[stderr]\n" + r.stderr : ""); out += r.timed_out ? "\n[timed out]" : `\n[exit code ${r.exit_code}]`;
    return new ToolResult(out, null, { cwd: r.cwd });
  },
  async recall(a: any, t: ToolContext) { const hits = t.svc.store.search(t.run.runId, a.query, a.k ?? 5); return new ToolResult(hits.map((h: any) => `[seq ${h.seq} ${h.type}] ${h.text.slice(0, 500)}`).join("\n") || "(no results)", "untrusted"); },
  async historyGet(a: any, t: ToolContext) {
    const store = t.svc.store;
    if ("tool_call_id" in a) {
      const ev = store.events(t.run.runId, { types: ["tool.call.completed"] }).find((e: any) => e.payload.tool_call_id === a.tool_call_id);
      if (!ev) throw new ToolError("history.not_found", "state", "no such tool call");
      const ref = ev.payload.result_ref, text = ref ? t.svc.blobs.get(ref).toString("utf8") : messageText(ev.payload.message);
      return new ToolResult(text.slice(a.offset ?? 0), "untrusted");
    }
    const lo = a.seq_from, hi = a.seq_to ?? a.seq_from;
    if (hi - lo > 50) throw new ToolError("history.range_too_large", "tool", "request at most 50 events", false, "Narrow the range");
    const out = store.events(t.run.runId, { afterSeq: lo - 1, upToSeq: hi }).map((e: any) => `[seq ${e.seq} ${e.type}] ${e.payload.search_text || ("message" in e.payload ? messageText(e.payload.message) : JSON.stringify(e.payload).slice(0, 500))}`);
    return new ToolResult(out.join("\n") || "(empty range)", "untrusted");
  },
  async stateWrite(a: any, t: ToolContext) {
    const name = a.name;
    if (!/^[A-Za-z0-9_.-]+$/.test(name) || name.startsWith(".")) throw new ToolError("state.bad_name", "tool", "name must be [A-Za-z0-9_.-]", false, "Use a simple name such as plan.md");
    const res = await t.memoryGuard.write(t.run, `state/${name}`, a.content, { source: "agent", trust: "tool_trusted" }, async () => { t.sandbox.executor.fsWrite(`${STATE_DIR}/${name}`, a.content); });
    if (res.status === "written") await t.run.emit("state.written", { name, content_hash: res.content_hash, path: `${STATE_DIR}/${name}`, search_text: `${name}: ${a.content.slice(0, 2000)}` });
    return `state ${name}: ${res.status}`;
  },
  async stateRead(a: any, t: ToolContext) { return new ToolResult(t.sandbox.executor.fsRead(`${STATE_DIR}/${a.name}`).content, "untrusted"); },
  /** Skills the agent wrote (workspace) or a third party supplied are untrusted boundaries: untrusted result, and the call needs approval (PRD §4.3 Trust Escalation Prevention). */
  async skillLoadUntrusted(a: any, t: ToolContext) {
    const name = a.name, source = a.source;
    if (typeof name !== "string" || name.includes("/") || name.includes("..") || name.startsWith(".")) throw new ToolError("skill.bad_name", "security", "invalid skill name");
    let text: string;
    if (source === "workspace") { try { text = t.sandbox.executor.fsRead(`/workspace/skills/${name}/SKILL.md`).content; } catch { throw new ToolError("skill.not_found", "tool", `no workspace skill '${name}'`); } }
    else if (source === "third_party") {
      const d = t.run.svc.config?.skills?.third_party_dir; if (!d) throw new ToolError("skill.unavailable", "environment", "no third-party skills directory is configured");
      const p = path.join(d, name, "SKILL.md"); if (!fs.existsSync(p)) throw new ToolError("skill.not_found", "tool", `no third-party skill '${name}'`); text = fs.readFileSync(p, "utf8");
    } else throw new ToolError("skill.bad_source", "tool", "source must be 'workspace' or 'third_party'");
    return new ToolResult(text, "untrusted");
  },
  async skillLoad(a: any, t: ToolContext) {
    if (!t.configDir) throw new ToolError("skill.unavailable", "environment", "no config repo configured");
    if (a.name.includes("/") || a.name.includes("..")) throw new ToolError("skill.bad_name", "security", "invalid skill name");
    const pins = t.run.pins, p = path.join(t.configDir, "skills", a.name, "SKILL.md");
    if (pins?.active) {   // a pinned skill is read from the manifest, never from whatever is on disk now
      try { return new ToolResult(pins.resolve(`skill.${a.name}`), "tool_trusted"); }
      catch (e: any) {
        if (!(e instanceof DefinitionMismatch)) throw e;
        if (e.expected === null && !fs.existsSync(p)) throw new ToolError("skill.not_found", "tool", `no skill '${a.name}'`);   // it does not exist at all
        throw new ToolError("definition.mismatch", "security", `skill '${a.name}' is not available as pinned: ${e.reason}`);   // it exists now but is not what the run began with
      }
    }
    if (!fs.existsSync(p)) throw new ToolError("skill.not_found", "tool", `no skill '${a.name}'`);
    return new ToolResult(fs.readFileSync(p, "utf8"), "tool_trusted");
  },
  async toolSearch(a: any, t: ToolContext) {
    const found = t.registry.search(a.query, t.agent.tool_allowlist), names = found.map((m: any) => m.name);
    if (names.length) await t.run.emit("tools.exposed", { names });
    return found.map((m: any) => `${m.name}: ${m.description}`).join("\n") || "(no matching tools)";
  },
  async submit(a: any, t: ToolContext) { if (!t.hooks.submit) throw new ToolError("submit.unavailable", "tool", "submit is only available in evaluation runs"); return t.hooks.submit(a.note); },
  async submitReconcile(_a: any, t: ToolContext) { return t.hooks.submit_resume ? t.hooks.submit_resume() : null; },
  async finish(a: any, t: ToolContext) { await t.hooks.finish(a.summary); return "completion proposed; verification will run"; },
};
export function registerBuiltins(reg: ToolRegistry) {
  reg.register(man("fs.read", "Read a file (absolute path under /workspace or /scratch).", S({ path: STR(), start_line: INT({ optional: true }), end_line: INT({ optional: true }) }), "sandbox", "idempotent", "read_only"), H.fsRead);
  reg.register(man("fs.write", "Create or overwrite a file.", S({ path: STR(), content: STR() }), "sandbox", "idempotent", "reversible_write", { world: { adapter: "workspace_file", resource_arg: "path", preconditions: [], expected_effects: ["content_equals"] } }), H.fsWrite);
  reg.register(man("fs.edit", "Replace one exact, unique occurrence of `old` with `new`.", S({ path: STR(), old: STR(), new: STR() }), "sandbox", "keyed", "reversible_write"), H.fsEdit);
  reg.register(man("fs.search", "Regex search over files.", S({ pattern: STR(), path: STR({ optional: true }) }), "sandbox", "idempotent", "read_only"), H.fsSearch);
  reg.register(man("fs.glob", "Glob files by absolute pattern.", S({ pattern: STR() }), "sandbox", "idempotent", "read_only"), H.fsGlob);
  reg.register(man("shell.run", "Run a shell command in a persistent session.", S({ cmd: STR(), timeout_s: INT({ minimum: 1, optional: true }) }), "sandbox", "non_idempotent", "irreversible", { timeout_s: 600 }), H.shellRun);
  reg.register(man("recall", "Search this run's history.", S({ query: STR(), k: INT({ minimum: 1, maximum: 20, optional: true }) }), "control_plane", "idempotent", "read_only"), H.recall);
  reg.register(man("history.get", "Retrieve original content from the log by seq range or tool_call_id.", { type: "object", properties: { seq_from: INT({ minimum: 1 }), seq_to: INT({ minimum: 1 }), tool_call_id: STR(), offset: INT({ minimum: 0 }) }, additionalProperties: false, anyOf: [{ required: ["seq_from"] }, { required: ["tool_call_id"] }] }, "control_plane", "idempotent", "read_only"), H.historyGet);
  reg.register(man("state.write", "Write a named working-state artifact (plan, todo, notes).", S({ name: STR(), content: STR() }), "control_plane", "idempotent", "reversible_write"), H.stateWrite);
  reg.register(man("state.read", "Read a named working-state artifact.", S({ name: STR() }), "control_plane", "idempotent", "read_only"), H.stateRead);
  reg.register(man("skill.load", "Load a skill's full instructions.", S({ name: STR() }), "control_plane", "idempotent", "read_only"), H.skillLoad);
  reg.register(man("skill.load_untrusted", "Load a skill the agent wrote or a third party supplied. Untrusted; requires approval.", S({ name: STR(), source: STR() }), "control_plane", "idempotent", "external_facing"), H.skillLoadUntrusted);
  reg.register(man("tool.search", "Find tools beyond the always-loaded set.", S({ query: STR() }), "control_plane", "idempotent", "read_only"), H.toolSearch);
  reg.register(man("submit", "Submit the current workspace for scoring.", S({ note: STR({ optional: true }) }), "control_plane", "non_idempotent", "reversible_write"), H.submit, H.submitReconcile);
  reg.register(man("finish", "Propose completion of the task.", S({ summary: STR() }), "control_plane", "idempotent", "reversible_write"), H.finish);
}
