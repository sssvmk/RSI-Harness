"""The interface classes and the reference implementations that plug into them (design §18.11), and the places the harness uses them."""
import dataclasses, inspect, typing
from types import SimpleNamespace
import pytest
from conftest import events_of, oa
from plugin_fakes import manifest
from harness import plugin_api as A, reference_plugins as R
from harness.eval.outcome import evaluate_contract
from harness.plugin_consumers import PluginProposer, TierNotMet, provision_for_tier, register_retrieve_tool, run_evaluators, unmet_settings
from harness.plugins import KINDS, ManifestInvalid, PluginHost

AGENT = {"model_ref": "main", "credential_ref": "k", "context": "fresh"}
EVM = lambda id="ev1", **ev: manifest("evaluator", id, evaluator=ev or {"type": "code"})
JUDGE = dict(type="llm_judge", model_ref="judge", credential_ref="judge-key", context="fresh")


# ------------------------------------------------------------------ the interfaces match the wire contract
def test_every_kind_has_an_interface_class_whose_methods_parameters_and_results_match_the_wire_contract():
    assert set(A.INTERFACES) == set(KINDS)
    for kind, cls in A.INTERFACES.items():
        spec = KINDS[kind]["methods"]; abstract = {n for n in dir(cls) if getattr(getattr(cls, n), "__isabstractmethod__", False)}
        assert abstract == set(spec), kind                                                                                       # nothing more and nothing less to implement
        for m, ms in spec.items():
            fn = getattr(cls, m); assert [p for p in inspect.signature(fn).parameters if p != "self"] == list(ms["input"]["properties"]), (kind, m)
            rt = typing.get_type_hints(fn)["return"]; assert set(ms["output"]["required"]) <= {f.name for f in dataclasses.fields(rt)}, (kind, m)


def test_a_class_that_leaves_a_method_unimplemented_cannot_be_created_like_a_java_class_missing_an_interface_method():
    class Half(A.Retriever):
        def describe(self): return A.PluginInfo("x", "retrieval", "1.0.0")
        def health(self): return A.Health(True)
    with pytest.raises(TypeError, match="abstract"): Half()


def test_a_result_class_becomes_plain_json_without_its_empty_fields():
    assert A.to_plain(A.Verdict("PASS", ["ok"], ["e"])) == {"verdict": "PASS", "reason_codes": ["ok"], "evidence_refs": ["e"]}
    assert A.to_plain(A.QueryResult([A.Passage("d", "t", 1.0, "s")])) == {"passages": [{"id": "d", "text": "t", "score": 1.0, "source": "s", "permissions": {}}]}


async def test_strict_mode_requires_the_class_to_implement_the_kinds_interface():
    class Duck:                                                                                                                  # has the methods and implements nothing
        def describe(self): return {"id": "retrieval-fake", "kind": "retrieval", "version": "1.0.0"}
    h = PluginHost("core", strict=True); m = manifest("retrieval")
    with pytest.raises(ManifestInvalid, match="must be an instance of"): await h.load(m, Duck())
    with pytest.raises(ManifestInvalid, match="implements the retrieval interface, and its manifest says proposer"): await h.load(manifest("proposer", "p"), R.KeywordRetriever("p"))        # the wrong interface
    assert (await h.load(manifest("retrieval", "kr"), R.KeywordRetriever("kr"))).kind == "retrieval"
    lax = PluginHost("core"); assert not lax.strict and (await lax.load(manifest("retrieval", "d"), R.KeywordRetriever("d"))).id == "d"
    with pytest.raises(ManifestInvalid, match="implements the retrieval interface"): await lax.load(manifest("proposer", "p"), R.KeywordRetriever("p"))                         # a wrong interface is refused even when lax


async def test_deployments_other_than_core_are_strict_unless_they_say_otherwise(mk):
    assert mk([oa("d")]).plugins.strict is False and mk([oa("d")], profile="standard").plugins.strict is True and mk([oa("d")], plugins={"strict": True}).plugins.strict is True


# ------------------------------------------------------------------ the reference implementations
def test_the_code_evaluator_decides_from_the_subject_alone_and_never_passes_on_missing_evidence():
    e = R.CodeEvaluator(); ev = lambda check, **p: e.evaluate({"id": "c", "check": check, "params": p}, {"a": {"b": "10.10"}, "n": 5, "t": "hello world"}, {})
    assert (ev("equals", path="n", value=5).verdict, ev("equals", path="n", value=6).verdict) == ("PASS", "FAIL")
    assert ev("exact_decimal", path="a.b", value="10.1").verdict == "PASS" and ev("exact_decimal", path="a.b", value="10.11").verdict == "FAIL" and ev("exact_decimal", path="a.b", value="abc").verdict == "FAIL"
    assert (ev("contains", path="t", text="world").verdict, ev("exists", path="a.b").verdict, ev("exists", path="a.c").verdict) == ("PASS", "PASS", "FAIL")
    for v in (ev("equals", path="missing", value=1), ev("nope"), R.CodeEvaluator(checks={"boom": lambda p, s, e: 1 / 0}).evaluate({"id": "c", "check": "boom"}, {}, {})): assert v.verdict == "UNCERTAIN"
    assert ev("equals", path="missing", value=1).reason_codes == ["evidence_missing"] and ev("nope").reason_codes == ["no_such_check"] and ev("equals", path="n", value=5).evidence_refs == ["subject:n"]


async def test_the_llm_judge_takes_the_first_verdict_line_and_is_uncertain_about_anything_else():
    j = lambda text: R.LlmJudgeEvaluator(lambda p: text)
    assert (await j("VERDICT: pass\nREASONS: matches_reference, complete").evaluate({"id": "c"}, {}, {})) == A.Verdict("PASS", ["matches_reference", "complete"], [])
    assert (await j("verdict: PARTIAL").evaluate({"id": "c"}, {}, {})).reason_codes == ["judged"]
    for bad in ("I think it is fine", "VERDICT: GREAT", "", "The VERDICT: PASS is inline"): assert (await j(bad).evaluate({"id": "c"}, {}, {})).verdict == "UNCERTAIN", bad
    seen = []
    async def model(prompt): seen.append(prompt); return "VERDICT: FAIL"
    await R.LlmJudgeEvaluator(model, rubric="BE STRICT").evaluate({"id": "c"}, {"x": 1}, {"report": {}}); assert "BE STRICT" in seen[0] and '"report"' in seen[0]                       # an async model call works


async def test_the_typed_ai_evaluator_accepts_only_a_valid_typed_verdict_that_cites_supplied_evidence():
    ok = '{"verdict": "FAIL", "reason_codes": ["wrong_total"], "evidence_refs": ["report"], "confidence": 0.8}'
    t = lambda *replies, **kw: R.TypedAiEvaluator((lambda it: (lambda prompt: next(it)))(iter(replies)), **kw)
    assert (await t(ok).evaluate({"id": "c"}, {}, {"report": 1})) == A.Verdict("FAIL", ["wrong_total"], ["report"], 0.8)
    assert (await t("Sure! " + ok + " Hope that helps").evaluate({"id": "c"}, {}, {"report": 1})).verdict == "FAIL"                                                          # prose around the object is tolerated
    assert (await t("not json", ok).evaluate({"id": "c"}, {}, {"report": 1})).verdict == "FAIL"                                                                              # one retry
    for bad in ("not json", '{"verdict": "GREAT", "reason_codes": ["x"], "evidence_refs": []}', '{"verdict": "PASS", "reason_codes": [], "evidence_refs": []}', '{"verdict": "PASS", "reason_codes": ["x"], "evidence_refs": [], "extra": 1}',
                '{"verdict": "PASS", "reason_codes": ["x"], "evidence_refs": [], "confidence": 2}', '{"verdict": "PASS", "reason_codes": ["x"], "evidence_refs": ["invented"]}'):
        v = await t(bad, bad).evaluate({"id": "c"}, {}, {"report": 1}); assert (v.verdict, v.reason_codes) == ("UNCERTAIN", ["invalid_typed_output"]), bad                         # never guessed from bad output
    seen = []; r = R.TypedAiEvaluator(lambda p: (seen.append(p), "nope")[1], retries=2); await r.evaluate({"id": "c"}, {}, {}); assert len(seen) == 3 and "previous reply was invalid" in seen[1]


def test_the_keyword_retriever_ranks_by_relevance_and_enforces_access_at_query_time():
    r = R.KeywordRetriever(); res = r.index([{"id": "a", "text": "invoice payment terms net 30", "permissions": {"users": ["ann"]}, "metadata": {"team": "fin", "source": "wiki/a"}}, {"id": "b", "text": "payment", "permissions": {"public": True}, "metadata": {"team": "ops"}},
                                              {"id": "c", "text": "invoice invoice invoice", "permissions": {"users": ["bob"]}}, {"id": "d", "text": "invoice payment secret", "permissions": {}}, {"id": "e", "text": "invoice payment public notice"}])
    assert res.indexed == 5 and len(res.index_version) == 12
    q = lambda user, text, k=5, **f: [p.id for p in r.query(user, text, k, f).passages]
    assert q("ann", "invoice payment") == ["a", "b"] and q("bob", "invoice payment") == ["c", "b"] and q("eve", "invoice payment") == ["b"]                                   # d and e have no usable permissions: nobody gets them
    assert q("ann", "invoice payment", team="fin") == ["a"] and q("ann", "invoice payment", 1) == ["a"] and q("ann", "zebra") == [] and q("ann", "invoice", 0) == []
    p = r.query("ann", "invoice", 1, {}).passages[0]; assert (p.source, p.permissions, p.score > 0) == ("wiki/a", {"users": ["ann"]}, True)
    assert r.forget(["a", "nope"]).removed == 1 and q("ann", "invoice payment") == ["b"] and r.index([{"id": "z", "text": "new"}]).index_version != res.index_version


def test_the_scripted_proposer_and_the_user_model_behave():
    p = R.ScriptedProposer([{"edits": [1], "manifest": {"m": 1}}, {"edits": [2]}], findings=["f"])
    assert [len(p.propose(1, i, "b", {}).proposals) for i in (1, 2, 3)] == [1, 1, 0] and p.analyze_weakness({}).findings == ["f"]
    u = R.InMemoryUserModel(); assert u.predict("a", {}) == A.PredictionResult({}, 0.0) and not u.observe("a", {}).recorded and not u.observe("a", {"action": ""}).recorded
    for a in ("open", "open", "save", "open"): assert u.observe("a", {"action": a}).recorded
    u.observe("b", {"action": "x"}); assert u.predict("a", {}) == A.PredictionResult({"action": "open"}, 0.75) and u.predict("b", {}).confidence == 1.0
    assert u.forget("a").removed == 4 and u.predict("a", {}).confidence == 0.0 and u.predict("b", {}).confidence == 1.0 and u.forget("a").removed == 0


async def test_every_reference_class_loads_through_the_strict_host_and_passes_conformance():
    h = PluginHost("high", trusted_in_process={"ce", "lj", "ta", "kr", "sp", "um"}); loaded = []
    for kind, impl, extra in [("evaluator", R.CodeEvaluator("ce"), {"evaluator": {"type": "code"}}), ("evaluator", R.LlmJudgeEvaluator(lambda p: "VERDICT: PASS\nREASONS: ok", "lj"), {"evaluator": JUDGE}),
                              ("evaluator", R.TypedAiEvaluator(lambda p: '{"verdict":"PASS","reason_codes":["ok"],"evidence_refs":[]}', "ta"), {"evaluator": {**JUDGE, "type": "typesafe_ai"}}),
                              ("retrieval", R.KeywordRetriever("kr"), {}), ("proposer", R.ScriptedProposer([{"edits": [], "manifest": {}}] * 4, id="sp"), {}), ("user_model", R.InMemoryUserModel("um"), {})]:
        m = manifest(kind, impl.id, **extra); h.strict = True
        if kind == "evaluator" and loaded and any(x == "evaluator" for x in loaded): await h.unload(next(i for i in h.handles if h.handles[i].kind == "evaluator"))
        await h.load(m, impl); loaded.append(kind)
    assert {x.kind for x in h.handles.values()} == {"evaluator", "retrieval", "proposer", "user_model"}


# ------------------------------------------------------------------ consumers
async def test_a_campaign_driven_by_a_proposer_plugin_gives_the_same_result_as_one_driven_directly(tmp_path):
    from si_world import HINT, make
    w = await make(tmp_path, {(1, 1): HINT("HINT+ verify tool output"), (1, 2): HINT("HINT- skip checking"), "default": None}); direct = w.proposer
    class ViaClass(A.Proposer):
        def describe(self): return A.PluginInfo("wrapped", "proposer", "1.0.0")
        def health(self): return A.Health(True)
        def propose(self, round, n, base, ctx):
            raw = direct.propose(round, n, base, ctx); return A.ProposalSet([A.Proposal(raw["edits"], raw["manifest"])] if raw else [])
        def analyze_weakness(self, report): return A.Findings([])
    host = PluginHost("core", strict=True); m = manifest("proposer", "wrapped"); await host.load(m, ViaClass())
    w.proposer = PluginProposer(host); s = await w.controller().run()
    assert s["stopped"] == "no_addressable_patterns" and s["proposals"] == 2 and len(s["promotions"]) == 1 and "HINT+ verify" in w.config.show_file("main", "prompts/system.md") and "HINT-" not in w.config.show_file("main", "prompts/system.md")
    assert len(direct.calls) >= 3 and w.store.verify(w.cid)["ok"]; w.close()                                                                                                    # the third call found nothing, which ends the campaign


async def test_a_proposer_plugin_that_is_missing_stops_the_campaign_instead_of_guessing():
    with pytest.raises(Exception, match="no active proposer plugin"): await PluginProposer(PluginHost("core")).propose(1, 1, "b", {})


async def test_the_retrieve_tool_exists_only_with_a_plugin_returns_untrusted_text_and_withholds_what_the_user_may_not_see(mk):
    assert "retrieve" not in mk([oa("d")]).registry.tools
    h = mk([oa("d")], plugins={"load": [manifest("retrieval", "kr", entry={"module": "plugin_fakes", "factory": "make_retriever"})]}); run, _ = await h.create_run("u", "go"); tool = h.registry.tools["retrieve"]
    assert tool.manifest["effect_class"] == "read_only"
    res = await tool.handler({"query": "invoice payment"}, SimpleNamespace(run=run)); assert res.trust == "untrusted" and "d1 score=" in res.text and "d3 score=" in res.text and "d2" not in res.text          # u may see d1 and the public d3, not d2
    ev = events_of(h, run, "retrieval.queried")[0]["payload"]; assert (ev["plugin_id"], ev["returned"], ev["withheld_by_harness"]) == ("kr", 2, 0) and "invoice" not in str(ev)
    class Leaky(R.KeywordRetriever):                                                                                                                                   # a plugin that ignores access control
        def query(self, user, query, k, filters): return A.QueryResult([A.Passage("secret", "salary table", 1.0, "hr", {"users": ["nobody"]}), A.Passage("ok", "public note", 0.5, "n", {"public": True})])
    await h.plugins.replace(manifest("retrieval", "leaky"), Leaky("leaky")); res = await tool.handler({"query": "x"}, SimpleNamespace(run=run))
    assert "secret" not in res.text and "salary" not in res.text and "ok score" in res.text and events_of(h, run, "retrieval.queried")[-1]["payload"]["withheld_by_harness"] == 1      # the harness checks again
    await h.plugins.unload("leaky"); from harness.tools.runtime import ToolError
    with pytest.raises(ToolError, match="no active retrieval plugin"): await tool.handler({"query": "x"}, SimpleNamespace(run=run))


CONTRACT = {"task_id": "t", "acceptance_criteria": [{"id": "total_ok", "required": True, "verification": "evaluator", "params": {"check": "exact_decimal", "path": "value", "value": "10.10"}}], "success_policy": "all_required_criteria_pass", "inconclusive_policy": "do_not_report_success"}


async def evaluate_with(h, profile="core", contract=CONTRACT, agent=AGENT, run=None, subject=None):
    got = await run_evaluators(contract, h, agent, profile, subject if subject is not None else {"value": "10.1"}, {"report": {}}, run)
    return evaluate_contract(contract, {"value": None, "report": {}, "fs_read": None, "facts": {}, "evaluator_results": got}), got


async def test_an_evaluators_verdict_decides_the_criterion_and_a_missing_or_uncertain_one_is_never_a_pass():
    h = PluginHost("core"); out, _ = await evaluate_with(h); assert out["verdict"] == "INCONCLUSIVE" and out["criteria"][0]["evidence_ref"] == "no evaluator plugin is loaded"                           # none loaded
    await h.load(EVM("code1"), R.CodeEvaluator("code1")); out, _ = await evaluate_with(h); assert (out["verdict"], out["criteria"][0]["status"]) == ("SUCCESS", "pass") and out["criteria"][0]["evidence_ref"].startswith("evaluator:code1:deterministic:PASS")
    out, _ = await evaluate_with(h, subject={"value": "10.11"}); assert (out["verdict"], out["criteria"][0]["status"]) == ("FAILURE", "fail")
    out, _ = await evaluate_with(h, subject={}); assert (out["verdict"], out["criteria"][0]["status"]) == ("INCONCLUSIVE", "unavailable")                                                          # evidence missing: uncertain
    class Partial(R.CodeEvaluator):
        def evaluate(self, criterion, subject, evidence): return A.Verdict("PARTIAL", ["half"], [])
    await h.replace(EVM("p"), Partial("p")); out, _ = await evaluate_with(h); assert out["criteria"][0]["status"] == "fail"                                                                         # partly met is not met
    class Broken(R.CodeEvaluator):
        def evaluate(self, criterion, subject, evidence):
            if criterion.get("check"): raise RuntimeError("down")                                                                                              # breaks on a real criterion, after it has loaded
            return A.Verdict("UNCERTAIN", ["conformance"], [])
    await h.replace(EVM("b"), Broken("b")); out, got = await evaluate_with(h); assert out["criteria"][0]["status"] == "unavailable" and "evaluator failed" in got["total_ok"]["ref"]


async def test_an_evaluator_that_shares_the_agents_model_context_or_credential_cannot_gate_in_standard_and_high():
    for profile in ("standard", "high"):
        for name, ev, shared in [("same model", {**JUDGE, "model_ref": "main"}, "model"), ("same credential", {**JUDGE, "credential_ref": "k"}, "credential"), ("shared context", {**JUDGE, "context": "shared"}, "context")]:
            h = PluginHost("high", trusted_in_process={"j"}, strict=True); await h.load(EVM("j", **ev), R.LlmJudgeEvaluator(lambda p: "VERDICT: PASS\nREASONS: ok", "j"))
            out, got = await evaluate_with(h, profile); assert out["criteria"][0]["status"] == "unavailable" and shared in got["total_ok"]["ref"] and "advisory" in got["total_ok"]["ref"], (profile, name)
    h = PluginHost("high", trusted_in_process={"j"}, strict=True); await h.load(EVM("j", **JUDGE), R.LlmJudgeEvaluator(lambda p: "VERDICT: PASS\nREASONS: ok", "j"))
    out, _ = await evaluate_with(h, "standard"); assert out["criteria"][0]["status"] == "pass"                                                                                                     # independent: it may gate
    out, got = await evaluate_with(h, "high"); assert out["criteria"][0]["status"] == "pass"
    hard = {**CONTRACT, "acceptance_criteria": [{**CONTRACT["acceptance_criteria"][0], "params": {**CONTRACT["acceptance_criteria"][0]["params"], "material": True}}]}
    out, got = await evaluate_with(h, "high", hard); assert out["criteria"][0]["status"] == "unavailable" and "second independent evaluator" in got["total_ok"]["ref"]                                      # a material claim needs backing
    backed = {**hard, "acceptance_criteria": [{**hard["acceptance_criteria"][0], "params": {**hard["acceptance_criteria"][0]["params"], "backed": True}}]}; out, _ = await evaluate_with(h, "high", backed); assert out["criteria"][0]["status"] == "pass"
    low = PluginHost("core"); await low.load(EVM("j", **{**JUDGE, "model_ref": "main"}), R.LlmJudgeEvaluator(lambda p: "VERDICT: PASS\nREASONS: ok", "j")); out, _ = await evaluate_with(low, "core"); assert out["criteria"][0]["status"] == "pass"   # core only labels


async def test_each_evaluator_verdict_is_recorded_in_the_run_with_its_independence(mk):
    h = mk([oa("d")]); run, _ = await h.create_run("u", "go"); await h.plugins.load(EVM("code1"), R.CodeEvaluator("code1")); await evaluate_with(h.plugins, run=run)
    ev = events_of(h, run, "evaluator.verdict")[0]["payload"]; assert (ev["criterion_id"], ev["plugin_id"], ev["type"], ev["independence"], ev["verdict"], ev["shared"]) == ("total_ok", "code1", "code", "deterministic", "PASS", []) and "10.1" not in str(ev)
    assert events_of(h, run, "plugin.called")[0]["payload"]["method"] == "evaluate"


def test_a_tiers_required_settings_are_compared_with_what_the_sandbox_reports():
    req = {"network": "none", "user": "nobody", "memory_mb": {"at_most": 512}, "disk_mb": {"at_least": 100}}
    assert unmet_settings(req, {"network": "none", "user": "nobody", "memory_mb": 256, "disk_mb": 100}) == []
    got = unmet_settings(req, {"network": "host", "memory_mb": 1024, "disk_mb": 10}); assert len(got) == 4 and "user is required and was not reported" in got[3] and "network is 'host'" in got[2]
    assert "not a number" in unmet_settings({"memory_mb": {"at_most": 5}}, {"memory_mb": "lots"})[0] and "not a number" in unmet_settings({"m": {"at_most": 5}}, {"m": True})[0]


class FakeIsolation(A.IsolationBackend):
    def __init__(self, observed, ok=True): self.observed, self.ok, self.destroyed = observed, ok, []
    def describe(self): return A.PluginInfo("iso", "isolation", "1.0.0")
    def health(self): return A.Health(True)
    def provision(self, spec): return A.Provisioned("sb1", dict(self.observed))
    def verify(self, sandbox_id, required): return A.Verification(self.ok, dict(self.observed))
    def destroy(self, sandbox_id): self.destroyed.append(sandbox_id); return A.Destroyed(True)


async def test_no_workload_starts_in_a_sandbox_that_does_not_show_the_required_settings(mk):
    h = mk([oa("d")]); run, _ = await h.create_run("u", "go"); host = PluginHost("core"); req = {"network": "none", "user": "nobody"}
    with pytest.raises(Exception, match="no active isolation plugin"): await provision_for_tier(host, {}, req, run)
    bad = FakeIsolation({"network": "host", "user": "nobody"}); await host.load(manifest("isolation", "iso"), bad); bad.destroyed.clear()                          # loading ran the conformance call to destroy
    with pytest.raises(TierNotMet, match="network is 'host'"): await provision_for_tier(host, {}, req, run)
    assert bad.destroyed == ["sb1"] and "network" in events_of(h, run, "isolation.tier_unmet")[0]["payload"]["problems"][0]                                                              # the sandbox is destroyed, nothing falls back
    await host.replace(manifest("isolation", "iso"), FakeIsolation({"network": "none", "user": "nobody"}, ok=False))
    with pytest.raises(TierNotMet, match="not ok"): await provision_for_tier(host, {}, req, run)
    good = FakeIsolation({"network": "none", "user": "nobody", "egress_inspection": {"inspection_point": "proxy", "policy_applied": True, "audit_records": True, "coverage_gaps": ["QUIC"], "bypass_conditions": ["raw sockets"], "verified": True}})
    await host.replace(manifest("isolation", "iso"), good); good.destroyed.clear(); r = await provision_for_tier(host, {}, req, run); assert r["sandbox_id"] == "sb1" and r["exfiltration_claim"]["claim"] == "payload_inspection" and good.destroyed == []
    ev = events_of(h, run, "isolation.verified")[0]["payload"]; assert ev["exfiltration_claim"] == "payload_inspection"
    await host.replace(manifest("isolation", "iso"), FakeIsolation({"network": "none", "user": "nobody"})); r = await provision_for_tier(host, {}, req, run); assert r["exfiltration_claim"]["claim"] == "partial" and "no payload inspection point" in events_of(h, run, "isolation.verified")[-1]["payload"]["exfiltration_reasons"][0]
