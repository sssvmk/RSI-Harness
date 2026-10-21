/** Plugins that satisfy each kind's contract, for tests. `make(manifest)` is the factory a manifest's entry points at. */
import { KINDS, PluginRejection } from "../src/plugins.js";
export const OUT: Record<string, Record<string, any>> = {
  proposer: { propose: { proposals: [{ edits: [], manifest: {} }] }, analyze_weakness: { findings: [] } },
  retrieval: { index: { indexed: 1, index_version: "v1" }, query: { passages: [{ id: "d1", text: "hello", score: 0.9, source: "s", permissions: {} }] }, forget: { removed: 1 } },
  user_model: { observe: { recorded: true }, predict: { prediction: {}, confidence: 0.5 }, forget: { removed: 1 } },
  computer_use: { open: { session: "s1" }, observe: { observation: {} }, act: { observation: {} }, close: { closed: true } },
  research: { fork: { fork_id: "f1" }, step: { state: {} }, stop: { stopped: true } },
  isolation: { provision: { sandbox_id: "sb1", effective: {} }, verify: { ok: true, observed: {} }, destroy: { destroyed: true } },
  assurance: { attest: { attestation: {} }, broker_child: { child_id: "c1" } },
  evaluator: { evaluate: { verdict: "PASS", reason_codes: [], evidence_refs: [] } } };
/** A conforming plugin. `over` replaces what a method returns (a function replaces the method, an Error is thrown), and `calls` records what was called. Only the contract's methods exist. */
export class Fake {
  calls: [string, any][] = []; [k: string]: any;
  constructor(public m: any, public over: Record<string, any> = {}) {
    for (const name of Object.keys(KINDS[m.kind].methods)) this[name] = (p: any) => {
      this.calls.push([name, p]);
      let r = name === "describe" ? { id: m.id, kind: m.kind, version: m.version } : name === "health" ? { ok: true } : OUT[m.kind][name];
      if (name in this.over) r = this.over[name];
      if (typeof r === "function") return r(p); if (r instanceof Error) throw r; return r;
    };
  }
}
export function manifest(kind: string, id?: string, version = "1.0.0", trust = "in_process", over: any = {}) {
  const m: any = { id: id ?? `${kind}-fake`, kind, version, interface_version: KINDS[kind].interface_version, trust, grants: [], entry: trust === "in_process" ? { module: new URL(import.meta.url).pathname, factory: "make" } : { command: ["python3", "plugin_proc.py"] } };
  if (kind === "evaluator") m.evaluator = { type: "code" };
  return { ...m, ...over };
}
export const make = (m: any) => new Fake(m);

/** A plugin that follows its contract's error rules: it throws the declared error for each of the spec's probe inputs, and otherwise behaves like Fake. */
export class ProbeAware extends Fake {
  constructor(m: any, over: Record<string, any> = {}) {
    super(m, over);
    for (const [name, spec] of Object.entries<any>(KINDS[m.kind].methods)) {
      const inner = this[name];
      this[name] = (p: any) => { for (const e of spec.errors ?? []) if (e.probe && JSON.stringify(e.probe) === JSON.stringify(p)) { this.calls.push([name, p]); throw new PluginRejection(e.code, `${name}: ${e.code}`); } return inner(p); };
    }
  }
}
export const makeProbeAware = (m: any) => new ProbeAware(m);

/** A retriever that follows its contract, as a class a deployment can name in its configuration: an in-memory keyword index, with the contract's errors. */
export class DummyRetriever {
  docs = new Map<string, string>();
  constructor(public config: any = {}) {}
  describe() { return { id: this.config.id ?? "dummy-retriever", kind: "retrieval", version: this.config.version ?? "1.0.0" }; }
  health() { return { ok: true }; }
  index(p: any) { if (p.documents.some((d: any) => !d.id || !d.text)) throw new PluginRejection("invalid_argument", "a document has an empty id or text"); for (const d of p.documents) this.docs.set(d.id, d.text); return { indexed: p.documents.length, index_version: `v${this.docs.size}` }; }
  query(p: any) {
    if (p.k < 1 || !p.query) throw new PluginRejection("invalid_argument", "k must be at least 1 and the query not empty"); if (p.user === "__no_access__") throw new PluginRejection("permission_denied", "no access");
    const words = p.query.toLowerCase().split(/\W+/).filter(Boolean), scored = [...this.docs].map(([id, text]) => ({ id, text, score: words.filter((w: string) => text.toLowerCase().includes(w)).length })).filter((x) => x.score > 0).sort((a, b) => b.score - a.score || (a.id < b.id ? -1 : 1));
    return { passages: scored.slice(0, p.k).map((x) => ({ ...x, source: "dummy-index", permissions: { visibility: "all" } })) };
  }
  forget(p: any) { if (!p.ids.length) throw new PluginRejection("invalid_argument", "no ids"); let n = 0; for (const id of p.ids) if (this.docs.delete(id)) n++; return { removed: n }; }
}
export class HalfRetriever { describe() { return { id: "half", kind: "retrieval", version: "1.0.0" }; } health() { return { ok: true }; } }
