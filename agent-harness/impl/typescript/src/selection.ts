/** Tool-selection monitoring and alerting (design §9.13). Mirrors selection.py: observed selection errors online, a labelled benchmark offline, and alert rules whose sustained breach is one alert. */
export const RULE = "selection.error_rate", UNSAFE_RULE = "selection.unsafe_execution";
export const DEFAULTS = { window: 200, min_sample: 30, warn: 0.05, critical: 0.10 };   // [proposed P80]
export const SELECTION_ERRORS = ["tool.unknown", "tool.invalid_arguments"];
/** Validate and complete `alerts.selection`. An invalid rule is refused at startup, not ignored. */
export function rules(cfg: any) {
  const r: any = { ...DEFAULTS, ...(cfg?.selection ?? {}) };
  if (!(Number.isInteger(r.window) && r.window >= 1)) throw new Error("alerts.selection.window must be a positive integer");
  if (!(Number.isInteger(r.min_sample) && r.min_sample >= 1 && r.min_sample <= r.window)) throw new Error("alerts.selection.min_sample must be between 1 and the window size");
  for (const k of ["warn", "critical"]) if (typeof r[k] !== "number" || !(r[k] > 0 && r[k] <= 1)) throw new Error(`alerts.selection.${k} must be a number above 0 and at most 1`);
  if (r.critical < r.warn) throw new Error("alerts.selection.critical must not be below warn");
  return r as { window: number; min_sample: number; warn: number; critical: number };
}
/** A rolling window of selection outcomes for one run. `observe` returns the alert transition, if any: a raise, an escalation, or a resolve. A breach that continues, or eases, is silent. */
export class SelectionMonitor {
  r: ReturnType<typeof rules>; window: number[] = []; active: string | null = null;
  constructor(cfg: any = null) { this.r = rules({ selection: cfg ?? {} }); }
  /** Rebuild the window after a restart from what the log says happened. A call still in flight has no outcome yet and is left out. */
  static fromEvents(events: any[], cfg: any = null): SelectionMonitor {
    const m = new SelectionMonitor(cfg), out = new Map<string, boolean>(), order: string[] = [];
    for (const e of events) {
      const t = e.type, p = e.payload;
      if (t === "tool.call.requested") order.push(p.tool_call_id);
      else if (t === "tool.call.failed" && SELECTION_ERRORS.includes(p.error?.code)) out.set(p.tool_call_id, true);
      else if (t === "policy.decided" && p.decision === "deny") out.set(p.tool_call_id, true);
      else if (t === "tool.call.started" && !out.has(p.tool_call_id)) out.set(p.tool_call_id, false);
    }
    for (const cid of order) if (out.has(cid)) { m.window.push(out.get(cid) ? 1 : 0); if (m.window.length > m.r.window) m.window.shift(); }
    for (const a of activeAlerts(events)) if (a.rule === RULE) m.active = a.severity;
    return m;
  }
  rate(): number | null { return this.window.length ? this.window.reduce((a, b) => a + b, 0) / this.window.length : null; }
  observe(error: boolean, context: any = null): { event: string; payload: any } | null {
    this.window.push(error ? 1 : 0); if (this.window.length > this.r.window) this.window.shift();
    if (this.window.length < this.r.min_sample) return null;   // too few decisions to say anything: one failed selection is not an alert
    const rate = this.window.reduce((a, b) => a + b, 0) / this.window.length, sev = rate >= this.r.critical ? "critical" : rate >= this.r.warn ? "warning" : null;
    if (sev === this.active) return null;
    const prev = this.active; this.active = sev;
    if (sev !== null && prev !== null && sev === "warning" && prev === "critical") return null;   // a breach that eases but continues is still the same alert
    if (sev === null) return { event: "alert.resolved", payload: { rule: RULE, value: rate, window: this.window.length } };
    return { event: "alert.raised", payload: { rule: RULE, severity: sev, window: this.window.length, value: rate, threshold: sev === "critical" ? this.r.critical : this.r.warn, previous_severity: prev, context: context ?? {} } };
  }
}
/** A prohibited or unauthorized selection that reached execution is an immediate alert, whatever the error rate. The payload names the tool and the decision, never the arguments. */
export const unsafeExecutionAlert = (tool: string, decision: string, context: any = {}) => ({ event: "alert.raised", payload: { rule: UNSAFE_RULE, severity: "security", window: 0, value: 1, threshold: 0, context: { tool, decision, ...context } } });
export function selectionStats(events: any[]) {
  let decisions = 0, errors = 0, denied = 0; const byTool: Record<string, number> = {}, req = new Map<string, string>();
  for (const e of events) {
    const t = e.type, p = e.payload;
    if (t === "tool.call.requested") { decisions++; req.set(p.tool_call_id, p.tool); }
    else if (t === "tool.call.failed" && SELECTION_ERRORS.includes(p.error?.code)) { errors++; const k = req.get(p.tool_call_id) ?? p.tool ?? "?"; byTool[k] = (byTool[k] ?? 0) + 1; }
    else if (t === "policy.decided" && p.decision === "deny") { denied++; errors++; const k = req.get(p.tool_call_id) ?? "?"; byTool[k] = (byTool[k] ?? 0) + 1; }
  }
  return { decisions, errors, policy_denials: denied, error_rate: decisions ? errors / decisions : null, errors_by_tool: Object.fromEntries(Object.entries(byTool).sort(([a], [b]) => (a < b ? -1 : a > b ? 1 : 0))) };
}
/** Alerts raised and not yet resolved, from the log. */
export function activeAlerts(events: any[]): any[] {
  let live = new Map<string, any>();
  for (const e of events) {
    if (e.type === "alert.raised") live.set(`${e.payload.rule}\u0000${String(e.payload.context?.tool ?? "")}`, { run_id: e.run_id, seq: e.seq, ...e.payload });
    else if (e.type === "alert.resolved") live = new Map([...live].filter(([k]) => k.split("\u0000")[0] !== e.payload.rule));
  }
  return [...live.values()];
}
/** Offline selection quality against a labelled benchmark: rates over evaluated selection decisions, overall and per model, selector version, tool and task. */
export function evaluateSelection(benchmark: any, decisions: any[]) {
  const tasks = new Map<string, any>(benchmark.tasks.map((t: any) => [t.id, t])), n = { decisions: 0, wrong_tool: 0, invalid_call: 0, unnecessary: 0, unsafe: 0, incorrect: 0 };
  const by: Record<string, Record<string, any>> = { model: {}, selector_version: {}, tool: {}, task: {} }, perTask = new Map<string, boolean[]>();
  for (const d of decisions) {
    const t = tasks.get(d.task); if (!t) continue;
    const wrong = !t.acceptable.includes(d.tool), inv = !(d.valid_args ?? true), unnec = !wrong && d.contributed === false, uns = Boolean(d.unsafe);
    n.decisions++; n.wrong_tool += +wrong; n.invalid_call += +inv; n.unnecessary += +unnec; n.unsafe += +uns; n.incorrect += +(wrong || inv || uns);
    (perTask.get(d.task) ?? perTask.set(d.task, []).get(d.task)!).push(wrong);
    for (const dim of ["model", "selector_version", "tool", "task"]) { const key = dim === "task" ? d.task : d[dim] ?? d.tool, b = (by[dim][key] ??= { decisions: 0, wrong_tool: 0, invalid_call: 0 }); b.decisions++; b.wrong_tool += +wrong; b.invalid_call += +inv; }
  }
  let miss = 0, recovered = 0, wrongTasks = 0;
  for (const [tid, seq] of perTask) {
    const t = tasks.get(tid), chosenOk = seq.some((w) => !w);
    if (!chosenOk && t.acceptable.some((a: string) => (t.available ?? []).includes(a))) miss++;   // a suitable tool was available and never selected
    if (seq.some(Boolean)) { wrongTasks++; const first = seq.indexOf(true); if (seq.slice(first + 1).some((w) => !w)) recovered++; }   // a wrong selection followed by an acceptable one
  }
  const rate = (x: number) => (n.decisions ? x / n.decisions : null), out: any = { decisions: n.decisions, wrong_tool_rate: rate(n.wrong_tool), invalid_call_rate: rate(n.invalid_call), unnecessary_call_rate: rate(n.unnecessary), unsafe_selection_rate: rate(n.unsafe),
    miss_rate: perTask.size ? miss / perTask.size : null, recovery_rate: wrongTasks ? recovered / wrongTasks : null, error_rate: rate(n.incorrect) };
  for (const [k, v] of Object.entries(by)) out[`by_${k}`] = Object.fromEntries(Object.entries(v).sort(([a], [b]) => (a < b ? -1 : a > b ? 1 : 0)).map(([kk, vv]: [string, any]) => [kk, { ...vv, wrong_tool_rate: vv.wrong_tool / vv.decisions }]));
  return out;
}
