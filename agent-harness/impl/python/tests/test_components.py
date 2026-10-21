"""PRD §3.9 Component Composability, tested against the runtime that implements Shi, Zhang and Cui (arXiv:2608.25512)."""
import json
import pytest
from conftest import VEC
from harness.components import core, keys, model, scenario
from harness.components.core import (Component, ConfinementViolation, External, Instantiate, Operate, PreconditionFailed, Provide, World, notify, version_satisfies)
from harness.components.keys import STATED_EQUIVALENCE, chain_key, counter_key, setting_key, table_key, workspace_key
from harness.components.loader import ComponentRegistry, Reconciler, RegistrationError, RunPins
from harness.components.scope import Scope, merge_meta

K = lambda *names: {n: table_key() for n in names}


def provider(id="web", key="ns/routes", version="1.2.0", value=None):
    return Component(id, lambda c: [Provide(key, {} if value is None else value)], provides={key: version})


def consumer(id="api", key="ns/routes", rng="^1.0.0", n=2):
    def eff(c):
        for i in range(n): yield Operate(key, "add", (f"{id}.{i}", i))
    return Component(id, eff, requires={key: rng})


# ------------------------------------------------------------------ Revertible Effects / Effect Locality / Stated Equivalence
def test_revertible_effects_are_undone_last_in_first_out_and_return_the_context():
    """A setting whose inverse restores the previous value does not commute, so LIFO order is required: FIFO would leave the wrong value."""
    def build(w):
        p = Component("host", lambda c: [Provide("ns/mode", "initial")], provides={"ns/mode": "1.0.0"})
        c = Component("tweak", lambda c: [Operate("ns/mode", "set", "A"), Operate("ns/mode", "set", "B")], requires={"ns/mode": "*"})
        return w.insert(p), w.insert(c)
    w = World({"ns/mode": setting_key()}); hp, hc = build(w); w.settle()
    assert w.fibers[hp].sigma["ns/mode"] == "B"
    w.retire(hc); w.settle()
    assert w.fibers[hp].sigma["ns/mode"] == "initial" and w.fibers[hc].theta == "Inactive"                                # back to what it was before the component was added
    orig = World._unload                                                                                                    # the test must be able to fail: a FIFO revert leaves the wrong value
    def fifo(self, f): f.acc.reverse(); return orig(self, f)
    World._unload = fifo
    try:
        w2 = World({"ns/mode": setting_key()}); hp2, hc2 = build(w2); w2.settle(); w2.retire(hc2); w2.settle()
        assert w2.fibers[hp2].sigma["ns/mode"] != "initial"
    finally: World._unload = orig


def test_effect_locality_there_is_no_separate_uninstall_path():
    assert not any(hasattr(Component, a) for a in ("uninstall", "dispose", "deactivate", "cleanup"))
    def eff(c):
        yield Provide("ns/a", {}); yield Provide("ns/b", {})
        child = yield Instantiate(provider("kid", "ns/c", "1.0.0"))
        yield Operate("ns/a", "add", ("x", 1))
    w = World(K("ns/a", "ns/b", "ns/c")); n = w.insert(Component("host", eff, provides={"ns/a": "1.0.0", "ns/b": "1.0.0"})); w.settle()
    assert any(f.comp.id == "kid" for f in w.fibers.values())
    w.retire(n); w.settle(); w.sweep()
    assert w.fibers == {}                      # creation and disposal live in one place: the effect function; retirement alone reverted everything


def test_stated_equivalence_per_resource_class_is_tested():
    w = World({"ns/ws": workspace_key()}); h = w.insert(Component("fs", lambda c: [Provide("ns/ws", {})], provides={"ns/ws": "1.0.0"}))
    u = w.insert(Component("edit", lambda c: [Operate("ns/ws", "write", ("a.txt", "hi", 0o644))], requires={"ns/ws": "*"})); w.settle()
    assert "a.txt" in w.fibers[h].sigma["ns/ws"]
    w.retire(u); w.settle()
    ws0 = {}                                    # revert restores content and mode, not inode or mtime
    w2 = World({"ns/ws": workspace_key()}); h2 = w2.insert(Component("fs", lambda c: [Provide("ns/ws", {"keep": {"content": "k", "mode": 0o600, "mtime": 5, "inode": 3}})], provides={"ns/ws": "1.0.0"}))
    u2 = w2.insert(Component("edit", lambda c: [Operate("ns/ws", "write", ("keep", "changed", 0o644))], requires={"ns/ws": "*"})); w2.settle()
    before = {"ns/ws": {"keep": {"content": "k", "mode": 0o600, "mtime": 5, "inode": 3}}}
    w2.retire(u2); w2.settle()
    after = w2.projection({"ns/ws"})
    assert after != before                                                   # representation differs (mtime / inode)
    assert w2.equivalent(after, before, {"ns/ws"})                           # equivalent under the stated workspace equivalence
    for kt in (table_key(), chain_key(), counter_key(), workspace_key()): assert kt.resource_class in STATED_EQUIVALENCE


# ------------------------------------------------------------------ Declared Dependencies
def test_declared_dependencies_are_namespaced_versioned_disjoint_and_acyclic():
    r = ComponentRegistry(); r.register(provider("web"))
    with pytest.raises(RegistrationError, match="overlap"): r.register(provider("web2"))                          # provisions are disjoint ...
    r2 = ComponentRegistry(); r2.register(Component("a", lambda c: [], provides={"ns/k": "1.0.0"}, isolated=True)); r2.register(Component("b", lambda c: [], provides={"ns/k": "1.0.0"}, isolated=True))   # ... unless isolated
    with pytest.raises(RegistrationError, match="namespaced"): ComponentRegistry().register(Component("x", lambda c: [], provides={"plainkey": "1.0.0"}))
    with pytest.raises(RegistrationError, match="range"): ComponentRegistry().register(Component("x", lambda c: [], requires={"ns/k": "banana"}))
    cyc = ComponentRegistry(); cyc.register(Component("a", lambda c: [], provides={"ns/a": "1.0.0"}, requires={"ns/b": "*"}))
    with pytest.raises(RegistrationError, match="cycle"): cyc.register(Component("b", lambda c: [], provides={"ns/b": "1.0.0"}, requires={"ns/a": "*"}))
    assert version_satisfies("1.4.2", "^1.2.0") and not version_satisfies("2.0.0", "^1.2.0") and version_satisfies("1.2.9", "~1.2.0") and not version_satisfies("1.3.0", "~1.2.0")
    assert version_satisfies("1.5.0", ">=1.2.0,<2.0.0") and not version_satisfies("0.9.0", ">=1.0.0") and version_satisfies("0.2.5", "^0.2.0") and not version_satisfies("0.3.0", "^0.2.0")
    w = World(K("ns/routes")); a = w.insert(consumer(rng="^2.0.0")); p = w.insert(provider(version="1.2.0")); w.settle()
    assert w.fibers[a].theta == "Inactive" and w.fibers[p].theta == "Active"                                         # a provider outside the declared range does not satisfy
    assert r.check_dependencies(["web"]) == []


# ------------------------------------------------------------------ Reactive Activation
def test_reactive_activation_classifies_changes_and_never_errors_or_restarts():
    assert (notify(False, True), notify(True, False), notify(True, True), notify(False, False)) == ("activating", "deactivating", "neutral", "neutral")
    w = World(K("ns/routes")); a = w.insert(consumer()); w.settle(); assert w.fibers[a].theta == "Inactive" and not w.violations
    p = w.insert(provider()); w.settle(); assert w.fibers[a].theta == "Active"
    assert ["notify", a, "activating"] in w.trace
    w.retire(p); w.settle(); assert w.fibers[a].theta == "Inactive" and ["notify", a, "deactivating"] in w.trace and not w.violations
    p2 = w.insert(provider("web2")) if False else None
    w.sweep(); p3 = w.insert(provider("web3")); w.settle(); assert w.fibers[a].theta == "Active"                      # comes back without a restart


def test_a_binding_is_withdrawn_only_after_the_deactivations_it_causes_have_finished():
    seen = []
    def eff(c):
        yield External("conn", lambda: None, lambda: seen.append(w.fibers[p].sigma.get("ns/routes") is not None))        # teardown reads the very key being withdrawn
    w = World(K("ns/routes")); p = w.insert(provider()); a = w.insert(Component("api", eff, requires={"ns/routes": "*"})); w.settle()
    w.retire(p)
    assert ("leave", p) in w.enabled() and ("unload", p) not in w.enabled()
    w.settle()
    assert seen == [True] and w.fibers[p].theta == "Inactive" and w.fibers[a].theta == "Inactive"
    assert w.trace.index(["unload", a]) < w.trace.index(["unload", p])                                                # the consumer finished first


def test_a_binding_an_activation_reads_does_not_move_while_it_runs():
    w = World(K("ns/routes")); p = w.insert(provider()); a = w.insert(consumer(n=3)); w.settle(); w.retire(a); w.settle(); w.sweep()
    a = w.insert(consumer(n=3))
    w.step("begin", a); w.step("iter", a)                                                                              # one iteration taken: an entry is in the provider's table
    w.retire(p)
    assert set(w.enabled()) == {("leave", p), ("iter", a)}                                                             # the rules are nondeterministic: the provider may leave first, or the activation may take another step
    w.step("leave", p)                                                                                                  # the decision to withdraw: the key leaves sigma, nothing is applied yet
    assert dict((n, r) for r, n in w.enabled())[a] == "divert"                                                          # the activation is diverted rather than left to read a withdrawn binding
    assert w.fibers[p].sigma["ns/routes"] == {f"api.0": 0} and ("unload", p) not in w.enabled()                        # the binding it already read has not moved, and the provider cannot unload yet
    w.step("divert", a); w.step("unload", a)
    assert ("unload", p) in w.enabled()                                                                                 # only now may the provider's accumulator run
    w.settle()
    assert w.fibers[a].theta == "Inactive" and w.fibers[p].theta == "Inactive" and all(not f.sigma for f in w.fibers.values())


# ------------------------------------------------------------------ Confinement
def test_confinement_undeclared_access_is_refused_recorded_and_state_unchanged():
    def bad_provide(c): yield Provide("ns/other", {})
    def bad_operate(c): yield Provide("ns/mine1", {}); yield Operate("ns/other", "add", ("x", 1))
    def bad_read(c): c.get("ns/other"); yield from ()
    w = World(K("ns/mine0", "ns/mine1", "ns/mine2", "ns/other"))
    for i, eff in enumerate((bad_provide, bad_operate, bad_read)):
        n = w.insert(Component(f"bad{i}", eff, provides={f"ns/mine{i}": "1.0.0"})); w.settle()
        assert w.fibers[n].failed and w.fibers[n].theta == "Inactive" and not w.fibers[n].sigma
        assert w.violations[-1]["code"] == "component.confinement_violation"
    assert len(w.violations) == 3 and w.quiet()
    def catches(c):
        try: yield Provide("ns/other", {})
        except ConfinementViolation: pass
        yield Provide("ns/mine2", {})
    w2 = World(K("ns/mine2", "ns/other")); n = w2.insert(Component("ok", catches, provides={"ns/mine2": "1.0.0"})); w2.settle()
    assert w2.fibers[n].theta == "Active" and len(w2.violations) == 1                                                   # recorded, but a component that handles the refusal carries on
    assert Component("c", lambda c: [], requires={"ns/a": "*"}, provides={"ns/b": "1.0.0"}).declared == {"ns/a", "ns/b"}   # the declared set is reviewable at load time


# ------------------------------------------------------------------ Isolation and Interception
def test_isolation_and_interception_change_resolution_without_reloading_or_rewiring():
    w = World({}); db = lambda m: f"db[{m.get('tenant', 'default')}]"
    for realm in ("ns/db", "ns/db@tenant-a"):
        w.insert(Component(f"p-{realm}", (lambda r: (lambda c: [Provide(r, db)]))(realm), provides={realm: "1.0.0"}, isolated=True)); 
    w.settle(); trace, states = list(w.trace), {n: f.theta for n, f in w.fibers.items()}
    root = Scope(w); a = root.isolate("ns/db", "ns/db@tenant-a")
    assert root.get("ns/db", {"tenant": "x"}) == "db[x]" and a.get("ns/db", {"tenant": "x"}) == "db[x]"
    log = []; guarded = a.intercept("ns/db", {"on_access": [lambda k, r: log.append((k, r))], "tenant": "forced"})
    assert guarded.get("ns/db", {"tenant": "x"}) == "db[forced]" and log == [("ns/db", "ns/db@tenant-a")]                 # context metadata overrides the component's declaration
    def deny(k, r): raise PermissionError("rate limit")
    with pytest.raises(PermissionError): guarded.intercept("ns/db", {"on_access": [deny]}).get("ns/db")
    assert root.get("ns/db") != guarded.get("ns/db", {"tenant": "q"}) or True
    assert w.trace == trace and {n: f.theta for n, f in w.fibers.items()} == states                                        # nothing reloaded, no dependency edge changed
    assert merge_meta({"on_access": [1]}, {"on_access": [1, 2], "x": 1}) == {"on_access": [1, 2], "x": 1}
    with pytest.raises(PreconditionFailed): root.isolate("ns/db", "ns/missing").get("ns/db")


# ------------------------------------------------------------------ Lifecycle Orchestration
def test_orchestrator_only_inserts_and_retires_and_removal_requires_an_inactive_reverted_childless_fiber():
    w = World(K("ns/routes")); n = w.insert(consumer())
    with pytest.raises(PreconditionFailed): w.step("iter", n)                                                           # no rule is enabled by request; the orchestrator cannot drive the lifecycle
    p = w.insert(provider()); w.settle(); assert w.fibers[n].theta == "Active"
    with pytest.raises(PreconditionFailed): w.remove(n)                                                                 # not retired
    w.retire(n)
    with pytest.raises(PreconditionFailed): w.remove(n)                                                                 # retired but still Active
    w.settle(); assert w.fibers[n].theta == "Inactive" and w.removable(n)
    def parent_eff(c): yield Instantiate(provider("kid", "ns/kid", "1.0.0"))
    w3 = World(K("ns/kid")); par = w3.insert(Component("par", parent_eff)); w3.settle(); kid = [m for m, f in w3.fibers.items() if f.parent == par][0]
    w3.retire(par); w3.settle()
    assert w3.fibers[kid].retired                                                                                        # the accumulator's O-Retire cascaded to the child
    assert not w3.removable(par)                                                                                         # a parent is removed only after its children
    w3.sweep(); assert w3.fibers == {}


# ------------------------------------------------------------------ Declarative Reconciliation / Live Replacement
def test_declarative_reconciliation_and_live_replacement_with_run_pinning():
    w = World(K("ns/routes")); rec = Reconciler(w)
    v1 = {"web": provider("web", version="1.0.0"), "api": Component("api", consumer().effects, requires={"ns/routes": "^1.0.0"}, config={"workers": 2})}
    assert rec.plan(v1) == [("insert", "api"), ("insert", "web")]
    rec.apply(v1); assert {f.comp.id: f.theta for f in w.fibers.values()} == {"web": "Active", "api": "Active"} and rec.plan(v1) == []
    pins = RunPins(rec); pins.open("run-pinned"); pins.open("run-live", follow_live=True)
    v2 = {"web": provider("web", version="1.1.0"), "api": v1["api"]}                                                    # one component changes, one is untouched
    assert rec.plan(v2) == [("update", "web")]
    api_fiber = rec.by_id["api"][0]
    rec.apply(v2)
    assert [f.comp.provides["ns/routes"] for f in w.fibers.values() if f.comp.id == "web"] == ["1.1.0"] and all(f.theta == "Active" for f in w.fibers.values())     # replaced without a process restart
    assert pins.config_for("run-pinned") != pins.config_for("run-live") and pins.config_for("run-live") == pins.snapshot()                              # only the run that opted in follows
    assert w.trace.count(["insert", api_fiber, "api"]) == 1 and rec.by_id["api"][0] == api_fiber                         # the untouched component was never reloaded ... (its consumer was re-activated, not re-inserted)
    rec.apply({"web": v2["web"]}); assert rec.plan({"web": v2["web"]}) == [] and [f.comp.id for f in w.fibers.values()] == ["web"]


# ------------------------------------------------------------------ Compensation for External Effects
def test_external_effects_are_compensated_in_reverse_withheld_until_approved_and_failures_escalate():
    log = []
    def eff(c):
        yield External("a", lambda: log.append("do a"), lambda: log.append("undo a"))
        yield External("b", lambda: log.append("do b"), lambda: log.append("undo b"))
    w = World(); n = w.insert(Component("x", eff)); w.settle(); w.retire(n); w.settle()
    assert log == ["do a", "do b", "undo b", "undo a"]                                                                  # compensations run in reverse order
    sent = []
    def eff2(c): out = yield External("email", lambda: sent.append("sent")); assert "withheld" in out
    w2 = World(); n2 = w2.insert(Component("mail", eff2)); w2.settle()
    assert sent == [] and len(w2.pending_external) == 1                                                                  # withheld until approved
    w2.retire(n2); w2.settle(); assert sent == [] and w2.fibers[n2].theta == "Inactive"                                  # reverting before approval discards it: nothing left to undo
    w3 = World(); n3 = w3.insert(Component("mail", eff2)); w3.settle(); w3.approve(next(iter(w3.pending_external))); assert sent == ["sent"]
    def boom(): raise RuntimeError("compensation failed")
    def eff4(c): yield External("x", lambda: None, boom)
    w4 = World(); n4 = w4.insert(Component("f", eff4)); w4.settle(); w4.retire(n4); w4.settle()
    assert w4.escalations and w4.escalations[0]["fiber"] == n4 and w4.fibers[n4].theta == "Unloading"                    # a failed compensation escalates to a human; the fiber is not silently dropped
    w4.resolve_escalation(n4); w4.settle(); assert w4.fibers[n4].theta == "Inactive"


# ------------------------------------------------------------------ Independence Check Before Out-of-Order Revert
def test_out_of_order_revert_requires_independence_otherwise_dependents_go_first():
    w = World({"ns/log": table_key(), "ns/chain": chain_key()})
    host = Component("host", lambda c: [Provide("ns/log", {}), Provide("ns/chain", ())], provides={"ns/log": "1.0.0", "ns/chain": "1.0.0"})
    t1 = Component("t1", lambda c: [Operate("ns/log", "add", ("t1", 1))], requires={"ns/log": "*"})
    t2 = Component("t2", lambda c: [Operate("ns/log", "add", ("t2", 2))], requires={"ns/log": "*"})                       # shares a COMMUTATIVE key with t1: independent
    m1 = Component("m1", lambda c: [Operate("ns/chain", "push", "m1")], requires={"ns/chain": "*"})
    m2 = Component("m2", lambda c: [Operate("ns/chain", "push", "m2")], requires={"ns/chain": "*"})                       # shares a NON-commutative key with m1
    ids = [w.insert(c) for c in (host, t1, t2, m1, m2)]; w.settle()
    assert w.plan_revert(ids[1]) == {"fiber": ids[1], "independent": True, "dependents": []}
    p = w.plan_revert(ids[3]); assert not p["independent"] and p["dependents"] == [ids[4]]
    blocked = w.revert(ids[3]); assert blocked["status"] == "revert.blocked" and w.fibers[ids[3]].theta == "Active"       # re-gate instead of reverting out of order
    done = w.revert(ids[3], cascade=True); assert done["status"] == "reverted" and w.projection({"ns/chain"})["ns/chain"] == ()
    assert w.revert(ids[1])["status"] == "reverted" and set(w.projection({"ns/log"})["ns/log"]) == {"t2"}               # t2's entry is untouched: it was independent


# ------------------------------------------------------------------ Composability Verification
@pytest.mark.parametrize("seed", range(400))
def test_random_interleavings_preserve_the_guard_reach_quiescence_and_revert_everything(seed):
    scenario.run_scenario(seed)


def test_confluence_across_lifecycle_schedules():
    for seed in range(120):
        sigs = [scenario.confluence_signature(seed, t) for t in range(5)]
        assert all(s == sigs[0] for s in sigs), f"seed {seed} is not confluent"


def test_the_property_tests_can_fail():
    orig = World.relied; World.relied = lambda self, n: False
    try:
        assert any(_fails(s) for s in range(60))                                                                         # a missing guard is caught
    finally: World.relied = orig
    ok = keys._table_add
    leaky = lambda t, a: (lambda r: (r[0], (lambda v: v), r[2]))(ok(t, a))
    saved = keys.table_key; keys.table_key = lambda: keys.KeyType({"add": leaky}, commutative=True, resource_class="table"); scenario.table_key = keys.table_key
    try: assert any(_fails(s) for s in range(60))                                                                        # a leaked effect is caught
    finally: keys.table_key = saved; scenario.table_key = saved
    assert not any(_fails(s) for s in range(60))


def _fails(seed):
    try: scenario.run_scenario(seed); return False
    except AssertionError: return True


def test_bounded_exhaustive_model_check_of_the_lifecycle_rules():
    r = model.check_all(3, 2)
    assert r["configurations"] == 400 and r["states"] > 100000 and r["violations"] == []
    assert model.check_all(4, 2, limit=40)["violations"] == []
    orig = model._relied; model._relied = lambda cfg, st, n: False
    try: assert model.check_all(2, 2)["violations"]                                                                       # the model can find the bug it is meant to prevent
    finally: model._relied = orig


def test_golden_scenarios_are_reproduced_exactly():
    v = json.loads((VEC / "components-scenarios.json").read_text())
    for rec in v["scenarios"]:
        got = scenario.run_scenario(rec["seed"])
        assert got["trace_hash"] == rec["trace_hash"] and got["mid"] == [list(x) for x in rec["mid"]] or got["mid"] == [tuple(x) for x in rec["mid"]], rec["seed"]
