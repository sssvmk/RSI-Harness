/** FailureClassifier (PRD §3.7, design §15.3): a versioned taxonomy of rules, extension layers, secondary tags and human override. Mirrors failure.py:
 *  base rules in spec/failure-rules.yaml; an agent developer extends the taxonomy with files in spec/failure-rules.d/*.yaml, every code namespaced `<layer>.<class>`;
 *  core rules are evaluated first, so an extension can classify its own errors but cannot change how a core failure is classified. */
import fs from "node:fs";
import path from "node:path";
import YAML from "yaml";
import { canonicalize, sha256Hex } from "./util.js";
import { globMatch } from "./policy.js";
export const SPEC_DIR = path.resolve(import.meta.dirname, "../../../spec");
export class TaxonomyError extends Error {}
const LAYER_RE = /^[a-z][a-z0-9_]*$/, CORE_NAMES = new Set(["tool", "state", "environment", "env", "architecture", "arch", "security", "sec", "eval_integrity", "selfmod"]);
const EXT_KEYS = new Set(["layer", "classes", "rules", "owner"]), RULE_KEYS = new Set(["reason", "last_error", "class", "code"]);
export function buildRules(base: any, extensions: any[] = []): any {
  const codes: Record<string, any> = {}; for (const [c, v] of Object.entries<any>(base.codes ?? {})) codes[c] = { ...v };
  const core: any[] = [], fallback: any[] = [];
  for (const r of base.rules) {
    if (r.class !== null && !(r.code in codes)) throw new TaxonomyError(`base rule emits unknown code '${r.code}'`);
    const out: any = {}; for (const k of ["reason", "last_error", "class", "code"]) if (k in r) out[k] = r[k];
    (r.fallback ? fallback : core).push(out);
  }
  const layers: Record<string, any> = {}, extRules: any[] = [];
  for (const ext of [...extensions].sort((a, b) => (String(a.layer) < String(b.layer) ? -1 : String(a.layer) > String(b.layer) ? 1 : 0))) {
    const unknown = Object.keys(ext).filter((k) => !EXT_KEYS.has(k)); if (unknown.length) throw new TaxonomyError(`extension: unknown setting(s) ${JSON.stringify(unknown.sort())}`);
    const layer = ext.layer; if (typeof layer !== "string" || !LAYER_RE.test(layer)) throw new TaxonomyError(`extension layer ${JSON.stringify(layer)} must be a lowercase identifier`);
    if (CORE_NAMES.has(layer)) throw new TaxonomyError(`layer '${layer}' reuses a core name`); if (layer in layers) throw new TaxonomyError(`layer '${layer}' is declared twice`);
    layers[layer] = ext.owner ?? null; const pat = new RegExp(`^${layer}\\.[a-z][a-z0-9_]*$`);
    for (const c of ext.classes ?? []) { const code = typeof c === "object" ? c.code : c; if (!pat.test(code)) throw new TaxonomyError(`'${code}' is not a valid class of layer '${layer}' (expected '${layer}.<class>')`); if (code in codes) throw new TaxonomyError(`class '${code}' is already defined`); codes[code] = { class: layer }; }
    for (const r of ext.rules ?? []) {
      const rk = Object.keys(r).filter((k) => !RULE_KEYS.has(k)); if (rk.length) throw new TaxonomyError(`${layer}: rule has unknown key(s) ${JSON.stringify(rk.sort())}`);
      if (r.class !== layer || !(r.code in codes) || codes[r.code].class !== layer) throw new TaxonomyError(`${layer}: a rule may only emit a class of its own layer (got class='${r.class}', code='${r.code}')`);
      extRules.push({ ...r });
    }
  }
  const ordered = [...core, ...extRules, ...fallback], out: any = { taxonomy_version: base.taxonomy_version, classes: [...new Set([...(base.classes ?? []), ...Object.keys(layers)])].sort(), codes, rules: ordered, layers };
  out.fingerprint = sha256Hex(canonicalize({ v: out.taxonomy_version, codes: Object.keys(codes).sort(), rules: ordered, layers: Object.keys(layers).sort() })); return out;
}
export function loadRules(p = path.join(SPEC_DIR, "failure-rules.yaml"), extensionDir?: string): any {
  const base = YAML.parse(fs.readFileSync(p, "utf8")), d = extensionDir ?? path.join(path.dirname(p), "failure-rules.d");
  const exts = fs.existsSync(d) && fs.statSync(d).isDirectory() ? fs.readdirSync(d).filter((f) => f.endsWith(".yaml")).sort().map((f) => YAML.parse(fs.readFileSync(path.join(d, f), "utf8"))) : [];
  return buildRules(base, exts);
}
/** One primary class (how the run ended) plus secondary tags (other classes whose error codes appeared earlier in the run). */
export function classify(reason: string, lastError: string | null, rules: any, errorCodes: string[] = []) {
  let primary: any = null;
  for (const r of rules.rules) {
    if (!globMatch(r.reason, reason)) continue;
    if ("last_error" in r && !(lastError && globMatch(r.last_error, lastError))) continue;
    if (r.class === null) return null;
    primary = r; break;
  }
  if (primary === null) primary = { class: "architecture", code: "arch.recovery" };
  const secondary: string[] = [];
  for (const r of rules.rules) if ("last_error" in r && r.code && r.code !== primary.code && !secondary.includes(r.code) && errorCodes.some((c) => globMatch(r.last_error, c))) secondary.push(r.code);
  return { class: primary.class, code: primary.code, secondary, taxonomy_version: rules.taxonomy_version, taxonomy_fingerprint: rules.fingerprint ?? null };
}
export const knownCode = (rules: any, code: string) => code in rules.codes;
/** The payload of a `failure.observed` event: a class observed on something other than a run's termination (a candidate, a round, a held-out set). */
export function observation(rules: any, code: string, subject: string, evidence: any = {}) {
  if (!knownCode(rules, code)) throw new TaxonomyError(`unknown failure code '${code}'`);
  return { code, class: rules.codes[code].class, subject, evidence, taxonomy_version: rules.taxonomy_version };
}
/** The payload of a `failure.reclassified` event: a human override, audited with who and why. The harness's own classification stays in the log. */
export function reclassification(rules: any, prior: any, toCode: string, actor: string, reason: string) {
  if (!knownCode(rules, toCode)) throw new TaxonomyError(`unknown failure code '${toCode}'`); if (!actor || !reason) throw new TaxonomyError("an override needs an actor and a reason");
  return { from_code: prior?.code ?? null, to_code: toCode, class: rules.codes[toCode].class, actor, reason, taxonomy_version: rules.taxonomy_version };
}
export function effectiveClassification(events: any[]): any {
  let cur: any = null;
  for (const e of events) {
    if (e.type === "failure.classified") cur = { ...e.payload, overridden: false };
    else if (e.type === "failure.reclassified") cur = { class: e.payload.class, code: e.payload.to_code, secondary: cur?.secondary ?? [], overridden: true, taxonomy_version: e.payload.taxonomy_version };
  }
  return cur;
}
