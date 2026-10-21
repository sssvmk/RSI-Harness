/** Tool manifests and registry (design §9.1, §9.3, §9.5). */
import fs from "node:fs";
import path from "node:path";
import Ajv2020 from "ajv/dist/2020.js";
import { canonicalize, sha256Hex } from "./util.js";
import { globMatch } from "./policy.js";
import { SPEC_DIR } from "./failure.js";
export const loadSchema = (name: string) => JSON.parse(fs.readFileSync(path.join(SPEC_DIR, "schemas", name), "utf8"));
const AjvCtor: any = (Ajv2020 as any).default ?? Ajv2020;
export const newAjv = () => new AjvCtor({ strict: false, allErrors: false });
export class ManifestError extends Error {}
export const manifestHash = (m: any) => { const { content_hash: _, ...rest } = m; return sha256Hex(canonicalize(rest)); };
export type Handler = (args: any, ctx: any) => Promise<any>;
export interface Tool { manifest: any; handler: Handler; reconcile?: Handler | null; validate?: any }
export class ToolRegistry {
  private ajv = newAjv(); private mv = this.ajv.compile(loadSchema("tool.manifest.v1.json"));
  tools = new Map<string, Tool>(); warnings: any[] = [];
  register(manifest: any, handler: Handler, reconcile: Handler | null = null) {
    const m = { ...manifest };
    if (!this.mv(m)) throw new ManifestError(`${m.name}: ${this.ajv.errorsText(this.mv.errors)}`);
    let validate: any;
    try { validate = newAjv().compile(m.input_schema); } catch (e: any) { throw new ManifestError(`${m.name}: invalid input_schema: ${e.message}`); }
    if (!("idempotency" in m)) { m.idempotency = "non_idempotent"; this.warnings.push({ tool: m.name, field: "idempotency", default: "non_idempotent" }); }
    if (!("effect_class" in m)) { m.effect_class = null; this.warnings.push({ tool: m.name, field: "effect_class", default: "requires_approval" }); }
    m.result_cap_tokens ??= 4000; m.timeout_s ??= 120; m.parallel_safe ??= false;
    const declared = m.content_hash; m.content_hash = manifestHash(m);
    if (declared && declared !== m.content_hash) throw new ManifestError(`${m.name}: content_hash mismatch`);
    this.tools.set(m.name, { manifest: m, handler, reconcile, validate });
    return m;
  }
  get(name: string) { return this.tools.get(name); }
  allowed(allow: string[]) { return [...this.tools.keys()].filter((n) => allow.some((g) => globMatch(g, n))).sort(); }
  exposed(allow: string[], always: string[], extra: string[], threshold = 20) {
    let names = this.allowed(allow);
    if (names.length > threshold) { const keep = new Set([...names.filter((n) => always.some((g) => globMatch(g, n))), ...extra]); names = names.filter((n) => keep.has(n)); }
    return names.map((n) => this.tools.get(n)!.manifest);
  }
  search(query: string, allow: string[], limit = 5) {
    const words = query.toLowerCase().split(/\s+/).filter(Boolean), scored: [number, string][] = [];
    for (const n of this.allowed(allow)) { const hay = (n + " " + this.tools.get(n)!.manifest.description).toLowerCase(); const s = words.reduce((a, w) => a + hay.split(w).length - 1, 0); if (s) scored.push([-s, n]); }
    scored.sort((a, b) => a[0] - b[0] || (a[1] < b[1] ? -1 : 1));
    return scored.slice(0, limit).map(([, n]) => this.tools.get(n)!.manifest);
  }
}
/** Hash-pinned MCP registry check (§9.5). */
export class McpGateway {
  constructor(public registry: ToolRegistry, public pinned: Record<string, Record<string, string>>) {}
  async connect(run: any, server: string, client: { listTools(): Promise<any[]> }) {
    const accepted: string[] = [];
    for (const tool of await client.listTools()) {
      const actual = sha256Hex(canonicalize(tool)), expected = this.pinned[server]?.[tool.name] ?? null;
      if (expected !== actual) { await run.emit("mcp.definition_changed", { server, tool: tool.name, expected_hash: expected, actual_hash: actual }); continue; }
      accepted.push(tool.name);
    }
    return accepted;
  }
}
