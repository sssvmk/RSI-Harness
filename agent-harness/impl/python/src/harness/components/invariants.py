"""Checks of the paper's theorems, applied step by step to a running World (arXiv:2608.25512, §4.3). Each check names the statement it tests.
These are executable readings of the theorems, not proofs: a failure is a counterexample to the *runtime*, a pass is evidence over the runs tried."""
from __future__ import annotations
import copy, json
from .core import Component, World


class InvariantViolation(AssertionError):
    pass


def well_formed(w: World) -> None:
    """Definition 63, clauses 1-4, plus dom(sigma_n) within p_n (Definition 48)."""
    names = set(w.fibers)
    provs: dict[str, str] = {}
    for n, f in w.fibers.items():
        if f.parent != "root" and f.parent not in names: raise InvariantViolation(f"Def63(1): parent of {n} is {f.parent!r}")
        for k in f.comp.p:
            if k in provs: raise InvariantViolation(f"Def63(2): {k} is provided by both {provs[k]} and {n}")
            provs[k] = n
        if not set(f.sigma) <= f.comp.p: raise InvariantViolation(f"Def48: sigma_{n} binds keys outside p_{n}")
        if f.installed():
            if f.omega is None or set(f.omega) != set(f.comp.requires) or any(m not in names for m in f.omega.values()):
                raise InvariantViolation(f"Def63(3): committed view of {n} is not total on d_{n} or names an absent fiber")
            for k, m in f.omega.items():
                if not w.fibers[m].installed(): raise InvariantViolation(f"Def63(4): {n} resolves {k} to {m}, which is not installed")


def _snap(w: World) -> dict:
    return {n: {"theta": f.theta, "retired": f.retired, "omega": dict(f.omega) if f.omega else f.omega, "sigma": copy.deepcopy(f.sigma), "parent": f.parent,
                "comp": f.comp, "acc": len(f.acc)} for n, f in w.fibers.items()}


def signature(w: World, ignore_vestigial: bool = True) -> list:
    """The settled state, comparable across schedules: fibers by name with lifecycle, retirement and table."""
    out = []
    for n, f in w.fibers.items():
        if ignore_vestigial and f.retired and f.theta == "Inactive" and not f.sigma and not any(g.parent == n for g in w.fibers.values()): continue
        out.append((n, f.theta, f.retired, json.dumps(f.sigma, sort_keys=True)))
    return sorted(out)


class Monitor:
    """Wraps orchestration and lifecycle steps and checks Lemma 59, Theorems 64, 70, 71 and 73 and Corollary 69 around each one."""
    def __init__(self, w: World, K: int):
        self.w, self.K, self.t = w, K, 0
        self.S: dict[str, int] = {}                    # S(n): lifecycle steps acting on n
        self.V: dict[str, int] = {}                    # V(n): times target_n turns
        self.tgt = {n: json.dumps(w.target(n), sort_keys=True) for n in w.fibers}
        self.episodes: dict[str, list[list]] = {}      # fiber -> [[open_index, close_index|None], ...]
        self.consumes: list[tuple[str, int, str]] = [] # (consumer, open_index, provider)
        self.ops: list[tuple] = []                     # orchestration steps, for the static-assembly replay
        self.prev = _snap(w)

    # ---- orchestration
    def insert(self, comp: Component, name: str | None = None) -> str:
        n = self.w.insert(comp, name=name); self.ops.append(("insert", comp.id, n)); self._after("insert", n); return n

    def retire(self, n: str) -> None:
        self.w.retire(n); self.ops.append(("retire", n)); self._after("retire", n)

    def sweep(self) -> list[str]:
        removed, progress = [], True
        while progress:                                    # children are removed before their parent (O-Remove), so repeat until nothing more is removable
            progress = False
            for n in list(self.w.fibers):
                if self.w.removable(n):
                    root = self.w.fibers[n].parent == "root"
                    self.w.remove(n)
                    if root: self.ops.append(("remove", n))    # entries of instantiated fibers are vestigial and not part of the orchestration input
                    self._after("remove", n); removed.append(n); progress = True
        return removed

    # ---- lifecycle
    def step(self, rule: str, n: str) -> None:
        w = self.w
        if rule in ("iter", "finish"):
            f = w.fibers[n]
            if json.dumps(w.target(n), sort_keys=True) != json.dumps(f.omega, sort_keys=True): raise InvariantViolation(f"Thm71: an iteration of {n} ran against a resolution that is no longer its target")
        if rule == "unload":
            if w.relied(n): raise InvariantViolation(f"guard: {n} unloaded while relied upon")
        w.step(rule, n); self.S[n] = self.S.get(n, 0) + 1; self._after(rule, n)

    def _after(self, rule: str, n: str) -> None:
        w, prev = self.w, self.prev
        self.t += 1; cur = _snap(w)
        well_formed(w)                                                                                            # Theorem 64 (Preservation)
        acting_d = set(w.fibers[n].comp.requires) if n in w.fibers else set(prev[n]["comp"].requires) if n in prev else set()
        for m in prev:
            if m not in cur:
                if rule != "remove" or m != n: raise InvariantViolation(f"Lemma59: {m} disappeared at {rule}({n})")
                continue
            a, b = prev[m], cur[m]
            if a["parent"] != b["parent"] or a["comp"] is not b["comp"]: raise InvariantViolation(f"Lemma59(5): pi/d/p/e of {m} changed")
            if a["retired"] and not b["retired"]: raise InvariantViolation(f"Lemma59(5): tau of {m} went back to false")
            if not a["retired"] and b["retired"] and not ((rule == "retire" and m == n) or rule == "unload"): raise InvariantViolation(f"Lemma59(5): tau of {m} written by {rule}({n})")
            ia, ib = a["theta"] != "Inactive", b["theta"] != "Inactive"
            if (not ia and ib) and not (rule == "begin" and m == n): raise InvariantViolation(f"Lemma59(4): {m} became installed by {rule}({n})")
            if (ia and not ib) and not (rule == "unload" and m == n): raise InvariantViolation(f"Lemma59(4): {m} stopped being installed by {rule}({n})")
            if ia and ib and a["omega"] != b["omega"]: raise InvariantViolation(f"Lemma59(2): committed view of {m} moved inside an episode")
            if a["sigma"] != b["sigma"]:                                                                          # Lemma 59(1): tables move only inside a state map
                if rule in ("insert", "retire", "remove", "begin", "leave", "divert"): raise InvariantViolation(f"Table1: {rule} moved the table of {m}")
                if m != n:
                    if set(a["sigma"]) != set(b["sigma"]): raise InvariantViolation(f"Lemma59(1): the domain of sigma_{m} moved at {rule}({n})")
                    changed = {k for k in a["sigma"] if a["sigma"][k] != b["sigma"][k]}
                    if not changed <= acting_d: raise InvariantViolation(f"Lemma59(1): {rule}({n}) changed {sorted(changed - acting_d)} in sigma_{m}, outside d_{n}")
            if rule == "unload" and m == n and b["sigma"]: raise InvariantViolation(f"Cor69: sigma_{n} is not empty after unload")   # terminal recovery
        # Theorem 70(3): a binding a committed view names stays bound while its consumer is installed
        for m, g in w.fibers.items():
            if g.installed() and g.omega:
                for k, p in g.omega.items():
                    if k not in w.fibers[p].sigma: raise InvariantViolation(f"Thm70(3): {k} left the table of {p} while {m} still resolves it there")
        # episodes (Definition 58) and Theorem 70(2)
        for m in cur:
            ia = m in prev and prev[m]["theta"] != "Inactive"; ib = cur[m]["theta"] != "Inactive"
            if not ia and ib:
                self.episodes.setdefault(m, []).append([self.t, None])
                for k, p in (cur[m]["omega"] or {}).items(): self.consumes.append((m, self.t, p))
            if ia and not ib: self.episodes[m][-1][1] = self.t
        # V(n): turns of the target view; Theorem 73(1): no deadlock
        for m in w.fibers:
            d = json.dumps(w.target(m), sort_keys=True)
            if m in self.tgt and self.tgt[m] != d: self.V[m] = self.V.get(m, 0) + 1
            self.tgt[m] = d
        if not w.quiet() and not w.enabled(): raise InvariantViolation("Thm73(1): not quiescent, yet no lifecycle rule applies")
        self.prev = cur

    def finish(self) -> None:
        # Theorem 70(2): the provider's episode opens before, and (if it closes) closes after, every consumer episode that resolved through it
        for m, b2, p in self.consumes:
            ep_c = next(e for e in self.episodes[m] if e[0] == b2)
            ep_p = [e for e in self.episodes[p] if e[0] < b2 and (e[1] is None or e[1] >= b2)]
            if not ep_p: raise InvariantViolation(f"Thm70(2): {p} had no open episode when {m} began")
            if ep_p[0][1] is not None and ep_c[1] is None: raise InvariantViolation(f"Thm70(2): {p} closed while {m} was still installed")
            if ep_p[0][1] is not None and not ep_c[1] < ep_p[0][1]: raise InvariantViolation(f"Thm70(2): {p} closed no later than its consumer {m}")
        # Theorem 73(2): S(n) <= (K+3)(V(n)+1)
        for n, s in self.S.items():
            if s > (self.K + 3) * (self.V.get(n, 0) + 1): raise InvariantViolation(f"Thm73(2): S({n})={s} exceeds (K+3)(V+1)={(self.K + 3) * (self.V.get(n, 0) + 1)}")


def replay_static(ops: list[tuple], comps: dict[str, Component], new_world) -> World:
    """Theorem 80(1): the same orchestration steps, in the same order, all taken before any lifecycle step; then settle."""
    w = new_world()
    for op in ops:
        if op[0] == "insert": w.insert(comps[op[1]], name=op[2])
        elif op[0] == "retire": w.retire(op[1])
        else: w.remove(op[1])
    w.settle()
    return w


def _vestigial_closure(w: World) -> set[str]:
    """Lemma 62, applied until nothing changes: retired, Inactive, empty tables, and every child entry already vestigial."""
    v: set[str] = set()
    changed = True
    while changed:
        changed = False
        for n, f in w.fibers.items():
            if n not in v and f.retired and f.theta == "Inactive" and not f.sigma and all(g.parent != n or m in v for m, g in w.fibers.items()):
                v.add(n); changed = True
    return v


def signature_by_component(w: World) -> list:
    """Names of instantiated fibers are drawn fresh (Lemma 61), so compare by component and parent component, ignoring vestigial entries (Lemma 62)."""
    v = _vestigial_closure(w)
    return sorted((f.comp.id, w.fibers[f.parent].comp.id if f.parent != "root" else "root", f.theta, f.retired, json.dumps(f.sigma, sort_keys=True)) for n, f in w.fibers.items() if n not in v)
