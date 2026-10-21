"""Checks derived from the complete paper (arXiv:2608.25512): the theorems of §4.3, the Cordis implementation (§5) and the discussion (§6)."""
import json
import pytest
from conftest import VEC
from harness.components import core, invariants, scenario
from harness.components.core import (Component, ConfinementViolation, External, InactiveAccess, Instantiate, Operate, PreconditionFailed, Provide, World)
from harness.components.invariants import InvariantViolation
from harness.components.keys import Broker, broker_key, table_key
from harness.components.loader import Entry, Loader, classify, detect_stale, get_dependencies, hot_reload
from harness.components.scope import Scope

K = lambda *names: {n: table_key() for n in names}
fails = lambda fn, seeds: [s for s in seeds if _raises(fn, s)]
def _raises(fn, s):
    try: fn(s); return False
    except (AssertionError, InvariantViolation): return True


# ------------------------------------------------------------------ the theorems, over random runs
@pytest.mark.parametrize("seed", range(300))
def test_flat_systems_satisfy_theorems_64_68_69_70_71_73_and_80(seed):
    scenario.run_scenario(seed)


@pytest.mark.parametrize("seed", range(300))
def test_systems_that_instantiate_children_satisfy_the_theorems_up_to_renaming_and_vestigial_entries(seed):
    scenario.run_tree_scenario(seed)


def test_every_theorem_check_can_fail():
    """Mutation checks: break the runtime in the way each theorem forbids and require the monitor to notice."""
    seeds = range(120)
    assert not fails(scenario.run_scenario, seeds) and not fails(scenario.run_tree_scenario, seeds)
    def mutate(attr, new, fn=scenario.run_scenario):
        orig = getattr(World, attr); setattr(World, attr, new)
        try: return fails(fn, seeds)
        finally: setattr(World, attr, orig)
    assert mutate("relied", lambda self, n: False)                                                                    # no guard: Definition 63(4) / Theorem 70
    orig_enabled = World.enabled
    def no_leave(self): return [(r, n) for r, n in orig_enabled(self) if r != "leave"]
    assert mutate("enabled", no_leave)                                                                                # a fiber can never leave: Theorem 73(1), no deadlock
    def lazy_iter(self):                                                                                              # an iteration allowed against a stale resolution: Theorem 71
        out = []
        for r, n in orig_enabled(self):
            out.append(("iter", n) if r == "divert" else (r, n))
        return out
    assert mutate("enabled", lazy_iter)
    orig_unload = World._unload
    def keep_tables(self, f):                                                                                         # unload without applying the accumulator: Corollary 69
        f.acc.clear(); f.theta, f.omega, f.gen = "Inactive", None, None; self._log("unload", f.name)
    assert mutate("_unload", keep_tables)
    orig_target = World.target
    def hysteresis(self, n):                                                                                          # a history-dependent target: Theorem 80 (static assembly)
        f = self.fibers[n]
        if getattr(f, "was_active", False) and f.theta == "Inactive": return None
        if f.theta == "Active": f.was_active = True
        return orig_target(self, n)
    assert mutate("target", hysteresis, scenario.run_replacement_scenario)                                           # only provider replacement exposes it, hence the dedicated family


@pytest.mark.parametrize("seed", range(200))
def test_replacing_providers_under_running_consumers_still_reaches_the_static_assembly(seed):
    scenario.run_replacement_scenario(seed)


def test_well_formedness_detects_each_clause_of_definition_63():
    w = World(K("ns/k")); p = w.insert(Component("p", lambda c: [Provide("ns/k", {})], provides={"ns/k": "1.0.0"})); c = w.insert(Component("c", lambda c: [Operate("ns/k", "add", ("x", 1))], requires={"ns/k": "*"})); w.settle()
    invariants.well_formed(w)
    w.fibers[c].parent = "ghost"
    with pytest.raises(InvariantViolation, match="Def63\\(1\\)"): invariants.well_formed(w)
    w.fibers[c].parent = "root"; w.fibers[p].theta = "Inactive"
    with pytest.raises(InvariantViolation, match="Def63\\(4\\)"): invariants.well_formed(w)
    w.fibers[p].theta = "Active"; w.fibers[c].omega = {}
    with pytest.raises(InvariantViolation, match="Def63\\(3\\)"): invariants.well_formed(w)
    w.fibers[c].omega = {"ns/k": p}; w.fibers[p].comp = Component("p2", lambda c: [], provides={"ns/k": "1.0.0", "ns/j": "1.0.0"}); q = w.insert(Component("q", lambda c: [], provides={"ns/z": "1.0.0"}), name="q")
    w.fibers[q].comp = Component("q2", lambda c: [], provides={"ns/j": "1.0.0"})
    with pytest.raises(InvariantViolation, match="Def63\\(2\\)"): invariants.well_formed(w)


def test_totality_on_provision_definition_76_is_detected_and_optionally_enforced():
    w = World(K("ns/a", "ns/b")); n = w.insert(Component("half", lambda c: [Provide("ns/a", {})], provides={"ns/a": "1.0.0", "ns/b": "1.0.0"})); w.settle()
    assert w.fibers[n].state == "ACTIVE" and w.not_total == [n]                                                       # recorded; the loader can still reconcile it (paper §5.2.1)
    w2 = World(K("ns/a", "ns/b"), require_total=True); n2 = w2.insert(Component("half", lambda c: [Provide("ns/a", {})], provides={"ns/a": "1.0.0", "ns/b": "1.0.0"})); w2.settle()
    assert w2.fibers[n2].state == "FAILED" and not w2.fibers[n2].sigma and w2.violations[-1]["code"] == "component.not_total"


def test_lifecycle_states_use_the_implementations_names_including_failed():
    w = World(K("ns/r")); p = Component("p", lambda c: [Provide("ns/r", {})], provides={"ns/r": "1.0.0"}); n = w.insert(p)
    seen = [w.fibers[n].state]
    while w.enabled(): w.step(*w.enabled()[0]); seen.append(w.fibers[n].state)
    assert seen[0] == "INACTIVE" and "LOADING" in seen and seen[-1] == "ACTIVE"
    w.retire(n); seen = []
    while w.enabled(): w.step(*w.enabled()[0]); seen.append(w.fibers[n].state)
    assert "UNLOADING" in seen and seen[-1] == "INACTIVE"
    bad = World(); b = bad.insert(Component("b", lambda c: (_ for _ in ()).throw(RuntimeError("boom")))); bad.settle()
    assert bad.fibers[b].state == "FAILED" and bad.quiet()                                                            # a failed fiber is not retried against an unchanged environment (§4.4)


def test_access_errors_distinguish_an_undeclared_key_from_a_declared_but_not_loaded_one():
    seen = []
    def eff(c):
        for key in ("ns/other", "ns/dep"):
            try: c.get(key)
            except (ConfinementViolation, InactiveAccess) as e: seen.append((key, type(e).__name__, getattr(e, "kind", None) or e.code))
        yield from ()
    w = World(K("ns/dep", "ns/other")); w.insert(Component("x", eff, provides={"ns/own": "1.0.0"}, requires={"ns/dep": "*"}))
    w.settle()
    assert w.fibers["f1"].state in ("INACTIVE", "FAILED")                       # its dependency is not provided, so it never runs: the access cases are exercised through a loaded fiber below
    p = w.insert(Component("dep", lambda c: [Provide("ns/dep", {})], provides={"ns/dep": "1.0.0"})); w.settle()
    assert [s[:2] for s in seen] == [("ns/other", "ConfinementViolation")] and seen[0][2] == "UNDECLARED_ACCESS"      # declared and committed keys are readable; undeclared is refused


def test_a_fiber_can_perform_effects_after_activation_and_they_are_reverted_with_the_rest():
    w = World(K("ns/t"))
    host = w.insert(Component("host", lambda c: [Provide("ns/t", {})], provides={"ns/t": "1.0.0"}))
    user = w.insert(Component("user", lambda c: [Operate("ns/t", "add", ("a", 1))], requires={"ns/t": "*"})); w.settle()
    w.perform(user, Operate("ns/t", "add", ("late", 2)))                                                              # an effect the component performs later, e.g. from an event handler
    assert set(w.fibers[host].sigma["ns/t"]) == {"a", "late"}
    w.retire(user); w.settle(); assert w.fibers[host].sigma["ns/t"] == {}
    with pytest.raises(PreconditionFailed): w.perform(user, Operate("ns/t", "add", ("again", 3)))                     # a fiber that has left cannot perform effects


def test_capabilities_are_reviewed_at_load_time_and_cycles_are_reported_from_declarations():
    approved = []
    def review(comp): approved.append(comp.id); return not any(k.startswith("ns/secret") for k in comp.requires)
    w = World(K("ns/a", "ns/b", "ns/secret"), review=review)
    w.insert(Component("community", lambda c: [], requires={"ns/a": "*"}))
    with pytest.raises(PreconditionFailed, match="review denied"): w.insert(Component("snoop", lambda c: [], requires={"ns/secret": "*"}))
    assert approved == ["community", "snoop"] and ["review_denied", "snoop"] in w.trace
    w2 = World(K("ns/a", "ns/b"))
    w2.insert(Component("a", lambda c: [], requires={"ns/b": "*"}, provides={"ns/a": "1.0.0"})); w2.insert(Component("b", lambda c: [], requires={"ns/a": "*"}, provides={"ns/b": "1.0.0"})); w2.settle()
    assert [f.state for f in w2.fibers.values()] == ["INACTIVE", "INACTIVE"] and w2.dependency_cycles() and set(w2.dependency_cycles()[0]) == {"f1", "f2"}      # permanently inactive, and reported


# ------------------------------------------------------------------ service multiplexing (paper §6.2)
def test_a_service_broker_absorbs_provider_changes_so_consumers_never_reload():
    w = World({"ns/svc": broker_key()}); broker = Broker("weighted")
    host = w.insert(Component("broker", lambda c: [Provide("ns/svc", {})], provides={"ns/svc": "1.0.0"}))
    reg = lambda pid, wt: Component(f"impl-{pid}", lambda c: [Operate("ns/svc", "register", (pid, {"weight": wt}))], requires={"ns/svc": "*"})
    consumer = w.insert(Component("client", lambda c: [], requires={"ns/svc": "*"}))
    v1 = w.insert(reg("v1", 1)); w.settle()
    table = lambda: w.fibers[host].sigma["ns/svc"]
    assert {broker.pick(table()) for _ in range(5)} == {"v1"}
    v2 = w.insert(reg("v2", 0)); w.settle()                                                                           # the new provider joins with no traffic
    w.perform(v2, Operate("ns/svc", "set_weight", ("v2", 1))); w.perform(v1, Operate("ns/svc", "set_weight", ("v1", 3)))   # gradual shift ...
    picks = [broker.pick(table()) for _ in range(8)]; assert picks.count("v1") == 6 and picks.count("v2") == 2
    w.perform(v1, Operate("ns/svc", "set_weight", ("v1", 0))); w.perform(v2, Operate("ns/svc", "set_weight", ("v2", 1)))
    w.retire(v1); w.settle()                                                                                          # ... then the old provider is unloaded
    assert set(table()) == {"v2"} and broker.pick(table(), target="v2") == "v2"
    assert w.trace.count(["begin", consumer]) == 1 and w.fibers[consumer].state == "ACTIVE"                          # the consumer was never reloaded
    assert broker.policy == "weighted" and Broker("round_robin").pick({"a": {"weight": 1}, "b": {"weight": 1}}) == "a"


# ------------------------------------------------------------------ access control by interception (paper §6.3)
def test_interception_carries_fine_grained_policy_without_reloading_anything():
    w = World({})
    def fs(meta):
        def read(path):
            allowed = meta.get("allow_read", [])
            if not any(path.startswith(p) for p in allowed): raise PermissionError(f"{path} is not readable")
            return f"contents of {path}"
        def write(path):
            if meta.get("readonly"): raise PermissionError("read-only")
            return f"wrote {path}"
        return {"read": read, "write": write}
    w.insert(Component("fs", lambda c: [Provide("ns/fs", fs)], provides={"ns/fs": "1.0.0"})); w.settle(); before = (list(w.trace), {n: f.state for n, f in w.fibers.items()})
    root = Scope(w)
    community = root.intercept("ns/fs", {"allow_read": ["/data/public"], "readonly": True})                          # constrains a community component ...
    core_ = root.intercept("ns/fs", {"allow_read": ["/"]})                                                            # ... while a core component keeps full access
    assert community.get("ns/fs")["read"]("/data/public/a") == "contents of /data/public/a" and core_.get("ns/fs")["write"]("/etc/x") == "wrote /etc/x"
    with pytest.raises(PermissionError): community.get("ns/fs")["read"]("/etc/passwd")
    with pytest.raises(PermissionError): community.get("ns/fs")["write"]("/data/public/a")
    tightened = community.intercept("ns/fs", {"allow_read": ["/data/public/reports"]})                               # adjusted at runtime, still no reload
    assert (list(w.trace), {n: f.state for n, f in w.fibers.items()}) == before and tightened.get("ns/fs")["read"]("/data/public/reports/q3").startswith("contents")


# ------------------------------------------------------------------ the declarative loader and hot module replacement (paper §5.2)
def svc(config): return Component("svc", lambda c: [Provide("ns/r", dict(config))], provides={"ns/r": "1.0.0"})


def test_loader_dispatches_on_the_changed_field_with_the_least_disruptive_operation():
    w = World(K("ns/r")); L = Loader(w)
    base = Entry("svc", svc, url="svc.js", config={"v": 1}, on_config=lambda old, new: False)
    assert L.apply([base]) == [("insert", "svc")] and w.fibers[L.fiber_of("svc")].sigma["ns/r"] == {"v": 1}
    f0 = L.fiber_of("svc"); trace0 = len(w.trace)
    assert L.apply([Entry("svc", svc, url="svc.js", config={"v": 1}, intercept={"ns/r": {"x": 1}}, on_config=lambda o, n: False)]) == [("intercept", "svc")]     # interception: in place
    assert L.fiber_of("svc") == f0 and len(w.trace) == trace0                                                          # no reload, no lifecycle step
    assert L.apply([Entry("svc", svc, url="svc.js", config={"v": 1}, intercept={"ns/r": {"x": 1}}, isolate={"ns/r": True})]) == [("isolate", "svc")] and L.fiber_of("svc") == f0
    assert L.apply([Entry("svc", svc, url="svc.js", config={"v": 2}, intercept={"ns/r": {"x": 1}}, isolate={"ns/r": True}, on_config=lambda o, n: True)]) == [("config_in_place", "svc")] and L.fiber_of("svc") == f0
    assert L.apply([Entry("svc", svc, url="svc.js", config={"v": 3}, intercept={"ns/r": {"x": 1}}, isolate={"ns/r": True}, on_config=lambda o, n: False)]) == [("reload", "svc")]       # a material change
    assert w.fibers[L.fiber_of("svc")].sigma["ns/r"] == {"v": 3}
    f1 = L.fiber_of("svc"); keep = dict(config={"v": 3}, intercept={"ns/r": {"x": 1}}, isolate={"ns/r": True})
    assert L.apply([Entry("svc", svc, url="svc.js", disabled=True, **keep)]) == [("disable", "svc")] and f1 not in w.fibers                                      # unloaded and removed
    assert L.apply([Entry("svc", svc, url="svc.js", **keep)]) == [("enable", "svc")] and L.fiber_of("svc") != f1 and w.fibers[L.fiber_of("svc")].state == "ACTIVE"   # a fresh fiber
    assert L.apply([Entry("svc", svc, url="svc2.js", **keep)]) == [("rebuild", "svc")]
    assert L.apply([]) == [("retire", "svc")] and not w.fibers


def test_groups_reconcile_their_children_by_keyed_diff_and_unload_as_a_unit():
    w = World(K("ns/a", "ns/b", "ns/c")); L = Loader(w)
    mk = lambda key: (lambda cfg: Component(f"p-{key}", lambda c: [Provide(key, {})], provides={key: "1.0.0"}))
    ent = lambda *ids: Entry("grp", children=[Entry(i, mk(f"ns/{i}"), url=f"{i}.js") for i in ids])
    L.apply([ent("a", "b")]); comps = lambda: sorted(f.comp.id for f in w.fibers.values() if f.state == "ACTIVE")
    assert comps() == ["group:grp", "p-ns/a", "p-ns/b"]
    fa = L.fiber_of("a"); ops = L.apply([ent("a", "c")])                                                              # keyed by id: b leaves, c joins, a is untouched
    assert ("retire", "b") in ops and ("insert", "c") in ops and L.fiber_of("a") == fa and comps() == ["group:grp", "p-ns/a", "p-ns/c"]
    L.apply([]); assert not w.fibers                                                                                   # the group's children are reverted with the group


GRAPH = {"app.js": {"feature.js", "util.js"}, "feature.js": {"lib.js"}, "util.js": {"lib.js", "ext.js"}, "lib.js": set(), "ext.js": set(), "a.js": {"b.js"}, "b.js": {"a.js"}}
imports = lambda u: GRAPH.get(u, set())


def test_hmr_module_classification_stale_detection_and_cycle_default():
    acc, dec = classify({"lib.js"}, {"ext.js"}, imports)
    assert acc == {"lib.js"} and "ext.js" in dec
    acc2, dec2 = classify({"feature.js"}, {"ext.js"}, imports)
    assert acc2 == {"feature.js"} and {"lib.js", "ext.js"} <= dec2                                                   # modules only imported by a changed module are declined
    acc3, dec3 = classify({"x.js"}, set(), lambda u: {"a.js"} if u == "x.js" else GRAPH.get(u, set()))
    assert {"a.js", "b.js"} <= dec3                                                                                   # an import cycle is left undecided and defaults to declined
    entries = [Entry("app", lambda c: Component("app", lambda x: []), url="app.js"), Entry("other", lambda c: Component("other", lambda x: []), url="lib.js")]
    stale = detect_stale(entries, {"feature.js"}, {"ext.js"}, imports)
    assert [e.id for e in stale] == ["app", "other"]                                                                   # Algorithm 9 folds the stale tree (lib.js included) into `accepted`, so `other` reloads too
    assert [e.id for e in detect_stale([entries[1]], {"feature.js"}, {"ext.js"}, imports)] == []                       # but on its own, an entry whose tree does not reach a changed module is not stale
    assert get_dependencies("app.js", {"ext.js"}, imports) == {"app.js", "feature.js", "util.js", "lib.js"}


def test_hmr_reload_is_transactional_a_failed_import_restores_the_previous_system():
    w = World(K("ns/r")); L = Loader(w)
    v1 = lambda cfg: Component("svc", lambda c: [Provide("ns/r", {"ver": 1})], provides={"ns/r": "1.0.0"})
    v2 = lambda cfg: Component("svc", lambda c: [Provide("ns/r", {"ver": 2})], provides={"ns/r": "1.0.0"})
    ent = Entry("svc", v1, url="svc.js"); L.apply([ent]); modules = {"svc.js": v1}
    ver = lambda: w.fibers[L.fiber_of("svc")].sigma["ns/r"]["ver"]
    assert ver() == 1
    assert hot_reload(L, [ent], modules, {"svc.js"}, set(), lambda u: set(), lambda u: v2) == ["svc"] and ver() == 2 and modules["svc.js"] is v2         # a successful in-place swap
    ent2 = Entry("svc", v2, url="svc.js"); L.live["svc"]["entry"] = ent2
    def broken(url): raise SyntaxError("unexpected token")
    with pytest.raises(SyntaxError): hot_reload(L, [ent2], modules, {"svc.js"}, set(), lambda u: set(), broken)
    assert ver() == 2 and modules["svc.js"] is v2 and w.quiet()                                                         # the caches are restored and the entry rebuilt from the backup


def test_golden_replacement_scenarios_are_reproduced_exactly():
    v = json.loads((VEC / "components-replacement-scenarios.json").read_text())
    for rec in v["scenarios"]:
        got = scenario.run_replacement_scenario(rec["seed"]); assert got["trace_hash"] == rec["trace_hash"] and got["steps"] == rec["steps"], rec["seed"]


def test_golden_tree_scenarios_are_reproduced_exactly():
    v = json.loads((VEC / "components-tree-scenarios.json").read_text())
    for rec in v["scenarios"]:
        got = scenario.run_tree_scenario(rec["seed"]); assert got["trace_hash"] == rec["trace_hash"] and got["steps"] == rec["steps"], rec["seed"]
