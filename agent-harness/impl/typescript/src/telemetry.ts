/** Operator telemetry (design §14.4, §15.2) and trajectory metrics (§13.11). Mirrors telemetry.py: pure functions of a run's event log. Collection is declared, not inferred: a category the build
 *  does not collect is `unavailable`, never concluded from the absence of events. */
import { activeAlerts, selectionStats } from "./selection.js";

export const ANALYZER_VERSION = 1;
export const flags = { ALERTS_COLLECTED: true };   // the tool-selection monitor (§9.13) raises `alert.raised` and `alert.resolved`
const CONTENT_KEYS = new Set(["message", "message_ref", "args", "args_ref", "search_text", "result", "summary", "text", "instruction", "content", "note", "detail", "reason_text", "feedback"]);
const ERROR_TYPES = new Set(["tool.call.failed", "model.failed", "definition.mismatch", "integrity.alert", "failure.classified", "failure.observed", "recovery.uncertain", "alert.raised"]);
const SECRET_RE = /(sk-[A-Za-z0-9_\-]{12,}|AKIA[0-9A-Z]{12,}|Bearer\s+[A-Za-z0-9._\-]{12,}|(?:password|secret|token|api[_-]?key)\s*[=:]\s*\S+)/gi;
export const redact = (s: string) => s.replace(SECRET_RE, "[REDACTED]");
/** Nearest-rank percentile, in integer arithmetic so both languages agree exactly. */
export function pct(xs: number[], q: number): number | null { if (!xs.length) return null; const s = [...xs].sort((a, b) => a - b); return s[Math.max(0, Math.floor((Math.round(q * 1000) * s.length + 999) / 1000) - 1)]; }
const hist = (xs: number[]) => (xs.length ? { count: xs.length, min: Math.min(...xs), p50: pct(xs, 0.5), p95: pct(xs, 0.95), max: Math.max(...xs), mean: xs.reduce((a, b) => a + b, 0) / xs.length } : { count: 0, min: null, p50: null, p95: null, max: null, mean: null });
function isError(e: any): boolean {
  const t = e.type, p = e.payload;
  if (t === "stuck_loop.detected") return ["escalate", "terminate"].includes(p.action);
  if (t === "run.terminated") return p.reason !== "completed";
  if (t === "alert.raised") return ["critical", "security"].includes(p.severity);
  return ERROR_TYPES.has(t) && t !== "alert.raised";
}
export function runMetrics(events: any[]) {
  const c: any = { turns: 0, tool_calls: 0, tool_failures: 0, model_calls: 0, model_failures: 0, tool_retries: 0, model_retries: 0, input_tokens: 0, output_tokens: 0, policy_denials: 0,
    approvals: { requested: 0, granted: 0, denied: 0, expired: 0, automatic: 0 }, budget_terminations: {}, stuck_loop: { warnings: 0, escalations: 0, terminations: 0 },
    eval_integrity_events: { total: 0, by_code: {} }, log_integrity_alerts: 0, operator_actions: 0, alerts: 0 };
  const lat: number[] = []; let cost: number | null = null;
  for (const e of events) {
    const t = e.type, p = e.payload;
    if (t === "turn.completed") c.turns++; else if (t === "tool.call.requested") c.tool_calls++; else if (t === "tool.call.failed") c.tool_failures++;
    else if (t === "tool.call.started" && (p.attempt ?? 1) > 1) c.tool_retries++; else if (t === "model.requested") c.model_calls++; else if (t === "model.failed") c.model_failures++;
    else if (t === "model.responded") { const u = p.usage ?? {}; c.input_tokens += u.input_tokens ?? 0; c.output_tokens += u.output_tokens ?? 0; c.model_retries += Math.max(0, (p.attempt ?? 1) - 1); if (p.latency_ms !== undefined && p.latency_ms !== null) lat.push(p.latency_ms); }
    else if (t === "policy.decided" && p.decision === "deny") c.policy_denials++;
    else if (t === "approval.requested") c.approvals.requested++;
    else if (t === "approval.decided") { c.approvals[p.status === "granted" ? "granted" : p.status === "denied" ? "denied" : p.status === "expired" ? "expired" : "granted"]++; if (String(p.decided_by ?? "").startsWith("standing:")) c.approvals.automatic++; }
    else if (t === "run.terminated" && String(p.reason ?? "").startsWith("budget_exhausted:")) { const d = p.reason.split(":")[1]; c.budget_terminations[d] = (c.budget_terminations[d] ?? 0) + 1; }
    else if (t === "stuck_loop.detected") c.stuck_loop[({ guidance: "warnings", escalate: "escalations", terminate: "terminations" } as any)[p.action] ?? "warnings"]++;
    else if (t === "failure.observed" && p.class === "eval_integrity") { c.eval_integrity_events.total++; c.eval_integrity_events.by_code[p.code] = (c.eval_integrity_events.by_code[p.code] ?? 0) + 1; }
    else if (t === "integrity.alert") c.log_integrity_alerts++; else if (t === "operator.action") c.operator_actions++; else if (t === "alert.raised") c.alerts++;
    else if (t === "budget.updated" && p.dimension === "cost" && (p.budget_set ?? "agent") === "agent") cost = p.used;
  }
  c.retries = { tool: c.tool_retries, model: c.model_retries, total: c.tool_retries + c.model_retries }; c.tokens = { input: c.input_tokens, output: c.output_tokens, total: c.input_tokens + c.output_tokens };
  c.cost = cost; c.selection_error_rate = selectionStats(events).error_rate;   // observed: unknown tool, invalid arguments, policy denial; null when nothing was selected
  return { counters: c, histograms: { model_latency_ms: hist(lat) } };
}
/** The deterministic trajectory metrics of a run (design §13.11). A model-based assessment is optional and labelled. */
export function trajectoryMetrics(events: any[]) {
  const m = runMetrics(events).counters, used: Record<string, any> = {};
  for (const e of events) if (e.type === "budget.updated" && (e.payload.budget_set ?? "agent") === "agent") used[e.payload.dimension] = { used: e.payload.used, limit: e.payload.limit };
  const safety: any = { policy_denials: m.policy_denials, quarantined: events.filter((e) => e.type === "memory.quarantined").length, integrity_alerts: m.log_integrity_alerts, definition_mismatches: events.filter((e) => e.type === "definition.mismatch").length };
  safety.total = Object.values<number>(safety).reduce((a, b) => a + b, 0);
  return { analyzer_version: ANALYZER_VERSION, steps: m.turns, tool_calls: m.tool_calls, tool_errors: m.tool_failures, retries: m.retries.total, budget_use: Object.fromEntries(Object.entries(used).sort(([a], [b]) => (a < b ? -1 : 1))),
    policy_denials: m.policy_denials, approvals: m.approvals.requested, safety_events: safety, stuck_loops: m.stuck_loop.warnings + m.stuck_loop.escalations + m.stuck_loop.terminations };
}
export function usage(events: any[]) {
  const by: Record<string, any> = {}; let ref: any = null, cost: any = null;
  for (const e of events) {
    const t = e.type, p = e.payload;
    if (t === "model.requested") ref = p.model_ref;
    else if (t === "model.responded") { const u = p.usage ?? {}, k = String(ref), b = (by[k] ??= { calls: 0, input_tokens: 0, output_tokens: 0 }); b.calls++; b.input_tokens += u.input_tokens ?? 0; b.output_tokens += u.output_tokens ?? 0; }
    else if (t === "run.finished") { const est = p.fields?.usage?.value ?? {}; if (est.estimated_cost !== undefined && est.estimated_cost !== null) cost = { estimated_cost: est.estimated_cost, currency: est.currency }; }
  }
  const i = Object.values<any>(by).reduce((a, b) => a + b.input_tokens, 0), o = Object.values<any>(by).reduce((a, b) => a + b.output_tokens, 0);
  return { tokens: { input_tokens: i, output_tokens: o, total: i + o }, by_model: Object.fromEntries(Object.entries(by).sort(([a], [b]) => (a < b ? -1 : 1))), cost };
}
/** `inspect --telemetry`: each category is `available` or `unavailable`, following what this build collects and what the run's own manifest says, never the mere absence of events. */
export function categories(events: any[], runId: string) {
  const created = events.find((e) => e.type === "run.created"), man = created?.payload?.manifest, fin = [...events].reverse().find((e) => e.type === "run.finished"), m = runMetrics(events).counters;
  const sc = events.filter((e) => e.type === "run.state_changed"), term = [...events].reverse().find((e) => e.type === "run.terminated")?.payload;
  const av = (value: any) => ({ status: "available", value }), un = (reason: string) => ({ status: "unavailable", reason }), costEst = fin?.payload?.fields?.usage?.value ?? {}, errs: Record<string, number> = {};
  for (const e of events) if (e.type === "tool.call.failed") { const k = e.payload.error?.code ?? "unknown"; errs[k] = (errs[k] ?? 0) + 1; }
  const models = man?.fields?.models ?? {}, costKnown = (costEst.estimated_cost !== undefined && costEst.estimated_cost !== null) || m.cost !== null;
  return { run_id: runId, categories: {
    status: av({ state: sc.length ? sc[sc.length - 1].payload.to : null, terminal_reason: term?.reason ?? null }),
    model_and_version: models.status === "recorded" ? av(models.value) : un("this run has no provenance manifest"),
    tool_calls: av({ requested: m.tool_calls, failed: m.tool_failures, retries: m.retries.tool }),
    errors_and_termination: av({ tool_errors_by_code: Object.fromEntries(Object.entries(errs).sort(([a], [b]) => (a < b ? -1 : 1))), model_failures: m.model_failures, termination: term ?? null }),
    usage_and_cost: costKnown ? av({ tokens: m.tokens, cost: costEst.estimated_cost ?? null }) : { ...av({ tokens: m.tokens, cost: null }), note: "cost is unavailable: no price or cost budget was configured" },
    interventions: av({ approvals: m.approvals, operator_actions: m.operator_actions }),
    alerts: flags.ALERTS_COLLECTED ? av({ raised: m.alerts, active: activeAlerts(events).map((a) => ({ rule: a.rule, severity: a.severity, seq: a.seq })) }) : un("alerting is not implemented in this build") } };
}
const view = (v: any, role: string): any => Array.isArray(v) ? v.map((x) => view(x, role)) : v && typeof v === "object" ? Object.fromEntries(Object.entries(v).map(([k, x]) => [k, CONTENT_KEYS.has(k) && role === "operator" ? "[gated: reviewer or admin role required]" : view(x, role)])) : typeof v === "string" ? redact(v) : v;
/** Events for the operator. An `operator` sees metadata and the codes and counts that explain a run, with message and tool content gated; a `reviewer` or `admin` sees content. Everything is redacted, and every row carries its run id. */
export function eventRows(events: any[], runId: string, role = "operator", typeFilter: string | null = null, errorsOnly = false) {
  const rows: any[] = [];
  for (const e of events) {
    if (typeFilter && !(e.type === typeFilter || e.type.startsWith(typeFilter.replace(/\*+$/, "")))) continue;
    if (errorsOnly && !isError(e)) continue;
    rows.push({ run_id: runId, seq: e.seq, ts: e.ts, type: e.type, actor: e.actor.kind, payload: view(e.payload, role) });
  }
  return rows;
}
