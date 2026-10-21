/** ToolRuntime: validate -> policy -> approval -> execute -> shape (design §9.4, §9.7, §9.8). */
import { BudgetController, ConcurrencyLimiter, QueueFull } from "./budgets.js";
import { Gate, VersionConflict, WorkspaceFileAdapter, riskOf } from "./reconcile.js";
import { parseTs } from "./util.js";
import { SelectionMonitor, rules as selectionRules, unsafeExecutionAlert } from "./selection.js";
import { decide, globMatch } from "./policy.js";
import { ExecError } from "./sandbox.js";
import { sha256Hex } from "./util.js";
import { ToolRegistry } from "./registry.js";
export const MAX_RETRY_ATTEMPTS = 3;
export class ToolError extends Error {
  constructor(public code: string, public category: string, message: string, public retryable = false, public hint: string | null = null, public alternatives: string[] = []) { super(message); }
  toDict() { return { code: this.code, category: this.category, message: this.message, retryable: this.retryable, hint: this.hint, alternatives: this.alternatives }; }
}
export class ToolResult { constructor(public text: string, public trust: string | null = null, public extra: Record<string, any> = {}) {} }
export interface ToolContext { run: any; svc: any; sandbox: any; registry: ToolRegistry; agent: any; hooks: Record<string, any>; state: Record<string, any>; configDir: string | null; memoryGuard: any }
export class ToolMetrics { calls = 0; selectionErrors = 0; rate() { return this.calls ? this.selectionErrors / this.calls : 0; } }
const withTimeout = <T>(p: Promise<T>, s: number): Promise<T> => new Promise((res, rej) => { const t = setTimeout(() => rej(Object.assign(new Error("timeout"), { isTimeout: true })), s * 1000); p.then((v) => { clearTimeout(t); res(v); }, (e) => { clearTimeout(t); rej(e); }); });

export class ToolRuntime {
  metrics = new ToolMetrics(); limiter: any; alertCfg: any; monitors = new Map<string, SelectionMonitor>(); gate: Gate; criticalTools: Set<string>;
  constructor(public registry: ToolRegistry, public ruleset: any, public approvals: any, public svc: any, public redactor: any = null, public divisor = 3, public alertThreshold = 0.05) { const b = svc?.config?.budgets ?? {}; this.limiter = new ConcurrencyLimiter(b.workers ?? 4, b.queue ?? 100); this.alertCfg = selectionRules(svc?.config?.alerts); const rc = svc?.config?.reconcile ?? {}; this.gate = new Gate({ workspace_file: new WorkspaceFileAdapter() }, rc.approval_max_age_s ?? 900); this.criticalTools = new Set(rc.critical_tools ?? []); }   // the pre-action gate (§5.13)   // B_concurrency: a bounded pool and a bounded queue (§7.4.4)
  /** Feed one selection outcome to the run's monitor; a transition (raise, escalate, resolve) becomes an event with the tool but never the arguments. */
  private async observe(run: any, error: boolean, tool: string) {
    let mon = this.monitors.get(run.runId); if (!mon) { mon = SelectionMonitor.fromEvents(this.svc.store.events(run.runId), { ...this.alertCfg }); this.monitors.set(run.runId, mon); }
    const t = mon.observe(error, { tool }); if (t) await run.emit(t.event, t.payload);
  }
  /** The action as the gate sees it. The risk follows the potential impact of the effect and not the tool's name or the agent that called it; a policy can mark a tool critical. */
  worldAction(run: any, cid: string, name: string, args: any, m: any, decision = "allow"): any | null {
    const w = m.world; if (!w) return null; let approval: any = null;
    if (decision === "require_approval") {
      const evs = this.svc.store.events(run.runId, { types: ["approval.requested", "approval.decided", "approval.revoked"] }), aid = [...evs].reverse().find((e: any) => e.type === "approval.requested" && e.payload.tool_call_id === cid)?.payload.approval_id;
      const dec = [...evs].reverse().find((e: any) => e.type === "approval.decided" && e.payload.approval_id === aid);
      if (dec) approval = { status: dec.payload.status, decided_at_s: parseTs(dec.ts) / 1000, revoked: evs.some((e: any) => e.type === "approval.revoked" && e.payload.approval_id === aid), by: dec.payload.decided_by ?? "" };
    }
    const risk = riskOf(m.effect_class, w.risk, this.criticalTools.has(name));
    return { id: cid, tool: name, risk, adapter: w.adapter, resource: String(args[w.resource_arg] ?? ""), args, preconditions: w.preconditions ?? [], expected_effects: w.expected_effects ?? [], expected_version: args.if_version ?? null, authorized: true,
      approval_required: decision === "require_approval", approval, human_approved: !!(approval && approval.status === "granted" && !String(approval.by).startsWith("standing:")) };
  }
  private redact(s: string) { return this.redactor ? this.redactor.redact(s) : s; }
  private async fail(run: any, cid: string, tool: string, err: ToolError, trust: string, reqSeq: number | null) {
    let text = `Error [${err.code}]: ${err.message}` + (err.hint ? `\nHint: ${err.hint}` : "") + (err.alternatives.length ? `\nAlternatives: ${err.alternatives.join(", ")}` : "");
    text = this.redact(text);
    const msg = { role: "tool", parts: [{ type: "tool_result", call_id: cid, parts: [{ type: "text", text }], is_error: true }], provenance: { source: tool, trust } };
    await run.emit("tool.call.failed", { tool_call_id: cid, tool, error: err.toDict(), message: msg, trust, search_text: text.slice(0, 2000) }, null, reqSeq, cid);
    return { ok: false, digest: sha256Hex(text), text, error: err.toDict() } as any;
  }
  async execute(run: any, call: any, tctx: ToolContext): Promise<any> {
    const { id: cid, name, arguments: args } = call, agent = tctx.agent, bc = new BudgetController(run);
    this.metrics.calls++;
    const tool = this.registry.get(name), allowed = !!tool && agent.tool_allowlist.some((g: string) => globMatch(g, name)), m = tool?.manifest ?? {};
    const argsPayload = this.svc.blobs.inlineOrRef(args, "args", "args_ref");
    const req = await run.emit("tool.call.requested", { tool_call_id: cid, tool: name, effect_class: m.effect_class ?? null, idempotency: m.idempotency ?? null, ...argsPayload }, null, null, cid);
    if (!allowed) {
      this.metrics.selectionErrors++; await this.observe(run, true, name);
      return this.fail(run, cid, name, new ToolError("tool.unknown", "tool", `unknown or unavailable tool '${name}'`, false, "Use tool.search to discover tools", this.registry.allowed(agent.tool_allowlist).slice(0, 5)), "tool_trusted", req.seq);
    }
    if (!tool!.validate(args)) {
      this.metrics.selectionErrors++; await this.observe(run, true, name);
      const e = tool!.validate.errors[0], where = (e.instancePath || "").replace(/^\//, "") || "(root)";
      return this.fail(run, cid, name, new ToolError("tool.invalid_arguments", "tool", `${where}: ${e.message}`, false, `Arguments must satisfy: ${JSON.stringify(m.input_schema).slice(0, 300)}`), "tool_trusted", req.seq);
    }
    const d = decide(this.ruleset, { tool: name, effect_class: m.effect_class, profile: run.profile, actor_role: "agent", args });
    await run.emit("policy.decided", { tool_call_id: cid, decision: d.decision, matched_rule_ids: d.matched_rule_ids, default_used: d.default_used }, null, req.seq, cid);
    if (d.decision === "deny") { await this.observe(run, true, name); return this.fail(run, cid, name, new ToolError("policy.denied", "security", `policy denied ${name}`, false, `Rules: ${d.matched_rule_ids.join(", ")}`), "tool_trusted", req.seq); }
    let status = "granted";
    if (d.decision === "require_approval") {
      const open = await run.proj("approvals_open");
      const existing = Object.entries<any>(open).find(([, a]) => a.tool_call_id === cid)?.[0];
      const r = existing ? { approval_id: existing, status: "pending" } : await this.approvals.request(run, cid, name, args, d.winning_rule_id, d.approval, m.effect_class, req.seq);
      status = r.status;
      if (status === "pending") {
        if ((await run.state()) === "RUNNING") await run.transition("AWAITING_APPROVAL", `approval ${r.approval_id}`);
        status = await this.approvals.wait(run, r.approval_id);
        if (Object.keys(await run.proj("approvals_open")).length === 0 && (await run.state()) === "AWAITING_APPROVAL") await run.transition("RUNNING", "approval decided");
      }
      if (status !== "granted") return this.fail(run, cid, name, new ToolError(`approval.${status}`, "security", `approval ${status} for ${name}`, false, "Choose a different approach or ask the operator"), "tool_trusted", req.seq);
    }
    const ex = await bc.precheck({ tools: 1 });
    if (ex) return { ok: false, terminate: `budget_exhausted:${ex}`, digest: "", text: "" };
    await this.observe(run, false, name);
    let world: any = null; const action = this.worldAction(run, cid, name, args, m, d.decision);
    if (action) {   // the single gateway for consequential actions: the agent cannot skip it
      const emit = (t: string, p: any) => run.emit(t, p, null, req.seq, cid), pre = await this.gate.pre(emit, action, tctx, this.svc.clock.nowMs() / 1000);
      if (pre.status === "blocked") { const sec = ["authorization_denied", "approval_stale_or_revoked", "human_intervention_required"].includes(pre.reason!); return this.fail(run, cid, name, new ToolError("action.blocked", sec ? "security" : "environment", `${name} did not run: ${pre.reason}`, false, "Its preconditions, the state it assumed, or its approval no longer hold; re-check the resource and try again"), "tool_trusted", req.seq); }
      world = [action, pre.actual, emit];
    }
    return this.runWithRetries(run, cid, name, args, tool!, m, tctx, req.seq, d.decision, d.decision !== "require_approval" || status === "granted", world);
  }
  async runWithRetries(run: any, cid: string, name: string, args: any, tool: any, m: any, tctx: ToolContext, reqSeq: number, decision = "allow", approved = true, world: any = null) {
    if (decision === "deny" || !approved) {   // unreachable by construction; if it is ever reached, nothing executes and it is an immediate alert, whatever the error rate (§9.13)
      const a = unsafeExecutionAlert(name, decision === "deny" ? "deny" : "unapproved"); await run.emit(a.event, a.payload);
      return this.fail(run, cid, name, new ToolError("policy.violation", "security", `${name} was not authorized to run`), "tool_trusted", reqSeq);
    }
    const bc = new BudgetController(run); let attempt = 0, err: ToolError | null = null, result: ToolResult | null = null;
    for (;;) {
      attempt++;
      await run.emit("tool.call.started", { tool_call_id: cid, tool: name, attempt, idempotency_key: m.idempotency === "keyed" ? cid : null }, null, reqSeq, cid);
      await bc.add({ tools: 1 });
      try {
        const raw = await this.limiter.slot(() => (world ? this.gate.guarded(world[0], world[1], () => withTimeout(tool.handler(args, tctx), m.timeout_s), tctx) : withTimeout(tool.handler(args, tctx), m.timeout_s)));   // version-conditional: a change since the check is not missed
        result = raw instanceof ToolResult ? raw : new ToolResult(typeof raw === "string" ? raw : JSON.stringify(raw)); err = null;
      } catch (e: any) {
        if (e instanceof VersionConflict) { await world[2]("action.blocked", { action_id: cid, reason: "stale_state", risk: world[0].risk }); await this.gate.state(world[2], world[0], "mismatch", true); err = new ToolError("action.blocked", "environment", `${name} did not run: stale_state (the resource changed after it was checked)`, false, "Read the resource again and decide whether the action still applies"); }
        else if (e instanceof QueueFull) err = new ToolError("tool.queue_full", "environment", e.message, true, "Too many tool calls are running; retry shortly");
        else if (e instanceof ToolError) err = e;
        else if (e instanceof ExecError) err = new ToolError(e.code, e.category, e.message, e.retryable, e.hint, e.alternatives);
        else if (e?.isTimeout) err = new ToolError("tool.timeout", "environment", `${name} exceeded ${m.timeout_s}s`, true);
        else err = new ToolError("tool.internal", "tool", `${e?.constructor?.name ?? "Error"}: ${e?.message ?? e}`);   // tool bugs are observations, never crashes
      }
      if (err === null) break;
      if (world && err.code === "tool.timeout") {   // a timeout is not a failure: what actually happened is read from state before anything is retried
        const est = await this.gate.establishOutcome(world[2], world[0], world[1], tctx);
        if (est.outcome === "applied") { result = new ToolResult("(the action had already taken effect before it timed out; verified from the resource's state, so it was not repeated)"); err = null; break; }
        if (est.outcome === "unknown") { err = new ToolError("reconcile.unknown", "environment", `${name} timed out and its outcome could not be established from state; it was not retried`, false, "Check the resource yourself before trying again"); break; }
      }
      if (err.retryable && (m.idempotency === "idempotent" || m.idempotency === "keyed") && attempt < MAX_RETRY_ATTEMPTS) continue;   // §9.8
      break;
    }
    const trustDefault = m.location === "control_plane" ? "tool_trusted" : "untrusted";
    if (err === null && world && !(result!.text ?? "").startsWith("(the action had already")) {
      const post = await this.gate.post(world[2], world[0], world[1], result, tctx);   // read the result back; the tool's acknowledgment alone is not trusted
      if (post.status === "reconciliation_required") err = new ToolError("reconcile.effect_mismatch", "environment", `${name} reported success but the state read back does not match the expected effect`, false, "Read the resource and repeat or repair the change");
      else if (post.status === "pending_reconciliation") result = new ToolResult(result!.text + "\n[the harness could not read the result back; it is recorded as unverified and the run cannot complete until it is verified]", result!.trust, result!.extra);
    }
    return err ? this.fail(run, cid, name, err, trustDefault, reqSeq) : this.complete(run, cid, name, result!, trustDefault, reqSeq);
  }
  async complete(run: any, cid: string, name: string, result: ToolResult, trustDefault: string, reqSeq: number | null) {
    const full = this.redact(result.text), digest = sha256Hex(full), capTokens = this.registry.get(name)!.manifest.result_cap_tokens;
    const capBytes = Math.trunc(capTokens * this.divisor), raw = Buffer.from(full, "utf8"); const payload: any = { truncated: false }; let text = full;
    if (raw.length > capBytes) {
      const ref = this.svc.blobs.put(raw);
      text = raw.subarray(0, capBytes).toString("utf8").replace(/\uFFFD+$/, "");
      const tot = Math.ceil(raw.length / this.divisor), shown = Math.ceil(capBytes / this.divisor);
      text += `\n[truncated: ${shown} of ${tot} tokens; fetch more with history.get(tool_call_id="${cid}", offset=${Buffer.byteLength(text)})]`;
      Object.assign(payload, { truncated: true, result_ref: ref });
    }
    const trust = result.trust ?? trustDefault;
    const msg = { role: "tool", parts: [{ type: "tool_result", call_id: cid, parts: [{ type: "text", text }], is_error: false }], provenance: { source: name, trust } };
    await run.emit("tool.call.completed", { tool_call_id: cid, tool: name, message: msg, trust, search_text: text.slice(0, 8000), ...payload, ...result.extra }, null, reqSeq, cid);
    return { ok: true, digest, text };
  }
}
