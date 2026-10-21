import fs from "node:fs";
import path from "node:path";
import { DatabaseSync } from "node:sqlite";
import { describe, expect, it } from "vitest";
import { EventStore, EventWriter, ProjectionManager, UnknownRunError } from "../src/eventlog.js";
import { NAMES, foldAll } from "../src/projections.js";
import { BlobStore } from "../src/blobs.js";
import { computeHash, FakeClock, ulid } from "../src/util.js";
import { deleteContent } from "../src/retention.js";
import { tmpdir } from "./helpers.js";

function setup() { const dir = tmpdir(), s = new EventStore(path.join(dir, "l.db")), w = new EventWriter(s, new FakeClock()).start(); return { dir, s, w }; }
const created = async (w: EventWriter, rid = ulid()) => { await w.submit({ run_id: rid, type: "run.created", payload: { owner: "u", profile: "core", config_commit: "c" } }); return rid; };

describe("event log", () => {
  it("WAL + synchronous FULL", () => { const { s } = setup(); expect(s.pragma("journal_mode")).toBe("wal"); expect(s.pragma("synchronous")).toBe(2); });
  it("seq starts at 1, increments by exactly 1, chain verifies and matches an independent recompute", async () => {
    const { s, w } = setup(), rid = await created(w);
    for (let i = 0; i < 10; i++) await w.submit({ run_id: rid, type: "user.message", payload: { i, x: 1.5 } });
    const evs = s.events(rid); expect(evs.map((e) => e.seq)).toEqual(Array.from({ length: 11 }, (_, i) => i + 1));
    expect(s.verify(rid).ok).toBe(true); expect(computeHash(evs[0].hash, evs[1])).toBe(evs[1].hash);
  });
  it("tampering is detected; the append-only trigger blocks casual edits; partial verification covers only the tail", async () => {
    const { s, w } = setup(), rid = await created(w);
    for (let i = 0; i < 5; i++) await w.submit({ run_id: rid, type: "user.message", payload: { i } });
    expect(() => s.db.exec(`UPDATE events SET payload='{}' WHERE run_id='${rid}' AND seq=3`)).toThrow(/append-only/);
    expect(() => s.db.exec(`DELETE FROM events WHERE run_id='${rid}'`)).toThrow(/append-only/);
    const c = new DatabaseSync(s.path); c.exec("DROP TRIGGER events_no_update"); c.exec(`UPDATE events SET payload='{"i":99}' WHERE run_id='${rid}' AND seq=3`); c.close();
    expect(s.verify(rid)).toMatchObject({ ok: false, bad_seq: 3, reason: "hash mismatch" }); expect(s.verify(rid, 2).ok).toBe(true); expect(s.verify(rid, 4).ok).toBe(false);
  });
  it("isolates a failing item inside a batch and rejects bad types / unsafe integers", async () => {
    const { s, w } = setup(), rid = await created(w);
    const r = await Promise.allSettled([w.submit({ run_id: rid, type: "user.message", payload: { i: 1 } }), w.submit({ run_id: ulid(), type: "user.message", payload: {} }), w.submit({ run_id: rid, type: "user.message", payload: { i: 2 } })]);
    expect(r[0].status).toBe("fulfilled"); expect((r[1] as any).reason).toBeInstanceOf(UnknownRunError); expect((r[2] as any).value.seq).toBe(3); expect(s.verify(rid).ok).toBe(true);
    await expect(w.submit({ run_id: rid, type: "BadType", payload: {} })).rejects.toThrow(); await expect(w.submit({ run_id: rid, type: "a.b", payload: { n: 2 ** 60 } })).rejects.toThrow(/2\^53/);
    await w.submit({ run_id: rid, type: "a.b", payload: { n: String(2n ** 60n) } });
  });
  it("group commit: concurrent submissions share transactions and keep per-run order", async () => {
    const { s, w } = setup(), rids = await Promise.all(Array.from({ length: 5 }, () => created(w)));
    await Promise.all(rids.map(async (r) => { for (let i = 0; i < 40; i++) await w.submit({ run_id: r, type: "user.message", payload: { i } }); }));
    for (const r of rids) { expect(s.events(r).slice(1).map((e) => e.payload.i)).toEqual(Array.from({ length: 40 }, (_, i) => i)); expect(s.verify(r).ok).toBe(true); }
    expect(w.commits).toBeLessThan(5 * 41);
  });
  it("durable before ack: a fresh connection sees every acknowledged event", async () => {
    const { s, w } = setup(), rid = await created(w); await w.submit({ run_id: rid, type: "user.message", payload: { a: 1 } });
    expect(new EventStore(s.path).events(rid)).toHaveLength(2);
  });
  it("FTS5 bm25 ranking and sanitised queries", async () => {
    const { s, w } = setup(), rid = await created(w);
    await w.submit({ run_id: rid, type: "user.message", payload: { search_text: "the database migration failed on step three" } }); await w.submit({ run_id: rid, type: "user.message", payload: { search_text: "lunch menu for friday" } });
    expect(s.search(rid, 'migration failing?? (step) "quoted" AND OR')[0].seq).toBe(2); expect(s.search(rid, "!!!")).toEqual([]);
  });
  it("projection cache refolds on version mismatch and equals a full rebuild", async () => {
    const { s, w } = setup(), rid = await created(w), pm = new ProjectionManager(s, w);
    await w.submit({ run_id: rid, type: "run.state_changed", payload: { from: "CREATED", to: "PROVISIONING" } });
    expect((await pm.get(rid, "run_state")).state).toBe("PROVISIONING"); expect(s.getProjection(rid, "run_state")![0]).toBe(2);
    await w.execute("UPDATE projections SET data=? WHERE run_id=? AND name='run_state'", [JSON.stringify({ version: 0, state: { state: "WRONG" } }), rid]);
    expect((await pm.get(rid, "run_state")).state).toBe("PROVISIONING");
    for (const n of NAMES) expect(await pm.rebuild(rid, n)).toEqual(foldAll(n, s.events(rid)));
  });
  it("content deletion removes blobs and FTS rows but keeps the chain intact", async () => {
    const { dir, s, w } = setup(), blobs = new BlobStore(path.join(dir, "b")), rid = await created(w), h = blobs.put("sensitive text");
    await w.submit({ run_id: rid, type: "tool.call.completed", payload: { result_ref: h, search_text: "sensitive text" } });
    const clock = new FakeClock(), fake: any = { rm: { get: () => ({ emit: (t: string, p: any, a: any) => w.submit({ run_id: rid, type: t, payload: p, actor: a }) }) }, blobs, writer: w, store: s, clock };
    expect(await deleteContent(fake, rid, [h], "admin", "gdpr")).toMatchObject({ deleted: 1 }); expect(blobs.has(h)).toBe(false); expect(s.search(rid, "sensitive")).toEqual([]);
    expect(s.events(rid).pop()!.type).toBe("content.deleted"); expect(s.verify(rid).ok).toBe(true); void clock; void fs;
  });
  it("blob store is content-addressed, atomic and verifies on read", () => {
    const b = new BlobStore(tmpdir()), h = b.put("hello"); expect(b.put("hello")).toBe(h); expect(b.get(h).toString()).toBe("hello");
    fs.writeFileSync(b.p(h), "EVIL"); expect(() => b.get(h)).toThrow(/integrity/);
  });
});
