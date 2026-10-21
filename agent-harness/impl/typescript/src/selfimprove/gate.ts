/** The gate (§17.8.2): identical in every implementation. Pure functions of observations; no I/O. See the Python module for the recorded interpretations
 *  (Holm applies to the target test per target model; each check has its own bootstrap seed; `needs_heldout` when only the held-out data is missing). */
import { bootstrapMeans, improvements, intervalFromSorted, mean, seedFor } from "../stats.js";
export const DEFAULT_PARAMS = { alpha: 0.05, B: 10000, holm: true, margins: { default: 0.02 } as Record<string, number>, heldout_margin: 0.02, preserved_margin: 0.02 };
export const VARIANTS = ["canonical", "format_varied"];
export function paramsOf(p?: any) { const out: any = { ...DEFAULT_PARAMS, ...(p ?? {}) }; out.margins = { ...DEFAULT_PARAMS.margins, ...(p?.margins ?? {}) }; return out; }
function iv(x: number[], seed: bigint, alpha: number, B: number) { const means = bootstrapMeans(x, seed, B), [lower, upper] = intervalFromSorted(means, 1 - alpha); return { delta: mean(x), lower, upper } as any; }
export function targetPValue(x: number[], seed: bigint, B: number): number { const means = bootstrapMeans(x, seed, B); let c = 0; for (const m of means) if (m <= 0) c++; return c / B; }
export function dominated(s: any, archive: any[]): boolean {
  for (const a of archive) {
    const ge = a.target >= s.target && a.cost <= s.cost && a.latency <= s.latency, strict = a.target > s.target || a.cost < s.cost || a.latency < s.latency;
    if (ge && strict) return true;
  }
  return false;
}
/** Nominal per-rank levels alpha/(m-k+1); the step-down stop is applied by the caller once a test fails. */
export function holmLevels(pvalues: (number | null)[], alpha: number, holm = true): (number | null)[] {
  const idx = pvalues.map((p, i) => [p, i] as [number | null, number]).filter(([p]) => p !== null).sort((a, b) => (a[0]! - b[0]!) || (a[1] - b[1])).map(([, i]) => i);
  const m = idx.length, lv: (number | null)[] = pvalues.map(() => null);
  idx.forEach((i, k0) => { lv[i] = holm ? alpha / (m - (k0 + 1) + 1) : alpha; });
  return lv;
}
function checkVariant(runId: string, pid: string, model: string, variant: string, v: any, P: any, alphaT: number | null, out: any): boolean {
  let ok = true; const tag = `${model}:${variant}`, B = P.B, t = v.target, x = improvements(t.b, t.c, t.direction);
  let tv: any;
  if (alphaT === null) { out.reasons.push(`holm:stopped(${tag})`); tv = iv(x, seedFor(runId, `${pid}:${model}:${variant}:target`), P.alpha, B); ok = false; }
  else { tv = iv(x, seedFor(runId, `${pid}:${model}:${variant}:target`), alphaT, B); if (!(tv.lower > 0)) { out.reasons.push(`target:no_significant_improvement(${tag})`); ok = false; } }
  tv.alpha = alphaT !== null ? alphaT : P.alpha;
  (out.target ??= {})[variant] = tv;
  const grs: any[] = [];
  for (const g of v.guardrails ?? []) {
    const gx = improvements(g.b, g.c, g.direction), margin = g.margin !== undefined ? g.margin : (P.margins[g.metric] ?? P.margins.default);
    const gi = iv(gx, seedFor(runId, `${pid}:${model}:${variant}:guardrail:${g.metric}`), P.alpha, B);
    gi.metric = g.metric; gi.margin = margin; gi.pass = gi.lower >= -margin;
    if (!gi.pass) { out.reasons.push(`guardrail:${g.metric}(${tag})`); ok = false; }
    grs.push(gi);
  }
  (out.guardrails ??= {})[variant] = grs;
  const pr = v.preserved;
  if (pr) {
    const idx: number[] = []; pr.b.forEach((b: number, i: number) => { if (b >= 1) idx.push(i); });
    if (idx.length) {
      const px = idx.map((i) => pr.c[i] - pr.b[i]), pi = iv(px, seedFor(runId, `${pid}:${model}:${variant}:preserved`), P.alpha, B);
      pi.pass = pi.lower >= -P.preserved_margin;
      if (!pi.pass) { out.reasons.push(`preserved:non_inferiority(${tag})`); ok = false; }
      (out.preserved ??= {})[variant] = pi;
    }
    const bad = (pr.must_pass ?? []).filter((i: number) => pr.b[i] >= 1 && pr.c[i] < 1);
    if (bad.length) { out.reasons.push(`preserved:must_pass_regressed(${tag}:[${bad.join(", ")}])`); ok = false; }
  }
  return ok;
}
function checkHeldout(runId: string, pid: string, model: string, h: any, P: any, out: any): boolean {
  let ok = true; const t = h.target, x = improvements(t.b, t.c, t.direction), v = iv(x, seedFor(runId, `${pid}:${model}:heldout:target`), P.alpha, P.B);
  v.pass = v.lower >= -P.heldout_margin; out.heldout = v;
  if (!v.pass) { out.reasons.push(`heldout:regressed(${model})`); ok = false; }
  const pr = h.preserved;
  if (pr) { const bad = (pr.must_pass ?? []).filter((i: number) => pr.b[i] >= 1 && pr.c[i] < 1); if (bad.length) { out.reasons.push(`heldout:must_pass_regressed(${model}:[${bad.join(", ")}])`); ok = false; } }
  return ok;
}
export function evaluateCandidate(runId: string, cand: any, params: any, alphaByModel: Record<string, number | null>): any {
  const P = paramsOf(params), pid = cand.proposal_id;
  const dec: any = { proposal_id: pid, tier: cand.tier, accepted: false, needs_heldout: false, accepted_models: [], reasons: [], models: {} };
  const pending: string[] = [];
  for (const [model, data] of Object.entries<any>(cand.models)) {
    const out: any = { reasons: [] }; let ok = true;
    for (const variant of VARIANTS) {
      if (!data.variants || !(variant in data.variants)) { out.reasons.push(`variant:missing(${model}:${variant})`); ok = false; continue; }
      ok = checkVariant(runId, pid, model, variant, data.variants[variant], P, alphaByModel[model] === undefined ? null : alphaByModel[model], out) && ok;
    }
    if (ok) {
      if (data.heldout === undefined || data.heldout === null) { out.status = "needs_heldout"; pending.push(model); }
      else { ok = checkHeldout(runId, pid, model, data.heldout, P, out); out.status = ok ? "pass" : "fail"; }
    } else out.status = "fail";
    dec.models[model] = out;
    if (out.status === "pass") dec.accepted_models.push(model);
  }
  dec.needs_heldout = pending.length > 0 && dec.accepted_models.length === 0;
  if (cand.summary != null && cand.archive != null && dominated(cand.summary, cand.archive)) { dec.reasons.push("dominated"); dec.accepted_models = []; dec.needs_heldout = false; }
  dec.accepted = dec.accepted_models.length > 0;
  for (const [model, out] of Object.entries<any>(dec.models)) { dec.reasons.push(...out.reasons); if (out.status === "fail" && !out.reasons.length) dec.reasons.push(`model:${model}:failed`); }
  const first = Object.values<any>(dec.models)[0] ?? {}, t = first.target?.canonical ?? {};
  dec.target = { delta: t.delta ?? null, lower: t.lower ?? null, upper: t.upper ?? null };
  dec.guardrails = (first.guardrails?.canonical ?? []).map((g: any) => ({ metric: g.metric, delta: g.delta, lower: g.lower, upper: g.upper, margin: g.margin, pass: g.pass }));
  return dec;
}
export function roundAlphas(runId: string, candidates: any[], params?: any): Record<string, number | null>[] {
  const P = paramsOf(params), models = [...new Set(candidates.flatMap((c) => Object.keys(c.models)))].sort();
  const alphaFor: Record<string, number | null>[] = candidates.map(() => ({}));
  for (const model of models) {
    const ps: (number | null)[] = candidates.map((c) => {
      const v = c.models[model]?.variants?.canonical; if (!v) return null;
      return targetPValue(improvements(v.target.b, v.target.c, v.target.direction), seedFor(runId, `${c.proposal_id}:${model}:canonical:target`), P.B);
    });
    const levels = holmLevels(ps, P.alpha, P.holm); let stopped = false;
    const order = ps.map((p, i) => [p, i] as [number | null, number]).filter(([p]) => p !== null).sort((a, b) => (a[0]! - b[0]!) || (a[1] - b[1])).map(([, i]) => i);
    for (const i of order) {   // step-down in ascending p: the first failure stops every later candidate
      if (stopped) { alphaFor[i][model] = null; continue; }
      const t = candidates[i].models[model].variants.canonical.target, x = improvements(t.b, t.c, t.direction);
      alphaFor[i][model] = levels[i];
      if (!(iv(x, seedFor(runId, `${candidates[i].proposal_id}:${model}:canonical:target`), levels[i]!, P.B).lower > 0)) stopped = true;
    }
  }
  return alphaFor;
}
export const improvementsOf = (t: any) => improvements(t.b, t.c, t.direction);
export function decideRound(runId: string, candidates: any[], params?: any): any[] { const a = roundAlphas(runId, candidates, params); return candidates.map((c, i) => evaluateCandidate(runId, c, params, a[i])); }
