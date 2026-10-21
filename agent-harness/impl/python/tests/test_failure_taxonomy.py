"""Failure taxonomy v2 (PRD §3.7): extension layers, secondary tags, human override, and the self-modification / evaluation-integrity classes."""
import pytest, yaml
from pathlib import Path
from harness.failure import TaxonomyError, build_rules, classify, effective_classification, load_rules, observation, reclassification
from harness.selfimprove import observe as OBS

SPEC = Path(__file__).resolve().parents[3] / "spec"
BASE = yaml.safe_load((SPEC / "failure-rules.yaml").read_text())
APP = {"layer": "app", "classes": [{"code": "app.order_not_found"}, {"code": "app.refund_over_limit"}],
       "rules": [{"reason": "fatal_error", "last_error": "app.order_not_found", "class": "app", "code": "app.order_not_found"}, {"reason": "fatal_error", "last_error": "app.refund_over_limit", "class": "app", "code": "app.refund_over_limit"}]}


def test_the_base_taxonomy_is_versioned_and_covers_every_family_in_the_prd():
    r = load_rules()
    assert r["taxonomy_version"] == 2 and {"tool", "state", "environment", "architecture", "security", "eval_integrity", "selfmod"} <= set(r["classes"])
    for code in ("tool.selection", "tool.contract", "tool.noisy_observation", "state.compaction_loss", "state.progress_lost", "env.sandbox_mismatch", "arch.recovery", "arch.subagent_conflict", "arch.budget_exhausted",
                 "sec.prompt_injection", "sec.skill_regression", "selfmod.invalid_proposal", "selfmod.post_promotion_regression", "selfmod.overfitting", "selfmod.reward_hacking", "selfmod.diversity_collapse",
                 "selfmod.implementation_drift", "selfmod.over_optimism", "selfmod.harness_adherence", "eval_integrity.tampering", "eval_integrity.state_carryover", "eval_integrity.over_granular_feedback",
                 "eval_integrity.selection_bias", "eval_integrity.clock_reset", "eval_integrity.stale_grants"):
        assert code in r["codes"], code


def test_a_run_gets_one_primary_class_and_secondary_tags_for_other_errors_seen_earlier():
    r = load_rules()
    assert classify("fatal_error", "tool.unknown", r)["code"] == "tool.selection" and classify("operator_kill", None, r) is None
    c = classify("budget_exhausted:tokens", None, r, ["tool.unknown", "fs.read_failed", "tool.unknown"])
    assert (c["class"], c["code"], c["secondary"]) == ("architecture", "arch.budget_exhausted", ["tool.selection", "tool.contract"])
    assert classify("fatal_error", "tool.unknown", r, ["tool.unknown"])["secondary"] == []                      # the primary is never repeated as a secondary
    assert c["taxonomy_version"] == 2 and len(c["taxonomy_fingerprint"]) == 64


def test_an_agent_developer_defines_application_classes_in_a_namespaced_layer_and_core_rules_still_win():
    r = build_rules(BASE, [APP])
    c = classify("fatal_error", "app.order_not_found", r, ["app.refund_over_limit"])
    assert (c["class"], c["code"], c["secondary"]) == ("app", "app.order_not_found", ["app.refund_over_limit"])
    assert classify("fatal_error", "tool.unknown", r)["code"] == "tool.selection"                              # an extension cannot change how a core failure is classified
    assert classify("fatal_error", None, r)["code"] == "env.sandbox_mismatch"                                    # the fallback still catches the rest
    assert build_rules(BASE, [APP])["fingerprint"] == r["fingerprint"] != build_rules(BASE)["fingerprint"]       # the taxonomy that classified a run is identifiable


@pytest.mark.parametrize("ext,why", [({"layer": "tool", "classes": [], "rules": []}, "core name"), ({"layer": "App", "classes": []}, "lowercase identifier"),
                                     ({"layer": "app", "classes": [{"code": "tool.x"}]}, "not a valid class"), ({"layer": "app", "classes": [{"code": "app.a"}], "rules": [{"reason": "x", "class": "tool", "code": "tool.selection"}]}, "own layer"),
                                     ({"layer": "app", "classes": [{"code": "app.a"}, {"code": "app.a"}]}, "already defined"), ({"layer": "app", "extra": 1}, "unknown setting"),
                                     ({"layer": "app", "classes": [{"code": "app.a"}], "rules": [{"reason": "x", "class": "app", "code": "app.a", "weird": 1}]}, "unknown key")])
def test_extensions_that_reuse_core_names_or_emit_foreign_classes_are_refused(ext, why):
    with pytest.raises(TaxonomyError, match=why): build_rules(BASE, [ext])
    with pytest.raises(TaxonomyError, match="declared twice"): build_rules(BASE, [APP, APP])


def test_extension_files_beside_the_rules_file_are_loaded_and_examples_are_not(tmp_path):
    (tmp_path / "failure-rules.yaml").write_text((SPEC / "failure-rules.yaml").read_text())
    d = tmp_path / "failure-rules.d"; d.mkdir()
    (d / "app.yaml").write_text(yaml.safe_dump(APP)); (d / "other.yaml.example").write_text("layer: nope\n")
    r = load_rules(tmp_path / "failure-rules.yaml")
    assert "app.order_not_found" in r["codes"] and "nope" not in r["layers"] and classify("fatal_error", "app.refund_over_limit", r)["code"] == "app.refund_over_limit"
    assert "app" not in load_rules()["layers"]                                                                    # the shipped example is not active


def test_a_human_can_override_a_classification_and_the_override_is_audited():
    r = build_rules(BASE, [APP]); first = classify("fatal_error", "tool.unknown", r)
    ev = [{"type": "failure.classified", "payload": first}]
    assert effective_classification(ev)["code"] == "tool.selection" and not effective_classification(ev)["overridden"]
    ev.append({"type": "failure.reclassified", "payload": reclassification(r, first, "app.order_not_found", "alice", "the tool was right; the order id was wrong")})
    eff = effective_classification(ev)
    assert (eff["code"], eff["class"], eff["overridden"]) == ("app.order_not_found", "app", True) and ev[0]["payload"]["code"] == "tool.selection"      # the harness's own classification stays in the log
    for args in (("nope.code", "alice", "r"), ("tool.selection", "", "r"), ("tool.selection", "alice", "")):
        with pytest.raises(TaxonomyError): reclassification(r, first, *args)
    with pytest.raises(TaxonomyError): observation(r, "nope.code", "x")


# ------------------------------------------------------------------ observers
def test_each_automatic_self_modification_class_fires_at_its_threshold_and_not_below():
    t = OBS.th(None)
    assert OBS.from_rejection([{"code": "screen.answer_echo"}, {"code": "path.unregistered"}]) == [("selfmod.reward_hacking", {"screens": ["screen.answer_echo"]}), ("selfmod.invalid_proposal", {"reasons": ["path.unregistered"]})]
    assert OBS.from_rejection([{"code": "manifest.incomplete"}])[0][0] == "selfmod.invalid_proposal" and OBS.from_rejection([]) == []
    dec = lambda sd, hd, ok=True, lower=0.05, acc=True: {"accepted": acc, "target": {"delta": sd, "lower": lower, "upper": sd + 0.1}, "models": {"main": {"heldout": {"delta": hd, "pass": ok}}}}
    assert OBS.from_decision(dec(0.30, 0.15), t)[0][0] == "selfmod.overfitting"                                    # gap 0.15 > 0.10
    assert OBS.from_decision(dec(0.30, 0.25), t) == []                                                           # gap 0.05, below the threshold
    assert OBS.from_decision(dec(0.20, 0.19, ok=False), t)[0][0] == "selfmod.overfitting"                        # a held-out regression is non-transfer whatever the gap
    assert OBS.from_decision(dec(0.20, 0.19, lower=0.004), t)[0][0] == "selfmod.over_optimism" and OBS.from_decision(dec(0.20, 0.19, lower=0.006), t) == []
    assert OBS.from_decision(dec(0.20, 0.19, lower=0.004, acc=False), t) == []                                   # only an accepted, barely-significant result is over-optimistic
    assert OBS.from_breach({"breach": True, "breaches": [{"metric": "cost"}], "action": "rollback"})[0][0] == "selfmod.post_promotion_regression" and OBS.from_breach({"breach": False}) == []
    same, different = "always verify the tool output carefully before you answer", "totally unrelated words about spreadsheets formulas and pivots here"
    assert OBS.from_round([same, same], t)[0][0] == "selfmod.diversity_collapse" and OBS.from_round([same, different], t) == [] and OBS.from_round([same], t) == []


def test_selection_bias_is_signalled_by_the_number_of_heldout_queries_a_set_has_served():
    assert OBS.selection_bias(15, 20) is None and OBS.selection_bias(16, 20)[0] == "eval_integrity.selection_bias" and OBS.selection_bias(16, 20)[1] == {"queries": 16, "cap": 20, "fraction": 0.8}
    assert OBS.selection_bias(2, 3) is None and OBS.selection_bias(3, 3) is not None and OBS.selection_bias(4, 20, {"selection_bias_warn_fraction": 0.2}) is not None


def test_only_the_two_classes_without_an_automatic_detector_take_reviewer_labels():
    r = load_rules()
    p = OBS.reviewer_label(r, "selfmod.implementation_drift", "cand-1", {"note": "diff drifted from the manifest"}, "bob", "reviewed the diff")
    assert p["code"] == "selfmod.implementation_drift" and p["labelled_by"] == "bob" and p["evidence"]["reason"] == "reviewed the diff"
    assert OBS.reviewer_label(r, "selfmod.harness_adherence", "run-9", {}, "bob", "model ignored the new prompt")["class"] == "selfmod"
    for code in OBS.AUTOMATIC:
        with pytest.raises(TaxonomyError, match="automatically"): OBS.reviewer_label(r, code, "x", {}, "bob", "why")
    with pytest.raises(TaxonomyError): OBS.reviewer_label(r, "selfmod.implementation_drift", "x", {}, "", "why")
