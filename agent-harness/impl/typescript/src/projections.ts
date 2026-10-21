/** Projections (design §5.6): pure, versioned folds. Must match the Python implementation exactly (shared vectors). */
import { applyEvent } from "./reconcile.js";
import { parseTs, deepClone } from "./util.js";
export const TRUST_RANK: Record<string, number> = { untrusted: 0, tool_trusted: 1, user: 2, developer: 3, system: 4 };
export const ACTIVE_STATES = new Set(["RUNNING", "AWAITING_APPROVAL", "SCORING", "COMPLETING"]);
export const TERMINAL_STATES = new Set(["COMPLETED", "TERMINATED", "FAILED_INTEGRITY"]);
export const VERSIONS: Record<string, number> = { run_state: 1, budgets: 2, reconcile: 1, conversation: 1, approvals_open: 1, tool_calls: 1, submissions: 1, grants_active: 1, sandboxes: 1, ephemeral: 1, working_state: 1, loop: 1 };
export const NAMES = Object.keys(VERSIONS);
export const minTrust = (ts: string[]) => (ts.length ? ts.reduce((a, b) => ((TRUST_RANK[b] ?? 0) < (TRUST_RANK[a] ?? 0) ? b : a)) : "user");

export function initial(name: string): any {
  return ({
    run_state: { state: "CREATED", terminal_reason: null, owner: null, profile: null, active_ms: 0, active_since_ms: null, excluded_ms: 0, entered_completing_seq: null, verified_seq: null, verification_failures: 0 },
    budgets: { agent: {}, evaluator: {}, reservations: {}, allocations: {}, unreconciled: {} },
    reconcile: { subjects: {} },
    conversation: { entries: [] }, approvals_open: {}, tool_calls: {}, submissions: {}, grants_active: {}, sandboxes: {},
    ephemeral: { guidance: [] }, working_state: {},
    loop: { turn: 0, idle_streak: 0, last_signature: null, repeat_count: 0, stuck_warned: false, exposed_tools: [], completion_pending: null },
  } as any)[name];
}

function runState(s: any, ev: any) {
  const { type: t, payload: p } = ev, ts = parseTs(ev.ts);
  if (t === "run.created") { s.owner = p.owner; s.profile = p.profile; }
  else if (t === "run.state_changed") {
    const was = ACTIVE_STATES.has(s.state), now = ACTIVE_STATES.has(p.to);
    if (was && !now && s.active_since_ms !== null) { s.active_ms += ts - s.active_since_ms; s.active_since_ms = null; }
    else if (!was && now) s.active_since_ms = ts;
    s.state = p.to;
    if (p.to === "COMPLETING") s.entered_completing_seq = ev.seq;
    if (TERMINAL_STATES.has(p.to) && p.reason) s.terminal_reason = s.terminal_reason || p.reason;
  } else if (t === "run.terminated") s.terminal_reason = p.reason;
  else if (t === "run.recovered") s.excluded_ms += p.excluded_ms ?? 0;
  else if (t === "verification.completed") { if (p.passed) s.verified_seq = ev.seq; else s.verification_failures += 1; }
  return s;
}
export function activeTimeMs(rs: any, nowMs: number): number {
  const total = rs.active_ms + (rs.active_since_ms !== null ? nowMs - rs.active_since_ms : 0);
  return Math.max(0, total - rs.excluded_ms);
}
function budgets(s: any, ev: any) {
  const { type: t, payload: p } = ev;
  if (t === "run.created") {
    for (const [set, key] of [["agent", "budgets"], ["evaluator", "evaluator_budgets"]] as const)
      for (const [dim, cfg] of Object.entries<any>(p[key] ?? {})) { const lim = cfg.limit; s[set][dim] = { limit: lim, used: 0, reserved: 0, unreconciled: 0, warn_at: cfg.warn_at ?? lim * 0.8, warned: false, exhausted: false }; }
  } else if (t === "budget.updated" || t === "budget.warning" || t === "budget.exhausted") {
    const d = s[p.budget_set ?? "agent"][p.dimension];
    if (!d) return s;
    if (t === "budget.updated") d.used = p.used; else if (t === "budget.warning") d.warned = true; else d.exhausted = true;
  } else if (t === "budget.reserved") {
    const set = p.budget_set ?? "agent", agent = p.agent_id ?? null;
    s.reservations[p.reservation_id] = { set, deltas: { ...p.deltas }, agent_id: agent };
    for (const [dim, amt] of Object.entries<number>(p.deltas)) {
      if (dim in s[set]) s[set][dim].reserved += amt;
      if (agent && s.allocations[agent] && dim in s.allocations[agent].dims) s.allocations[agent].dims[dim].reserved += amt;
    }
  } else if (t === "budget.committed" || t === "budget.released") {
    const r = s.reservations[p.reservation_id]; delete s.reservations[p.reservation_id];
    if (r) {
      const { set, agent_id: agent } = r;
      for (const [dim, amt] of Object.entries<number>(r.deltas)) {
        if (dim in s[set]) s[set][dim].reserved -= amt;
        if (agent && s.allocations[agent] && dim in s.allocations[agent].dims) s.allocations[agent].dims[dim].reserved -= amt;
      }
      if (t === "budget.committed") {
        for (const [dim, amt] of Object.entries<number>(p.actual ?? {})) if (agent && s.allocations[agent] && dim in s.allocations[agent].dims) s.allocations[agent].dims[dim].used += amt;
        if (p.unreconciled) {
          const est: Record<string, number> = {}; for (const [dim, amt] of Object.entries<number>(p.actual ?? {})) if (dim in s[set]) est[dim] = amt;
          s.unreconciled[p.reservation_id] = { set, deltas: est };
          for (const [dim, amt] of Object.entries(est)) s[set][dim].unreconciled += amt;
        }
      }
    }
    if (t === "budget.committed" && p.reconciles) {
      const u = s.unreconciled[p.reservation_id]; delete s.unreconciled[p.reservation_id];
      if (u) for (const [dim, amt] of Object.entries<number>(u.deltas)) s[u.set][dim].unreconciled -= amt;
    }
  } else if (t === "budget.allocated") {
    s.allocations[p.agent_id] = { set: p.budget_set ?? "agent", ended: false, dims: Object.fromEntries(Object.entries<number>(p.limits).map(([d, lim]) => [d, { limit: lim, used: 0, reserved: 0 }])) };
  } else if (t === "budget.allocation_ended") { if (s.allocations[p.agent_id]) s.allocations[p.agent_id].ended = true; }
  return s;
}
/** Reconciliation state per subject (design §5.13), from `reconcile.state_changed`. `unknown` never becomes `consistent` without authoritative evidence. */
function reconcileFold(s: any, ev: any) {
  if (ev.type === "reconcile.state_changed") { const p = ev.payload, prev = s.subjects[p.subject]?.state ?? null; s.subjects[p.subject] = { state: applyEvent(prev, p.event, Boolean(p.authoritative)), action_id: p.action_id ?? null, seq: ev.seq }; }
  return s;
}
function msgEntry(ev: any, defaultTrust: string) {
  const p = ev.payload, msg = p.message ?? null;
  const callIds = p.call_ids ?? (msg?.parts ?? []).filter((x: any) => x.type === "tool_call").map((x: any) => x.id);
  return { seq: ev.seq, type: ev.type, trust: p.trust ?? defaultTrust, message: msg, message_ref: p.message_ref ?? null, cleared: false, call_ids: callIds,
    call_id: p.tool_call_id ?? null, tool: p.tool ?? null, id_map: p.id_map ?? {} };
}
function conversation(s: any, ev: any) {
  const { type: t, payload: p } = ev, es = s.entries;
  if (t === "user.message") es.push(msgEntry(ev, "user"));
  else if (t === "model.responded") es.push(msgEntry(ev, "tool_trusted"));
  else if (t === "tool.call.completed" || t === "tool.call.failed") { if ("message" in p || "message_ref" in p) es.push(msgEntry(ev, "untrusted")); }
  else if (t === "context.hydrated") { if ("message" in p) { const e: any = msgEntry(ev, "untrusted"); e.type = "recalled"; e.hit_seqs = p.hit_seqs ?? []; es.push(e); } }
  else if (t === "context.compacted") {
    const fr = p.from_seq, to = p.to_seq, removed = es.filter((e: any) => e.seq >= fr && e.seq <= to);
    if (removed.length) {
      const idx = es.indexOf(removed[0]);
      const summ: any = { seq: ev.seq, type: "summary", trust: minTrust(removed.map((e: any) => e.trust)), summary: p.summary ?? null, summary_ref: p.summary_ref ?? null, from_seq: fr, to_seq: to,
        cleared: false, call_ids: [], call_id: null, tool: null, message: null, message_ref: null, id_map: {} };
      for (const e of removed) Object.assign(summ.id_map, e.id_map ?? {});
      s.entries = es.filter((e: any) => !(e.seq >= fr && e.seq <= to));
      s.entries.splice(idx, 0, summ);
    }
    const cl = new Set(p.cleared_seqs ?? []);
    for (const e of s.entries) if (cl.has(e.seq)) e.cleared = true;
  }
  return s;
}
function approvals(s: any, ev: any) {
  const { type: t, payload: p } = ev;
  if (t === "approval.requested") s[p.approval_id] = { tool_call_id: p.tool_call_id ?? null, scope: p.scope ?? null, tool: p.tool ?? null, rule_id: p.rule_id ?? null, seq: ev.seq, expires_at: p.expires_at ?? null };
  else if (t === "approval.decided") delete s[p.approval_id];
  return s;
}
function toolCalls(s: any, ev: any) {
  const { type: t, payload: p } = ev, cid = p.tool_call_id;
  if (t === "tool.call.requested") s[cid] = { tool: p.tool, status: "requested", seq: ev.seq, effect_class: p.effect_class ?? null, idempotency: p.idempotency ?? null, attempt: 0, args: p.args ?? null, args_ref: p.args_ref ?? null };
  else if (t === "tool.call.started" && s[cid]) { s[cid].status = "started"; s[cid].attempt = p.attempt ?? 1; s[cid].started_seq = ev.seq; }
  else if (t === "tool.call.completed" && s[cid]) s[cid].status = "completed";
  else if (t === "tool.call.failed" && s[cid]) s[cid].status = "failed";
  return s;
}
function submissions(s: any, ev: any) {
  const { type: t, payload: p } = ev, sid = p.submission_id;
  if (t === "submission.created") s[sid] = { seq: Object.keys(s).length + 1, created_seq: ev.seq, snapshot_id: p.snapshot_id ?? null, status: "created", valid: null, primary: null, report_ref: null };
  else if (t === "snapshot.created" && s[sid]) { s[sid].snapshot_id = p.snapshot_id; s[sid].status = "snapshotted"; }
  else if (t === "judge.started" && s[sid]) s[sid].status = "judging";
  else if (t === "judge.completed" && s[sid]) Object.assign(s[sid], { status: "scored", valid: p.valid ?? null, primary: p.primary ?? null, report_ref: p.score_report_ref ?? null });
  else if (t === "feedback.delivered" && s[sid]) s[sid].status = "feedback";
  return s;
}
function grants(s: any, ev: any) {
  const { type: t, payload: p } = ev;
  if (t === "grant.issued") s[p.grant_id] = { kind: p.kind, spec: p.spec ?? null, issued_seq: ev.seq };
  else if (t === "grant.revoked") delete s[p.grant_id];
  return s;
}
function sandboxes(s: any, ev: any) {
  const { type: t, payload: p } = ev;
  if (t === "sandbox.created") s[p.sandbox_id] = { role: p.role ?? null, backend: p.backend ?? null, tier: p.tier ?? null, seq: ev.seq };
  else if (t === "sandbox.destroyed") delete s[p.sandbox_id];
  return s;
}
function ephemeral(s: any, ev: any) {
  if (ev.type === "guidance.injected") s.guidance.push({ kind: ev.payload.kind ?? null, text: ev.payload.text ?? null, seq: ev.seq });
  else if (ev.type === "model.requested") s.guidance = [];
  return s;
}
function workingState(s: any, ev: any) { if (ev.type === "state.written") s[ev.payload.name] = { hash: ev.payload.content_hash, seq: ev.seq }; return s; }
function loop(s: any, ev: any) {
  const { type: t, payload: p } = ev;
  if (t === "turn.completed") {
    s.turn = p.turn;
    const sig = p.signature ?? null;
    if (p.idle) s.idle_streak += 1;
    else { s.idle_streak = 0; if (sig !== null) { if (sig === s.last_signature) s.repeat_count += 1; else { s.last_signature = sig; s.repeat_count = 1; s.stuck_warned = false; } } }
  } else if (t === "stuck_loop.detected" && p.action === "guidance") s.stuck_warned = true;
  else if (t === "completion.proposed") s.completion_pending = p.summary;
  else if (t === "verification.completed") s.completion_pending = null;
  else if (t === "tools.exposed") for (const n of p.names) if (!s.exposed_tools.includes(n)) s.exposed_tools.push(n);
  return s;
}
const FOLDS: Record<string, (s: any, ev: any) => any> = { run_state: runState, budgets, reconcile: reconcileFold, conversation, approvals_open: approvals, tool_calls: toolCalls, submissions, grants_active: grants, sandboxes, ephemeral, working_state: workingState, loop };
export const fold = (name: string, state: any, ev: any) => FOLDS[name](state, ev);
export function foldAll(name: string, events: any[], state?: any) {
  let s = state === undefined ? deepClone(initial(name)) : state;
  for (const e of events) s = fold(name, s, e);
  return s;
}
