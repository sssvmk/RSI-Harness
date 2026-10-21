"""The plugin interface (design §18.11): the harness defines the contract and polices every plugin; the capabilities themselves are supplied at run time."""
import asyncio, io, contextlib, json, sys
from pathlib import Path
import pytest
from conftest import events_of, oa
from plugin_fakes import Fake, OUT, make, manifest
from harness import plugins as P
from harness.components.core import Component, Provide
from harness.plugins import KINDS, PluginHost

HERE = Path(__file__).parent
PROC = lambda *extra: manifest("retrieval", "proc-retrieval", trust="subprocess", entry={"command": [sys.executable, str(HERE / "plugin_proc.py"), *extra]})
host = lambda profile="high", **kw: PluginHost(profile, trusted_in_process={f"{k}-fake" for k in KINDS}, **kw)


# ------------------------------------------------------------------ the contracts
def test_every_kind_defines_a_complete_contract_with_a_sample_that_satisfies_its_own_input_schema():
    assert set(KINDS) == {"proposer", "retrieval", "user_model", "computer_use", "research", "isolation", "assurance", "evaluator"}
    for k, v in KINDS.items():
        assert {"describe", "health"} <= set(v["methods"]) and v["min_profile"] in P.RANK and v["trust"] in P.TRUST and v["interface_version"].count(".") == 2, k
        for name, spec in v["methods"].items():
            assert set(spec) == {"input", "output", "sample", "timeout_s", "effect", "purpose", "returns_doc", "errors"} and spec["effect"] in ("read_only", "write"), (k, name)
            assert P._errors(spec["input"], spec["sample"]) == [], (k, name)
        assert set(OUT[k]) == set(v["methods"]) - {"describe", "health"}, k                                                                             # the test fakes cover every method


async def test_a_conforming_plugin_of_every_kind_loads_and_each_method_returns_what_the_contract_says():
    h = host()
    for kind in KINDS:
        handle = await h.load(manifest(kind))
        for name, spec in KINDS[kind]["methods"].items():
            r = await handle.call(name, spec["sample"]); assert P._errors(spec["output"], r.value) == [] and r.plugin_id == f"{kind}-fake" and r.method == name
    assert sorted(x.kind for x in h.handles.values()) == sorted(KINDS) and all(h.has(k) for k in KINDS)
    await h.close(); assert not h.handles and not h.world.fibers


async def test_what_a_model_or_an_index_produced_is_marked_untrusted_and_what_the_operators_infrastructure_reported_is_not():
    h = host(); r = await (await h.load(manifest("retrieval"))).call("query", KINDS["retrieval"]["methods"]["query"]["sample"]); iso = await (await h.load(manifest("isolation"))).call("verify", KINDS["isolation"]["methods"]["verify"]["sample"])
    assert (r.trust, iso.trust) == ("untrusted", "tool_trusted")


# ------------------------------------------------------------------ what is refused before a plugin is registered
@pytest.mark.parametrize("name,change,needle", [
    ("unknown kind", {"kind": "telepathy"}, "unknown plugin kind"), ("bad version", {"version": "1.0"}, "does not match"), ("bad id", {"id": "Bad Id"}, "does not match"),
    ("incompatible interface", {"interface_version": "2.0.0"}, "not compatible"), ("grants beyond the kind", {"grants": ["host"]}, "not allowed"),
    ("in_process without a factory", {"entry": {"module": "x"}}, "needs entry.module and entry.factory"), ("subprocess without a command", {"trust": "subprocess", "entry": {"module": "x", "factory": "y"}}, "needs entry.command")])
async def test_an_invalid_manifest_is_refused_and_nothing_is_registered(name, change, needle):
    h = host()
    with pytest.raises(P.ManifestInvalid, match=needle): await h.load({**manifest("retrieval"), **change}, Fake(manifest("retrieval")))
    assert not h.handles and not h.world.fibers and h.history[-1]["event"] == "plugin.rejected", name


async def test_only_one_plugin_of_a_kind_is_active_and_the_same_id_cannot_load_twice():
    h = PluginHost("core"); await h.load(manifest("retrieval", "a"), Fake(manifest("retrieval", "a")))
    with pytest.raises(P.ManifestInvalid, match="already loaded"): await h.load(manifest("retrieval", "b"), Fake(manifest("retrieval", "b")))
    with pytest.raises(P.ManifestInvalid, match="already loaded"): await h.load(manifest("retrieval", "a"), Fake(manifest("retrieval", "a")))


async def test_the_deployment_profile_decides_which_kinds_and_which_trust_levels_are_allowed():
    core = PluginHost("core")
    with pytest.raises(P.PolicyDenied, match="needs a high"): await core.load(manifest("research"), Fake(manifest("research")))
    with pytest.raises(P.PolicyDenied, match="needs a standard"): await core.load(manifest("computer_use"), Fake(manifest("computer_use")))
    await core.load(manifest("retrieval"), Fake(manifest("retrieval")))                                                                                     # retrieval needs nothing special, and core allows in-process code
    std = PluginHost("standard")
    with pytest.raises(P.PolicyDenied, match="out of process"): await std.load(manifest("retrieval"), Fake(manifest("retrieval")))                             # in-process code is not trusted by default
    await std.load(PROC()); assert std.has("retrieval")                                                                                                     # a subprocess plugin is
    await std.close(); trusted = PluginHost("standard", trusted_in_process={"retrieval-fake"}); await trusted.load(manifest("retrieval"), Fake(manifest("retrieval"))); assert trusted.has("retrieval")


async def test_the_code_a_manifest_names_must_be_the_code_that_is_loaded(tmp_path):
    f = tmp_path / "plug.py"; f.write_text("x = 1\n"); good = P.file_sha256(f)
    h = host(); m = manifest("retrieval", entry={"module": "plugin_fakes", "factory": "make", "code_sha256": "0" * 64})
    with pytest.raises(P.ManifestInvalid, match="does not match the hash"): await h.load(m, Fake(m), code_path=f)
    ok = manifest("retrieval", entry={"module": "plugin_fakes", "factory": "make", "code_sha256": good}); h2 = await h.load(ok, Fake(ok), code_path=f); assert h2.code_sha256 == good


# ------------------------------------------------------------------ conformance
@pytest.mark.parametrize("name,over,needle", [
    ("describe disagrees with the manifest", {"describe": {"id": "other", "kind": "retrieval", "version": "1.0.0"}}, "describe says"),
    ("health is not ok", {"health": {"ok": False}}, "health is not ok"),
    ("a method returns the wrong shape", {"query": {"passages": "none"}}, "query: output"),
    ("a method is missing a required field", {"index": {"indexed": 1}}, "index: output"),
    ("a method raises", {"forget": RuntimeError("down")}, "forget: RuntimeError")])
async def test_a_plugin_that_does_not_satisfy_the_contract_is_not_loaded(name, over, needle):
    h, m = host(), manifest("retrieval")
    with pytest.raises(P.ConformanceFailed, match=needle): await h.load(m, Fake(m, over))
    assert not h.handles and not h.world.fibers and h.history[-1]["event"] == "plugin.rejected", name


async def test_a_plugin_that_is_missing_a_method_or_hangs_is_not_loaded(monkeypatch):
    class Partial:
        def describe(self): return {"id": "retrieval-fake", "kind": "retrieval", "version": "1.0.0"}
        def health(self): return {"ok": True}
    h = host()
    with pytest.raises(P.ConformanceFailed, match="no method"): await h.load(manifest("retrieval"), Partial())
    monkeypatch.setitem(KINDS["retrieval"]["methods"]["index"], "timeout_s", 0.2)
    async def hang(**kw): await asyncio.sleep(5)
    with pytest.raises(P.ConformanceFailed, match="index: timed out"): await h.load(manifest("retrieval"), Fake(manifest("retrieval"), {"index": hang}))


# ------------------------------------------------------------------ calls go through the handle
async def test_input_is_validated_before_the_plugin_is_called_and_output_after():
    h = host(); m = manifest("retrieval"); fake = Fake(m); handle = await h.load(m, fake); fake.calls.clear()
    with pytest.raises(P.ContractViolation, match="input"): await handle.call("query", {"user": "u", "query": "q"})
    with pytest.raises(P.ContractViolation, match="no method"): await handle.call("delete_everything", {})
    assert fake.calls == []                                                                                                                                  # the plugin never saw a bad request
    fake.over["query"] = {"passages": [{"id": 1}]}
    with pytest.raises(P.ContractViolation, match="output"): await handle.call("query", KINDS["retrieval"]["methods"]["query"]["sample"])                     # and a bad answer is not passed on


async def test_a_slow_call_times_out_and_repeated_failures_quarantine_the_plugin(monkeypatch):
    monkeypatch.setitem(KINDS["retrieval"]["methods"]["query"], "timeout_s", 0.2)
    h = host(max_failures=3); m = manifest("retrieval"); fake = Fake(m); handle = await h.load(m, fake); sample = KINDS["retrieval"]["methods"]["query"]["sample"]
    async def slow(**kw): await asyncio.sleep(2)
    fake.over["query"] = slow
    for i in range(3):
        with pytest.raises(P.PluginTimeout): await handle.call("query", sample)
    assert handle.state == "quarantined" and not h.has("retrieval")
    with pytest.raises(P.PluginUnavailable, match="quarantined"): await handle.call("query", sample)                                                         # it is no longer called
    with pytest.raises(P.PluginUnavailable, match="no active retrieval"): h.get("retrieval")


async def test_a_success_resets_the_failure_count_so_a_flaky_plugin_is_not_quarantined_by_scattered_errors():
    h = host(max_failures=3); m = manifest("retrieval"); fake = Fake(m); handle = await h.load(m, fake); sample = KINDS["retrieval"]["methods"]["query"]["sample"]
    for _ in range(5):
        fake.over["query"] = RuntimeError("blip")
        for _ in range(2):
            with pytest.raises(P.PluginFailed): await handle.call("query", sample)
        fake.over.pop("query"); await handle.call("query", sample)
    assert handle.state == "active"


# ------------------------------------------------------------------ in a run: events, budget, pins, preflight
async def run_with_plugin(mk, script=None, manifest_over=None, **cfg):
    m = manifest("retrieval", **(manifest_over or {}))
    h = mk(script or [oa("d")], plugins={"load": [m], "required": ["retrieval"], **cfg}); run, loop = await h.create_run("u", "go"); return h, run, loop, h.plugins.get("retrieval")


async def test_every_call_in_a_run_is_audited_without_its_payload_and_charged_to_the_tool_budget(mk):
    h, run, loop, handle = await run_with_plugin(mk); before = (await run.proj("budgets"))["agent"]["tools"]["used"] if "tools" in (await run.proj("budgets"))["agent"] else 0
    await handle.call("query", {"user": "u", "query": "secret question", "k": 1, "filters": {}}, run=run)
    ev = events_of(h, run, "plugin.called")[0]["payload"]; assert (ev["plugin_id"], ev["kind"], ev["method"], ev["ok"], ev["trust"]) == ("retrieval-fake", "retrieval", "query", True, "untrusted") and "secret question" not in json.dumps(ev)
    st = (await run.proj("budgets"))["agent"]
    if "tools" in st: assert st["tools"]["used"] == before + 1 and st["tools"]["reserved"] == 0
    with pytest.raises(P.ContractViolation): await handle.call("query", {"user": "u"}, run=run)


async def test_a_plugin_call_that_would_exceed_the_budget_does_not_run(mk):
    h = mk([oa("d")], budgets={"turns": 10, "tokens": 100000, "tools": 1}, plugins={"load": [manifest("retrieval")]}); run, loop = await h.create_run("u", "go", budgets={"turns": 10, "tokens": 100000, "tools": 1}); handle = h.plugins.get("retrieval")
    fake = handle.impl.obj; fake.calls.clear(); sample = KINDS["retrieval"]["methods"]["query"]["sample"]
    await handle.call("query", sample, run=run)
    with pytest.raises(P.PluginBudgetExhausted): await handle.call("query", sample, run=run)
    assert [c for c in fake.calls if c[0] == "query"] and len([c for c in fake.calls if c[0] == "query"]) == 1                                                  # the second call never reached the plugin


async def test_failures_and_quarantine_are_recorded_in_the_run(mk):
    h, run, loop, handle = await run_with_plugin(mk, manifest_over={}); handle.impl.obj.over["query"] = RuntimeError("down"); sample = KINDS["retrieval"]["methods"]["query"]["sample"]
    for _ in range(3):
        with pytest.raises(P.PluginFailed): await handle.call("query", sample, run=run)
    assert [e["payload"]["consecutive_failures"] for e in events_of(h, run, "plugin.failed")] == [1, 2, 3] and events_of(h, run, "plugin.quarantined")[0]["payload"]["after_failures"] == 3


async def test_a_run_pins_the_plugins_it_uses_and_a_plugin_swapped_during_the_run_stops_it(mk):
    h, run, loop, handle = await run_with_plugin(mk); pin = run.pins.by_id["plugin.retrieval-fake"]; assert run.pins.resolve("plugin.retrieval-fake")["manifest"]["version"] == "1.0.0" and pin["version"] == 1
    await h.plugins.replace(manifest("retrieval", version="2.0.0"), Fake(manifest("retrieval", version="2.0.0")))                                            # someone swaps the plugin after the run was pinned
    await h.rm.start(run); await loop.run_loop()
    mm = events_of(h, run, "definition.mismatch")[0]["payload"]; assert mm["id"] == "plugin.retrieval-fake" and "changed after the run was pinned" in mm["reason"] and not events_of(h, run, "model.requested")


async def test_a_plugin_that_was_removed_during_the_run_stops_it(mk):
    h, run, loop, handle = await run_with_plugin(mk); await h.plugins.unload("retrieval-fake"); await h.rm.start(run); await loop.run_loop()
    assert "no longer loaded" in events_of(h, run, "definition.mismatch")[0]["payload"]["reason"] and not events_of(h, run, "model.requested")


async def test_preflight_requires_the_plugin_kinds_a_deployment_says_it_needs(mk):
    h = mk([oa("d")], plugins={"required": ["retrieval", "proposer"]}); await h.load_plugins()
    pf = {c["id"]: c for c in h.preflight()["checks"]}; assert not pf["PF-P01"]["ok"] and "no active retrieval plugin" in pf["PF-P01"]["message"] and "PF-P01" in {c["id"] for c in h.preflight()["failed"]}
    h2 = mk([oa("d")], plugins={"load": [manifest("retrieval")], "required": ["retrieval"]}); await h2.load_plugins(); assert "PF-P01" not in {c["id"] for c in h2.preflight()["failed"]}
    assert "PF-P01" not in {c["id"] for c in mk([oa("d")]).preflight()["failed"]}                                                                           # nothing required, nothing to check


async def test_a_plugin_that_fails_verification_stops_startup_instead_of_being_skipped(mk):
    bad = manifest("retrieval", entry={"module": "plugin_fakes", "factory": "make"}); bad["interface_version"] = "9.0.0"
    h = mk([oa("d")], plugins={"load": [bad]})
    with pytest.raises(P.ManifestInvalid): await h.create_run("u", "go")


# ------------------------------------------------------------------ the component runtime: features follow plugins
async def test_a_feature_that_needs_a_plugin_activates_when_it_loads_and_deactivates_when_it_is_removed():
    h = host(); feat = Component("feature.search", lambda c: [Provide("feature.search", {})], requires={"plugin.retrieval": "^1.0.0"}, provides={"feature.search": "1.0.0"}); n = h.world.insert(feat); h.world.settle()
    assert h.world.fibers[n].state != "ACTIVE"                                                                                                              # nothing provides retrieval yet
    await h.load(manifest("retrieval", version="1.4.0"), Fake(manifest("retrieval", version="1.4.0"))); assert h.world.fibers[n].state == "ACTIVE"
    await h.unload("retrieval-fake"); assert h.world.fibers[n].state != "ACTIVE" and not h.has("retrieval")
    await h.load(manifest("retrieval", version="2.0.0"), Fake(manifest("retrieval", version="2.0.0"))); assert h.world.fibers[n].state != "ACTIVE"            # a 2.x plugin does not satisfy ^1.0.0


async def test_replace_swaps_the_plugin_of_a_kind():
    h = host(); await h.load(manifest("retrieval", version="1.0.0"), Fake(manifest("retrieval", version="1.0.0"))); await h.replace(manifest("retrieval", version="1.1.0"), Fake(manifest("retrieval", version="1.1.0")))
    assert h.get("retrieval").manifest["version"] == "1.1.0" and len(h.handles) == 1 and h.definitions()["plugin.retrieval-fake"]["manifest"]["version"] == "1.1.0"


# ------------------------------------------------------------------ out of process
async def test_a_plugin_in_its_own_process_speaks_the_same_contract_and_is_killed_when_it_hangs(monkeypatch):
    h = PluginHost("standard"); handle = await h.load(PROC()); r = await handle.call("query", KINDS["retrieval"]["methods"]["query"]["sample"])
    assert r.value["passages"][0]["text"] == "from a process" and r.trust == "untrusted" and handle.impl.proc.returncode is None
    pid = handle.impl.proc.pid; await h.unload("proc-retrieval"); await asyncio.sleep(0.1)
    with pytest.raises(ProcessLookupError): __import__("os").kill(pid, 0)                                                                                    # unloading kills it
    slow = PluginHost("standard"); monkeypatch.setitem(KINDS["retrieval"]["methods"]["query"], "timeout_s", 0.5)
    with pytest.raises(P.ConformanceFailed, match="query: timed out"): await slow.load(PROC("--slow"))                                                       # a hanging process fails conformance, and is not left running


async def test_a_subprocess_that_reports_an_error_or_dies_is_a_plugin_failure_not_a_crash():
    h = PluginHost("standard"); handle = await h.load(PROC())
    with pytest.raises(P.PluginFailed, match="boom"): await handle.impl.rpc("boom", {})
    handle.impl.proc.kill(); await handle.impl.proc.wait()
    r = await handle.call("query", KINDS["retrieval"]["methods"]["query"]["sample"]); assert r.value["passages"]                                              # the next call starts a fresh process
    await h.close()


def test_the_plugins_command_lists_every_kind_and_what_it_needs():
    from harness import cli
    out = io.StringIO()
    with contextlib.redirect_stdout(out): assert cli.main(["plugins", "--methods"]) == 0
    text = out.getvalue(); assert all(k in text for k in KINDS) and "research" in text and "needs high" in text and "query(read)" in text and "index(write)" in text


# ------------------------------------------------------------------ the evaluator kind (design §13.4)
EV = lambda type, **kw: manifest("evaluator", "ev1", evaluator={"type": type, **kw})
AGENT = {"model_ref": "agent-model", "credential_ref": "agent-key", "context": "fresh"}


@pytest.mark.parametrize("name,ev,needle", [("no evaluator block", None, "must declare evaluator.type"), ("a model judge without a model", {"type": "llm_judge", "credential_ref": "k"}, "must declare the model_ref and credential_ref"),
    ("a typed AI without a credential", {"type": "typesafe_ai", "model_ref": "m"}, "must declare the model_ref and credential_ref"), ("a code evaluator that names a model", {"type": "code", "model_ref": "m"}, "uses no model"),
    ("an unknown type", {"type": "oracle"}, "oracle")])
async def test_an_evaluators_manifest_must_declare_its_type_and_what_a_model_based_one_uses(name, ev, needle):
    m = manifest("evaluator"); m.pop("evaluator")
    if ev is not None: m["evaluator"] = ev
    with pytest.raises(P.ManifestInvalid, match=needle): await host().load(m, Fake(m))
    notev = manifest("retrieval", evaluator={"type": "code"})
    with pytest.raises(P.ManifestInvalid, match="only an evaluator plugin"): await host().load(notev, Fake(notev))


def test_independence_is_derived_from_the_agent_under_test_and_never_declared():
    code = P.independence_level(EV("code"), AGENT); assert (code["level"], code["shared"]) == ("deterministic", [])
    fresh = lambda **kw: P.independence_level(EV("llm_judge", model_ref="judge-model", credential_ref="judge-key", context="fresh", **kw), AGENT)
    assert fresh()["level"] == "independent_model" and fresh()["shared"] == []
    for name, ev, shared in [("the same model", {"model_ref": "agent-model", "credential_ref": "judge-key", "context": "fresh"}, ["model"]), ("the same credential", {"model_ref": "judge-model", "credential_ref": "agent-key", "context": "fresh"}, ["credential"]),
                             ("the agent's context", {"model_ref": "judge-model", "credential_ref": "judge-key", "context": "shared"}, ["context"]), ("all three", {"model_ref": "agent-model", "credential_ref": "agent-key", "context": "shared"}, ["model", "credential", "context"])]:
        r = P.independence_level(EV("typesafe_ai", **ev), AGENT); assert (r["level"], r["shared"], r["type"]) == ("same_model_low_independence", shared, "typesafe_ai"), name
    assert P.independence_level(EV("llm_judge", model_ref="m", credential_ref="k"), AGENT)["shared"] == ["context"]                                          # an unstated context is treated as shared, not assumed fresh


def test_what_may_gate_depends_on_the_independence_level_and_the_profile():
    D, I, L = "deterministic", "independent_model", "same_model_low_independence"
    assert [P.may_gate(l, "core")[0] for l in (D, I, L)] == [True, True, True]                                                                                    # core labels only
    assert [P.may_gate(l, "standard")[0] for l in (D, I, L)] == [True, True, False]
    assert [P.may_gate(l, "high")[0] for l in (D, I, L)] == [True, True, False]
    assert not P.may_gate(I, "high", material_claim=True)[0] and "second independent evaluator" in P.may_gate(I, "high", material_claim=True)[1]
    assert P.may_gate(I, "high", material_claim=True, backed=True)[0] and P.may_gate(D, "high", material_claim=True)[0] and P.may_gate(I, "standard", material_claim=True)[0]
    assert "advisory" in P.may_gate(L, "standard")[1]


async def test_an_evaluator_of_each_type_loads_and_its_verdict_is_one_of_four():
    for t, kw in (("code", {}), ("llm_judge", {"model_ref": "m", "credential_ref": "k", "context": "fresh"}), ("typesafe_ai", {"model_ref": "m", "credential_ref": "k", "context": "fresh"})):
        h = PluginHost("core"); m = EV(t, **kw); handle = await h.load(m, Fake(m)); r = await handle.call("evaluate", KINDS["evaluator"]["methods"]["evaluate"]["sample"])
        assert r.value["verdict"] in ("PASS", "FAIL", "PARTIAL", "UNCERTAIN") and r.trust == "untrusted", t
    bad = EV("code"); h = PluginHost("core")
    with pytest.raises(P.ConformanceFailed, match="evaluate: output"): await h.load(bad, Fake(bad, {"evaluate": {"verdict": "MAYBE", "reason_codes": [], "evidence_refs": []}}))                       # a verdict outside the four is refused
    with pytest.raises(P.ConformanceFailed, match="evaluate: output"): await h.load(bad, Fake(bad, {"evaluate": {"verdict": "PASS"}}))                                                    # and so is one without reasons and evidence


# ------------------------------------------------------------------ exfiltration monitoring: partial in the core, payload inspection only when demonstrated (design §10.6)
GOOD = {"inspection_point": "egress proxy (endpoint instrumentation before encryption)", "policy_applied": True, "audit_records": True, "coverage_gaps": ["QUIC"], "bypass_conditions": ["a process that opens a raw socket"], "verified": True}


def test_the_core_claims_partial_conformance_and_payload_inspection_only_on_demonstration():
    assert P.exfiltration_claim(None)["claim"] == "partial" and P.exfiltration_claim({})["claim"] == "partial" and "no payload inspection point" in P.exfiltration_claim({})["reasons"][0]
    assert P.exfiltration_claim({"tls_interception": True})["claim"] == "partial"                                                                       # supporting interception is not enough
    ok = P.exfiltration_claim({"egress_inspection": GOOD}); assert (ok["claim"], ok["reasons"], ok["coverage_gaps"], ok["bypass_conditions"]) == ("payload_inspection", [], ["QUIC"], ["a process that opens a raw socket"])
    for name, change, needle in [("no point", {"inspection_point": ""}, "no inspection point"), ("policy not applied", {"policy_applied": False}, "policy is not applied"), ("not audited", {"audit_records": False}, "not recorded for audit"),
                                 ("gaps undocumented", {"coverage_gaps": None}, "coverage gaps are not documented"), ("bypasses undocumented", {"bypass_conditions": "none"}, "bypass conditions are not documented"), ("unverified", {"verified": False}, "not been verified")]:
        r = P.exfiltration_claim({"egress_inspection": {**GOOD, **change}}); assert r["claim"] == "partial" and any(needle in x for x in r["reasons"]), name
    assert P.exfiltration_claim({"egress_inspection": "yes"})["claim"] == "partial" and P.exfiltration_claim({"egress_inspection": {**GOOD, "coverage_gaps": []}})["claim"] == "payload_inspection"      # an empty list is documented; a missing one is not
    assert {P.exfiltration_claim(o)["claim"] for o in (None, {}, {"egress_inspection": GOOD})} <= {"partial", "payload_inspection"}                    # there is no 'complete' claim to make


# ------------------------------------------------------------------ conformance runs on the live plugin, so it must not touch real data
async def test_conformance_uses_reserved_ids_for_anything_that_changes_state_and_never_a_real_ones():
    m = manifest("retrieval"); f = Fake(m); await host().load(m, f)
    assert [kw for n, kw in f.calls if n == "index"] == [{"documents": [{"id": "__conformance_doc__", "text": "conformance check"}]}] and [kw for n, kw in f.calls if n == "forget"] == [{"ids": ["__conformance_doc__"]}]
    m2 = manifest("user_model"); f2 = Fake(m2); await host().load(m2, f2); assert {kw["user"] for n, kw in f2.calls if "user" in kw} == {"__conformance_user__"}                          # no real user is observed, predicted or forgotten


async def test_conformance_uses_the_ids_the_plugin_created_and_never_expects_a_particular_one():
    from harness.plugin_api import PluginRejection
    def needs(created, field, out):
        def fn(**kw):
            if kw[field] != created: raise PluginRejection("not_found", f"{kw[field]!r}")
            return out
        return fn
    cases = {"computer_use": ({"open": {"session": "abc-123"}, "observe": needs("abc-123", "session", {"observation": {}}), "act": needs("abc-123", "session", {"observation": {}}), "close": needs("abc-123", "session", {"closed": True})}, "session"),
             "research": ({"fork": {"fork_id": "fk-7"}, "step": needs("fk-7", "fork_id", {"state": {}}), "stop": needs("fk-7", "fork_id", {"stopped": True})}, "fork_id"),
             "isolation": ({"provision": {"sandbox_id": "box-9", "effective": {}}, "verify": needs("box-9", "sandbox_id", {"ok": True, "observed": {}}), "destroy": needs("box-9", "sandbox_id", {"destroyed": True})}, "sandbox_id")}
    for kind, (over, field) in cases.items():
        m = manifest(kind); f = Fake(m, over); h = host(); await h.load(m, f); assert h.has(kind), kind                                                                                 # a plugin that hands out its own ids loads
        used = [kw[field] for n, kw in f.calls if field in kw]; assert used and set(used) == {"abc-123", "fk-7", "box-9"} & set(used) and len(set(used)) == 1, kind                    # and every later call used that id
    wrong = Fake(manifest("computer_use"), {"open": {"session": "abc-123"}, "observe": needs("something-else", "session", {"observation": {}})})
    with pytest.raises(P.ConformanceFailed, match="observe"): await host().load(manifest("computer_use"), wrong)                                                                          # and a plugin that breaks its own ids does not load
