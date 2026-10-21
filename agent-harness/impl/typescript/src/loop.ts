/** LoopController: the agent turn (design §7.2). */
import { PluginUnavailable } from "./plugins.js";
import { UNVERIFIED } from "./reconcile.js";
import { DefinitionMismatch } from "./definitions.js";
import fs from "node:fs";
import path from "node:path";
import { BudgetController, costOf, reserveCost } from "./budgets.js";
import { buildContext, Compactor } from "./context.js";
import { activeTimeMs } from "./projections.js";
import { GatewayError } from "./gateway.js";
import { globMatch } from "./policy.js";
import { callSignature, stuckAction, turnSignature } from "./stuck.js";
import { ALWAYS_LOADED, messageText } from "./builtin.js";
import { canonicalize } from "./util.js";
const NUDGE = "Your last response contained no tool call and no completion request. Use a tool to make progress, or call finish(summary) when done.";
const callsOf = (m: any) => m.parts.filter((p: any) => p.type === "tool_call");

export class LoopController {
  svc: any; budgets: BudgetController; model: any; completionSummary: string | null = null; skills: [string, string][]; tctxState: any = {}; compactor: Compactor;
  maxIdle: number; stuckThreshold: number; stuckExtra: number; maxVerifFailures: number; divisor: number;
  constructor(public run: any, public rm: any, public gateway: any, public runtime: any, public registry: any, public agent: any, public models: any, public config: any,
              public verifier: any = null, public hooks: Record<string, any> = {}, public configDir: string | null = null, public memoryGuard: any = null) {
    this.svc = run.svc; this.budgets = new BudgetController(run); this.model = models[agent.model_ref]; this.skills = this.loadSkills();
    this.hooks.finish = (s: string) => this.finishHook(s);
    const lc = config.loop ?? {}; this.maxIdle = lc.max_idle_turns ?? 3; this.stuckThreshold = lc.stuck?.threshold ?? 4; this.stuckExtra = lc.stuck?.extra_repeats ?? 2; this.maxVerifFailures = lc.max_verification_failures ?? 3;
    this.divisor = config.tokens?.divisor ?? 3;
    this.compactor = new Compactor(run, gateway, config.context ?? {}, agent.model_ref, (r) => this.svc.blobs.get(r), this.budgets, this.model.price);
  }
  private loadSkills(): [string, string][] {
    return (this.agent.skills ?? []).map((name: string) => {
      let desc = name;
      if (this.run.pins.active && this.run.pins.has(`skill.${name}`)) { const m = this.run.pins.resolve(`skill.${name}`).match(/^description:\s*(.+)$/m); return [name, m ? m[1].trim() : name] as [string, string]; }   // skills come from the manifest, not from whatever is on disk now
      if (this.configDir) { const p = path.join(this.configDir, "skills", name, "SKILL.md"); if (fs.existsSync(p)) { const m = fs.readFileSync(p, "utf8").match(/^description:\s*(.+)$/m); desc = m ? m[1].trim() : name; } }
      return [name, desc];
    });
  }
  sandbox(): any { for (const h of this.rm.sandboxes.handles.values()) if (h.labels["harness.run_id"] === this.run.runId && h.role === "work") return h; throw new Error("no work sandbox"); }
  private async finishHook(summary: string) { this.completionSummary = summary; await this.run.emit("completion.proposed", { summary, via: "finish" }); }   // durable, so a crash cannot lose it
  tctx() { return { run: this.run, svc: this.svc, sandbox: this.sandbox(), registry: this.registry, agent: this.agent, hooks: this.hooks, state: this.tctxState, configDir: this.configDir, memoryGuard: this.memoryGuard }; }
  build = async () => {
    const conv = await this.run.proj("conversation"), eph = await this.run.proj("ephemeral"), ws = await this.run.proj("working_state"), lp = await this.run.proj("loop");
    const names: string[] = this.config.context?.working_state ?? ["*"], working: Record<string, string> = {};
    for (const [n, v] of Object.entries<any>(ws)) if (names.some((g) => globMatch(g, n))) working[n] = this.svc.blobs.get(v.hash).toString("utf8");
    const tools = this.registry.exposed(this.agent.tool_allowlist, ALWAYS_LOADED, lp.exposed_tools, this.config.tools?.dynamic_threshold ?? 20);
    const pins = this.run.pins; let prompt = this.agent.system_prompt;
    if (pins.active) { prompt = pins.resolve("prompt.system"); for (const t of tools) pins.checkHash(`tool.${t.name}`, t); }   // definitions are resolved through the manifest and verified before use (§13.9)
    return buildContext({ systemPrompt: prompt, profile: this.run.profile, tools, skills: this.skills, conversation: conv, workingState: working, guidance: eph.guidance, resolve: (r) => this.svc.blobs.get(r), divisor: this.divisor, window: this.model.window });
  };
  static searchable(msg: any, calls: any[]) { return [messageText(msg), ...calls.map((c) => `${c.name} ${canonicalize(c.arguments)}`)].filter(Boolean).join("\n").slice(0, 8000); }
  private async checkpoint(): Promise<boolean> {
    for (;;) { const st = await this.run.state(); if (st === "PAUSED") { await this.run.waitResumed(); continue; } return st === "RUNNING" || st === "COMPLETING"; }
  }
  async runLoop(): Promise<string> {
    try { return await this.runLoopInner(); }
    catch (e) { if (this.svc.writer.closed) return "CLOSED"; throw e; }   // harness shut down underneath us: stop quietly, like a dead process
  }
  private async definitionFailure(e: DefinitionMismatch): Promise<string> {
    await this.run.emit("definition.mismatch", { id: e.defId, expected: e.expected, observed: e.observed, reason: e.reason });
    await this.rm.terminate(this.run, "fatal_error", `definition.mismatch: ${e.message}`); return "TERMINATED";   // controlled, and nothing is substituted
  }
  private async runLoopInner(): Promise<string> {
    const pins = this.run.pins;
    if (pins.active) {   // verify every pinned definition when the run starts, and again when it resumes
      let ok: any[];
      try {
        ok = pins.verifyAll(); const host = this.svc.plugins;
        for (const pid of [...pins.byId.keys()].filter((i) => i.startsWith("plugin."))) {   // a plugin that was swapped or removed since the run was pinned is a mismatch
          try { pins.checkHash(pid, host.definition(pid.slice(7))); } catch (e) { if (e instanceof PluginUnavailable) throw new DefinitionMismatch(pid, pins.byId.get(pid).sha256, null, "the plugin is no longer loaded"); throw e; }
        }
      } catch (e) { if (e instanceof DefinitionMismatch) return this.definitionFailure(e); throw e; }
      await this.run.emit("definition.verified", { count: ok.length, definitions: ok });
    }
    for (;;) {
      if (!(await this.checkpoint())) return this.run.state();
      if ((await this.run.state()) !== "RUNNING") return this.run.state();
      let out: string;
      try { out = await this.turn(); }
      catch (e: any) {
        if (e instanceof DefinitionMismatch) return this.definitionFailure(e);
        await this.rm.terminate(this.run, "fatal_error", `${e?.constructor?.name}: ${e?.message}`); return "TERMINATED";   // a bug must not leave the run dangling
      }
      if (["done", "terminated", "halted"].includes(out)) return this.run.state();
    }
  }
  async turn(): Promise<string> {
    const run = this.run, lp0 = await run.proj("loop"), turnNo = lp0.turn + 1;
    const compacted = await this.compactor.maybeCompact(this.build, this.model.window);
    if (compacted) await this.compactor.hydrate(compacted.event.payload.from_seq, compacted.event.payload.to_seq);
    const ctx = await this.build();
    await run.emit("context.assembled", { turn: turnNo, segment_hashes: ctx.segment_hashes, est_tokens: ctx.est_tokens, window: ctx.window });
    const maxOut = this.agent.max_output_tokens ?? 4096, margin = this.svc.config?.budgets?.safety_margin ?? 0.05;
    const [rid, rex] = await this.budgets.reserve({ turns: 1, tokens: ctx.est_tokens + maxOut, cost: reserveCost(ctx.est_tokens, maxOut, this.model.price, margin) });   // worst case, reserved before the call (§7.4.4)
    let ex: string | null = rex;
    if (ex) { await this.rm.terminate(run, `budget_exhausted:${ex}`, `before turn ${turnNo}`); return "terminated"; }
    const request = { model_ref: this.agent.model_ref, messages: ctx.messages, tools: ctx.tools, params: { max_output_tokens: this.agent.max_output_tokens ?? 4096 }, metadata: { run_id: run.runId, turn: turnNo } };
    const reqRef = this.svc.blobs.put(canonicalize(request));
    await run.emit("model.requested", { request_ref: reqRef, model_ref: this.agent.model_ref, provider: this.model.provider, est_input_tokens: ctx.est_tokens, turn: turnNo });
    const rs = await run.proj("run_state"), tlim = (await run.proj("budgets")).agent.time_ms;
    const remaining = tlim ? tlim.limit - activeTimeMs(rs, this.svc.clock.nowMs()) : null;
    let resp: any;
    try { resp = await this.gateway.call(request, ctx.id_map, (info: any) => run.emit("model.failed", info), remaining); }
    catch (e: any) {
      if (e instanceof GatewayError) {
        if (e.code === "gateway.connection") await this.budgets.commit(rid!, { turns: 1, tokens: ctx.est_tokens + maxOut, cost: reserveCost(ctx.est_tokens, maxOut, this.model.price, margin) }, { unreconciled: true });   // may have reached the provider: charge the estimate, flagged
        else await this.budgets.commit(rid!, { turns: 1 });                                                                                                                                                     // provably did not run: the turn counts, the rest is released
        await this.rm.terminate(run, "fatal_error", `${e.code}: ${e.message}`); return "terminated";
      }
      await this.budgets.commit(rid!, { turns: 1 }); throw e;
    }
    const msg = resp.message, calls = callsOf(msg), stored = this.svc.blobs.inlineOrRef(msg, "message", "message_ref");
    await run.emit("model.responded", { ...stored, call_ids: calls.map((c: any) => c.id), finish_reason: resp.finish_reason, usage: resp.usage, latency_ms: resp.latency_ms ?? 0, attempt: resp.attempt ?? 1, id_map: resp.provider_meta.id_map ?? {}, search_text: LoopController.searchable(msg, calls) });
    ex = await this.budgets.commit(rid!, { turns: 1, tokens: resp.usage.input_tokens + resp.usage.output_tokens, cost: costOf(resp.usage, this.model.price) });
    await this.budgets.snapshotTime();
    if (ex) { await this.rm.terminate(run, `budget_exhausted:${ex}`, `after turn ${turnNo}`); return "terminated"; }
    let sigs: string[] = [], idle = false;
    if (calls.length) {
      const results = await this.executeCalls(calls);
      for (const r of results) if (r.terminate) { await this.rm.terminate(run, r.terminate, `turn ${turnNo}`); return "terminated"; }
      sigs = calls.map((c: any, i: number) => callSignature(c.name, c.arguments, results[i].digest));
      if (!(await this.checkpoint())) return "halted";
    } else if (this.verifier !== null && resp.finish_reason === "stop") {
      this.completionSummary = messageText(msg) || "(end of turn)";
      await run.emit("completion.proposed", { summary: this.completionSummary, via: "end_turn" });
    } else {
      idle = true;
      if (resp.finish_reason === "length") await run.emit("guidance.injected", { kind: "policy", text: "Your response was cut off by the output limit. Respond more briefly and use tools." });
    }
    await run.emit("turn.completed", { turn: turnNo, signature: turnSignature(sigs), idle });
    if (this.completionSummary !== null) { const s = this.completionSummary; this.completionSummary = null; return this.complete(s); }
    const lp = await run.proj("loop");
    if (idle) {
      if (lp.idle_streak > this.maxIdle) { await this.rm.terminate(run, "verification_failed_limit", `${lp.idle_streak} consecutive idle turns`); return "terminated"; }
      await run.emit("guidance.injected", { kind: "policy", text: NUDGE }); return "continue";
    }
    const act = stuckAction(lp, this.stuckThreshold, this.stuckExtra);
    if (act === "guidance") {
      await run.emit("stuck_loop.detected", { signature: lp.last_signature, count: lp.repeat_count, action: "guidance" });
      await run.emit("guidance.injected", { kind: "stuck_loop", text: "You are repeating the same actions with the same results. Change your approach." });
    } else if (act === "terminate") {
      const high = run.profile === "high";
      await run.emit("stuck_loop.detected", { signature: lp.last_signature, count: lp.repeat_count, action: high ? "escalate" : "terminate" });
      if (high) { await run.transition("AWAITING_HUMAN", "stuck loop"); return "halted"; }
      await this.rm.terminate(run, "stuck_loop", `signature repeated ${lp.repeat_count} times`); return "terminated";
    }
    return "continue";
  }
  async executeCalls(calls: any[]): Promise<any[]> {
    const tctx = this.tctx();
    if (calls.length > 1 && calls.every((c) => this.registry.get(c.name)?.manifest.parallel_safe)) return Promise.all(calls.map((c) => this.runtime.execute(this.run, c, tctx)));
    const out: any[] = [];
    for (const c of calls) { if (!(await this.checkpoint())) break; out.push(await this.runtime.execute(this.run, c, tctx)); }
    while (out.length < calls.length) out.push({ digest: "", ok: false });
    return out;
  }
  async resume(plan: any): Promise<string> {
    const run = this.run, st = await run.state();
    if (plan.halted) return st;
    if (st === "TERMINATING") {
      const ev = this.svc.store.events(run.runId, { types: ["run.state_changed"] }).filter((e: any) => e.payload.to === "TERMINATING").pop();
      await this.rm.terminate(run, ev.payload.reason, "completed teardown after crash"); return run.state();
    }
    if (st === "COMPLETING") { await this.complete("(recovered after crash)"); return run.state(); }
    if (["PROVISIONING", "READY", "CREATED"].includes(st)) { await this.rm.terminate(run, "fatal_error", `crashed during ${st}`); return run.state(); }
    if (plan.pending_calls.length) {
      const results = await this.executeCalls(plan.pending_calls);
      for (const r of results) if (r.terminate) { await this.rm.terminate(run, r.terminate, "after recovery"); return "TERMINATED"; }
      await run.emit("turn.completed", { turn: (await run.proj("loop")).turn + 1, signature: null, idle: false });
    }
    const pending = (await run.proj("loop")).completion_pending;
    if (pending !== null) { this.completionSummary = null; await this.complete(pending); if ((await run.state()) !== "RUNNING") return run.state(); }
    return this.runLoop();
  }
  async complete(summary: string): Promise<string> {
    const run = this.run;
    await run.transition("COMPLETING", "agent proposed completion");
    const unverified = Object.entries<any>((await run.proj("reconcile")).subjects).filter(([, v]) => UNVERIFIED.includes(v.state));
    const res: any = unverified.length ? { passed: false, detail: "required effects remain unverified: " + unverified.sort(([a], [b]) => (a < b ? -1 : 1)).map(([k, v]) => `${k} (${v.state})`).join(", "), evidence: { unverified: Object.fromEntries(unverified) } }   // a run is not complete while required effects remain unverified (§5.13)
      : this.verifier === null ? { passed: false, detail: "no verifier configured: the agent's claim of success is not evidence", evidence: {} } : await this.verifier.verify(run, this.sandbox());
    const ref = this.svc.blobs.putJson({ summary, ...res });
    await run.emit("verification.completed", { passed: res.passed, detail: res.detail, evidence_ref: ref });
    if (res.passed) { await this.rm.complete(run, ref); return "done"; }
    if ((await run.proj("run_state")).verification_failures >= this.maxVerifFailures) { await this.rm.terminate(run, "verification_failed_limit", res.detail); return "terminated"; }
    await run.transition("RUNNING", "verification failed");
    await run.emit("guidance.injected", { kind: "policy", text: `Verification failed: ${res.detail}. Fix the problem before finishing.` });
    return "continue";
  }
}
