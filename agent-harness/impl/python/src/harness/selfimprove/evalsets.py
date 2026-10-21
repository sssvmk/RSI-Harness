"""EvalSetRegistry (§17.4): disjointness, near-duplicate detection, held-out query caps, reproducible seeds."""
from __future__ import annotations
import hashlib, re
from ..canonical import canonicalize, sha256_hex
from ..clock import format_ts

ROLES = ("feedback", "scoring", "heldout", "confirm", "transfer", "meta")
VAULTED = ("heldout", "confirm", "transfer", "meta")


class EvalSetError(ValueError):
    def __init__(self, code: str, msg: str):
        super().__init__(msg)
        self.code = code


def shingle_set(text: str, k: int = 3) -> set[str]:
    t = re.findall(r"[a-z0-9]+", text.lower())
    return {" ".join(t[i:i + k]) for i in range(max(1, len(t) - k + 1))} if t else set()


def jaccard(a: set, b: set) -> float:
    if not a and not b:
        return 1.0
    return len(a & b) / len(a | b)


def eval_seed(campaign_id: str, rnd: int, candidate_id: str, set_id: str) -> str:
    """First 8 bytes (big-endian) of SHA-256(campaign:round:candidate:set), as a decimal string (portable across languages)."""
    return str(int(sha256_hex(f"{campaign_id}:{rnd}:{candidate_id}:{set_id}")[:16], 16))


class EvalSetRegistry:
    def __init__(self, store, writer, clock, max_queries_per_set: int = 20, near_dup_threshold: float = 0.9):
        self.store, self.writer, self.clock = store, writer, clock
        self.max_q, self.thr = max_queries_per_set, near_dup_threshold
        self.sets: dict[str, dict] = {}     # set_id -> {role, ids:set, shingles:{id:set}}

    async def register(self, set_id: str, role: str, instances: list[dict], public: bool = False) -> str:
        if role not in ROLES:
            raise EvalSetError("evalset.bad_role", f"unknown role {role}")
        if not instances or any("id" not in i or "text" not in i for i in instances):
            raise EvalSetError("evalset.invalid", "instances need id and text")
        if public and role in VAULTED:
            raise EvalSetError("evalset.public_cannot_be_held_out", f"instances from public tests cannot be registered as a {role} set (public tests are development aids)")
        ids = {i["id"] for i in instances}
        if len(ids) != len(instances):
            raise EvalSetError("evalset.duplicate_ids", "instance ids must be unique within a set")
        sh = {i["id"]: shingle_set(i["text"]) for i in instances}
        for oid, o in self.sets.items():
            if o["role"] == role:
                continue                                    # only *other* roles must be disjoint
            inter = ids & o["ids"]
            if inter:
                raise EvalSetError("evalset.id_overlap", f"instance ids overlap set {oid} ({o['role']}): {sorted(inter)[:3]}")
            for iid, s in sh.items():
                for oiid, os_ in o["shingles"].items():
                    if jaccard(s, os_) >= self.thr:
                        raise EvalSetError("evalset.near_duplicate", f"instance {iid} is a near-duplicate of {oiid} in set {oid} ({o['role']})")
        content_hash = sha256_hex(canonicalize(sorted(instances, key=lambda i: i["id"])))
        self.sets[set_id] = {"role": role, "ids": ids, "shingles": sh}
        await self.writer.execute("INSERT OR REPLACE INTO eval_sets(set_id,role,content_hash,n_instances,status,created_at) VALUES(?,?,?,?,'active',?)",
                                  (set_id, role, content_hash, len(instances), format_ts(self.clock.now_ms())))
        return content_hash

    def usage(self, set_id: str) -> int:
        return self.store.conn().execute("SELECT COUNT(*) c FROM eval_set_usage WHERE set_id=?", (set_id,)).fetchone()["c"]

    def status(self, set_id: str) -> str:
        r = self.store.conn().execute("SELECT status FROM eval_sets WHERE set_id=?", (set_id,)).fetchone()
        if r is None:
            raise EvalSetError("evalset.unknown", set_id)
        return r["status"]

    async def record_query(self, set_id: str, campaign_id: str, candidate_id: str | None) -> int:
        """Counts one held-out/confirm query; the set is exhausted for gating once it reaches the cap (H-6.4-repeatedly-queried-scorers)."""
        c = self.store.conn()
        if c.execute("SELECT 1 FROM eval_set_usage WHERE set_id=? AND campaign_id=? AND candidate_id IS ?", (set_id, campaign_id, candidate_id)).fetchone():
            return self.usage(set_id)                       # the same (set, campaign, candidate) query is counted once, so a replay after a crash cannot double count
        if self.status(set_id) != "active":
            raise EvalSetError("evalset.exhausted", f"{set_id} is {self.status(set_id)}; a human must register a fresh set")
        await self.writer.execute("INSERT INTO eval_set_usage(set_id,campaign_id,candidate_id,at) VALUES(?,?,?,?)", (set_id, campaign_id, candidate_id, format_ts(self.clock.now_ms())))
        n = self.usage(set_id)
        if n >= self.max_q:
            await self.writer.execute("UPDATE eval_sets SET status='exhausted' WHERE set_id=?", (set_id,))
        return n
