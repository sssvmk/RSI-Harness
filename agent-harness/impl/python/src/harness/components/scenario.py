"""A deterministic random-scenario generator and executor, identical in Python and TypeScript, used for property tests and as a conformance vector.
Scenarios interleave insert, retire, lifecycle steps and sweeps from a seeded xoshiro256** stream (the same generator the statistics use)."""
from __future__ import annotations
import hashlib, json
from ..stats import Xoshiro256SS
from .core import Component, Instantiate, Operate, Provide, World
from .invariants import InvariantViolation, Monitor, replay_static, signature, signature_by_component
from .keys import table_key


def build_components(rng: Xoshiro256SS, n: int, n_keys: int) -> list[Component]:
    """An acyclic system: a component may require only keys provided by lower-numbered components, so every system can eventually fully activate."""
    owner: dict[int, int] = {}
    for k in range(n_keys):
        o = rng.index(n + 1)
        if o < n: owner[k] = o
    comps = []
    for i in range(n):
        prov = sorted(k for k, o in owner.items() if o == i)
        req = sorted(k for k, o in owner.items() if o < i and rng.index(2) == 1)
        def make(i=i, prov=prov, req=req):
            def effects(ctx):
                for k in prov: yield Provide(f"ns/k{k}", {})
                for k in req: yield Operate(f"ns/k{k}", "add", (f"c{i}.k{k}", i))
            return effects
        comps.append(Component(f"c{i}", make(), requires={f"ns/k{k}": "*" for k in req}, provides={f"ns/k{k}": "1.0.0" for k in prov}))
    return comps


def new_world(n_keys: int) -> World:
    return World({f"ns/k{k}": table_key() for k in range(n_keys)})


def guard_invariant(w: World) -> bool:
    """Theorem 70 (as I first read it); the full set of the paper's statements is checked by `invariants.Monitor`."""
    return all(w.fibers[p].theta != "Inactive" for m in w.fibers.values() if m.installed() and m.omega for p in m.omega.values() if p in w.fibers)


def run_scenario(seed: int, check: bool = True) -> dict:
    rng = Xoshiro256SS(seed)
    n, n_keys = 3 + rng.index(4), 2 + rng.index(3)
    comps = build_components(rng, n, n_keys)
    K = max(len(c.provides) + len(c.requires) + 1 for c in comps)        # len(e_n): one iteration per stage, plus this runtime's finishing step
    w = new_world(n_keys); mon = Monitor(w, K)
    names: dict[int, str] = {}
    for _ in range(12 + rng.index(20)):
        op = rng.index(6)
        free = [i for i in range(n) if i not in names]
        live = [i for i in sorted(names) if not w.fibers[names[i]].retired]
        if op <= 1 and free:
            i = free[rng.index(len(free))]; names[i] = mon.insert(comps[i])
        elif op == 2 and live:
            mon.retire(names[live[rng.index(len(live))]])
        elif op == 3:
            mon.sweep(); names = {i_: n_ for i_, n_ in names.items() if n_ in w.fibers}      # a removed fiber may be inserted again later
        else:
            en = w.enabled()
            if en: mon.step(*en[rng.index(len(en))])
    while True:                                                           # settle under a random schedule, every step monitored
        en = w.enabled()
        if not en: break
        mon.step(*en[rng.index(len(en))])
    names = {i_: n_ for i_, n_ in names.items() if n_ in w.fibers}
    assert w.quiet() and guard_invariant(w), f"not quiescent (seed {seed})"
    snap = lambda: sorted((n_, f.theta, f.retired, json.dumps(f.sigma, sort_keys=True)) for n_, f in w.fibers.items())
    mid = snap()
    if check:                                                              # Theorem 80(1): the settled state is the statically assembled one
        ref = replay_static(mon.ops, {c.id: c for c in comps}, lambda: new_world(n_keys))
        assert signature(w, False) == signature(ref, False), f"dynamic run and static assembly disagree (seed {seed})"
    # temporal composability: retire every consumer, settle; every provider table must be back to empty
    for i in sorted(names):
        if comps[i].requires: mon.retire(names[i])
    while True:
        en = w.enabled()
        if not en: break
        mon.step(*en[rng.index(len(en))])
    residual = {k: v for f in w.fibers.values() if not f.comp.requires for k, v in f.sigma.items() if v}
    if check: assert not residual, f"residual entries after reverting all consumers: {residual} (seed {seed})"
    for i in sorted(names): mon.retire(names[i])
    while True:
        en = w.enabled()
        if not en: break
        mon.step(*en[rng.index(len(en))])
    mon.sweep()
    if check:
        assert not w.fibers, f"fibers left after retiring everything: {list(w.fibers)} (seed {seed})"
        mon.finish()
    return {"seed": seed, "trace_hash": w.trace_hash(), "mid": mid, "steps": len(w.trace), "violations": len(w.violations)}


def confluence_signature(seed: int, schedule_seed: int) -> list:
    """Same orchestration (inserts then retires in a fixed order), different lifecycle scheduling: the settled result must be identical."""
    rng = Xoshiro256SS(seed)
    n, n_keys = 3 + rng.index(4), 2 + rng.index(3)
    comps = build_components(rng, n, n_keys)
    w, sched = new_world(n_keys), Xoshiro256SS(schedule_seed)
    names = []
    order = list(range(n))
    for j in range(n - 1, 0, -1):
        k = rng.index(j + 1); order[j], order[k] = order[k], order[j]
    retire_set = [i for i in order if rng.index(3) == 0]
    for i in order:
        names.append((i, w.insert(comps[i])))
        for _ in range(sched.index(4)):
            en = w.enabled()
            if en: w.step(*en[sched.index(len(en))])
    for i in retire_set:
        w.retire(dict(names)[i])
        for _ in range(sched.index(4)):
            en = w.enabled()
            if en: w.step(*en[sched.index(len(en))])
    w.settle(lambda m: sched.index(m))
    return sorted((f.comp.id, f.theta, f.retired, json.dumps(f.sigma, sort_keys=True)) for f in w.fibers.values())


def build_tree_components(rng: Xoshiro256SS, n: int, n_keys: int) -> list[Component]:
    """Like build_components, but every component instantiates a child component (Definition 52) that consumes some keys, possibly keys its own parent provides."""
    owner: dict[int, int] = {}
    for k in range(n_keys):
        o = rng.index(n + 1)
        if o < n: owner[k] = o
    comps = []
    for i in range(n):
        prov = sorted(k for k, o in owner.items() if o == i)
        req = sorted(k for k, o in owner.items() if o < i and rng.index(2) == 1)
        creq = sorted(k for k, o in owner.items() if o <= i and rng.index(2) == 1)
        def make(i=i, prov=prov, req=req, creq=creq):
            def child_effects(ctx):
                for k in creq: yield Operate(f"ns/k{k}", "add", (f"c{i}.child.k{k}", i))
            child = Component(f"c{i}.child", child_effects, requires={f"ns/k{k}": "*" for k in creq})
            def effects(ctx):
                for k in prov: yield Provide(f"ns/k{k}", {})
                for k in req: yield Operate(f"ns/k{k}", "add", (f"c{i}.k{k}", i))
                yield Instantiate(child)
            return effects
        comps.append(Component(f"c{i}", make(), requires={f"ns/k{k}": "*" for k in req}, provides={f"ns/k{k}": "1.0.0" for k in prov}))
    return comps


def run_tree_scenario(seed: int) -> dict:
    rng = Xoshiro256SS(seed)
    n, n_keys = 3 + rng.index(3), 2 + rng.index(3)
    comps = build_tree_components(rng, n, n_keys)
    K = max(len(c.provides) + len(c.requires) + 2 for c in comps) + n_keys + 1
    w = new_world(n_keys); mon = Monitor(w, K)
    names: dict[int, str] = {}; counter = 0
    for _ in range(10 + rng.index(16)):
        op = rng.index(6)
        free = [i for i in range(n) if i not in names]
        live = [i for i in sorted(names) if not w.fibers[names[i]].retired]
        if op <= 1 and free:
            i = free[rng.index(len(free))]; counter += 1; names[i] = mon.insert(comps[i], name=f"o{counter}")
        elif op == 2 and live: mon.retire(names[live[rng.index(len(live))]])
        elif op == 3: mon.sweep(); names = {i_: n_ for i_, n_ in names.items() if n_ in w.fibers}
        else:
            en = w.enabled()
            if en: mon.step(*en[rng.index(len(en))])
    while True:
        en = w.enabled()
        if not en: break
        mon.step(*en[rng.index(len(en))])
    assert w.quiet(), f"not quiescent (seed {seed})"
    ref = replay_static(mon.ops, {c.id: c for c in comps}, lambda: new_world(n_keys))
    assert signature_by_component(w) == signature_by_component(ref), f"dynamic and static assembly disagree (seed {seed})"
    for i in sorted(names):
        if names[i] in w.fibers: mon.retire(names[i])
    while True:
        en = w.enabled()
        if not en: break
        mon.step(*en[rng.index(len(en))])
    mon.sweep()
    assert not w.fibers, f"fibers left after retiring everything: {list(w.fibers)} (seed {seed})"
    mon.finish()
    return {"seed": seed, "trace_hash": w.trace_hash(), "steps": len(w.trace)}


def run_replacement_scenario(seed: int) -> dict:
    """Systems whose providers are replaced while consumers run (retire, settle part-way, remove, insert again): every consumer must deactivate in order and come back against
    the replacement, and the settled state must still equal the statically assembled one (Theorem 80)."""
    rng = Xoshiro256SS(seed)
    n, n_keys = 3 + rng.index(4), 2 + rng.index(3)
    comps = build_components(rng, n, n_keys)
    w = new_world(n_keys); mon = Monitor(w, max(len(c.provides) + len(c.requires) + 1 for c in comps)); live: dict[int, str] = {}; counter = 0
    def partial(k):
        for _ in range(k):
            en = w.enabled()
            if en: mon.step(*en[rng.index(len(en))])
    def settle_random():
        while True:
            en = w.enabled()
            if not en: return
            mon.step(*en[rng.index(len(en))])
    order = list(range(n))
    for j in range(n - 1, 0, -1):
        k = rng.index(j + 1); order[j], order[k] = order[k], order[j]
    for i in order:
        counter += 1; live[i] = mon.insert(comps[i], name=f"o{counter}"); partial(rng.index(4))
    settle_random()
    for _ in range(2 + rng.index(3)):                                               # replace some component: retire, let consumers react part-way, remove it, insert it again
        cands = [i for i in sorted(live) if comps[i].provides]
        if not cands: break
        i = cands[rng.index(len(cands))]
        mon.retire(live[i]); partial(rng.index(5)); settle_random(); mon.sweep()
        live = {a: b for a, b in live.items() if b in w.fibers}
        counter += 1; live[i] = mon.insert(comps[i], name=f"o{counter}"); partial(rng.index(4))
    settle_random()
    assert w.quiet(), f"not quiescent (seed {seed})"
    ref = replay_static(mon.ops, {c.id: c for c in comps}, lambda: new_world(n_keys))
    assert signature(w) == signature(ref), f"after provider replacement the dynamic state differs from the static assembly (seed {seed})"
    mon.finish()
    return {"seed": seed, "trace_hash": w.trace_hash(), "steps": len(w.trace)}
