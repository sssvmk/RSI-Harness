"""Declared dependencies, the component registry, the declarative reconciler, run pinning and live replacement (PRD §3.9)."""
from __future__ import annotations
import hashlib, json, re
from typing import Any, Callable
from .core import Component, ComponentError, Instantiate, KEY_RE, World, version_satisfies
from .scope import Scope


class RegistrationError(ComponentError):
    code = "component.registration_rejected"


class ComponentRegistry:
    """Static checks at registration time: namespaced keys, version ranges, disjoint provisions unless isolated, and no dependency cycle."""
    def __init__(self):
        self.components: dict[str, Component] = {}

    def register(self, comp: Component) -> None:
        if comp.id in self.components:
            raise RegistrationError(f"duplicate component id {comp.id!r}")
        for k in {**comp.requires, **comp.provides}:
            if not KEY_RE.match(k):
                raise RegistrationError(f"key {k!r} is not namespaced as '<namespace>/<name>'")
        for k, rng in comp.requires.items():
            try: version_satisfies("0.0.0", rng)
            except ValueError as e: raise RegistrationError(f"bad version range for {k!r}: {e}")
        for other in self.components.values():
            clash = set(comp.provides) & set(other.provides)
            if clash and not (comp.isolated and other.isolated):
                raise RegistrationError(f"provisions of {comp.id!r} and {other.id!r} overlap on {sorted(clash)} (provisions are disjoint unless isolated)")
        trial = {**self.components, comp.id: comp}
        cyc = self._cycle(trial)
        if cyc:
            raise RegistrationError("dependency cycle: " + " -> ".join(cyc))
        self.components[comp.id] = comp

    @staticmethod
    def _cycle(comps: dict[str, Component]) -> list[str] | None:
        provider = {k: c.id for c in comps.values() for k in c.provides}
        edges = {c.id: sorted({provider[k] for k in c.requires if k in provider and provider[k] != c.id} | ({c.id} if any(provider.get(k) == c.id for k in c.requires) else set())) for c in comps.values()}
        state: dict[str, int] = {}
        def dfs(n: str, path: list[str]):
            state[n] = 1
            for m in edges[n]:
                if state.get(m) == 1:
                    return path[path.index(m):] + [m] if m in path else [n, m]
                if state.get(m) is None:
                    r = dfs(m, path + [m])
                    if r: return r
            state[n] = 2
            return None
        for n in sorted(edges):
            if state.get(n) is None:
                r = dfs(n, [n])
                if r: return r
        return None

    def check_dependencies(self, comp_ids: list[str]) -> list[str]:
        """Which declared requirements of the listed components no listed component can satisfy (an advisory pre-flight, the runtime stays reactive)."""
        comps = [self.components[i] for i in comp_ids]
        have = {k: v for c in comps for k, v in c.provides.items()}
        return sorted(f"{c.id}:{k}" for c in comps for k, rng in c.requires.items() if k not in have or not version_satisfies(have[k], rng))


class Reconciler:
    """Turns a declarative desired set (stable id -> component) into insert / retire / update operations (PRD: Declarative Reconciliation)."""
    def __init__(self, world: World):
        self.world = world
        self.by_id: dict[str, tuple[str, str]] = {}      # stable id -> (fiber name, config fingerprint)

    @staticmethod
    def fingerprint(comp: Component) -> str:
        return comp.config_hash()

    def plan(self, desired: dict[str, Component]) -> list[tuple[str, str]]:
        ops = []
        for sid in sorted(self.by_id):
            if sid not in desired: ops.append(("retire", sid))
        for sid in sorted(desired):
            if sid not in self.by_id: ops.append(("insert", sid))
            elif self.by_id[sid][1] != self.fingerprint(desired[sid]): ops.append(("update", sid))
        return ops

    def _retire_and_remove(self, sid: str) -> None:
        name = self.by_id.pop(sid)[0]
        self.world.retire(name)
        self.world.settle()
        self.world.sweep()                                # remove every retired fiber that has become removable (children first)

    def apply(self, desired: dict[str, Component]) -> list[tuple[str, str]]:
        ops = self.plan(desired)
        for op, sid in ops:
            if op in ("retire", "update"):
                self._retire_and_remove(sid)
        for op, sid in ops:
            if op in ("insert", "update"):
                name = self.world.insert(desired[sid])
                self.by_id[sid] = (name, self.fingerprint(desired[sid]))
        self.world.settle()
        return ops


class RunPins:
    """A replacement applies to a run only if that run opted in to following live changes; other runs keep their pinned configuration (PRD: Live Replacement)."""
    def __init__(self, reconciler: Reconciler):
        self.rec, self.runs = reconciler, {}

    def snapshot(self) -> dict[str, str]:
        return {sid: fp for sid, (_, fp) in sorted(self.rec.by_id.items())}

    def open(self, run_id: str, follow_live: bool = False) -> None:
        self.runs[run_id] = {"pinned": self.snapshot(), "follow_live": follow_live}

    def config_for(self, run_id: str) -> dict[str, str]:
        r = self.runs[run_id]
        return self.snapshot() if r["follow_live"] else dict(r["pinned"])



# ---------------------------------------------------------------------------------------------------------------------------------- declarative entries (paper §5.2.1)
class Entry:
    """Definition 81. `id` is the stable reconciliation key; `url` identifies the module (for HMR); `factory(config)` builds the component (the config is bound into the
    effect function); `children` makes the entry a group whose config is its list of child entries."""
    def __init__(self, id: str, factory: Callable[[Any], Component] | None = None, url: str | None = None, config: Any = None, disabled: bool = False,
                 isolate: dict | None = None, intercept: dict | None = None, children: list["Entry"] | None = None, on_config: Callable[[Any, Any], bool] | None = None):
        self.id, self.factory, self.url, self.config, self.disabled = id, factory, url or id, config, disabled
        self.isolate, self.intercept, self.children, self.on_config = dict(isolate or {}), dict(intercept or {}), children, on_config


class Loader:
    """Keeps a running World in step with a configuration tree, applying the least disruptive operation per changed field (paper §5.2.1):
    id/url -> rebuild; isolate -> reassigned in place; intercept -> updated in place (no reload); config -> handed to the component, which reloads only on a material change;
    disabled -> unload when set, a fresh fiber when cleared. A group's config is its child list, reconciled by a keyed diff over child ids."""
    def __init__(self, world: World, scope: Scope | None = None):
        self.w = world
        self.scope = scope or Scope(world)
        self.live: dict[str, dict] = {}          # entry id -> {"entry": Entry (as last applied), "fiber": name|None, "scope": Scope, "parent": fiber name|None}
        self.ops: list[tuple[str, str]] = []

    # ---- the group component: instantiating its children is an ordinary tracked effect of the group (Definition 52)
    def _component_for(self, e: Entry, parent: str | None) -> Component:
        if e.children is None: return e.factory(e.config)
        loader = self
        def effects(ctx):
            for child in list(loader.live[e.id]["entry"].children or []):
                if child.disabled: continue
                name = yield Instantiate(loader._component_for(child, ctx.name))
                loader.live[child.id] = {"entry": child, "fiber": name, "scope": loader._scope_for(child), "parent": ctx.name}
                loader._start_children(child, name)
        return Component(f"group:{e.id}", effects)

    def _start_children(self, e: Entry, fiber: str) -> None:
        pass                                      # a child that is itself a group instantiates its own children when it activates

    def _scope_for(self, e: Entry) -> Scope:
        sc = self.scope
        for k, r in e.isolate.items(): sc = sc.isolate(k, e.id if r is True else r)       # True: a realm local to the entry, tagged by its id; a string: a shared global realm
        for k, m in e.intercept.items(): sc = sc.intercept(k, m)
        return sc

    def _insert(self, e: Entry, parent: str | None) -> None:
        rec = {"entry": e, "fiber": None, "scope": self._scope_for(e), "parent": parent}
        self.live[e.id] = rec
        if e.disabled: return
        comp = self._component_for(e, parent)
        if parent is None: rec["fiber"] = self.w.insert(comp)
        else: rec["fiber"] = self.w.perform(parent, Instantiate(comp))

    def _dispose(self, eid: str) -> None:
        rec = self.live.pop(eid, None)
        if not rec: return
        for child in [c for c in list(self.live) if self.live[c]["parent"] == rec["fiber"] and rec["fiber"] is not None]: self.live.pop(child, None)
        if rec["fiber"] and rec["fiber"] in self.w.fibers:
            self.w.retire(rec["fiber"]); self.w.settle(); self.w.sweep()

    def apply(self, entries: list[Entry], parent: str | None = None, _scope_ids: set[str] | None = None) -> list[tuple[str, str]]:
        ops: list[tuple[str, str]] = []
        want = {e.id: e for e in entries}
        current = {i for i, r in self.live.items() if r["parent"] == parent}
        for eid in sorted(current - set(want)): self._dispose(eid); ops.append(("retire", eid))
        for e in entries:
            rec = self.live.get(e.id) if e.id in current else None
            if rec is None:
                self._insert(e, parent); ops.append(("insert" if not e.disabled else "declare", e.id)); continue
            old: Entry = rec["entry"]
            if old.url != e.url:                                                                                       # id/url: the component itself changed
                self._dispose(e.id); self._insert(e, parent); ops.append(("rebuild", e.id)); continue
            if old.isolate != e.isolate: rec["scope"] = self._scope_for(e); ops.append(("isolate", e.id))
            elif old.intercept != e.intercept: rec["scope"] = self._scope_for(e); ops.append(("intercept", e.id))       # in place: no reload
            if old.disabled != e.disabled:
                if e.disabled: self._dispose(e.id); self.live[e.id] = {"entry": e, "fiber": None, "scope": self._scope_for(e), "parent": parent}; ops.append(("disable", e.id))
                else: self._insert(e, parent); ops.append(("enable", e.id))                                        # a fresh fiber: the entry survives, the enablement is a new identity
                continue
            if old.children is not None and e.children is not None:
                rec["entry"] = e
                if rec["fiber"] is not None and self.w.fibers[rec["fiber"]].theta == "Active": ops += self.apply(e.children, rec["fiber"])       # keyed diff over child ids
                continue
            if old.config != e.config:
                if e.on_config is not None and e.on_config(old.config, e.config): ops.append(("config_in_place", e.id))
                else: self._dispose(e.id); self._insert(e, parent); ops.append(("reload", e.id)); continue
            rec["entry"] = e
        if parent is None: self.w.settle()                                    # Theorem 73: the system quiesces, so a reconciliation is complete once its operations are issued
        self.ops += ops
        return ops

    def fiber_of(self, eid: str) -> str | None:
        r = self.live.get(eid); return r["fiber"] if r else None


# ---------------------------------------------------------------------------------------------------------------------------------- hot module replacement (paper §5.2.2, Algorithms 8-10)
def classify(stashed: set[str], externals: set[str], get_imports: Callable[[str], set[str]]) -> tuple[set[str], set[str]]:
    """Algorithm 8. A module is accepted once one of its imports is accepted and declined once all its imports are declined; anything left undecided (an import cycle) is declined."""
    accepted, declined, pending = set(stashed), set(externals), set()
    for url in stashed: pending |= get_imports(url) - (accepted | declined)
    progress = True
    while progress:
        progress = False
        for url in sorted(pending):
            imps = get_imports(url)
            if imps & accepted: accepted.add(url); pending.discard(url); progress = True
            elif imps <= declined: declined.add(url); pending.discard(url); progress = True
            else: pending |= imps - (accepted | declined)
    return accepted, declined | pending


def get_dependencies(root: str, declined: set[str], get_imports: Callable[[str], set[str]]) -> set[str]:
    deps: set[str] = set()
    def traverse(url):
        if url in deps or url in declined: return
        deps.add(url)
        for c in sorted(get_imports(url)): traverse(c)
    traverse(root); return deps


def detect_stale(entries: list[Entry], accepted: set[str], declined: set[str], get_imports: Callable[[str], set[str]]) -> list[Entry]:
    """Algorithm 9: an entry is stale exactly when its dependency tree intersects `accepted`; that tree is then folded into `accepted`."""
    stale, accepted = [], set(accepted)
    for e in entries:
        tree = get_dependencies(e.url, declined, get_imports)
        if tree & accepted: accepted |= tree; stale.append(e)
    return stale


def hot_reload(loader: Loader, entries: list[Entry], modules: dict[str, Callable], stashed: set[str], externals: set[str], get_imports: Callable[[str], set[str]],
               import_module: Callable[[str], Callable]) -> list[str]:
    """Algorithm 10, transactional: if any module fails to import, the caches are restored and every stale entry is rebuilt from the backup, so the system never rests half-reloaded."""
    accepted, declined = classify(stashed, externals, get_imports)
    stale = detect_stale(entries, accepted, declined, get_imports)
    backup = {u: modules[u] for u in accepted if u in modules}
    for u in backup: del modules[u]                                           # invalidate
    done: list[Entry] = []
    try:
        for e in stale:
            loader._dispose(e.id)
            fresh = Entry(e.id, import_module(e.url), url=e.url, config=e.config, disabled=e.disabled, isolate=e.isolate, intercept=e.intercept)
            loader._insert(fresh, None); modules[e.url] = fresh.factory; done.append(fresh)
    except Exception:
        modules.update(backup)                                                # restore the caches ...
        for e in stale:                                                       # ... and rebuild every stale entry from the previous component
            loader._dispose(e.id)
            loader._insert(Entry(e.id, backup.get(e.url, e.factory), url=e.url, config=e.config, disabled=e.disabled, isolate=e.isolate, intercept=e.intercept), None)
        loader.w.settle()
        raise
    loader.w.settle()
    return [e.id for e in stale]
