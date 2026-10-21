/** The memory write path (PRD §4.3 Memory Write-Path Integrity), configurable by the administrator as `memory.write_path`. Mirrors memory_guard.py. */
export const BEHAVIOR_PREFIXES = ["prompts/", "tools/", "skills/", "memory/", "subagents/", "middleware/", "context/"];
export const DEFAULT_WRITE_POLICY = { low_trust_sources: ["email", "web", "advertisement", "third_party", "subagent"], confirmation: "required", screen_patterns: [] as any[] };
export function resolveWritePolicy(cfg?: any): any {
  const over = cfg ?? {}, unknown = Object.keys(over).filter((k) => !(k in DEFAULT_WRITE_POLICY)); if (unknown.length) throw new Error(`memory.write_path: unknown setting(s) ${JSON.stringify(unknown.sort())}`);
  const p: any = { ...JSON.parse(JSON.stringify(DEFAULT_WRITE_POLICY)), ...over };
  if (!Array.isArray(p.low_trust_sources) || !p.low_trust_sources.every((x: any) => typeof x === "string")) throw new Error("memory.write_path.low_trust_sources must be a list of strings");
  if (!["required", "none"].includes(p.confirmation)) throw new Error("memory.write_path.confirmation must be 'required' or 'none'");
  if (!Array.isArray(p.screen_patterns)) throw new Error("memory.write_path.screen_patterns must be a list");
  p.screen_patterns = p.screen_patterns.map((pat: any) => {
    const d = typeof pat === "string" ? { re: pat, flags: "" } : pat && typeof pat === "object" && !Array.isArray(pat) && Object.keys(pat).every((k) => k === "re" || k === "flags") && typeof pat.re === "string" ? { flags: "", ...pat } : null;
    if (d === null || !["", "i"].includes(d.flags)) throw new Error(`memory.write_path.screen_patterns: ${JSON.stringify(pat)} must be a string or {re, flags} with flags '' or 'i'`);
    try { new RegExp(d.re); } catch (e: any) { throw new Error(`memory.write_path.screen_patterns: '${d.re}' is not a valid expression (${e.message})`); }
    return d;
  });
  return p;
}
/** The decision alone, without side effects: {status: write|refused|quarantined, reason}. */
export function decideWrite(policy: any, p: string, provenance: any, content: string): any {
  if (BEHAVIOR_PREFIXES.some((x) => p.replace(/^\/+/, "").startsWith(x))) return { status: "refused", reason: "behavior-changing path must go through the self-improvement gate" };
  const src = provenance.source;
  if (policy.confirmation === "required" && policy.low_trust_sources.includes(src) && !provenance.confirmed_by) return { status: "quarantined", reason: `low-trust source '${src}' needs confirmation` };
  const hit = policy.screen_patterns.find((pat: any) => new RegExp(pat.re, pat.flags.includes("i") ? "i" : "").test(content));
  if (hit) return { status: "quarantined", reason: "contextual-integrity screen", pattern: hit.re };
  if (provenance.trust === "untrusted") return { status: "quarantined", reason: "untrusted provenance" };
  return { status: "write", reason: null };
}
