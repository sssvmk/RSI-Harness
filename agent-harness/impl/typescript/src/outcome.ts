/** Outcome contract and verdicts (design §13.11, PRD §3.6). Mirrors eval/outcome.py: a verdict is derived from evidence against a contract fixed before execution, never from the agent's claim.
 *  Precedence over the required criteria: failed and passed -> PARTIAL; failed, none passed -> FAILURE; none failed but some unverified -> INCONCLUSIVE; all passed -> SUCCESS. */
import crypto from "node:crypto";
import { canonicalize, sha256Hex } from "./util.js";
import { loadSchema, newAjv } from "./registry.js";

export const VERDICTS = ["SUCCESS", "FAILURE", "PARTIAL", "INCONCLUSIVE"];
export class ContractError extends Error {}
const PASS = "pass", FAIL = "fail";

// exact decimals: a value is a scaled big integer, so 0.1 + 0.2 style errors cannot arise (JavaScript has no Decimal)
interface Dec { m: bigint; e: number }   // m * 10^-e
export function decimal(x: any): Dec | null {
  if (x === null || x === undefined || typeof x === "boolean") return null;
  const m = /^([+-]?)(\d+\.?\d*|\.\d+)(?:[eE]([+-]?\d+))?$/.exec(String(x).trim()); if (!m) return null;
  const [int, frac = ""] = m[2].split("."), exp = m[3] ? parseInt(m[3], 10) : 0, digits = BigInt((m[1] === "-" ? "-" : "") + ((int + frac).replace(/^0+(?=\d)/, "") || "0"));
  return { m: digits, e: frac.length - exp };
}
function cmp(a: Dec, b: Dec): number { const s = Math.max(a.e, b.e), x = a.m * 10n ** BigInt(s - a.e), y = b.m * 10n ** BigInt(s - b.e); return x < y ? -1 : x > y ? 1 : 0; }
const OPS: Record<string, (c: number) => boolean> = { ">=": (c) => c >= 0, "<=": (c) => c <= 0, ">": (c) => c > 0, "<": (c) => c < 0, "==": (c) => c === 0 };

export function validateContract(c: any): string[] {
  const ajv = newAjv(), v = ajv.compile(loadSchema("outcome.contract.v1.json"));
  if (!v(c)) return [`outcome contract invalid: ${ajv.errorsText(v.errors)}`];
  const errs: string[] = [], ids = c.acceptance_criteria.map((x: any) => x.id);
  for (const i of new Set<string>(ids.filter((i: string, k: number) => ids.indexOf(i) !== k))) errs.push(`criterion id '${i}' is used twice`);
  if (!c.acceptance_criteria.some((x: any) => x.required)) errs.push("an outcome contract needs at least one required criterion");
  for (const x of c.acceptance_criteria) if (x.verification === "test_result" && !((x.params?.op ?? ">=") in OPS)) errs.push(`${x.id}: unknown operator '${x.params?.op}'`);
  return errs;
}
export const contractHash = (c: any): string => sha256Hex(canonicalize(c));
export function deriveVerdict(results: { required: boolean; status: string }[]): string {
  const req = results.filter((r) => r.required), failed = req.filter((r) => r.status === FAIL), passed = req.filter((r) => r.status === PASS);
  if (failed.length) return passed.length ? "PARTIAL" : "FAILURE";
  if (passed.length < req.length || !req.length) return "INCONCLUSIVE";
  return "SUCCESS";
}
type Ctx = { value?: any; report?: any; fs_read?: ((p: string) => any) | null; facts?: Record<string, any> };
type Result = { status: string; ref: string };
function testResult(p: any, ctx: Ctx): Result {
  const metric = p.metric ?? "value", rep = ctx.report ?? {}, raw = metric === "value" ? ctx.value : metric === "primary" ? rep.primary : (rep.metrics ?? {})[metric];
  if (raw === null || raw === undefined) return { status: "unavailable", ref: `metric:${metric}` };
  const a = decimal(raw), b = decimal(p.value ?? 0); if (!a || !b) return { status: "unavailable", ref: `metric:${metric}` };
  if (p.op === undefined && !("value" in p)) return { status: (rep.valid ?? true) ? PASS : FAIL, ref: `metric:${metric}` };      // no threshold: the submission only has to be valid
  return { status: OPS[p.op ?? ">="](cmp(a, b)) ? PASS : FAIL, ref: `metric:${metric}=${raw}` };
}
function exactDecimal(p: any, ctx: Ctx): Result {
  const src = p.actual !== undefined && p.actual !== null ? p.actual : p.actual_from ? (ctx.facts ?? {})[p.actual_from] : undefined;
  const a = decimal(p.actual !== undefined || p.actual_from ? src : null), b = decimal(p.expected); if (!a || !b) return { status: "unavailable", ref: "decimal" };
  const eq = cmp(a, b) === 0, show = (d: Dec) => String(d.e <= 0 ? d.m * 10n ** BigInt(-d.e) : d.m).replace(/^(-?)(\d+)$/, (_s, sg, n) => (d.e > 0 ? `${sg}${n.padStart(d.e + 1, "0").slice(0, -d.e)}.${n.padStart(d.e + 1, "0").slice(-d.e)}` : `${sg}${n}`));
  return { status: eq ? PASS : FAIL, ref: `decimal:${show(a)}==${show(b)}` };
}
function artifactCheck(p: any, ctx: Ctx): Result {
  if (!ctx.fs_read) return { status: "unavailable", ref: p.path ?? "?" };
  let content: any; try { content = ctx.fs_read(p.path); } catch { return { status: FAIL, ref: `missing:${p.path}` }; }
  if ("sha256" in p && crypto.createHash("sha256").update(content).digest("hex") !== p.sha256) return { status: FAIL, ref: `hash_mismatch:${p.path}` };
  return { status: PASS, ref: `artifact:${p.path}` };
}
export const VERIFIERS: Record<string, (p: any, ctx: Ctx) => Result> = { test_result: testResult, exact_decimal_comparison: exactDecimal, artifact_check: artifactCheck };
/** Deployments register the adapters for their own systems; without one, that evidence is unavailable and never a SUCCESS. */
export const registerVerifier = (kind: string, fn: (p: any, ctx: Ctx) => Result) => { VERIFIERS[kind] = fn; };
export function evaluateContract(contract: any, ctx: Ctx) {
  const errs = validateContract(contract); if (errs.length) throw new ContractError(errs.join("; "));
  const criteria = contract.acceptance_criteria.map((x: any) => {
    const fn = VERIFIERS[x.verification]; let r: Result;
    try { r = fn ? fn(x.params ?? {}, ctx) : { status: "unavailable", ref: `no verifier for ${x.verification}` }; } catch (e: any) { r = { status: "unavailable", ref: `verifier error: ${e?.constructor?.name ?? "Error"}` }; }   // an adapter that fails proves nothing
    return { id: x.id, required: x.required, verification: x.verification, status: r.status, evidence_ref: r.ref ?? null };
  });
  return { verdict: deriveVerdict(criteria), criteria, contract_hash: contractHash(contract) };
}
/** A task without an explicit contract gets one from its pass threshold, so every evaluation run has a verdict. */
export function implicitContract(taskId: string, spec: any) {
  const thr = spec.scoring.pass_threshold, maxi = spec.scoring.primary.direction === "maximize";
  return { task_id: taskId, acceptance_criteria: [{ id: thr !== undefined ? "score_meets_threshold" : "valid_submission", required: true, verification: "test_result", params: thr === undefined ? { metric: "value" } : { metric: "value", op: maxi ? ">=" : "<=", value: thr } }],
    success_policy: "all_required_criteria_pass", inconclusive_policy: "do_not_report_success" };
}
/** The contract a task is judged by: its explicit one, or the one implied by its pass threshold. The manifest hashes exactly this. */
export const contractFor = (spec: any) => (spec.outcome ? { task_id: spec.id, ...spec.outcome } : implicitContract(spec.id, spec));
/** Rates by task category and risk class. The denominator is every evaluated task unless a *documented* exclusion policy applies. */
export function outcomeMetrics(records: any[], exclusionPolicy: string | null = null, exclude: (r: any) => boolean = () => false) {
  const kept = records.filter((r) => !exclude(r)); if (kept.length !== records.length && !exclusionPolicy) throw new ContractError("tasks were excluded from the denominator without a documented exclusion policy");
  const rates = (rs: any[]): Record<string, any> => {
    const n = rs.length, req = rs.reduce((a, r) => a + r.required, 0), ver = rs.reduce((a, r) => a + r.verified, 0), aud = rs.filter((r) => r.audited_verdict);
    return { n, ...Object.fromEntries(VERDICTS.map((v) => [`${v.toLowerCase()}_rate`, n ? rs.filter((r) => r.verdict === v).length / n : null])), verification_coverage: req ? ver / req : null, audited: aud.length, evaluation_error_rate: aud.length ? aud.filter((r) => r.audited_verdict !== r.verdict).length / aud.length : null };
  };
  const groups: Record<string, any[]> = {}; for (const r of kept) (groups[`${r.category}|${r.risk}`] ??= []).push(r);
  return { denominator: kept.length === records.length ? "all evaluated tasks" : `excluding: ${exclusionPolicy}`, overall: rates(kept), groups: Object.fromEntries(Object.keys(groups).sort().map((k) => [k, rates(groups[k])])) };
}
