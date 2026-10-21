"""The calculus of dynamic composition (Shi, Zhang and Cui, arXiv:2608.25512, §3-§4), as a runtime.

Mapping from the paper to this module
  effect function  e : G -> G x (G -> G)      a *stage* (Provide / Operate / Instantiate / External) executed against the World, which returns its inverse (Def. 8)
  accumulator      phi (composed inverses)    Fiber.acc, applied last-in-first-out on unload (Def. 18, Thm. 16)
  effect iterator  (Def. 17)                  a generator: each `yield` is one iteration, the value sent back is the outcome that selects the continuation
  coeffect context Sigma (Def. 19)            the union of the tables of ACTIVE fibers (Def. 50 / eq. 46), `World.sigma()`
  specification d, provision p (Def. 21, 48)  Component.requires / Component.provides
  notify_d (Def. 22)                          `notify`: activating | deactivating | neutral
  component (d, p, e), fiber (Def. 48, 49)    Component, Fiber with lifecycle Inactive | Reloading | Active | Unloading
  rules (S4.2): O-Insert O-Retire O-Remove    World.insert / retire / remove
                L-Begin L-Iter L-Finish        step "begin" / "iter" (an exhausted iterator is L-Finish)
                L-Divert L-Leave L-Unload      step "divert" / "leave" / "unload"
  target view, committed view, guard (Def. 53, 54)   World.target / Fiber.omega / World.relied
  confinement (Def. 55)                       a component reads and writes only the keys it declared; violations are refused and recorded

Deliberate deviations (each is covered by a test or noted in the README):
  * the last iteration of the paper's iterator is merged with L-Finish: a generator is only known to be finished when resumed, which performs no effect;
  * L-Divert uses the first alternative of the rule (abort the held iteration);
  * an effect that raises, or a compensation that fails, quarantines the fiber (target = bottom) so that it cannot loop; the paper has no failing effects;
  * version ranges and the namespaced key form are PRD requirements, not part of the paper's calculus.
"""
from __future__ import annotations
import hashlib, json, re
from typing import Any, Callable, Iterator

KEY_RE = re.compile(r"^[a-z0-9_.-]+/[a-z0-9_.@:-]+$")


class ComponentError(Exception):
    code = "component.error"


class ConfinementViolation(ComponentError):
    """Cordis UNDECLARED_ACCESS: the key is not in the component's declared set (Definition 55)."""
    code = "component.confinement_violation"
    kind = "UNDECLARED_ACCESS"


class PreconditionFailed(ComponentError):
    code = "component.precondition_failed"


class InactiveAccess(PreconditionFailed):
    """Cordis INACTIVE_ACCESS (Algorithm 6): the key is declared but not committed, so the dependency is not loaded."""
    code = "component.inactive_access"


class Stage:
    pass


class Provide(Stage):
    def __init__(self, key: str, value: Any): self.key, self.value = key, value


class Operate(Stage):
    def __init__(self, key: str, op: str, arg: Any = None): self.key, self.op, self.arg = key, op, arg


class Instantiate(Stage):
    def __init__(self, component: "Component"): self.component = component


class External(Stage):
    """An effect that leaves the system boundary and cannot be inverted: with `compensate` it runs now and the compensation is its inverse; without, it is withheld until approved."""
    def __init__(self, label: str, action: Callable[[], Any], compensate: Callable[[], None] | None = None):
        self.label, self.action, self.compensate = label, action, compensate


def parse_version(s: str) -> tuple[int, int, int]:
    m = re.fullmatch(r"(\d+)\.(\d+)\.(\d+)", s.strip())
    if not m:
        raise ValueError(f"bad version {s!r}")
    return int(m[1]), int(m[2]), int(m[3])


def version_satisfies(version: str, rng: str) -> bool:
    """`*`, `1.2.3`, `^1.2.3`, `~1.2.3`, `>=1.2.0`, `<2.0.0`, comma-joined clauses are ANDed."""
    v = parse_version(version)
    for clause in (c.strip() for c in rng.split(",")):
        if clause in ("", "*"):
            continue
        if clause.startswith("^"):
            b = parse_version(clause[1:]); hi = (b[0] + 1, 0, 0) if b[0] > 0 else (0, b[1] + 1, 0) if b[1] > 0 else (0, 0, b[2] + 1)
            ok = b <= v < hi
        elif clause.startswith("~"):
            b = parse_version(clause[1:]); ok = b <= v < (b[0], b[1] + 1, 0)
        elif clause.startswith(">="): ok = v >= parse_version(clause[2:])
        elif clause.startswith("<="): ok = v <= parse_version(clause[2:])
        elif clause.startswith(">"): ok = v > parse_version(clause[1:])
        elif clause.startswith("<"): ok = v < parse_version(clause[1:])
        else: ok = v == parse_version(clause.lstrip("="))
        if not ok:
            return False
    return True


class Component:
    """(d, p, e): requires (d, with version ranges), provides (p, with versions) and the effect iterator `effects(ctx)`."""
    def __init__(self, id: str, effects: Callable[["Ctx"], Any], requires: dict[str, str] | None = None, provides: dict[str, str] | None = None,
                 version: str = "1.0.0", config: Any = None, isolated: bool = False):
        self.id, self.effects, self.version, self.config, self.isolated = id, effects, version, config, isolated
        self.requires, self.provides = dict(requires or {}), dict(provides or {})
        if set(self.requires) & set(self.provides):
            raise ValueError(f"{id}: a component cannot both require and provide the same key")
        parse_version(version)
        for k, v in self.provides.items(): parse_version(v)

    @property
    def d(self) -> set[str]: return set(self.requires)
    @property
    def p(self) -> set[str]: return set(self.provides)
    @property
    def declared(self) -> set[str]: return self.d | self.p     # the set a confined component may touch (S = d u p)

    def config_hash(self) -> str:
        return hashlib.sha256(json.dumps({"id": self.id, "version": self.version, "config": self.config, "requires": self.requires, "provides": self.provides}, sort_keys=True, default=str).encode()).hexdigest()[:16]


class Fiber:
    def __init__(self, name: str, comp: Component, parent: str):
        self.name, self.comp, self.parent = name, comp, parent
        self.sigma: dict[str, Any] = {}                 # the fiber's own coeffect table (keys of its provision only)
        self.retired = False                            # tau
        self.failed = False                             # quarantined after an error (extension)
        self.theta = "Inactive"
        self.gen: Iterator | None = None                # remaining effect iterator while Reloading
        self.acc: list[tuple[str, Callable]] = []       # the accumulator: inverses in application order, applied in reverse
        self.omega: dict[str, str] | None = None        # committed view: key -> providing fiber
        self.started = False
        self.pending_out: Any = None
        self.pending_exc: Exception | None = None
        self.escalated = False

    def installed(self) -> bool: return self.theta != "Inactive"

    @property
    def state(self) -> str:
        """The lifecycle state under the implementation's names (paper Table 2): INACTIVE, LOADING, ACTIVE, UNLOADING, and FAILED, which carries the error outcome (§4.4)."""
        if self.failed and self.theta == "Inactive": return "FAILED"
        return {"Inactive": "INACTIVE", "Reloading": "LOADING", "Active": "ACTIVE", "Unloading": "UNLOADING"}[self.theta]


class Ctx:
    """The handle an effect function receives. Reads are confined to the declared keys (Definition 55, clause 2)."""
    def __init__(self, world: "World", fiber: Fiber): self._w, self._f = world, fiber
    @property
    def name(self) -> str: return self._f.name
    def get(self, key: str) -> Any: return self._w._read(self._f, key)


def notify(before_sat: bool, after_sat: bool) -> str:
    """Definition 22."""
    if not before_sat and after_sat: return "activating"
    if before_sat and not after_sat: return "deactivating"
    return "neutral"


class World:
    def __init__(self, key_types: dict[str, Any] | None = None, require_total: bool = False, review: Callable[["Component"], bool] | None = None):
        from .keys import KeyType
        self.require_total, self.review, self.not_total = require_total, review, []
        self.key_types: dict[str, KeyType] = dict(key_types or {})
        self.fibers: dict[str, Fiber] = {}
        self.trace: list[list] = []
        self.violations: list[dict] = []
        self.escalations: list[dict] = []
        self.pending_external: dict[str, External] = {}
        self._n = 0

    # ---------------------------------------------------------------- derived state (eq. 46, Def. 53, 54)
    def sigma(self) -> dict[str, tuple[str, Any]]:
        out: dict[str, tuple[str, Any]] = {}
        for n, f in self.fibers.items():
            if f.theta == "Active":
                for k, v in f.sigma.items(): out[k] = (n, v)
        return out

    def satisfied(self, requires: dict[str, str], sig: dict | None = None) -> bool:
        sig = self.sigma() if sig is None else sig
        return all(k in sig and version_satisfies(self.fibers[sig[k][0]].comp.provides[k], rng) for k, rng in requires.items())

    def target(self, n: str) -> dict[str, str] | None:
        f = self.fibers[n]
        if f.retired or f.failed: return None
        sig = self.sigma()
        if not self.satisfied(f.comp.requires, sig): return None
        return {k: sig[k][0] for k in sorted(f.comp.requires)}

    def relied(self, n: str) -> bool:
        return any(m != n and g.installed() and g.omega is not None and n in g.omega.values() for m, g in self.fibers.items())

    def quiet(self) -> bool:
        for n, f in self.fibers.items():
            t = self.target(n)
            if f.theta == "Inactive":
                if t is not None: return False
            elif f.theta == "Active":
                if t != f.omega: return False
            else:
                return False
        return True

    def projection(self, keys: set[str] | None = None) -> dict[str, Any]:
        """sigma^S (Definition 51): the bindings at keys S over every fiber, not only the active ones."""
        out: dict[str, Any] = {}
        for f in self.fibers.values():
            for k, v in f.sigma.items():
                if keys is None or k in keys: out[k] = v
        return out

    def equivalent(self, a: dict, b: dict, keys: set[str] | None = None) -> bool:
        """a ~=_S b (Definition 33): the same keys of S bound, to values related by each key's equivalence."""
        ka = {k for k in a if keys is None or k in keys}; kb = {k for k in b if keys is None or k in keys}
        if ka != kb: return False
        return all(self._equiv(k)(a[k], b[k]) for k in ka)

    def _equiv(self, k: str) -> Callable:
        kt = self.key_types.get(k)
        return kt.equiv if kt else (lambda x, y: x == y)

    # ---------------------------------------------------------------- orchestration rules
    def _log(self, *entry) -> None: self.trace.append(list(entry))

    def insert(self, comp: Component, parent: str = "root", name: str | None = None) -> str:
        """O-Insert: the name is fresh, the parent exists, and the provision is disjoint from every existing fiber's."""
        if parent != "root" and parent not in self.fibers: raise PreconditionFailed(f"O-Insert: parent {parent!r} absent")
        if self.review is not None and not self.review(comp):                     # load-time review of the declared capabilities (paper §6.3)
            self._log("review_denied", comp.id); raise PreconditionFailed(f"O-Insert: review denied for {comp.id!r}")
        for m, g in self.fibers.items():
            clash = comp.p & g.comp.p
            if clash: raise PreconditionFailed(f"O-Insert: provision {sorted(clash)} overlaps fiber {m!r}")
        self._n += 1
        n = name or f"f{self._n}"
        if n in self.fibers: raise PreconditionFailed(f"O-Insert: name {n!r} in use")
        self.fibers[n] = Fiber(n, comp, parent)
        self._log("insert", n, comp.id)
        return n

    def retire(self, n: str) -> None:
        """O-Retire: unconditional on the lifecycle state; a request that the lifecycle rules carry out."""
        if n not in self.fibers: raise PreconditionFailed(f"O-Retire: {n!r} absent")
        before = self._sat_snapshot(); self.fibers[n].retired = True; self._log("retire", n); self._notify(before)

    def removable(self, n: str) -> bool:
        f = self.fibers.get(n)
        return bool(f and f.retired and f.theta == "Inactive" and not f.sigma and not any(g.parent == n for g in self.fibers.values()))

    def remove(self, n: str) -> None:
        """O-Remove: retired, Inactive, holding no bindings, with no child left."""
        if not self.removable(n): raise PreconditionFailed(f"O-Remove: {n!r} is not removable")
        del self.fibers[n]; self._log("remove", n)

    def sweep(self) -> list[str]:
        removed = []
        progress = True
        while progress:
            progress = False
            for n in list(self.fibers):
                if self.removable(n): self.remove(n); removed.append(n); progress = True
        return removed

    # ---------------------------------------------------------------- reactivity (Def. 22)
    def _sat_snapshot(self) -> dict[str, bool]:
        sig = self.sigma()
        return {n: self.satisfied(f.comp.requires, sig) for n, f in self.fibers.items()}

    def _notify(self, before: dict[str, bool]) -> None:
        after = self._sat_snapshot()
        for n in self.fibers:
            if n in before:
                v = notify(before[n], after[n])
                if v != "neutral": self._log("notify", n, v)

    # ---------------------------------------------------------------- lifecycle rules
    def enabled(self) -> list[tuple[str, str]]:
        out = []
        for n, f in self.fibers.items():
            t = self.target(n)
            if f.theta == "Inactive" and t is not None: out.append(("begin", n))
            elif f.theta == "Reloading": out.append(("iter", n) if t == f.omega else ("divert", n))
            elif f.theta == "Active" and t != f.omega: out.append(("leave", n))
            elif f.theta == "Unloading" and not f.escalated and not self.relied(n): out.append(("unload", n))
        return out

    def step(self, rule: str, n: str) -> None:
        if (rule, n) not in self.enabled(): raise PreconditionFailed(f"rule {rule} is not enabled at {n!r}")
        before = self._sat_snapshot(); f = self.fibers[n]
        if rule == "begin":                                              # L-Begin
            f.theta, f.omega, f.acc, f.started, f.pending_out, f.pending_exc = "Reloading", self.target(n), [], False, None, None
            self._log("begin", n)
            try: f.gen = self._as_gen(f.comp.effects(Ctx(self, f)))
            except Exception as e:                                       # the effect function raised before yielding anything (paper §4.4, Failure): quarantine
                self.violations.append({"fiber": n, "code": getattr(e, "code", "component.error"), "detail": str(e)})
                f.failed = True; self._log("error", n, getattr(e, "code", "component.error")); self._abort_to_unloading(f, "divert")
        elif rule == "iter": self._iter(f)                               # L-Iter, or L-Finish when the iterator is exhausted
        elif rule == "divert": self._abort_to_unloading(f, "divert")     # L-Divert (first alternative)
        elif rule == "leave":                                            # L-Leave: record the decision, withdraw nothing yet
            f.theta = "Unloading"; self._log("leave", n)
        elif rule == "unload": self._unload(f)                           # L-Unload: the only rule that applies an accumulator
        self._notify(before)

    @staticmethod
    def _as_gen(x):
        if hasattr(x, "send"): return x
        def g():
            for s in (x or []): yield s
        return g()

    def _abort_to_unloading(self, f: Fiber, why: str) -> None:
        if f.gen is not None:
            try: f.gen.close()
            except Exception: pass
        f.gen, f.theta = None, "Unloading"; self._log(why, f.name)

    def _iter(self, f: Fiber) -> None:
        try:
            if f.pending_exc is not None: exc, f.pending_exc = f.pending_exc, None; st = f.gen.throw(exc)
            elif not f.started: f.started = True; st = next(f.gen)
            else: st = f.gen.send(f.pending_out)
        except StopIteration:
            if set(f.sigma) != f.comp.p:                                           # Definition 76: a finished activation has installed every key of p
                self.not_total.append(f.name); self._log("not_total", f.name)
                if self.require_total:
                    self.violations.append({"fiber": f.name, "code": "component.not_total", "detail": f"installed {sorted(f.sigma)} of {sorted(f.comp.p)}"})
                    f.failed = True; self._log("error", f.name, "component.not_total"); self._abort_to_unloading(f, "divert"); return
            f.theta, f.gen = "Active", None; self._log("finish", f.name); return
        except Exception as e:                                           # an effect function raised: quarantine, then unload what it did
            if not getattr(e, "recorded", False):                        # a refusal is recorded once, where it happened
                self.violations.append({"fiber": f.name, "code": getattr(e, "code", "component.error"), "detail": str(e)})
            f.failed = True; self._log("error", f.name, getattr(e, "code", "component.error")); self._abort_to_unloading(f, "divert"); return
        try:
            f.pending_out = self._exec(f, st); f.pending_exc = None
        except ComponentError as e:                                      # refused: deliver the refusal to the component at the point it yielded
            self.violations.append({"fiber": f.name, "code": e.code, "detail": str(e)}); self._log("violation", f.name, e.code)
            e.recorded = True; f.pending_exc, f.pending_out = e, None
        else:
            self._log("iter", f.name, type(st).__name__)

    # ---------------------------------------------------------------- stages (effect functions with witnessed inverses)
    def _read(self, f: Fiber, key: str) -> Any:
        if key not in f.comp.declared:
            self.violations.append({"fiber": f.name, "code": ConfinementViolation.code, "detail": f"read of undeclared key {key!r}"})
            e = ConfinementViolation(f"{f.name} reads undeclared key {key!r}"); e.recorded = True; raise e
        if key in f.sigma: return f.sigma[key]
        prov = (f.omega or {}).get(key)
        if prov is None or key not in self.fibers[prov].sigma: raise InactiveAccess(f"{key!r} is declared by {f.name} but not committed")
        return self.fibers[prov].sigma[key]

    def _exec(self, f: Fiber, st: Stage) -> Any:
        if isinstance(st, Provide):
            if st.key not in f.comp.p: raise ConfinementViolation(f"{f.name} provides undeclared key {st.key!r}")
            if st.key in f.sigma: raise PreconditionFailed(f"{st.key!r} is already provided by {f.name}")
            f.sigma[st.key] = st.value
            f.acc.append(("state", lambda w, _n=f.name, _k=st.key: w.fibers[_n].sigma.pop(_k, None)))
            return None
        if isinstance(st, Operate):
            if st.key not in f.comp.declared: raise ConfinementViolation(f"{f.name} operates on undeclared key {st.key!r}")
            prov = f.name if st.key in f.comp.p else (f.omega or {}).get(st.key)
            if prov is None or st.key not in self.fibers[prov].sigma: raise PreconditionFailed(f"{st.key!r} is not bound for {f.name}")
            kt = self.key_types.get(st.key)
            if kt is None or st.op not in kt.ops: raise ConfinementViolation(f"{st.key!r} offers no operation {st.op!r}")
            try: new, inv, outcome = kt.ops[st.op](self.fibers[prov].sigma[st.key], st.arg)
            except KeyError as e: raise PreconditionFailed(str(e))
            self.fibers[prov].sigma[st.key] = new
            def undo(w, _p=prov, _k=st.key, _inv=inv):
                if _p in w.fibers and _k in w.fibers[_p].sigma: w.fibers[_p].sigma[_k] = _inv(w.fibers[_p].sigma[_k])
            f.acc.append(("state", undo)); return outcome
        if isinstance(st, Instantiate):                                   # Definition 52: O-Insert under this fiber; the inverse is O-Retire
            child = self.insert(st.component, parent=f.name)
            f.acc.append(("state", lambda w, _c=child: w.retire(_c) if _c in w.fibers else None)); return child
        if isinstance(st, External):
            if st.compensate is None:
                eid = f"{f.name}:{st.label}:{len(self.pending_external)}"; self.pending_external[eid] = st; return {"withheld": eid}
            out = st.action()
            f.acc.append(("compensation", lambda w, _c=st.compensate: _c())); return out        # inverses receive the world; a user compensation takes no arguments
        raise ConfinementViolation(f"unknown stage {st!r}")

    def perform(self, n: str, stage: Stage) -> Any:
        """An effect performed by a fiber after its iterator has finished (Cordis `ctx.effect` at any time): its inverse joins the accumulator, so it is
        reverted with everything else, last in first out. Only an installed fiber that is not leaving may perform effects."""
        f = self.fibers[n]
        if f.theta not in ("Reloading", "Active"): raise PreconditionFailed(f"{n!r} is {f.state}; it cannot perform effects")
        try: out = self._exec(f, stage)
        except ComponentError as e:
            self.violations.append({"fiber": n, "code": e.code, "detail": str(e)}); self._log("violation", n, e.code); raise
        self._log("perform", n, type(stage).__name__); return out

    def dependency_cycles(self) -> list[list[str]]:
        """A cycle leaves its members permanently inactive; it is predictable from the declarations alone, so it is reported at load time (paper §6.5)."""
        prov = {k: n for n, f in self.fibers.items() for k in f.comp.p}
        edges = {n: sorted({prov[k] for k in f.comp.requires if k in prov}) for n, f in self.fibers.items()}
        out, state = [], {}
        def dfs(n, path):
            state[n] = 1
            for m in edges[n]:
                if state.get(m) == 1: out.append(path[path.index(m):] + [m] if m in path else [n, m])
                elif m not in state: dfs(m, path + [m])
            state[n] = 2
        for n in edges:
            if n not in state: dfs(n, [n])
        return out

    def approve(self, eid: str) -> Any:
        """Commit a withheld external effect (it cannot be reverted afterwards, by definition)."""
        return self.pending_external.pop(eid).action()

    def _unload(self, f: Fiber) -> None:
        while f.acc:
            kind, fn = f.acc[-1]
            try: fn(self)
            except Exception as e:                                        # a failed compensation escalates to a human; the remaining inverses stay held
                f.escalated = f.failed = True
                self.escalations.append({"fiber": f.name, "kind": kind, "detail": str(e)}); self._log("escalate", f.name); return
            f.acc.pop()
        f.theta, f.omega, f.gen = "Inactive", None, None; self._log("unload", f.name)

    def resolve_escalation(self, n: str) -> None:
        """A human has dealt with the failed compensation: drop it and let the remaining inverses run."""
        f = self.fibers[n]
        if f.acc and f.escalated: f.acc.pop()
        f.escalated = False

    # ---------------------------------------------------------------- scheduling
    def settle(self, pick: Callable[[int], int] | None = None, max_steps: int = 100000) -> int:
        """Apply lifecycle rules until none is enabled. Any order is legal (the rules are nondeterministic); `pick` chooses among the enabled steps."""
        n = 0
        while True:
            en = self.enabled()
            if not en: return n
            rule, name = en[(pick(len(en)) if pick else 0)]
            self.step(rule, name); n += 1
            if n > max_steps: raise RuntimeError("settle did not terminate")

    def trace_hash(self) -> str:
        return hashlib.sha256(json.dumps(self.trace, separators=(",", ":"), default=str).encode()).hexdigest()

    # ---------------------------------------------------------------- independence (Thm. 47) and ordered revert (PRD: Independence Check Before Out-of-Order Revert)
    def independent(self, a: Component, b: Component) -> bool:
        Sa, Sb = a.declared, b.declared
        if (a.p & Sb) or (b.p & Sa): return False
        return all(self.key_types.get(k) is not None and self.key_types[k].commutative for k in Sa & Sb)

    def plan_revert(self, n: str) -> dict:
        """Later fibers that are not independent of `n` must be reverted first (in reverse order) or the result re-gated."""
        names = list(self.fibers); idx = names.index(n)
        later = [m for m in names[idx + 1:] if self.fibers[m].installed() and not self.fibers[m].retired]
        dep = [m for m in later if not self.independent(self.fibers[n].comp, self.fibers[m].comp)]
        return {"fiber": n, "independent": not dep, "dependents": list(reversed(dep))}

    def revert(self, n: str, cascade: bool = False) -> dict:
        plan = self.plan_revert(n)
        if not plan["independent"] and not cascade:
            self._log("revert_blocked", n); return {**plan, "status": "revert.blocked"}
        for m in plan["dependents"]: self.retire(m)
        self.retire(n); self.settle(); self.sweep()
        return {**plan, "status": "reverted"}
