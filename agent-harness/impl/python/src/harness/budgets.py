"""Budget accounting and enforcement (design §7.4). State lives in the `budgets` projection."""
from __future__ import annotations
import asyncio, uuid
from contextlib import asynccontextmanager
from .eventlog.projections import active_time_ms

DIMENSIONS = ("turns", "tokens", "time_ms", "cost", "tools", "concurrency", "compute", "submissions")
TERMINATING = ("turns", "tokens", "time_ms", "cost", "tools")   # hard stops; submissions/concurrency/compute are guards on actions (§6.2)
RESERVABLE = ("turns", "tokens", "cost", "tools", "submissions")   # cumulative dimensions that are reserved before use (time is read from the clock; concurrency and compute are guards)
UNMETERED_DEFAULT = ("compute",)                                   # no platform accounting exists for compute here, so it is declared unmetered and never counted as zero
REQUIRED = {"core": ("turns", "tokens", "time_ms"), "standard": ("turns", "tokens", "time_ms", "cost", "tools"),
            "high": ("turns", "tokens", "time_ms", "cost", "tools", "concurrency", "compute", "submissions")}


def normalize_budgets(cfg: dict, warn_fraction: float = 0.8) -> dict:
    """Config names (time_s) -> canonical dimensions with {limit, warn_at}. The limits come from the agent's configuration, and so does when the agent is warned:
    `warn_fraction` (default 0.8) for every dimension and optional `warn_fractions` per dimension (keyed by the config name, e.g. time_s)."""
    cfg = cfg or {}
    wf, per = cfg.get("warn_fraction", warn_fraction), cfg.get("warn_fractions") or {}
    for name, f in [("warn_fraction", wf), *[(f"warn_fractions.{k}", v) for k, v in per.items()]]:
        if isinstance(f, bool) or not isinstance(f, (int, float)) or not (0 < f <= 1): raise ValueError(f"budgets.{name} must be a number above 0 and at most 1 (got {f!r})")
    out = {}
    for k, v in cfg.items():
        dim, val = ("time_ms", v * 1000) if k == "time_s" else (k, v)
        if dim in DIMENSIONS and v is not None:
            out[dim] = {"limit": val, "warn_at": val * per.get(k, wf)}
    return out


def cost_of(usage: dict, price: dict | None) -> float:
    """Price is per 1M tokens, {in, out}."""
    if not price:
        return 0.0
    return (usage.get("input_tokens", 0) * price.get("in", 0.0) + usage.get("output_tokens", 0) * price.get("out", 0.0)) / 1_000_000


class _Accounting:
    def __init__(self, run, budget_set: str = "agent"):
        self.run, self.set = run, budget_set

    async def _state(self) -> dict:
        return (await self.run.proj("budgets"))[self.set]

    async def used(self, dim: str) -> float:
        st = await self._state()
        if dim == "time_ms":
            rs = await self.run.proj("run_state")
            return active_time_ms(rs, self.run.svc.clock.now_ms())
        return st[dim]["used"] if dim in st else 0

    async def precheck(self, deltas: dict[str, float]) -> str | None:
        """Return the exhausted dimension (after emitting events), else None. Warnings fire once per dimension."""
        st = await self._state()
        for dim, d in st.items():
            if dim not in TERMINATING:
                continue
            delta = deltas.get(dim, 0)
            used = await self.used(dim)
            if used >= d["limit"] or used + delta > d["limit"]:
                if not d["exhausted"]:
                    await self.run.emit("budget.exhausted", {"dimension": dim, "used": used, "limit": d["limit"], "budget_set": self.set})
                return dim
            if not d["warned"] and used + delta >= d["warn_at"]:
                await self.run.emit("budget.warning", {"dimension": dim, "used": used, "limit": d["limit"], "budget_set": self.set})
                await self.run.emit("guidance.injected", {"kind": "budget", "text":
                    f"Budget warning: {dim} has reached {used + delta:g} of {d['limit']:g}. Wrap up and finish soon."})
        return None

    async def add(self, deltas: dict[str, float]) -> str | None:
        """Record actual consumption (absolute `used` goes into the event so replays are idempotent)."""
        st = await self._state()
        exhausted = None
        for dim, delta in deltas.items():
            if dim not in st or dim == "time_ms":
                continue
            used = st[dim]["used"] + delta
            await self.run.emit("budget.updated", {"dimension": dim, "used": used, "limit": st[dim]["limit"], "budget_set": self.set})
            lim = st[dim]["limit"]
            if (used > lim) or (used >= lim and dim in ("tokens", "cost")):
                if not st[dim]["exhausted"]:
                    await self.run.emit("budget.exhausted", {"dimension": dim, "used": used, "limit": lim, "budget_set": self.set})
                exhausted = exhausted or dim
        return exhausted

    async def snapshot_time(self) -> None:
        st = await self._state()
        if "time_ms" in st:
            await self.run.emit("budget.updated", {"dimension": "time_ms", "used": await self.used("time_ms"),
                                                   "limit": st["time_ms"]["limit"], "budget_set": self.set})


# --------------------------------------------------------------------------------------------------------------------------------- pure accounting (shared vectors: spec/vectors/budget-vectors.json)
def unspent(a: dict) -> float:
    """The part of an allocation that is still guaranteed to its agent: not yet used and not reserved."""
    return max(0, a["limit"] - a["used"] - a["reserved"])


def available(state: dict, bset: str, dim: str, agent_id: str | None = None) -> tuple[float, float | None]:
    """(global, local) headroom for `dim`. The global figure leaves other open allocations their guaranteed unspent capacity, so one agent cannot spend what another was promised;
    the local figure is the agent's own allocation, or None when it has none."""
    d = state[bset][dim]
    others = sum(unspent(al["dims"][dim]) for aid, al in state["allocations"].items() if not al["ended"] and al["set"] == bset and aid != agent_id and dim in al["dims"])
    glob = d["limit"] - d["used"] - d["reserved"] - others
    al = state["allocations"].get(agent_id) if agent_id else None
    local = None
    if al and not al["ended"] and dim in al["dims"]:
        a = al["dims"][dim]; local = a["limit"] - a["used"] - a["reserved"]
    return glob, local


def check_reserve(state: dict, bset: str, deltas: dict, agent_id: str | None = None) -> str | None:
    """The first dimension that cannot cover `deltas`, else None. A reservation never exceeds the global limit, or the agent's own allocation."""
    for dim, amt in deltas.items():
        if dim not in RESERVABLE or dim not in state[bset]: continue
        g, l = available(state, bset, dim, agent_id)
        if amt > g or (l is not None and amt > l): return dim
    return None


def check_allocation(state: dict, bset: str, limits: dict) -> str | None:
    """A local allocation can never exceed what remains globally, counting what other allocations already guarantee."""
    for dim, lim in limits.items():
        if dim not in state[bset]: return dim
        if lim > available(state, bset, dim)[0]: return dim
    return None


def reserve_cost(usage_in: int, max_out: int, price: dict | None, margin: float) -> float:
    """Worst-case cost of a call plus the safety margin for provider-side pricing differences."""
    return cost_of({"input_tokens": usage_in, "output_tokens": max_out}, price) * (1 + margin)


def unmetered(cfg_budgets: dict | None, metered: tuple = ()) -> list[str]:
    """Dimensions the platform does not measure. They are listed in the run manifest and are never treated as zero consumption."""
    return [d for d in UNMETERED_DEFAULT if d not in metered]


def safeguard_problems(cfg_budgets: dict | None, workers: int, queue: int) -> list[str]:
    """Independent safeguards that must exist while a dimension is unmetered: an iteration limit, a wall-clock limit, a bounded worker pool, and a bounded queue."""
    c = cfg_budgets or {}; out = []
    if not c.get("turns"): out.append("an unmetered dimension needs a turn (iteration) limit")
    if not (c.get("time_s") or c.get("time_ms")): out.append("an unmetered dimension needs a wall-clock limit")
    if not (isinstance(workers, int) and workers > 0): out.append("the worker pool must be bounded")
    if not (isinstance(queue, int) and queue >= 0): out.append("the queue must be bounded")
    return out


class QueueFull(Exception):
    """Backpressure: the bounded queue behind the worker pool is full."""


class DeadlineExceeded(Exception):
    """The caller's deadline passed while waiting for a worker."""


class ConcurrencyLimiter:
    """B_concurrency: at most `workers` operations run at once and at most `queue` wait behind them. A budget bounds cumulative use; this bounds simultaneous work.
    Permits are released on failure and on cancellation, and a waiter gives up at its deadline."""
    def __init__(self, workers: int = 4, queue: int = 100):
        if workers < 1 or queue < 0: raise ValueError("workers must be at least 1 and queue at least 0")
        self.workers, self.queue, self._sem = workers, queue, asyncio.Semaphore(workers)
        self.in_flight = self.waiting = self.peak = self.rejected = 0

    @asynccontextmanager
    async def slot(self, timeout_s: float | None = None):
        if self._sem.locked() and self.waiting >= self.queue:
            self.rejected += 1; raise QueueFull(f"{self.workers} running and {self.waiting} queued")
        self.waiting += 1
        try:
            if timeout_s is None: await self._sem.acquire()
            else:
                try: await asyncio.wait_for(self._sem.acquire(), timeout_s)
                except asyncio.TimeoutError: raise DeadlineExceeded(f"no worker free within {timeout_s}s") from None
        finally:
            self.waiting -= 1
        self.in_flight += 1; self.peak = max(self.peak, self.in_flight)
        try: yield
        finally:
            self.in_flight -= 1; self._sem.release()


# --------------------------------------------------------------------------------------------------------------------------------- the service
class BudgetController(_Accounting):
    """BudgetController with reservations (design §7.4.4): reserve before a call, then commit the actual usage or release. Every model call, tool call, and submission goes through it."""
    def _lock(self) -> asyncio.Lock:
        locks = self.run.svc.__dict__.setdefault("_budget_locks", {})
        return locks.setdefault(self.run.run_id, asyncio.Lock())

    async def _full(self) -> dict:
        return await self.run.proj("budgets")

    async def reserve(self, deltas: dict[str, float], agent_id: str | None = None) -> tuple[str | None, str | None]:
        """(reservation_id, None) when the reservation is made, else (None, the dimension that cannot cover it). Atomic: the check and the append happen under one lock,
        and the event store serializes appends, so concurrent callers cannot reserve the same remainder."""
        async with self._lock():
            full = await self._full(); st = full[self.set]
            if "time_ms" in st:                                  # time is read from the clock, not reserved: a run that is out of time cannot start another call
                used = await self.used("time_ms")
                if used >= st["time_ms"]["limit"]:
                    if not st["time_ms"]["exhausted"]: await self.run.emit("budget.exhausted", {"dimension": "time_ms", "used": used, "limit": st["time_ms"]["limit"], "budget_set": self.set})
                    return None, "time_ms"
            bad = check_reserve(full, self.set, deltas, agent_id)
            if bad:
                g, l = available(full, self.set, bad, agent_id)
                if not st[bad]["exhausted"]:
                    await self.run.emit("budget.exhausted", {"dimension": bad, "used": st[bad]["used"], "reserved": st[bad]["reserved"], "limit": st[bad]["limit"], "needed": deltas[bad], "budget_set": self.set})
                return None, bad
            for dim, d in st.items():
                if dim in RESERVABLE and not d["warned"] and d["used"] + deltas.get(dim, 0) >= d["warn_at"] and dim in deltas:
                    await self.run.emit("budget.warning", {"dimension": dim, "used": d["used"], "limit": d["limit"], "budget_set": self.set})
                    await self.run.emit("guidance.injected", {"kind": "budget", "text": f"Budget warning: {dim} has reached {d['used'] + deltas[dim]:g} of {d['limit']:g}. Wrap up and finish soon."})
            rid = uuid.uuid4().hex[:16]
            await self.run.emit("budget.reserved", {"reservation_id": rid, "deltas": {k: v for k, v in deltas.items() if k in RESERVABLE and k in st}, "agent_id": agent_id, "budget_set": self.set})
            return rid, None

    async def commit(self, rid: str, actual: dict[str, float], unreconciled: bool = False, reconciles: bool = False) -> str | None:
        """Replace the estimate with what was used (the rest of the reservation is released). `unreconciled` charges the estimate and flags it until the real usage is known; a later call
        with `reconciles=True` replaces it. Returns an exhausted dimension, as `add` does."""
        full = await self._full(); r = full["reservations"].get(rid)
        agent = r["agent_id"] if r else None
        await self.run.emit("budget.committed", {"reservation_id": rid, "actual": {k: v for k, v in actual.items() if k in RESERVABLE}, "agent_id": agent, "unreconciled": unreconciled, "reconciles": reconciles, "budget_set": self.set})
        if unreconciled: await self.run.emit("budget.unreconciled", {"reservation_id": rid, "estimate": actual, "budget_set": self.set})
        return await self.add(actual)

    async def release(self, rid: str, reason: str) -> None:
        """The call did not run: give the reservation back."""
        await self.run.emit("budget.released", {"reservation_id": rid, "reason": reason, "budget_set": self.set})

    async def allocate(self, agent_id: str, limits: dict[str, float]) -> str | None:
        """Give an agent or task a local budget, guaranteed out of what the workflow has left. Returns the dimension that cannot cover it, else None."""
        async with self._lock():
            full = await self._full()
            if agent_id in full["allocations"] and not full["allocations"][agent_id]["ended"]: raise ValueError(f"{agent_id} already has an allocation")
            bad = check_allocation(full, self.set, limits)
            if bad: return bad
            await self.run.emit("budget.allocated", {"agent_id": agent_id, "limits": limits, "budget_set": self.set}); return None

    async def end_allocation(self, agent_id: str) -> None:
        """The unspent part of the allocation returns to the workflow."""
        await self.run.emit("budget.allocation_ended", {"agent_id": agent_id, "budget_set": self.set})

    async def release_open(self, reason: str) -> int:
        """Give back every reservation still open (a run that ends must not hold budget). Returns how many."""
        n = 0
        for rid, r in list((await self._full())["reservations"].items()):
            if r["set"] == self.set: await self.release(rid, reason); n += 1
        return n
