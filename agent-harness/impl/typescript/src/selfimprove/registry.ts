/** Component registry (Appendix A.7) and PathPolicy (§17.2.3 layer 2). Mirrors the Python implementation; checked by selfimprove-pathpolicy.json. */
import { posix } from "node:path";
import YAML from "yaml";
export const TIERS = ["T0", "T1", "T2", "T3", "T4"];
export const COMPONENTS = new Set(["system_prompt", "tool_description", "tool_implementation", "middleware", "skill", "subagent_config", "long_term_memory", "program", "playbook", "workflow", "context", "retrieval_config"]);
export const PROTECTED = new Set(["components.yaml"]);   // the registry itself is never editable by the loop
export const tierRank = (t: string) => TIERS.indexOf(t);
export function globToRegex(pattern: string): RegExp {
  let out = "", i = 0;
  while (i < pattern.length) {
    const c = pattern[i];
    if (pattern.startsWith("**/", i)) { out += "(?:.*/)?"; i += 3; continue; }
    if (pattern.startsWith("**", i)) { out += ".*"; i += 2; continue; }
    out += c === "*" ? "[^/]*" : c === "?" ? "[^/]" : c.replace(/[.*+?^${}()|[\]\\\/]/g, "\\$&");
    i++;
  }
  return new RegExp("^" + out + "$", "s");
}
export function normalizePath(p: any): [string | null, string | null] {
  if (typeof p !== "string" || !p || p.includes("\0") || p.includes("\\")) return [null, "path.invalid"];
  if (p.startsWith("/")) return [null, "path.absolute"];
  if (p.split("/").includes("..")) return [null, "path.traversal"];
  const norm = posix.normalize(p);
  if (norm === "." || norm === "" || norm.endsWith("/") || p.endsWith("/")) return [null, "path.invalid"];
  return [norm, null];
}
export interface Entry { path: string; component: string; tier: string; owner: string; origin: string; raw: any; rx: RegExp }
export class RegistryError extends Error {}
export class ComponentRegistry {
  constructor(public entries: Entry[], public schemaVersion = 1) {}
  static fromDict(doc: any): ComponentRegistry {
    if (doc.schema_version !== 1) throw new RegistryError("schema_version must be 1");
    const entries: Entry[] = [];
    for (const e of doc.components ?? []) {
      for (const k of ["path", "component", "tier", "owner", "origin"]) if (!(k in e)) throw new RegistryError(`entry missing required field ${k}`);
      if (!["T0", "T1", "T2"].includes(e.tier)) throw new RegistryError(`registry tiers are T0-T2 only (got ${e.tier} for ${e.path})`);
      if (!["human", "learned"].includes(e.origin)) throw new RegistryError(`origin must be human|learned (${e.path})`);
      if (!COMPONENTS.has(e.component)) throw new RegistryError(`unknown component '${e.component}' (${e.path})`);
      entries.push({ path: e.path, component: e.component, tier: e.tier, owner: e.owner, origin: e.origin, raw: e, rx: globToRegex(e.path) });
    }
    return new ComponentRegistry(entries, doc.schema_version);
  }
  static fromYaml(text: string) { return ComponentRegistry.fromDict(YAML.parse(text)); }
  /** Longest matching entry (by pattern length); ties go to the earlier entry. Unregistered => null. */
  entryFor(p: string): Entry | null {
    const [norm, err] = normalizePath(p);
    if (err || PROTECTED.has(norm!)) return null;
    let best: Entry | null = null;
    for (const e of this.entries) if (e.rx.test(norm!) && (best === null || e.path.length > best.path.length)) best = e;
    return best;
  }
  tierOf(p: string) { return this.entryFor(p)?.tier ?? null; }
  check(p: string): [string | null, string | null] {
    const [norm, err] = normalizePath(p);
    if (err) return [null, err];
    if (PROTECTED.has(norm!)) return [null, "path.protected"];
    const e = this.entryFor(p);
    return e ? [e.tier, null] : [null, "path.unregistered"];
  }
  componentOf(p: string) { return this.entryFor(p)?.component ?? null; }
}
