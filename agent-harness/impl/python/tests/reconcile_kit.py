"""Scripted gate scenarios shared by the vector generator and the tests (the TypeScript tests replay the same scenarios)."""
from harness import reconcile as R


class Scripted(R.WorldAdapter):
    """An adapter whose reads, precondition result and effect check are scripted. A read of 'raise' raises, and an empty script reads None (unreadable)."""
    def __init__(self, reads, pre=True, eff=True): self.reads, self.pre, self.eff, self.n = list(reads), pre, eff, 0
    async def read_state(self, resource, ctx=None):
        self.n += 1
        if not self.reads: return None
        r = self.reads.pop(0)
        if r == "raise": raise RuntimeError("read failed")
        return r
    async def check_preconditions(self, action, actual): return self.pre
    async def effects_hold(self, action, before, after, result): return self.eff


async def run_scenario(sc: dict) -> dict:
    ev = []
    async def emit(t, p): ev.append([t, p])
    ad = sc.get("adapter"); adapter = Scripted(ad["reads"], ad.get("pre", True), ad.get("eff", True)) if ad is not None else None
    gate = R.Gate({"fake": adapter} if adapter else {}, sc.get("max_age_s", 900)); out = {}; act = sc["action"]
    call = sc["call"]
    if call in ("pre", "pre_post", "guarded"):
        pre = await gate.pre(emit, act, None, sc.get("now_s", 1000.0)); out["pre"] = {"status": pre["status"], "reason": pre["reason"], "actual": pre["actual"]}
        if pre["status"] == "ok" and call == "pre_post":
            post = await gate.post(emit, act, pre["actual"], "r"); out["post"] = {"status": post["status"], "reason": post.get("reason"), "observed": post["observed"]}
        if pre["status"] == "ok" and call == "guarded":
            did = []
            async def do(): did.append(1); return "done"
            try: await gate.guarded(act, pre["actual"], do); out["guarded"] = {"conflict": False, "executed": len(did)}
            except R.VersionConflict: out["guarded"] = {"conflict": True, "executed": len(did)}
    elif call == "establish":
        out["establish"] = await gate.establish_outcome(emit, act, sc.get("before"))
    out["events"] = ev; out["reads"] = adapter.n if adapter else 0
    return out
