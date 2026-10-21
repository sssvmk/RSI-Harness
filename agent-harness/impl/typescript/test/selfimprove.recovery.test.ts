/** §19.7 test 11: kill the campaign at every event boundary; the resumed run must match an uninterrupted one. No double-counted budget, no double promotion. */
import { afterEach, describe, expect, it } from "vitest";
import { HINT, World, editHint, proposal } from "./siWorld.js";
import { tmpdir } from "./helpers.js";
import path from "node:path";

const CID = "01JCRASHSWEEP000000000000A";
const script = () => ({ "1,1": HINT("HINT+ verify tool output carefully"), "1,2": HINT("HINT- skip verification entirely"), "2,1": proposal(editHint("HINT+ plan the steps ahead of time")), "2,2": HINT("OVERFIT memorise the examples"), default: null });
const KEY_TYPES = ["proposal.created", "proposal.rejected", "candidate.evaluated", "gate.decided", "heldout.queried", "promotion.committed", "campaign.round_completed", "surface.integrity_checked"];
class Crash extends Error {}
const worlds: World[] = [];
afterEach(() => { while (worlds.length) worlds.pop()!.close(); });

function snapshot(w: World, s: any) {
  const ev = w.store.events(w.cid), keyed = new Map<string, number>();
  for (const e of ev) if (e.correlation_id) keyed.set(`${e.correlation_id}|${e.type}`, (keyed.get(`${e.correlation_id}|${e.type}`) ?? 0) + 1);
  const { time_s: _t, ...used } = s.used;
  return { head: w.config.head(), promotions: s.promotions.map((p: any) => [p.promotion_id, p.commit]), stopped: s.stopped, used, heldout_usage: w.evalsets.usage("set-h"), scoring_usage: w.evalsets.usage("set-s"),
    types: Object.fromEntries(KEY_TYPES.map((t) => [t, ev.filter((e: any) => e.type === t).length])), dups: [...keyed].filter(([, n]) => n > 1).map(([k]) => k), chain: w.store.verify(w.cid).ok,
    decisions: ev.filter((e: any) => e.type === "gate.decided").map((e: any) => [e.payload.proposal_id, e.payload.accepted]).sort() };
}
async function reference(dir: string) { const w = new World(dir, script(), { cid: CID }); worlds.push(w); await w.setup(); const s = await w.controller().run(), snap = snapshot(w, s), n = w.store.events(w.cid).length; w.close(); return { snap, n }; }
async function withCrash(dir: string, k: number) {
  let w = new World(dir, script(), { cid: CID }); worlds.push(w); await w.setup();
  const orig = w.writer.submit.bind(w.writer); let count = 0, crashed = "";
  w.writer.submit = async (d: any) => { const ev = await orig(d); if (d.run_id === CID && ++count === k) { crashed = ev.type; throw new Crash(ev.type); } return ev; };
  try { await w.controller().run(); } catch (e) { if (!(e instanceof Crash)) throw e; }
  w.close();
  w = new World(dir, script(), { cid: CID }); worlds.push(w); await w.setup();          // a fresh process on the same disk
  const s = await w.controller().run(), snap = snapshot(w, s); w.close(); return { snap, crashed };
}
describe("campaign crash recovery", () => {
  it("the reference campaign is a meaningful scenario", async () => {
    const { snap, n } = await reference(tmpdir());
    expect(n).toBeGreaterThan(40); expect(snap.promotions).toHaveLength(2); expect(snap.types["proposal.rejected"]).toBe(0); expect(snap.types["gate.decided"]).toBe(4); expect(snap.heldout_usage).toBe(3); expect(snap.dups).toEqual([]); expect(snap.chain).toBe(true);
  });
  it("kill at every event boundary resumes to the same outcome (same main commit id, promotions, budgets, held-out usage; no duplicate events)", { timeout: 280000 }, async () => {
    const root = tmpdir(), { snap: ref, n } = await reference(path.join(root, "ref")), seen = new Set<string>();
    for (let k = 1; k <= n; k++) {
      const { snap, crashed } = await withCrash(path.join(root, `k${k}`), k); seen.add(crashed);
      expect(snap, `crash after event #${k} (${crashed}) diverged`).toEqual(ref);
    }
    for (const t of ["proposal.created", "candidate.evaluated", "gate.decided", "heldout.queried", "promotion.committed", "campaign.round_completed"]) expect(seen.has(t), t).toBe(true);   // boundaries of every kind were exercised
  });
});
