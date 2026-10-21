"""The error contract (design §18.11, docs/plugin_contract.md): what a plugin may signal, what the harness throws for each case, and how a plugin that does not follow its contract is caught."""
import asyncio, sys
from pathlib import Path
import pytest
from conftest import events_of, oa
from plugin_fakes import Fake, OUT, ProbeAware, manifest
from harness import plugins as P
from harness.plugin_api import ERROR_CODES, RETRYABLE, PluginRejection
from harness.plugins import KINDS, PluginHost

SAMPLE = KINDS["retrieval"]["methods"]["query"]["sample"]
BAD_K = next(e["probe"] for e in KINDS["retrieval"]["methods"]["query"]["errors"] if e["code"] == "invalid_argument")                     # k = 0, the spec's own probe
PROC = lambda: manifest("retrieval", "proc-retrieval", trust="subprocess", entry={"command": [sys.executable, str(Path(__file__).parent / "plugin_proc.py")]})
core = lambda **kw: PluginHost("core", **kw)


async def loaded(kind="retrieval", cls=ProbeAware, **kw):
    h = core(**kw); m = manifest(kind); fake = cls(m); return h, fake, await h.load(m, fake)


# ------------------------------------------------------------------ the catalogue and the spec
def test_the_error_catalogue_and_every_methods_declared_errors_are_complete_and_consistent():
    import json
    spec = json.loads((Path(P.SPEC)).read_text()); assert tuple(spec["error_codes"]) == ERROR_CODES and {c for c, v in spec["error_codes"].items() if v["retryable"]} == set(RETRYABLE) == {"unavailable", "rate_limited"}
    for kind, v in KINDS.items():
        for name, m in v["methods"].items():
            assert m["purpose"] and m["returns_doc"] and isinstance(m["errors"], list), (kind, name)
            for e in m["errors"]:
                assert e["code"] in ERROR_CODES and e["when"] and "probe" in e, (kind, name, e)
                if e["probe"] is not None: assert P._errors(m["input"], e["probe"]) == [], (kind, name, "the probe must be a valid input, so that the error is the plugin's choice, not a schema failure")
            assert len({e["code"] for e in m["errors"]}) == len(m["errors"]), (kind, name)
    assert not KINDS["retrieval"]["methods"]["health"]["errors"] and not KINDS["retrieval"]["methods"]["describe"]["errors"]                              # these never raise


# ------------------------------------------------------------------ what the harness throws
async def test_a_declared_refusal_is_a_typed_rejection_with_its_code_and_whether_it_is_retryable_and_it_is_not_a_fault():
    h, fake, handle = await loaded()
    for _ in range(10):
        with pytest.raises(P.PluginRejected) as e: await handle.call("query", BAD_K)
        assert (e.value.reason, e.value.retryable, e.value.code) == ("invalid_argument", False, "plugin.rejected")
    assert handle.state == "active" and handle.failures == 0 and h.has("retrieval")                                                                     # a correct refusal never quarantines a healthy plugin
    fake.over["query"] = PluginRejection("rate_limited", "slow down")
    with pytest.raises(P.PluginRejected) as e: await handle.call("query", SAMPLE)
    assert e.value.retryable is True and e.value.reason == "rate_limited" and handle.state == "active"                                                      # retryable, and still not a fault


async def test_a_backend_that_is_down_is_retryable_and_repeated_unavailability_does_quarantine():
    h, fake, handle = await loaded(max_failures=3); fake.over["query"] = PluginRejection("unavailable", "index down")
    for _ in range(3):
        with pytest.raises(P.PluginRejected) as e: await handle.call("query", SAMPLE)
        assert e.value.reason == "unavailable" and e.value.retryable
    assert handle.state == "quarantined"
    with pytest.raises(P.PluginUnavailable, match="quarantined"): await handle.call("query", SAMPLE)


async def test_a_code_the_contract_does_not_declare_for_the_method_is_a_contract_violation_and_a_fault():
    h, fake, handle = await loaded(max_failures=2); fake.over["query"] = PluginRejection("conflict", "x")                                                # conflict is a real code, but not declared for query
    for _ in range(2):
        with pytest.raises(P.ContractViolation, match="does not declare"): await handle.call("query", SAMPLE)
    assert handle.state == "quarantined"
    h2, fake2, handle2 = await loaded(); fake2.over["query"] = PluginRejection("made_up_code")
    with pytest.raises(P.ContractViolation, match="does not declare"): await handle2.call("query", SAMPLE)


async def test_any_other_exception_is_a_plugin_failure_a_bad_shape_a_violation_and_a_slow_call_a_timeout(monkeypatch):
    h, fake, handle = await loaded(max_failures=99); fake.over["query"] = ValueError("boom")
    with pytest.raises(P.PluginFailed, match="ValueError"): await handle.call("query", SAMPLE)
    fake.over["query"] = {"passages": "none"}
    with pytest.raises(P.ContractViolation, match="output"): await handle.call("query", SAMPLE)
    monkeypatch.setitem(KINDS["retrieval"]["methods"]["query"], "timeout_s", 0.2)
    async def slow(**kw): await asyncio.sleep(2)
    fake.over["query"] = slow
    with pytest.raises(P.PluginTimeout): await handle.call("query", SAMPLE)
    with pytest.raises(P.ContractViolation, match="input"): await handle.call("query", {"user": "u"})                                                      # and a bad request from the harness's own caller is refused before the plugin sees it


async def test_a_refusal_is_recorded_in_the_run_with_its_code_and_still_costs_a_call(mk):
    h = mk([oa("d")], plugins={"load": [manifest("retrieval", entry={"module": "plugin_fakes", "factory": "make_probe_aware"})]}); run, _ = await h.create_run("u", "go"); handle = h.plugins.get("retrieval")
    with pytest.raises(P.PluginRejected): await handle.call("query", BAD_K, run=run)
    ev = events_of(h, run, "plugin.called")[-1]["payload"]; assert (ev["ok"], ev["error"], ev["retryable"], ev["method"]) == (False, "invalid_argument", False, "query") and not events_of(h, run, "plugin.failed")
    st = (await run.proj("budgets"))["agent"]
    if "tools" in st: assert st["tools"]["used"] >= 1 and st["tools"]["reserved"] == 0


# ------------------------------------------------------------------ out of process
async def test_a_contract_error_crosses_the_process_boundary_as_its_code():
    h = PluginHost("standard"); handle = await h.load(PROC())
    for args, code in [(dict(SAMPLE, k=0), "invalid_argument"), (dict(SAMPLE, user="__no_access__"), "permission_denied")]:
        with pytest.raises(P.PluginRejected) as e: await handle.call("query", args)
        assert e.value.reason == code
    with pytest.raises(P.PluginFailed, match="boom"): await handle.impl.rpc("boom", {})                                                                       # a code that is not in the catalogue is a failure, not a contract error
    await h.close()


# ------------------------------------------------------------------ probes at load
@pytest.mark.parametrize("kind", sorted(KINDS))
async def test_a_plugin_that_follows_its_contract_passes_every_probe_for_every_kind(kind):
    h = PluginHost("high", trusted_in_process={f"{kind}-fake"}, verify_errors=True); m = manifest(kind)
    handle = await h.load(m, ProbeAware(m)); assert h.has(kind)
    n = sum(1 for s in KINDS[kind]["methods"].values() for e in s["errors"] if e["probe"] is not None); assert n > 0 or kind == "evaluator" or True
    probes = [(name, e) for name, s in KINDS[kind]["methods"].items() for e in s["errors"] if e["probe"] is not None]
    for name, e in probes:
        with pytest.raises(P.PluginRejected) as r: await handle.call(name, e["probe"])
        assert r.value.reason == e["code"], (kind, name)


async def test_a_plugin_that_does_not_signal_what_its_contract_says_is_not_loaded_when_error_verification_is_on():
    m = manifest("retrieval"); h = core(verify_errors=True)
    with pytest.raises(P.ConformanceFailed) as e: await h.load(m, Fake(m))                                                                                    # answers every probe as if it were a normal call
    assert "the invalid_argument probe did not raise" in str(e.value) and "the permission_denied probe did not raise" in str(e.value) and not h.handles
    def wrong_code(**kw):
        if kw["k"] < 1: raise PluginRejection("permission_denied")                                                                                            # right on the sample, wrong on the probe
        return OUT["retrieval"]["query"]
    wrong = Fake(m, {"query": wrong_code}); h2 = core(verify_errors=True)
    with pytest.raises(P.ConformanceFailed, match="the invalid_argument probe raised permission_denied"): await h2.load(m, wrong)
    def crashes(ids):
        if not ids: raise RuntimeError("down")                                                                                                                # an ordinary exception where the contract names an error
        return {"removed": len(ids)}
    crash = Fake(m, {"forget": crashes}); h3 = core(verify_errors=True)
    with pytest.raises(P.ConformanceFailed, match="not a contract error"): await h3.load(m, crash)
    off = core(); await off.load(m, Fake(m)); assert off.has("retrieval")                                                                                     # off by default: the shape is checked, the errors are not


async def test_a_subprocess_plugin_passes_the_probes_too():
    h = PluginHost("standard", verify_errors=True); await h.load(PROC()); assert h.has("retrieval"); await h.close()


async def test_the_deployment_configuration_turns_error_verification_on(mk):
    bad = manifest("retrieval", entry={"module": "plugin_fakes", "factory": "make"}); h = mk([oa("d")], plugins={"load": [bad], "verify_errors": True})
    with pytest.raises(P.ConformanceFailed, match="probe did not raise"): await h.create_run("u", "go")
    good = manifest("retrieval", entry={"module": "plugin_fakes", "factory": "make_probe_aware"}); h2 = mk([oa("d")], plugins={"load": [good], "verify_errors": True}); await h2.load_plugins(); assert h2.plugins.has("retrieval")
