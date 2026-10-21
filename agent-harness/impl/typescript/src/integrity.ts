/** Evaluation-integrity closure (design §15.6). Findings are tracked per detector, and the status of an integrity class is derived from the detectors it requires. The state is a fold over `integrity.detector_status` events, so the
 *  history is the log. NOT_RUN, WAIVED and NOT_APPLICABLE are never equivalent to PASS. Mirrors integrity.py. */
import fs from "node:fs";
import path from "node:path";

export const SPEC_PATH = path.resolve(import.meta.dirname, "../../../spec/integrity/detectors.json");
export const STATUSES = ["NOT_RUN", "OPEN", "FIXED_PENDING_VERIFICATION", "PASS", "FAIL", "WAIVED", "NOT_APPLICABLE"];
export const TRANSITIONS: Record<string, Set<string>> = { NOT_RUN: new Set(["PASS", "FAIL", "WAIVED", "NOT_APPLICABLE"]), OPEN: new Set(["FIXED_PENDING_VERIFICATION", "WAIVED", "NOT_APPLICABLE"]), FAIL: new Set(["OPEN", "FIXED_PENDING_VERIFICATION", "WAIVED"]),
  FIXED_PENDING_VERIFICATION: new Set(["PASS", "FAIL"]), PASS: new Set(["FAIL", "WAIVED"]), WAIVED: new Set(["OPEN", "PASS", "FAIL"]), NOT_APPLICABLE: new Set(["NOT_RUN", "OPEN"]) };
export class IntegrityError extends Error {}
export const loadSpec = (p: string = SPEC_PATH): any => JSON.parse(fs.readFileSync(p, "utf8"));
const t = (s: string) => Date.parse(s);
/** Every required detector starts NOT_RUN: nothing is assumed to pass. */
export function initial(spec: any) {
  const detectors: Record<string, any> = {};
  for (const [cls, c] of Object.entries<any>(spec.classes)) for (const d of c.required) detectors[d.id] = { class: cls, id: d.id, version: d.version, severity: d.severity, status: "NOT_RUN", evidence: null, owner: null, fixed_by: null, waiver: null, note: null, history: [] as any[] };
  return { detectors, rejected: [] as any[] };
}
/** One status change. Refused if the transition is not allowed or lacks what it needs: a PASS needs evidence for the detector's current version and an evaluation run, and after a fix a verifier who is not the person who made it;
 *  a waiver needs a rationale, an approver who is not the owner, and a future expiry; not-applicable needs a reason. */
export function apply(state: any, p: any, at: string) {
  const s = structuredClone(state), d = s.detectors[p.detector_id]; if (!d) throw new IntegrityError(`unknown detector '${p.detector_id}'`);
  const to = p.status, by = p.by, from = d.status;
  if (!STATUSES.includes(to)) throw new IntegrityError(`unknown status '${to}'`); if (!by) throw new IntegrityError("a status change needs the person or system that made it");
  if (!TRANSITIONS[from].has(to)) throw new IntegrityError(`${d.id}: ${from} cannot become ${to}`);
  if (p.owner) d.owner = p.owner;
  if (to !== "WAIVED") d.waiver = null;   // a waiver ends when the detector leaves WAIVED
  if (to === "PASS") {
    if (!p.evidence_ref || !p.run_id) throw new IntegrityError(`${d.id}: a PASS needs an evidence reference and the evaluation run it came from`);
    if (p.evidence_version !== d.version) throw new IntegrityError(`${d.id}: the evidence is for version ${p.evidence_version === undefined ? "undefined" : `'${p.evidence_version}'`} and the detector is version '${d.version}'`);
    if (from === "FIXED_PENDING_VERIFICATION" && by === d.fixed_by) throw new IntegrityError(`${d.id}: a fix must be verified by someone other than the person who made it`);
    d.evidence = { ref: p.evidence_ref, run_id: p.run_id, version: p.evidence_version, at };
  } else if (to === "FIXED_PENDING_VERIFICATION") { if (!p.note) throw new IntegrityError(`${d.id}: a fix needs a description of the remediation`); d.fixed_by = by; }
  else if (to === "WAIVED") {
    const w = p.waiver ?? {}; if (!(w.rationale && w.approved_by && w.expires_at)) throw new IntegrityError(`${d.id}: a waiver needs a rationale, an approver, and an expiry`);
    if (w.approved_by === (d.owner || by)) throw new IntegrityError(`${d.id}: a waiver must be approved by someone other than the owner`);
    if (t(w.expires_at) <= t(at)) throw new IntegrityError(`${d.id}: a waiver must expire in the future`);
    d.waiver = { approved_by: w.approved_by, rationale: w.rationale, expires_at: w.expires_at, granted_at: at };
  } else if (to === "NOT_APPLICABLE") { if (!p.note) throw new IntegrityError(`${d.id}: not-applicable needs the reason`); }
  d.note = p.note ?? d.note; d.status = to;
  d.history.push({ at, from, to, by, evidence_ref: p.evidence_ref ?? null, run_id: p.run_id ?? null, note: p.note ?? null, finding_id: p.finding_id ?? null });
  return s;
}
/** The state after a run of `integrity.detector_status` events. An event that is not allowed is recorded in `rejected` and does not change anything (or throws, if strict), so one bad event cannot hide the rest. */
export function fold(events: any[], spec?: any, strict = false) {
  spec = spec ?? loadSpec(); let s = initial(spec), i = 0;
  for (const e of events.filter((x) => x.type === "integrity.detector_status")) {
    try { s = apply(s, e.payload, e.payload.at ?? e.ts); } catch (err) { if (!(err instanceof IntegrityError) || strict) throw err; s.rejected.push({ index: i, detector_id: e.payload.detector_id ?? null, reason: err.message }); }
    i++;
  }
  return s;
}
/** pass, waived, not_applicable, or open. Only a PASS whose evidence is for the detector's current version (and recent enough) is a pass. */
export function effective(d: any, now: string, maxAgeS: number | null = null): string {
  if (d.status === "PASS") { const ev = d.evidence; return ev && ev.version === d.version && (maxAgeS === null || (t(now) - t(ev.at)) / 1000 <= maxAgeS) ? "pass" : "open"; }
  if (d.status === "WAIVED") return d.waiver && t(d.waiver.expires_at) > t(now) ? "waived" : "open";   // an expired waiver reopens the detector
  if (d.status === "NOT_APPLICABLE") return d.note ? "not_applicable" : "open";
  return "open";
}
/** CLOSED only when every required detector passes with current evidence. CLOSED_WITH_WAIVERS when the rest pass and an unexpired waiver covers the others. OPEN otherwise, including when nothing has passed. */
export function classStatus(state: any, cls: string, now: string, maxAgeS: number | null = null): string {
  const eff = Object.values<any>(state.detectors).filter((d) => d.class === cls).map((d) => effective(d, now, maxAgeS));
  if (!eff.length || eff.includes("open") || eff.every((e) => e === "not_applicable")) return "OPEN";
  return eff.includes("waived") ? "CLOSED_WITH_WAIVERS" : "CLOSED";
}
const cmp = (a: string, b: string) => (a < b ? -1 : a > b ? 1 : 0);
export function report(state: any, now: string, maxAgeS: number | null = null) {
  const classes: Record<string, any> = {};
  for (const cls of [...new Set(Object.values<any>(state.detectors).map((d) => d.class))].sort()) {
    const ds = Object.values<any>(state.detectors).filter((d) => d.class === cls), eff = (d: any) => effective(d, now, maxAgeS);
    classes[cls] = { status: classStatus(state, cls, now, maxAgeS), detectors: [...ds].sort((a, b) => cmp(a.id, b.id)).map((d) => ({ id: d.id, version: d.version, severity: d.severity, status: d.status, effective: eff(d) })),
      blocking: ds.filter((d) => eff(d) === "open").map((d) => `${d.id} is ${d.status}` + (d.status === "PASS" ? " but its evidence is not current" : "")).sort(),
      waivers: ds.filter((d) => d.status === "WAIVED" && d.waiver).map((d) => ({ detector: d.id, ...d.waiver })).sort((a, b) => cmp(a.detector, b.detector)) };
  }
  return { classes, rejected: state.rejected };
}
