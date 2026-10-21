/** Run comparison and aggregation (PRD §3.6): performance = F(model, harness, environment, task, grader). Runs are comparable in aggregate metrics only when they share the same
 *  configuration commit, models, task and grader; the report refuses to mix them unless asked, and says so. Mirrors reports.py. */
import { canonicalize, sha256Hex } from "./util.js";
export const KEY_FIELDS = ["config_commit", "models", "task_id", "task_version", "grader_hash", "harness_version", "profile"];
export class NotComparable extends Error { constructor(public groups: any[]) { super(`${groups.length} incomparable groups`); } }
export function comparabilityKey(created: any): Record<string, any> {
  const prov = created.provenance ?? {}, fallback = created.agent?.model_ref ? [created.agent.model_ref] : [], models = prov.models ?? fallback;
  return { config_commit: created.config_commit ?? null, models: Array.isArray(models) ? [...models].sort() : models, task_id: created.task_id ?? null, task_version: prov.task_version ?? null, grader_hash: prov.grader_hash ?? null, harness_version: prov.harness_version ?? null, profile: created.profile ?? null };
}
export function differences(a: any, b: any): { field: string; a: any; b: any }[] {
  const ka = comparabilityKey(a), kb = comparabilityKey(b); return KEY_FIELDS.filter((f) => canonicalize(ka[f]) !== canonicalize(kb[f])).map((f) => ({ field: f, a: ka[f], b: kb[f] }));
}
export function compareRuns(a: any, b: any) { const d = differences(a, b); return { comparable: d.length === 0, differences: d }; }
export function aggregate(runs: { run_id: string; created: any; value: number }[], allowMixed = false) {
  const groups = new Map<string, any>();
  for (const r of runs) { const k = comparabilityKey(r.created), fp = sha256Hex(canonicalize(k)).slice(0, 16); if (!groups.has(fp)) groups.set(fp, { key: k, fingerprint: fp, runs: [], values: [] }); groups.get(fp).runs.push(r.run_id); groups.get(fp).values.push(r.value); }
  const out: any[] = [...groups.values()].map((g) => { let t = 0; for (const v of g.values) t += v; return { key: g.key, fingerprint: g.fingerprint, runs: g.runs, n: g.values.length, mean: g.values.length ? t / g.values.length : null }; });
  out.sort((x, y) => (x.fingerprint < y.fingerprint ? -1 : x.fingerprint > y.fingerprint ? 1 : 0));
  if (out.length > 1) { for (const g of out.slice(1)) g.key_diff = KEY_FIELDS.filter((f) => canonicalize(g.key[f]) !== canonicalize(out[0].key[f])); if (!allowMixed) throw new NotComparable(out); }
  return { comparable: out.length <= 1, groups: out, non_comparable: out.length > 1 };
}
