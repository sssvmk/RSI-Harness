/** Gate policy per kind of edit (design §17.8-§17.10): defaults chosen by the deployment owner, and an administrator may make any environment stricter or leaner.
 *  Mirrors policy.py; see its header for the rules. T2 always needs a blocking approval and tiers above T2 are never promoted by the loop. */
import { canonicalize, sha256Hex } from "../util.js";
export const TIERS = ["T0", "T1", "T2"] as const;
export const REVIEW_MODES = ["none", "sampled", "blocking"];
const COMMON = () => ({ B: 10000, holm: true, margins: { default: 0.02 } as Record<string, number>, heldout_margin: 0.02, preserved_margin: 0.02 });
export const DEFAULTS: { tiers: Record<string, any> } = { tiers: {
  T0: { alpha: 0.05, ...COMMON(), review: { mode: "none", sample_rate: 0.0, window_h: 72 }, canary_fraction: 0.0 },
  T1: { alpha: 0.05, ...COMMON(), review: { mode: "sampled", sample_rate: 0.2, window_h: 72 }, canary_fraction: 0.0 },
  T2: { alpha: 0.01, ...COMMON(), review: { mode: "blocking", sample_rate: 1.0, window_h: 72 }, canary_fraction: 0.1 } } };
export const GATE_KEYS = ["alpha", "B", "holm", "margins", "heldout_margin", "preserved_margin"];
const TIER_KEYS = new Set([...GATE_KEYS, "review", "canary_fraction"]), REVIEW_KEYS = new Set(["mode", "sample_rate", "window_h"]);
export class PolicyError extends Error { constructor(public code: string, msg: string) { super(`${code}: ${msg}`); } }
const clone = <T>(x: T): T => JSON.parse(JSON.stringify(x));
const isObj = (x: any) => x !== null && typeof x === "object" && !Array.isArray(x);
const num = (x: any) => typeof x === "number" && Number.isFinite(x);
function merge(base: any, over: any): any {
  const out = clone(base);
  for (const [k, v] of Object.entries(over)) {
    if (k === "margins" && isObj(v)) out[k] = { ...(out[k] ?? {}), ...(v as object) };
    else if (isObj(v) && isObj(out[k])) out[k] = merge(out[k], v);
    else out[k] = v;
  }
  return out;
}
function validate(tier: string, t: any) {
  const bad = (c: string, m: string): never => { throw new PolicyError(c, m); };
  const unknown = Object.keys(t).filter((k) => !TIER_KEYS.has(k)); if (unknown.length) bad("policy.unknown_key", `${tier}: unknown setting(s) ${JSON.stringify(unknown.sort())}`);
  if (!num(t.alpha) || t.alpha < 1e-6 || t.alpha > 0.2) bad("policy.invalid_value", `${tier}.alpha must be between 0.000001 and 0.2 (got ${t.alpha}); 0 would make the gate unpassable`);
  if (!Number.isInteger(t.B) || t.B < 200 || t.B > 100000) bad("policy.invalid_value", `${tier}.B must be an integer between 200 and 100000`);
  if (typeof t.holm !== "boolean") bad("policy.invalid_value", `${tier}.holm must be true or false`);
  for (const k of ["heldout_margin", "preserved_margin"]) if (!num(t[k]) || t[k] < 0 || t[k] > 0.5) bad("policy.invalid_value", `${tier}.${k} must be between 0 and 0.5`);
  const m = t.margins; if (!isObj(m) || !("default" in m) || Object.values(m).some((v) => !num(v) || (v as number) < 0 || (v as number) > 0.5)) bad("policy.invalid_value", `${tier}.margins needs a default and values between 0 and 0.5`);
  const r = t.review, ru = Object.keys(r).filter((k) => !REVIEW_KEYS.has(k)); if (ru.length) bad("policy.unknown_key", `${tier}.review: unknown setting(s) ${JSON.stringify(ru.sort())}`);
  if (!REVIEW_MODES.includes(r.mode)) bad("policy.invalid_value", `${tier}.review.mode must be one of ${REVIEW_MODES}`);
  if (!num(r.sample_rate) || r.sample_rate < 0 || r.sample_rate > 1) bad("policy.invalid_value", `${tier}.review.sample_rate must be between 0 and 1`);
  if (!num(r.window_h) || r.window_h <= 0) bad("policy.invalid_value", `${tier}.review.window_h must be positive`);
  if (!num(t.canary_fraction) || t.canary_fraction < 0 || t.canary_fraction > 1) bad("policy.invalid_value", `${tier}.canary_fraction must be between 0 and 1`);
  if (tier === "T2" && r.mode !== "blocking") bad("policy.locked", "T2 always requires a blocking approval by an independent approver; it cannot be relaxed");
}
function direction(path: string, d: any, v: any): string {
  if (["alpha", "heldout_margin", "preserved_margin"].includes(path) || path.startsWith("margins.")) return v < d ? "stricter" : "leaner";
  if (path === "B" || path === "review.sample_rate") return v > d ? "stricter" : "leaner";
  if (path === "holm") return v ? "stricter" : "leaner";
  if (path === "review.mode") return REVIEW_MODES.indexOf(v) > REVIEW_MODES.indexOf(d) ? "stricter" : "leaner";
  return "changed";
}
function flatten(d: any, prefix = ""): Record<string, any> { const out: Record<string, any> = {}; for (const [k, v] of Object.entries(d)) Object.assign(out, isObj(v) ? flatten(v, `${prefix}${k}.`) : { [`${prefix}${k}`]: v }); return out; }
export class GatePolicy {
  constructor(public tiers: Record<string, any>, public environment: string | null = null, validated = true) { if (validated) for (const t of TIERS) validate(t, tiers[t]); }
  static resolve(config: any = null, environment: string | null = null): GatePolicy {
    const cfg = config ?? {}, extra = Object.keys(cfg).filter((k) => !["tiers", "environments", "environment"].includes(k));
    if (extra.length) throw new PolicyError("policy.unknown_key", `unknown top-level setting(s) ${JSON.stringify(extra.sort())}`);
    const env = environment ?? cfg.environment ?? null, tiers = clone(DEFAULTS.tiers), layers: [string, any][] = [["tiers", cfg.tiers ?? {}]];
    if (env !== null) {
      const envs = cfg.environments ?? {}; if (!(env in envs)) throw new PolicyError("policy.unknown_environment", `environment '${env}' is not defined (defined: ${JSON.stringify(Object.keys(envs).sort())})`);
      const ex = Object.keys(envs[env]).filter((k) => k !== "tiers"); if (ex.length) throw new PolicyError("policy.unknown_key", `environment '${env}': unknown setting(s) ${JSON.stringify(ex.sort())}`);
      layers.push([`environments.${env}`, envs[env].tiers ?? {}]);
    }
    for (const [where, layer] of layers) for (const [tier, over] of Object.entries<any>(layer)) {
      if (!(TIERS as readonly string[]).includes(tier)) throw new PolicyError("policy.unknown_tier", `${where}: only ${JSON.stringify(TIERS)} can be configured (T3 and above are never promoted by the loop), got '${tier}'`);
      tiers[tier] = merge(tiers[tier], over);
    }
    return new GatePolicy(tiers, env);
  }
  /** Backward compatible: one gate parameter set for every tier. */
  static uniform(params: any = {}): GatePolicy {
    const tiers = clone(DEFAULTS.tiers);
    for (const t of TIERS) for (const [k, v] of Object.entries<any>(params)) if (GATE_KEYS.includes(k)) tiers[t][k] = k === "margins" ? { ...tiers[t].margins, ...v } : clone(v);
    return new GatePolicy(tiers, null, false);
  }
  gateParams(tier: string): any { const t = this.tiers[tier]; return Object.fromEntries(GATE_KEYS.map((k) => [k, clone(t[k])])); }
  review(tier: string): any { return { ...this.tiers[tier].review }; }
  canaryFraction(tier: string): number { return this.tiers[tier].canary_fraction; }
  get resolved() { return { tiers: clone(this.tiers) }; }
  get fingerprint(): string { return sha256Hex(canonicalize(this.resolved)); }
  diff(): any[] {
    const out: any[] = [];
    for (const tier of TIERS) {
      const d = flatten(DEFAULTS.tiers[tier]), v = flatten(this.tiers[tier]);
      for (const path of [...new Set([...Object.keys(d), ...Object.keys(v)])].sort()) if (JSON.stringify(d[path]) !== JSON.stringify(v[path]))
        out.push({ tier, path, default: d[path] ?? null, value: v[path] ?? null, direction: path in d && path in v ? direction(path, d[path], v[path]) : "changed" });
    }
    return out;
  }
  relaxed(): any[] { return this.diff().filter((x) => x.direction === "leaner"); }
}
