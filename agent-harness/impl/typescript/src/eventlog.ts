/** SQLite Event Log (design §5) on node:sqlite: WAL + synchronous FULL, single writer with group commit. */
import { DatabaseSync } from "node:sqlite";
import { FakeClock, SystemClock, Clock, assertSafeInts, computeHash, formatTs, genesisHash, parseTs } from "./util.js";
import { VERSIONS, foldAll } from "./projections.js";

export const DDL = `
CREATE TABLE IF NOT EXISTS runs(run_id TEXT PRIMARY KEY, created_at TEXT NOT NULL, owner TEXT NOT NULL, profile TEXT NOT NULL, task_id TEXT, config_commit TEXT NOT NULL, state TEXT NOT NULL, last_seq INTEGER NOT NULL DEFAULT 0, terminal_reason TEXT);
CREATE TABLE IF NOT EXISTS events(run_id TEXT NOT NULL REFERENCES runs(run_id), seq INTEGER NOT NULL, ts TEXT NOT NULL, type TEXT NOT NULL, actor_kind TEXT NOT NULL, actor_id TEXT NOT NULL, causation_seq INTEGER, correlation_id TEXT, schema_version INTEGER NOT NULL, payload TEXT NOT NULL, prev_hash TEXT NOT NULL, hash TEXT NOT NULL, PRIMARY KEY(run_id, seq)) WITHOUT ROWID;
CREATE INDEX IF NOT EXISTS events_by_type ON events(run_id, type, seq);
CREATE INDEX IF NOT EXISTS events_by_corr ON events(run_id, correlation_id);
CREATE VIRTUAL TABLE IF NOT EXISTS events_fts USING fts5(text, run_id UNINDEXED, seq UNINDEXED, type UNINDEXED, tokenize='porter unicode61');
CREATE TABLE IF NOT EXISTS projections(run_id TEXT NOT NULL, name TEXT NOT NULL, as_of_seq INTEGER NOT NULL, data TEXT NOT NULL, PRIMARY KEY(run_id, name));
CREATE TABLE IF NOT EXISTS approvals(approval_id TEXT PRIMARY KEY, run_id TEXT NOT NULL, tool_call_id TEXT NOT NULL, rule_id TEXT, status TEXT NOT NULL, requested_at TEXT NOT NULL, decided_by TEXT, decided_at TEXT, expires_at TEXT NOT NULL, scope TEXT);
CREATE TABLE IF NOT EXISTS standing_approvals(id TEXT PRIMARY KEY, run_id TEXT, tool TEXT NOT NULL, args_pattern TEXT, expires_at TEXT NOT NULL, max_count INTEGER NOT NULL, used INTEGER NOT NULL DEFAULT 0, granted_by TEXT NOT NULL);
CREATE TABLE IF NOT EXISTS snapshots(snapshot_id TEXT PRIMARY KEY, run_id TEXT NOT NULL, taken_at_seq INTEGER NOT NULL, manifest_hash TEXT NOT NULL, kind TEXT NOT NULL, created_at TEXT NOT NULL);
CREATE TABLE IF NOT EXISTS blobs(hash TEXT PRIMARY KEY, size INTEGER NOT NULL, created_at TEXT NOT NULL, deleted_at TEXT);
CREATE TABLE IF NOT EXISTS content_access(id TEXT PRIMARY KEY, run_id TEXT, who TEXT NOT NULL, role TEXT NOT NULL, what TEXT NOT NULL, at TEXT NOT NULL);
CREATE TABLE IF NOT EXISTS definition_versions(def_id TEXT NOT NULL, version INTEGER NOT NULL, sha256 TEXT NOT NULL, kind TEXT NOT NULL, published_by TEXT NOT NULL, published_at TEXT NOT NULL, PRIMARY KEY(def_id, version));
CREATE TRIGGER IF NOT EXISTS defs_no_update BEFORE UPDATE ON definition_versions BEGIN SELECT RAISE(ABORT,'definitions are immutable'); END;
CREATE TRIGGER IF NOT EXISTS defs_no_delete BEFORE DELETE ON definition_versions BEGIN SELECT RAISE(ABORT,'definitions are immutable'); END;
CREATE TRIGGER IF NOT EXISTS events_no_update BEFORE UPDATE ON events BEGIN SELECT RAISE(ABORT,'events are append-only'); END;
CREATE TRIGGER IF NOT EXISTS events_no_delete BEFORE DELETE ON events BEGIN SELECT RAISE(ABORT,'events are append-only'); END;
`;
const TYPE_RE = /^[a-z_]+(\.[a-z_]+)+$/;
const ACTOR_KINDS = new Set(["agent", "control_plane", "user", "system", "evaluator"]);
export class UnknownRunError extends Error {}

export function ftsQuery(text: string, maxTerms = 12): string {
  return (text.match(/[A-Za-z0-9_]{2,}/g) ?? []).slice(0, maxTerms).map((w) => `"${w}"`).join(" OR ");
}
export function rowToEvent(r: any) {
  return { run_id: r.run_id, seq: Number(r.seq), ts: r.ts, type: r.type, actor: { kind: r.actor_kind, id: r.actor_id }, causation_seq: r.causation_seq === null ? null : Number(r.causation_seq),
    correlation_id: r.correlation_id, schema_version: Number(r.schema_version), payload: JSON.parse(r.payload), prev_hash: r.prev_hash, hash: r.hash };
}

export class EventStore {
  db: DatabaseSync;
  constructor(public path: string) {
    this.db = new DatabaseSync(path);
    this.db.exec("PRAGMA journal_mode=WAL; PRAGMA synchronous=FULL; PRAGMA foreign_keys=ON;");
    this.db.exec(DDL);
  }
  close() { try { this.db.close(); } catch { /* already closed */ } }
  pragma(name: string): any { return Object.values(this.db.prepare(`PRAGMA ${name}`).get() as any)[0]; }
  all(sql: string, ...params: any[]): any[] { return this.db.prepare(sql).all(...params) as any[]; }
  get(sql: string, ...params: any[]): any { return this.db.prepare(sql).get(...params) as any; }
  events(runId: string, o: { afterSeq?: number; types?: string[]; limit?: number; upToSeq?: number } = {}) {
    let q = "SELECT * FROM events WHERE run_id=? AND seq>?"; const args: any[] = [runId, o.afterSeq ?? 0];
    if (o.upToSeq !== undefined) { q += " AND seq<=?"; args.push(o.upToSeq); }
    if (o.types?.length) { q += ` AND type IN (${o.types.map(() => "?").join(",")})`; args.push(...o.types); }
    q += " ORDER BY seq"; if (o.limit) { q += " LIMIT ?"; args.push(o.limit); }
    return this.all(q, ...args).map(rowToEvent);
  }
  getEvent(runId: string, seq: number) { const r = this.get("SELECT * FROM events WHERE run_id=? AND seq=?", runId, seq); return r ? rowToEvent(r) : null; }
  lastSeq(runId: string): number { const r = this.get("SELECT last_seq FROM runs WHERE run_id=?", runId); return r ? Number(r.last_seq) : 0; }
  headHash(runId: string): string { const r = this.get("SELECT hash FROM events WHERE run_id=? ORDER BY seq DESC LIMIT 1", runId); return r ? r.hash : genesisHash(runId); }
  runRow(runId: string) { const r = this.get("SELECT * FROM runs WHERE run_id=?", runId); return r ? { ...r, last_seq: Number(r.last_seq) } : null; }
  listRuns() { return this.all("SELECT * FROM runs ORDER BY run_id").map((r) => ({ ...r })); }
  search(runId: string, text: string, k = 5) {
    const q = ftsQuery(text); if (!q) return [];
    return this.all("SELECT seq, type, text, bm25(events_fts) AS rank FROM events_fts WHERE events_fts MATCH ? AND run_id=? ORDER BY rank LIMIT ?", q, runId, k).map((r) => ({ seq: Number(r.seq), type: r.type, text: r.text, rank: r.rank }));
  }
  verify(runId: string, lastN?: number) {
    const last = this.lastSeq(runId), start = lastN === undefined ? 1 : Math.max(1, last - lastN + 1);
    const evs = this.events(runId, { afterSeq: start - 1 });
    let prev: string | null = start > 1 ? (this.getEvent(runId, start - 1)?.hash ?? null) : genesisHash(runId);
    let checked = 0;
    for (let i = 0; i < evs.length; i++) {
      const ev = evs[i];
      if (ev.seq !== start + i) return { ok: false, checked, bad_seq: start + i, reason: "sequence gap" };
      if (ev.prev_hash !== prev) return { ok: false, checked, bad_seq: ev.seq, reason: "prev_hash mismatch" };
      if (computeHash(prev!, ev) !== ev.hash) return { ok: false, checked, bad_seq: ev.seq, reason: "hash mismatch" };
      prev = ev.hash; checked++;
    }
    if (lastN === undefined && evs.length && evs[evs.length - 1].seq !== last) return { ok: false, checked, bad_seq: last, reason: "runs.last_seq mismatch" };
    return { ok: true, checked, bad_seq: null as number | null, reason: null as string | null };
  }
  getProjection(runId: string, name: string): [number, any] | null { const r = this.get("SELECT as_of_seq, data FROM projections WHERE run_id=? AND name=?", runId, name); return r ? [Number(r.as_of_seq), JSON.parse(r.data)] : null; }
}

type Item = { op: any[]; resolve: (v: any) => void; reject: (e: any) => void };
export class EventWriter {
  private queue: Item[] = []; private scheduled = false; private cache = new Map<string, [number, string, number]>(); commits = 0;
  constructor(public store: EventStore, public clock: Clock = new SystemClock()) {}
  start() { return this; }
  closed = false;
  /** After close the writer goes inert (like a dead process): orphaned loops hang harmlessly instead of throwing. */
  close() { this.closed = true; }
  private enqueue(op: any[]): Promise<any> {
    if (this.closed) return new Promise(() => {});
    return new Promise((resolve, reject) => { this.queue.push({ op, resolve, reject }); if (!this.scheduled) { this.scheduled = true; queueMicrotask(() => this.flush()); } });
  }
  submit(draft: any): Promise<any> { return this.enqueue(["event", draft]); }
  execute(sql: string, params: any[] = [], fetch = false): Promise<any> { return this.enqueue(["sql", sql, params, fetch]); }
  putProjection(runId: string, name: string, asOf: number, data: any) { return this.execute("INSERT OR REPLACE INTO projections(run_id,name,as_of_seq,data) VALUES(?,?,?,?)", [runId, name, asOf, JSON.stringify(data)]); }
  /** Group commit: everything submitted in the same tick shares one transaction; callers are acknowledged only after COMMIT. */
  private flush() {
    this.scheduled = false;
    const batch = this.queue.splice(0), db = this.store.db, pending = new Map<string, [number, string, number]>();
    const results: [Item, any, any][] = [];
    try {
      db.exec("BEGIN IMMEDIATE");
      for (const it of batch) {
        try {
          db.exec("SAVEPOINT item");
          let res: any;
          if (it.op[0] === "event") res = this.append(it.op[1], pending);
          else { const st = db.prepare(it.op[1]); if (it.op[3]) res = st.all(...it.op[2]); else res = Number(st.run(...it.op[2]).changes); }
          db.exec("RELEASE item"); results.push([it, res, null]);
        } catch (e) { db.exec("ROLLBACK TO item"); db.exec("RELEASE item"); results.push([it, null, e]); }
      }
      db.exec("COMMIT");
      for (const [k, v] of pending) this.cache.set(k, v);
      this.commits++;
    } catch (e) { try { db.exec("ROLLBACK"); } catch { /* ignore */ } for (const it of batch) it.reject(e); return; }
    for (const [it, res, err] of results) err ? it.reject(err) : it.resolve(res);
  }
  private head(runId: string, pending: Map<string, [number, string, number]>): [number, string, number] {
    return pending.get(runId) ?? this.cache.get(runId) ?? (() => {
      const r = this.store.get("SELECT seq, hash, ts FROM events WHERE run_id=? ORDER BY seq DESC LIMIT 1", runId);
      return r ? ([Number(r.seq), r.hash, parseTs(r.ts)] as [number, string, number]) : ([0, genesisHash(runId), 0] as [number, string, number]);
    })();
  }
  private append(d: any, pending: Map<string, [number, string, number]>) {
    const db = this.store.db, { run_id: runId, type } = d, actor = d.actor ?? { kind: "control_plane", id: "harness" }, payload = d.payload ?? {};
    if (!TYPE_RE.test(type)) throw new Error(`invalid event type ${type}`);
    if (!ACTOR_KINDS.has(actor.kind)) throw new Error(`invalid actor kind ${actor.kind}`);
    if (payload === null || typeof payload !== "object" || Array.isArray(payload)) throw new Error("payload must be an object");
    assertSafeInts(payload);
    const known = this.store.get("SELECT 1 AS x FROM runs WHERE run_id=?", runId);
    const [seq0, prev, lastTs] = this.head(runId, pending);
    const tsMs = Math.max(this.clock.nowMs(), lastTs);
    if (type === "run.created") {
      if (known) throw new Error("run already exists");
      db.prepare("INSERT INTO runs(run_id,created_at,owner,profile,task_id,config_commit,state,last_seq) VALUES(?,?,?,?,?,?,?,0)").run(runId, formatTs(tsMs), payload.owner, payload.profile, payload.task_id ?? null, payload.config_commit, "CREATED");
    } else if (!known) throw new UnknownRunError(runId);
    const seq = seq0 + 1;
    const ev: any = { run_id: runId, seq, ts: formatTs(tsMs), type, actor, causation_seq: d.causation_seq ?? null, correlation_id: d.correlation_id ?? null, schema_version: d.schema_version ?? 1, payload };
    ev.prev_hash = prev; ev.hash = computeHash(prev, ev);
    db.prepare("INSERT INTO events VALUES(?,?,?,?,?,?,?,?,?,?,?,?)").run(runId, seq, ev.ts, type, actor.kind, actor.id, ev.causation_seq, ev.correlation_id, ev.schema_version, JSON.stringify(payload), prev, ev.hash);
    db.prepare("UPDATE runs SET last_seq=?, state=COALESCE(?,state), terminal_reason=COALESCE(?,terminal_reason) WHERE run_id=?")
      .run(seq, type === "run.state_changed" ? payload.to : null, type === "run.terminated" ? payload.reason ?? null : null, runId);
    if (typeof payload.search_text === "string" && payload.search_text) db.prepare("INSERT INTO events_fts(text,run_id,seq,type) VALUES(?,?,?,?)").run(payload.search_text, runId, seq, type);
    pending.set(runId, [seq, ev.hash, tsMs]);
    return ev;
  }
}

export class ProjectionManager {
  constructor(public store: EventStore, public writer: EventWriter) {}
  async get(runId: string, name: string, persist = true) {
    const cached = this.store.getProjection(runId, name);
    let state: any, asOf = 0;
    if (cached && cached[1].version === VERSIONS[name]) { asOf = cached[0]; state = cached[1].state; }
    let evs = this.store.events(runId, { afterSeq: asOf });
    if (asOf > this.store.lastSeq(runId)) { state = undefined; asOf = 0; evs = this.store.events(runId); }
    state = foldAll(name, evs, state);
    if (evs.length && persist) await this.writer.putProjection(runId, name, evs[evs.length - 1].seq, { version: VERSIONS[name], state });
    return state;
  }
  async rebuild(runId: string, name: string) {
    const state = foldAll(name, this.store.events(runId));
    await this.writer.putProjection(runId, name, this.store.lastSeq(runId), { version: VERSIONS[name], state });
    return state;
  }
}
void FakeClock;
