/** Definition pinning (design §13.9, PRD §3.5). The control plane publishes definitions as immutable, content-hashed versions; a run's manifest lists exactly the versions it uses, and components resolve a definition
 *  only through the manifest. Before use the hash is verified, and a missing definition or a mismatch fails closed: nothing is substituted. Mirrors definitions.py. */
import fs from "node:fs";
import { canonicalize, formatTs, sha256Hex } from "./util.js";

const ID_RE = /^[a-z][a-z0-9_.\-]*$/;
const FLOATING = new Set(["latest", "stable", "head", "main", "master", "current", "default", "newest", "newer", "prod", "production"]);   // aliases that move; a pin must name an exact version
export class DefinitionError extends Error {}
export class DefinitionMismatch extends Error {
  constructor(public defId: string, public expected: string | null, public observed: string | null, public reason: string) { super(`${defId}: ${reason}`); }
}
export function encode(content: any): [Buffer, string] { return typeof content === "string" ? [Buffer.from(content, "utf8"), "text"] : [Buffer.from(canonicalize(content), "utf8"), "json"]; }
export function lintId(defId: string): void {
  if (!ID_RE.test(defId ?? "")) throw new DefinitionError(`definition id '${defId}' must be lower-case letters, digits, '.', '_' or '-' and start with a letter`);
  if (FLOATING.has(defId.split(".").at(-1)!)) throw new DefinitionError(`definition id '${defId}' is a floating alias`);
}
export class DefinitionStore {
  constructor(public svc: any) {}
  private rows(defId: string): any[] { return this.svc.store.db.prepare("SELECT version, sha256, kind FROM definition_versions WHERE def_id=? ORDER BY version").all(defId) as any[]; }
  /** Publish `content` as the next immutable version of `defId`. Publishing identical content again returns the existing version. */
  async publish(defId: string, content: any, by = "control_plane") {
    lintId(defId); const [data, kind] = encode(content), sha = sha256Hex(data);
    for (const r of this.rows(defId)) if (r.sha256 === sha) return { id: defId, version: r.version, sha256: sha };
    const rows = this.rows(defId), version = rows.length ? rows[rows.length - 1].version + 1 : 1;
    this.svc.blobs.put(data);
    await this.svc.writer.execute("INSERT INTO definition_versions(def_id,version,sha256,kind,published_by,published_at) VALUES(?,?,?,?,?,?)", [defId, version, sha, kind, by, formatTs(this.svc.clock.nowMs())]);
    return { id: defId, version, sha256: sha };
  }
  versions(defId: string) { return this.rows(defId).map((r) => ({ id: defId, version: r.version, sha256: r.sha256 })); }
  /** The definition's content, after checking it against the recorded hash. Anything else throws DefinitionMismatch. */
  fetch(defId: string, version: number, sha256: string): any {
    const row = this.svc.store.db.prepare("SELECT sha256, kind FROM definition_versions WHERE def_id=? AND version=?").get(defId, version) as any;
    if (!row) throw new DefinitionMismatch(defId, sha256, null, `version ${version} is not published`);
    if (row.sha256 !== sha256) throw new DefinitionMismatch(defId, sha256, row.sha256, "the published version has a different hash");
    let data: Buffer;
    try { data = this.svc.blobs.get(sha256); }
    catch {                                                  // the blob store refuses content that fails its own integrity check; report what is actually there
      let raw: Buffer; try { raw = fs.readFileSync(this.svc.blobs.p(sha256)); } catch { throw new DefinitionMismatch(defId, sha256, null, "content is missing"); }
      throw new DefinitionMismatch(defId, sha256, sha256Hex(raw), "content does not match its hash");
    }
    const observed = sha256Hex(data); if (observed !== sha256) throw new DefinitionMismatch(defId, sha256, observed, "content does not match its hash");
    return row.kind === "text" ? data.toString("utf8") : JSON.parse(data.toString("utf8"));
  }
}
/** The definitions one run uses, as its manifest lists them (plus any explicit, audited migrations). An inactive Pins (a run created without a manifest) pins nothing. */
export class Pins {
  active: boolean; byId = new Map<string, any>(); store: DefinitionStore;
  constructor(public svc: any, pins: any[] | null) { this.store = new DefinitionStore(svc); this.active = pins !== null; for (const p of pins ?? []) this.byId.set(p.id, { ...p }); }
  static load(svc: any, runId: string): Pins {
    const evs = svc.store.events(runId, { types: ["run.created", "definition.migrated"] }), created = evs.find((e: any) => e.type === "run.created"), m = created?.payload?.manifest;
    const pins = new Pins(svc, m ? m.fields?.definitions?.value ?? null : null);
    for (const e of evs) if (e.type === "definition.migrated") pins.byId.set(e.payload.id, { id: e.payload.id, version: e.payload.to_version, sha256: e.payload.to_sha256 });
    return pins;
  }
  has(defId: string) { return this.byId.has(defId); }
  /** The pinned content of `defId`, verified. A definition that is not in the manifest cannot be resolved: there is no 'latest' to fall back on. */
  resolve(defId: string): any {
    const p = this.byId.get(defId); if (!p) throw new DefinitionMismatch(defId, null, null, "not pinned in this run's manifest");
    return this.store.fetch(p.id, p.version, p.sha256);
  }
  /** For definitions the runtime builds itself (a tool's manifest): its current content must equal the pinned hash. */
  checkHash(defId: string, content: any): void {
    const p = this.byId.get(defId), observed = sha256Hex(encode(content)[0]);
    if (!p) throw new DefinitionMismatch(defId, null, observed, "not pinned in this run's manifest");
    if (observed !== p.sha256) throw new DefinitionMismatch(defId, p.sha256, observed, "the definition changed after the run was pinned");
  }
  verifyAll(): any[] { for (const p of this.byId.values()) this.resolve(p.id); return [...this.byId.values()].sort((a, b) => (a.id < b.id ? -1 : a.id > b.id ? 1 : 0)); }
  /** Move a running run to another published version. It is explicit and audited, and nothing else can change what a run uses. */
  async migrate(run: any, defId: string, toVersion: number, by: string, reason: string) {
    const rows = new Map(this.store.versions(defId).map((r) => [r.version, r])); if (!rows.has(toVersion)) throw new DefinitionError(`${defId} version ${toVersion} is not published`);
    const old = this.byId.get(defId) ?? {};
    await run.emit("definition.migrated", { id: defId, from_version: old.version ?? null, to_version: toVersion, from_sha256: old.sha256 ?? null, to_sha256: rows.get(toVersion)!.sha256, by, reason });
    const p = { id: defId, version: toVersion, sha256: rows.get(toVersion)!.sha256 }; this.byId.set(defId, p); return p;
  }
}
