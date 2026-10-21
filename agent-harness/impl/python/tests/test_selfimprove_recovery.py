"""§19.7 test 11: kill the campaign at event boundaries; the resumed run must match an uninterrupted one. No double-counted budget, no double promotion."""
from collections import Counter
import pytest
from si_world import *

CID = "01JCRASHSWEEP000000000000A"
SCRIPT = lambda: {(1, 1): proposal(edit_hint("HINT+ verify tool output carefully")), (1, 2): proposal(edit_hint("HINT- skip verification entirely")),
                  (2, 1): proposal(edit_hint("HINT+ plan the steps ahead of time", "Be careful.\nHINT+ verify tool output carefully\n\n".replace("\n\n", "\n"))),
                  (2, 2): proposal(edit_hint("OVERFIT memorise the examples")), "default": None}
KEY_TYPES = ("proposal.created", "proposal.rejected", "candidate.evaluated", "gate.decided", "heldout.queried", "promotion.committed", "campaign.round_completed", "surface.integrity_checked")


class Crash(BaseException):
    """Simulated SIGKILL: a BaseException, so nothing in the controller can swallow it."""


def snapshot(w, s):
    ev = w.store.events(w.cid)
    keyed = Counter((e["correlation_id"], e["type"]) for e in ev if e["correlation_id"])
    return {"head": w.config.head(), "promotions": [(p["promotion_id"], p["commit"]) for p in s["promotions"]], "stopped": s["stopped"],
            "used": {k: v for k, v in s["used"].items() if k != "time_s"}, "heldout_usage": w.evalsets.usage("set-h"), "scoring_usage": w.evalsets.usage("set-s"),
            "types": {t: sum(1 for e in ev if e["type"] == t) for t in KEY_TYPES}, "dups": [k for k, n in keyed.items() if n > 1], "chain": w.store.verify(w.cid)["ok"],
            "decisions": sorted((e["payload"]["proposal_id"], e["payload"]["accepted"]) for e in ev if e["type"] == "gate.decided")}


async def run_reference(tmp):
    w = World(tmp, SCRIPT(), cid=CID); await w.setup()
    s = await w.controller().run(); snap = snapshot(w, s); n = len(w.store.events(w.cid)); w.close()
    return snap, n


async def run_with_crash(tmp, k):
    w = World(tmp, SCRIPT(), cid=CID); await w.setup()
    orig, count = w.writer.submit, [0]
    async def submit(d):
        ev = await orig(d)
        if d["run_id"] == CID:
            count[0] += 1
            if count[0] == k:
                raise Crash(ev["type"])
        return ev
    w.writer.submit = submit
    crashed = None
    try:
        await w.controller().run()
    except Crash as c:
        crashed = str(c)
    w.close()
    w2 = World(tmp, SCRIPT(), cid=CID); await w2.setup()                       # a fresh process on the same disk
    s = await w2.controller().run(); snap = snapshot(w2, s); w2.close()
    return snap, crashed


async def test_reference_campaign_is_a_meaningful_scenario(tmp_path):
    snap, n = await run_reference(tmp_path)
    assert n > 40 and len(snap["promotions"]) == 2 and snap["types"]["proposal.rejected"] == 0 and snap["types"]["gate.decided"] == 4
    assert snap["heldout_usage"] == 3 and snap["dups"] == [] and snap["chain"]


async def test_kill_at_every_event_boundary_resumes_to_the_same_outcome(tmp_path):
    ref, n = await run_reference(tmp_path / "ref")
    points = list(range(1, n + 1))                      # every single event boundary
    seen_types = set()
    for k in sorted(set(points)):
        snap, crashed = await run_with_crash(tmp_path / f"k{k}", k)
        seen_types.add(crashed)
        assert snap == ref, f"crash after event #{k} ({crashed}) diverged:\n{ {x: (snap[x], ref[x]) for x in ref if snap[x] != ref[x]} }"
    assert {"proposal.created", "candidate.evaluated", "gate.decided", "heldout.queried", "promotion.committed", "campaign.round_completed"} <= seen_types   # boundaries of every kind were exercised
