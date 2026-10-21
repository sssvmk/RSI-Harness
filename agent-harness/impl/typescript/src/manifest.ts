/** Run provenance manifest (design §13.9, PRD §3.6). Mirrors manifest.py. Every field is `recorded`, `unavailable`, or `not_applicable`; an unavailable value is null with a reason and is never invented. */
import { canonicalize, sha256Hex } from "./util.js";
import { loadSchema, newAjv } from "./registry.js";
import { costOf } from "./budgets.js";

export const SCHEMA_VERSION = "1.0";
export const START = ["models", "harness_version", "config_commit", "profile", "limits", "definitions", "tier_policy", "image_digests", "build_hashes", "task", "inputs", "budgets", "test_visibility", "parent_run_id", "project"];
export const FINAL = ["termination_status", "tool_call_count", "outcome", "usage", "model_calls"];
const CORE = ["models", "harness_version", "config_commit", "profile", "limits", "budgets"], STD = [...CORE, "definitions", "tier_policy"];
export const REQUIRED: Record<string, string[]> = { core: CORE, standard: STD, high: [...STD, "image_digests", "build_hashes"] };
export class ManifestError extends Error {}
export const rec = (value: any, reason?: string) => ({ status: "recorded", value, ...(reason ? { reason } : {}) });
export const na = (reason: string) => ({ status: "not_applicable", value: null, reason });
export const unavailable = (reason: string) => ({ status: "unavailable", value: null, reason });
export function validate(m: any, profile: string): string[] {
  const ajv = newAjv(), v = ajv.compile(loadSchema("run.manifest.v1.json")); if (!v(m)) return [`manifest invalid: ${ajv.errorsText(v.errors)}`];
  const f = m.fields, errs: string[] = []; for (const k of START) if (!(k in f)) errs.push(`field ${k} is missing`);
  for (const k of [...(REQUIRED[profile] ?? REQUIRED.core)].sort()) if (k in f && f[k].status === "unavailable") errs.push(`required field ${k} is unavailable (${f[k].reason ?? ""})`);   // not_applicable is a legitimate absence; unavailable is a gap
  return errs;
}
export const pendingFinal = () => Object.fromEntries(FINAL.map((k) => [k, unavailable("the run has not finished")]));
export const manifestHash = (m: any): string => sha256Hex(canonicalize(m));
export const merged = (start: any, fin: any) => ({ ...start, fields: { ...start.fields, ...fin } });
/** Final fields, computed from the log at the moment the run finishes. */
export async function finalFields(svc: any, run: any, models: Record<string, any>, state?: string) {
  const evs = svc.store.events(run.runId), st = state ?? (await run.state()), term = [...evs].reverse().find((e: any) => e.type === "run.terminated")?.payload;
  const calls = evs.filter((e: any) => e.type === "tool.call.requested").length, outs = evs.filter((e: any) => e.type === "outcome.evaluated").map((e: any) => e.payload);
  let ref: string | null = null, cost = 0, priced = false, inTok = 0, outTok = 0;
  for (const e of evs) {   // each response is priced by the model that was most recently requested
    if (e.type === "model.requested") ref = e.payload.model_ref;
    else if (e.type === "model.responded") { const u = e.payload.usage, price = ref ? models[ref]?.price : undefined; inTok += u.input_tokens; outTok += u.output_tokens; if (price) { priced = true; cost += costOf(u, price); } }
  }
  const tv = [...evs].reverse().find((e: any) => e.type === "test_visibility.verified")?.payload;   // the declaration, plus what the environment was verified to do
  const out: any = { termination_status: rec({ state: st, reason: term?.reason ?? (st === "COMPLETED" ? "completed" : null) }), tool_call_count: rec(calls),
    outcome: rec({ verdict: outs.length ? outs[outs.length - 1].verdict : "not_evaluated", evidence_references: outs.length ? [outs[outs.length - 1].contract_hash] : [] }),
    usage: rec({ input_tokens: inTok, output_tokens: outTok, estimated_cost: priced ? Math.round(cost * 1e8) / 1e8 : null, currency: priced ? "USD" : null }, priced ? undefined : "no price is configured for the model") };
  out.model_calls = rec(evs.filter((e: any) => e.type === "model.requested").map((e: any) => ({ call_id: e.payload.request_ref, seq: e.seq, turn: e.payload.turn ?? null, model_ref: e.payload.model_ref })));   // each model call links to this run
  if (tv) out.test_visibility = rec(tv);
  return out;
}
