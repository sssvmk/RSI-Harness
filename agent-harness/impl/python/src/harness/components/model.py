"""A bounded, exhaustive, explicit-state model check of the lifecycle rules (PRD: Composability Verification, "machine-checked model within stated bounds").

It checks an *abstraction* of the runtime (the same nine rules over immutable tuples, effects reduced to a count of stages), not the runtime itself; the runtime is
checked by property tests over random interleavings (scenario.py). Bounds are stated in `BOUNDS`. This is an explicit-state search in Python, not TLA+/TLC."""
from __future__ import annotations
import itertools, sys
from collections import deque

INACTIVE, RELOADING, ACTIVE, UNLOADING = 0, 1, 2, 3
BOUNDS = {"components": 3, "keys": 2, "orchestration": "each component inserted at most once, retired at any time, removed when removable"}


def _derive(cfg, st):
    prov = {}
    for i, (P, R) in enumerate(cfg):
        if st[i][1] == 1 and st[i][2] == ACTIVE:                      # sigma: keys of ACTIVE fibers
            for k in P: prov[k] = i
    return prov


def _target(cfg, st, i):
    if st[i][0] != 1 or st[i][3]: return None                          # absent, or retired
    prov = _derive(cfg, st)
    R = cfg[i][1]
    return tuple(prov[k] for k in R) if all(k in prov for k in R) else None


def _relied(cfg, st, n):
    return any(m != n and st[m][1] == 1 and st[m][2] != INACTIVE and st[m][5] is not None and n in st[m][5] for m in range(len(cfg)))


# per component: (lifecycle_of_insertion 0 never | 1 present | 2 removed ... , present flag, theta, retired, prog, omega)
def _initial(cfg):
    return tuple((0, 0, INACTIVE, False, 0, None) for _ in cfg)


def _set(st, i, **kw):
    ins, pres, th, ret, prog, om = st[i]
    ins, pres, th, ret, prog, om = kw.get("ins", ins), kw.get("pres", pres), kw.get("th", th), kw.get("ret", ret), kw.get("prog", prog), kw.get("om", om)
    return st[:i] + ((ins, pres, th, ret, prog, om),) + st[i + 1:]


def _lifecycle(cfg, st):
    out = []
    for i, (P, R) in enumerate(cfg):
        ins, pres, th, ret, prog, om = st[i]
        if not pres: continue
        t = _target(cfg, st, i); stages = len(P) + len(R)
        if th == INACTIVE and t is not None: out.append((f"begin{i}", _set(st, i, th=RELOADING, prog=0, om=t)))
        elif th == RELOADING:
            if t == om:
                out.append((f"iter{i}", _set(st, i, prog=prog + 1) if prog < stages else _set(st, i, th=ACTIVE)))
            else: out.append((f"divert{i}", _set(st, i, th=UNLOADING)))
        elif th == ACTIVE and t != om: out.append((f"leave{i}", _set(st, i, th=UNLOADING)))
        elif th == UNLOADING and not _relied(cfg, st, i): out.append((f"unload{i}", _set(st, i, th=INACTIVE, prog=0, om=None)))
    return out


def _orchestration(cfg, st):
    out = []
    for i in range(len(cfg)):
        ins, pres, th, ret, prog, om = st[i]
        if ins == 0: out.append((f"insert{i}", _set(st, i, ins=1, pres=1)))
        if pres and not ret: out.append((f"retire{i}", _set(st, i, ret=True)))
        if pres and ret and th == INACTIVE and min(prog, len(cfg[i][0])) == 0: out.append((f"remove{i}", _set(st, i, pres=0, ins=2)))
    return out


def _quiet(cfg, st):
    for i in range(len(cfg)):
        if not st[i][1]: continue
        t = _target(cfg, st, i); th, om = st[i][2], st[i][5]
        if th == INACTIVE and t is not None: return False
        if th == ACTIVE and t != om: return False
        if th in (RELOADING, UNLOADING): return False
    return True


def check_config(cfg):
    """Explore every interleaving of orchestration and lifecycle steps. Returns (states, violations)."""
    start = _initial(cfg)
    seen, order, lc_edges, viol = {start}, deque([start]), {}, []
    while order:
        st = order.popleft()
        # I1 guard: no installed consumer's committed view names a provider whose accumulator has been applied (Inactive)
        for m in range(len(cfg)):
            if st[m][1] and st[m][2] != INACTIVE and st[m][5] is not None:
                for p in st[m][5]:
                    if st[p][2] == INACTIVE: viol.append(("guard", st))
        lc = _lifecycle(cfg, st); lc_edges[st] = [s for _, s in lc]
        for _, s in lc + _orchestration(cfg, st):
            if s not in seen: seen.add(s); order.append(s)
    # I3 progress (termination + quiescence) and I4 confluence over lifecycle-only paths
    color, sinks = {}, {}
    sys.setrecursionlimit(100000)
    def sink_set(st):
        if st in sinks: return sinks[st]
        if color.get(st) == 1: viol.append(("lifecycle cycle", st)); return frozenset()
        color[st] = 1
        nxt = lc_edges[st]
        if not nxt:
            if not _quiet(cfg, st): viol.append(("stuck: no step enabled but not quiet", st))
            res = frozenset([st])
        else:
            res = frozenset().union(*(sink_set(s) for s in nxt))
        color[st] = 2; sinks[st] = res
        return res
    for st in seen:
        ss = sink_set(st)
        if len(ss) > 1: viol.append(("not confluent", st))
        # I5 temporal composability: a settled state with every present fiber retired holds no bindings, so all are removable
        for q in ss:
            present = [i for i in range(len(cfg)) if q[i][1]]
            if present and all(q[i][3] for i in present) and any(q[i][2] != INACTIVE or min(q[i][4], len(cfg[i][0])) > 0 for i in present): viol.append(("leak after full retire", q))
    return len(seen), viol


def all_configs(n_comps: int = 3, n_keys: int = 2):
    """Every assignment of keys to at most one provider and every requirement set (a component never requires its own key)."""
    keys = range(n_keys)
    for owners in itertools.product(range(n_comps + 1), repeat=n_keys):          # owner == n_comps means "provided by nobody"
        for reqs in itertools.product(*[
                [tuple(sorted(c)) for r in range(n_keys + 1) for c in itertools.combinations([k for k in keys if owners[k] != i], r)] for i in range(n_comps)]):
            yield tuple((tuple(k for k in keys if owners[k] == i), reqs[i]) for i in range(n_comps))


def check_all(n_comps: int = 3, n_keys: int = 2, limit: int | None = None) -> dict:
    total_states, n, bad = 0, 0, []
    for cfg in all_configs(n_comps, n_keys):
        s, v = check_config(cfg); total_states += s; n += 1
        if v: bad.append((cfg, v[:2]))
        if limit and n >= limit: break
    return {"bounds": {**BOUNDS, "components": n_comps, "keys": n_keys}, "configurations": n, "states": total_states, "violations": bad}
