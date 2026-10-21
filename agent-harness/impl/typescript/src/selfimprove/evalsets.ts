/** EvalSetRegistry (§17.4): disjointness, near-duplicate detection, held-out query caps, reproducible seeds. */
import { canonicalize, formatTs, sha256Hex } from "../util.js";
export const ROLES = ["feedback", "scoring", "heldout", "confirm", "transfer", "meta"];
export const VAULTED = ["heldout", "confirm", "transfer", "meta"];
export class EvalSetError extends Error { constructor(public code: string, msg: string) { super(msg); } }
export function shingleSet(text: string, k = 3): Set<string> {
  const t = text.toLowerCase().match(/[a-z0-9]+/g) ?? [], out = new Set<string>();
  if (!t.length) return out;
  for (let i = 0; i < Math.max(1, t.length - k + 1); i++) out.add(t.slice(i, i + k).join(" "));
  return out;
}
export function jaccard(a: Set<string>, b: Set<string>): number {
  if (!a.size && !b.size) return 1.0;
  let inter = 0; for (const x of a) if (b.has(x)) inter++;
  return inter / (a.size + b.size - inter);
}
/** First 8 bytes (big-endian) of SHA-256(campaign:round:candidate:set), as a decimal string. */
export const evalSeed = (campaignId: string, round: number, candidateId: string, setId: string) => BigInt("0x" + sha256Hex(`${campaignId}:${round}:${candidateId}:${setId}`).slice(0, 16)).toString();
export class EvalSetRegistry {
  sets = new Map<string, { role: string; ids: Set<string>; shingles: Map<string, Set<string>> }>();
  constructor(public store: any, public writer: any, public clock: any, public maxQueries = 20, public nearDup = 0.9) {}
  async register(setId: string, role: string, instances: { id: string; text: string }[], pub = false): Promise<string> {
    if (pub && VAULTED.includes(role)) throw new EvalSetError("evalset.public_cannot_be_held_out", `instances from public tests cannot be registered as a ${role} set (public tests are development aids)`);
    if (!ROLES.includes(role)) throw new EvalSetError("evalset.bad_role", `unknown role ${role}`);
    if (!instances.length || instances.some((i) => i.id === undefined || i.text === undefined)) throw new EvalSetError("evalset.invalid", "instances need id and text");
    const ids = new Set(instances.map((i) => i.id));
    if (ids.size !== instances.length) throw new EvalSetError("evalset.duplicate_ids", "instance ids must be unique within a set");
    const sh = new Map(instances.map((i) => [i.id, shingleSet(i.text)] as [string, Set<string>]));
    for (const [oid, o] of this.sets) {
      if (o.role === role) continue;   // only *other* roles must be disjoint
      const inter = [...ids].filter((x) => o.ids.has(x));
      if (inter.length) throw new EvalSetError("evalset.id_overlap", `instance ids overlap set ${oid} (${o.role}): ${inter.slice(0, 3)}`);
      for (const [iid, s] of sh) for (const [oiid, os] of o.shingles) if (jaccard(s, os) >= this.nearDup) throw new EvalSetError("evalset.near_duplicate", `instance ${iid} is a near-duplicate of ${oiid} in set ${oid} (${o.role})`);
    }
    const contentHash = sha256Hex(canonicalize([...instances].sort((a, b) => (a.id < b.id ? -1 : a.id > b.id ? 1 : 0))));
    this.sets.set(setId, { role, ids, shingles: sh });
    await this.writer.execute("INSERT OR REPLACE INTO eval_sets(set_id,role,content_hash,n_instances,status,created_at) VALUES(?,?,?,?,'active',?)", [setId, role, contentHash, instances.length, formatTs(this.clock.nowMs())]);
    return contentHash;
  }
  usage(setId: string): number { return Number(this.store.get("SELECT COUNT(*) c FROM eval_set_usage WHERE set_id=?", setId).c); }
  status(setId: string): string { const r = this.store.get("SELECT status FROM eval_sets WHERE set_id=?", setId); if (!r) throw new EvalSetError("evalset.unknown", setId); return r.status; }
  /** Counts one held-out/confirm query (once per set, campaign and candidate); the set is exhausted for gating at the cap. */
  async recordQuery(setId: string, campaignId: string, candidateId: string | null): Promise<number> {
    if (this.store.get("SELECT 1 x FROM eval_set_usage WHERE set_id=? AND campaign_id=? AND candidate_id IS ?", setId, campaignId, candidateId)) return this.usage(setId);
    if (this.status(setId) !== "active") throw new EvalSetError("evalset.exhausted", `${setId} is ${this.status(setId)}; a human must register a fresh set`);
    await this.writer.execute("INSERT INTO eval_set_usage(set_id,campaign_id,candidate_id,at) VALUES(?,?,?,?)", [setId, campaignId, candidateId, formatTs(this.clock.nowMs())]);
    const n = this.usage(setId);
    if (n >= this.maxQueries) await this.writer.execute("UPDATE eval_sets SET status='exhausted' WHERE set_id=?", [setId]);
    return n;
  }
}
