"""Train 2 governance core: registry, EditApplier, screens, eval sets, gate and search (§17.2-17.9)."""
import os, random
from pathlib import Path
import pytest
from si_world import CONFIG_FILES, REGISTRY_YAML, MANIFEST
from harness.selfimprove import gate as G
from harness.selfimprove.edits import EditApplier
from harness.selfimprove.evalsets import EvalSetError, EvalSetRegistry, eval_seed, jaccard, shingle_set
from harness.selfimprove.gitrepo import GitRepo
from harness.selfimprove.registry import ComponentRegistry, RegistryError
from harness.selfimprove.screens import build_answer_index, run_screens
from harness.selfimprove import search as S
from harness.stats import Xoshiro256SS

REG = ComponentRegistry.from_yaml(REGISTRY_YAML)


def test_registry_validation_and_longest_match():
    bad = [{"schema_version": 1, "components": [{"path": "a/**", "component": "skill", "tier": "T3", "owner": "o", "origin": "human"}]},
           {"schema_version": 1, "components": [{"path": "a/**", "component": "skill", "tier": "T1", "owner": "o"}]},
           {"schema_version": 1, "components": [{"path": "a/**", "component": "nonsense", "tier": "T1", "owner": "o", "origin": "human"}]},
           {"schema_version": 2, "components": []}]
    for b in bad:
        with pytest.raises(RegistryError): ComponentRegistry.from_dict(b)
    reg = ComponentRegistry.from_dict({"schema_version": 1, "components": [{"path": "skills/**", "component": "skill", "tier": "T1", "owner": "o", "origin": "human"},
                                                                          {"path": "skills/triage/**", "component": "skill", "tier": "T2", "owner": "o", "origin": "learned"}]})
    assert reg.tier_of("skills/other/x") == "T1" and reg.tier_of("skills/triage/a/b") == "T2"


@pytest.mark.parametrize("path,tier,reason", [
    ("prompts/system.md", "T1", None), ("./prompts/system.md", "T1", None), ("memory/playbook.jsonl", "T0", None), ("skills/triage/run.sh", "T2", None),
    ("prompts/../harness-control/x", None, "path.traversal"), ("/etc/passwd", None, "path.absolute"), ("prompts\\x", None, "path.invalid"), ("components.yaml", None, "path.protected"),
    ("harness-improver/prompts/proposer.md", None, "path.unregistered"), ("unregistered.txt", None, "path.unregistered"), ("prompts/", None, "path.invalid"), ("", None, "path.invalid"),
    ("memory/playbook.jsonl/../../x", None, "path.traversal")])
def test_path_policy(path, tier, reason):
    assert REG.check(path) == (tier, reason)


@pytest.fixture
def repo(tmp_path):
    return GitRepo.init(tmp_path / "cfg", CONFIG_FILES)


def apply(repo, edits, tiers=("T0", "T1"), max_lines=200, name="b"):
    return EditApplier(repo, REG, max_lines).apply(repo.head(), edits, list(tiers), f"cand/x/1/{name}", "proposal P\n\nmanifest-hash: m")


def test_edit_applier_happy_path_and_proposal_tier_is_computed_by_the_control_plane(repo):
    r = apply(repo, [{"path": "prompts/system.md", "op": "replace", "search": "Be careful.\n", "replace": "Be careful.\nHINT+ x\n"},
                     {"path": "memory/playbook.jsonl", "op": "create", "content": "{}\n"}] if False else [{"path": "prompts/system.md", "op": "replace", "search": "Be careful.\n", "replace": "Be careful.\nHINT+ x\n"}])
    assert r.ok and r.tier == "T1" and repo.show_file(r.commit, "prompts/system.md").endswith("HINT+ x\n\n") is False
    r2 = apply(repo, [{"path": "tools/new.md", "op": "create", "content": "t\n"}, {"path": "memory/playbook.jsonl", "op": "replace", "search": "", "replace": "x"}], name="c")
    assert not r2.ok                                              # empty search is invalid, atomically: nothing was committed
    r3 = apply(repo, [{"path": "tools/new.md", "op": "create", "content": "t\n"}, {"path": "prompts/system.md", "op": "delete"}], name="d")
    assert r3.ok and r3.tier == "T1" and repo.show_file(r3.commit, "prompts/system.md") is None and repo.show_file(r3.commit, "tools/new.md") == "t\n"
    assert repo.head() != r.commit and not repo.exists("refs/heads/cand/x/1/c")      # failed attempts leave no branch behind


@pytest.mark.parametrize("edit,code", [
    ({"path": "prompts/../harness-control/x", "op": "create", "content": "x"}, "path.traversal"),
    ({"path": "harness-improver/prompts/proposer.md", "op": "replace", "search": "Propose", "replace": "x"}, "path.unregistered"),
    ({"path": "components.yaml", "op": "replace", "search": "T1", "replace": "T2"}, "path.protected"),
    ({"path": "skills/triage/SKILL.md", "op": "replace", "search": "Triage.", "replace": "x"}, "tier.exceeds_allowed"),
    ({"path": "prompts/system.md", "op": "replace", "search": "absent text", "replace": "x"}, "edit.no_unique_match"),
    ({"path": "prompts/system.md", "op": "replace", "search": "a", "replace": "b"}, "edit.no_unique_match"),            # 'a' occurs many times
    ({"path": "prompts/system.md", "op": "create", "content": "x"}, "edit.exists"),
    ({"path": "prompts/ghost.md", "op": "delete"}, "edit.missing"),
    ({"path": "prompts/system.md", "op": "rewrite"}, "edit.invalid"),
    ({"path": "prompts/system.md", "op": "create", "content": "x", "extra": 1}, "edit.invalid")])
def test_edit_applier_refuses(repo, edit, code):
    r = apply(repo, [edit])
    assert not r.ok and r.reasons[0]["code"] == code


def test_symlink_that_resolves_outside_the_surface_is_refused(tmp_path):
    outside = tmp_path / "harness-control"; outside.mkdir(); (outside / "secret.yaml").write_text("gate: x\n")
    repo = GitRepo.init(tmp_path / "cfg", CONFIG_FILES)
    os.symlink(outside, repo.path / "prompts" / "evil"); repo.git("add", "-A"); repo.git("commit", "-q", "-m", "link")
    for edit in ({"path": "prompts/evil/secret.yaml", "op": "replace", "search": "gate: x", "replace": "gate: y"}, {"path": "prompts/evil/new.yaml", "op": "create", "content": "x"}):
        r = apply(repo, [edit], name="s")
        assert not r.ok and r.reasons[0]["code"] == "path.symlink_escape"
    assert (outside / "secret.yaml").read_text() == "gate: x\n"          # the protected file was never touched


def test_edit_cap_counts_changed_lines(repo):
    big = "\n".join(f"line {i}" for i in range(150)) + "\n"
    assert apply(repo, [{"path": "tools/a.md", "op": "create", "content": big}], max_lines=200).ok
    r = apply(repo, [{"path": "tools/a.md", "op": "create", "content": big}, {"path": "tools/b.md", "op": "create", "content": big}], max_lines=200, name="e")
    assert not r.ok and r.reasons[0]["code"] == "edit.cap_exceeded"


def test_identical_edit_on_identical_base_gives_identical_commit_ids(tmp_path):
    ids = []
    for n in "ab":
        rp = GitRepo.init(tmp_path / n, CONFIG_FILES)
        ids.append(apply(rp, [{"path": "prompts/system.md", "op": "replace", "search": "Be careful.\n", "replace": "Be careful.\nHINT+ x\n"}]).commit)
    assert ids[0] == ids[1]


def test_worktree_hash_detects_uncommitted_tampering(repo):
    h = repo.worktree_hash(); (repo.path / "prompts" / "system.md").write_text("tampered\n")
    assert repo.worktree_hash() != h and repo.worktree_hash() == repo.worktree_hash()


# ---------------------------------------------------------------- screens
def E(path, added, before=None, after=None): return {"path": path, "added": added, "before": before, "after": after}


def test_screens_each_fire_on_their_own_input_and_stay_quiet_on_clean_input():
    idx = build_answer_index(["The capital of the republic is Zanzibar City"])
    clean = run_screens([E("prompts/a.md", "Check each step carefully before answering.")], idx, ["s001"])
    assert clean == []
    assert run_screens([E("prompts/a.md", "Remember: the capital of the republic is Zanzibar City!")], idx, [])[0]["screen"] == "answer_echo"
    dup = "Always double check the result of every tool call before moving on.\n" * 3
    assert run_screens([E("prompts/a.md", dup)], [], [])[0]["screen"] == "duplicate_blocks"
    for t in ("echo 1 > /scratch/score.json", "open('report.json','w').write(x)", "set report_path to x"):
        assert run_screens([E("tools/a.md", t)], [], [])[0]["screen"] == "output_interface", t
    for t in ("read /tests/grade.py", "look in harness-control", "check the vault", "use the heldout_set", "see verifier_path"):
        assert run_screens([E("prompts/a.md", t)], [], [])[0]["screen"] == "protected_refs", t
    assert run_screens([E("prompts/a.md", "for task s001 answer 7")], [], ["s001"])[0]["screen"] == "hardcoded_ids"
    assert run_screens([E("prompts/a.md", "set seed = 1234")], [], [])[0]["screen"] == "hardcoded_ids"


def test_t4_key_screen_catches_changes_inside_configuration_but_not_prose():
    before = "temperature: 0.2\nbudgets:\n  turns: 10\nmodel: base\nnested:\n  egress: []\n"
    for after, key in (("temperature: 0.2\nbudgets:\n  turns: 99\nmodel: base\nnested:\n  egress: []\n", "budgets"), ("temperature: 0.2\nbudgets:\n  turns: 10\nmodel: other\nnested:\n  egress: []\n", "model"),
                       ("temperature: 0.2\nbudgets:\n  turns: 10\nmodel: base\nnested:\n  egress: [evil.com]\n", "nested.egress"), (before + "approvals: none\n", "approvals")):
        v = run_screens([E("prompts/settings.yaml", "x", before, after)], [], [])
        assert v and v[0]["screen"] == "t4_keys" and key in v[0]["detail"], (key, v)
    assert run_screens([E("prompts/settings.yaml", "x", before, before.replace("0.2", "0.7"))], [], []) == []       # non-T4 keys may change
    md_b, md_a = "---\nmodel: a\ntitle: t\n---\nbody\n", "---\nmodel: b\ntitle: t\n---\nbody\n"
    assert run_screens([E("prompts/p.md", "x", md_b, md_a)], [], [])[0]["screen"] == "t4_keys"
    assert run_screens([E("prompts/p.md", "x", "plain model: base\n", "plain model: other\n")], [], []) == []         # prose is not configuration


# ---------------------------------------------------------------- eval sets
async def test_evalset_disjointness_near_duplicates_exhaustion_and_seeds(tmp_path):
    from harness.blobs import BlobStore
    from harness.clock import FakeClock
    from harness.eventlog.store import EventStore, EventWriter
    from harness.selfimprove.schema import apply_migration_002
    st = EventStore(str(tmp_path / "l.db")); w = EventWriter(st, FakeClock()).start(); await apply_migration_002(w)
    reg = EvalSetRegistry(st, w, FakeClock(), max_queries_per_set=3)
    await reg.register("s", "scoring", [{"id": "a", "text": "classify the sentiment of this short review about a restaurant"}])
    with pytest.raises(EvalSetError) as e: await reg.register("h", "heldout", [{"id": "a", "text": "totally different"}])
    assert e.value.code == "evalset.id_overlap"
    with pytest.raises(EvalSetError) as e: await reg.register("h", "heldout", [{"id": "z", "text": "classify the sentiment of this short review about a restaurant."}])
    assert e.value.code == "evalset.near_duplicate"
    await reg.register("h", "heldout", [{"id": "z", "text": "summarise the following legal contract into five bullet points"}])
    await reg.register("s2", "scoring", [{"id": "a2", "text": "classify the sentiment of this short review about a restaurant"}])      # same role may overlap
    for i in range(3):
        assert reg.status("h") == "active"; await reg.record_query("h", "camp", f"c{i}")
    assert reg.status("h") == "exhausted" and reg.usage("h") == 3
    with pytest.raises(EvalSetError) as e: await reg.record_query("h", "camp", "c9")
    assert e.value.code == "evalset.exhausted"
    assert eval_seed("C", 1, "cand", "set") == eval_seed("C", 1, "cand", "set") != eval_seed("C", 2, "cand", "set")
    assert 0 <= int(eval_seed("C", 1, "cand", "set")) < 2**64 and jaccard(shingle_set("a b c d"), shingle_set("a b c d")) == 1.0
    w.close()


# ---------------------------------------------------------------- gate
def arrays(n=40, delta=0.1, spread=0.1, seed=1):
    r = random.Random(seed); b = [r.random() for _ in range(n)]
    return b, [x + delta + r.uniform(-spread, spread) for x in b]


def variant(delta=0.1, spread=0.05, cost_delta=0.0, seed=1, broken=()):
    b, c = arrays(40, delta, spread, seed)
    pb = [1] * 40; pc = [0 if i in broken else 1 for i in range(40)]
    return {"target": {"b": b, "c": c, "direction": "maximize"}, "guardrails": [{"metric": "cost", "b": [1.0] * 40, "c": [1.0 + cost_delta] * 40, "direction": "minimize"}],
            "preserved": {"b": pb, "c": pc, "must_pass": [0, 1]}}


def cand(pid, canonical=None, fmt=None, heldout=None, tier="T1", seed=1):
    c = canonical or variant(seed=seed)
    h = heldout if heldout is not None else {"target": {"b": [0.5] * 20, "c": [0.5 + 0.1] * 20, "direction": "maximize"}}
    return {"proposal_id": pid, "tier": tier, "models": {"main": {"variants": {"canonical": c, "format_varied": fmt or c}, "heldout": h}}}


P = {"B": 600}


def test_gate_accepts_a_clear_win_and_rejects_a_no_op():
    win, noop = G.decide_round("C", [cand("win"), cand("noop", variant(delta=0.0, spread=0.0, seed=2))], P)
    assert win["accepted"] and win["reasons"] == [] and win["target"]["lower"] > 0 and win["accepted_models"] == ["main"]
    assert not noop["accepted"] and any(r.startswith("target:") for r in noop["reasons"])


def test_gate_rejects_a_target_win_that_regresses_a_guardrail():
    d = G.decide_round("C", [cand("g", variant(cost_delta=0.5))], P)[0]
    assert not d["accepted"] and any(r.startswith("guardrail:cost") for r in d["reasons"])
    assert G.decide_round("C", [cand("ok", variant(cost_delta=0.01))], P)[0]["accepted"]            # within the margin


def test_gate_rejects_a_must_pass_regression_strictly_and_a_preserved_non_inferiority_failure():
    d = G.decide_round("C", [cand("m", variant(broken={0}))], P)[0]
    assert not d["accepted"] and any("must_pass_regressed" in r for r in d["reasons"])
    d = G.decide_round("C", [cand("p", variant(broken=set(range(5, 25))))], P)[0]
    assert not d["accepted"] and any("preserved:non_inferiority" in r for r in d["reasons"])


def test_gate_requires_non_inferiority_on_the_format_varied_variant():
    d = G.decide_round("C", [cand("f", variant(), variant(delta=-0.1, spread=0.02, seed=3))], P)[0]
    assert not d["accepted"] and any("(main:format_varied)" in r for r in d["reasons"])


def test_gate_heldout_regression_blocks_a_candidate_that_wins_on_scoring():
    bad = {"target": {"b": [0.5] * 20, "c": [0.2] * 20, "direction": "maximize"}}
    d = G.decide_round("C", [cand("h", heldout=bad)], P)[0]
    assert not d["accepted"] and any(r.startswith("heldout:regressed") for r in d["reasons"]) and not d["needs_heldout"]
    must = {"target": {"b": [0.5] * 20, "c": [0.6] * 20, "direction": "maximize"}, "preserved": {"b": [1] * 20, "c": [0] + [1] * 19, "must_pass": [0]}}
    assert any("heldout:must_pass_regressed" in r for r in G.decide_round("C", [cand("h2", heldout=must)], P)[0]["reasons"])


def test_gate_asks_for_the_heldout_set_only_after_every_earlier_check_passes():
    c = cand("w"); c["models"]["main"].pop("heldout")
    d = G.decide_round("C", [c], P)[0]
    assert d["needs_heldout"] and not d["accepted"]
    c2 = cand("l", variant(delta=0.0, spread=0.0, seed=2)); c2["models"]["main"].pop("heldout")
    d2 = G.decide_round("C", [c2], P)[0]
    assert not d2["needs_heldout"] and not d2["accepted"]               # a failed candidate never spends a held-out query


def find_borderline():
    """Search for data whose target interval clears 0 at alpha=.05 alone but not at alpha/3 (a Holm-sensitive candidate)."""
    for seed in range(1, 400):
        b, c = arrays(40, 0.025, 0.15, seed)
        x = G.improvements(b, c, "maximize")
        s = G.seed_for("C", "bp:main:canonical:target")
        lo95 = G._iv(x, s, 0.05, 600)["lower"]; lo98 = G._iv(x, s, 0.05 / 3, 600)["lower"]
        if lo95 > 0 >= lo98:
            return seed
    raise AssertionError("no borderline data found")


def test_holm_adjustment_makes_a_round_stricter_than_a_single_candidate_gate():
    seed = find_borderline()
    mk = lambda pid: cand(pid, {"target": {"b": arrays(40, 0.025, 0.15, seed)[0], "c": arrays(40, 0.025, 0.15, seed)[1], "direction": "maximize"}, "guardrails": [], "preserved": None})
    # alone (m=1) the candidate is tested at alpha=.05 and passes; among 3 look-alikes the first is tested at alpha/3 and the step-down stops
    for c in (mk("bp"),):
        c["models"]["main"]["variants"]["canonical"].pop("preserved"); c["models"]["main"]["variants"]["format_varied"] = c["models"]["main"]["variants"]["canonical"]
    solo = G.decide_round("C", [mk("bp")], P)[0]
    trio = G.decide_round("C", [mk("bp"), mk("bq"), mk("br")], P)
    assert solo["accepted"], solo["reasons"]
    assert not any(d["accepted"] for d in trio) and all(any("holm:stopped" in r or "target:" in r for r in d["reasons"]) for d in trio)
    assert G.holm_levels([0.01, 0.04, 0.03], 0.05) == pytest.approx([0.05 / 3, 0.05 / 1, 0.05 / 2])
    assert G.holm_levels([0.01, None, 0.03], 0.05) == pytest.approx([0.05 / 2, None, 0.05 / 1])
    assert G.holm_levels([0.01, 0.04], 0.05, holm=False) == [0.05, 0.05]
    mixed = G.decide_round("C", [cand("clear"), mk("bp"), mk("bq")], P)                  # a clear winner still passes alongside borderline ones
    assert mixed[0]["accepted"]


def test_gate_is_deterministic_and_dominated_candidates_are_rejected():
    a = G.decide_round("C", [cand("d1"), cand("d2", seed=5)], P); b = G.decide_round("C", [cand("d1"), cand("d2", seed=5)], P)
    assert a == b
    c = cand("dom"); c["summary"] = {"target": 0.1, "cost": 2.0, "latency": 5.0}; c["archive"] = [{"target": 0.2, "cost": 1.0, "latency": 4.0}]
    d = G.decide_round("C", [c], P)[0]
    assert not d["accepted"] and "dominated" in d["reasons"]
    c["archive"] = [{"target": 0.05, "cost": 1.0, "latency": 4.0}]
    assert G.decide_round("C", [c], P)[0]["accepted"]


def test_gate_is_per_target_model_and_accepts_where_it_passes():
    c = cand("pm")
    c["models"]["gem"] = {"variants": {"canonical": variant(delta=-0.1, spread=0.02, seed=4), "format_varied": variant(delta=-0.1, spread=0.02, seed=4)}, "heldout": c["models"]["main"]["heldout"]}
    d = G.decide_round("C", [c], P)[0]
    assert d["accepted"] and d["accepted_models"] == ["main"] and d["models"]["gem"]["status"] == "fail"


# ---------------------------------------------------------------- search
def test_novelty_rejection_parent_weights_pareto_and_diversity():
    a = "always verify the tool output carefully before you write the final answer to the user"
    assert S.novelty_rejected(a, ["something else entirely about spreadsheets and formulas in detail please", a + " now"]) == 1
    assert S.novelty_rejected("a completely new instruction about handling timeouts gracefully in tools", [a]) is None
    w = S.parent_weights([0.9, 0.5, 0.5], [0, 0, 3], 0.2)
    assert w[0] > w[1] > w[2] and w[1] == pytest.approx(w[2] * 4)       # strong parents preferred; heavily used parents down-weighted by 1/(1+children)
    rng, picks = Xoshiro256SS(7), [S.sample_parent(w, Xoshiro256SS(i)) for i in range(400)]
    assert picks.count(0) > picks.count(1) > picks.count(2) and S.sample_parent(w, Xoshiro256SS(7)) == S.sample_parent(w, Xoshiro256SS(7))
    pts = [{"target": 0.5, "cost": 1, "latency": 1}, {"target": 0.6, "cost": 2, "latency": 1}, {"target": 0.4, "cost": 3, "latency": 3}, {"target": 0.6, "cost": 1, "latency": 1}]
    assert S.pareto_front(pts) == [3]
    assert S.mean_pairwise_similarity([a, a]) == 1.0 and S.mean_pairwise_similarity([a, "completely different words here and there across many tokens in this text"]) < 0.1


def test_a_file_created_directly_at_the_worktree_root_is_allowed_when_registered(tmp_path):
    reg = ComponentRegistry.from_dict({"schema_version": 1, "components": [{"path": "NOTES.md", "component": "playbook", "tier": "T0", "owner": "o", "origin": "learned"}]})
    repo = GitRepo.init(tmp_path / "cfg", {"NOTES.md.keep": "x\n"})
    r = EditApplier(repo, reg).apply(repo.head(), [{"path": "NOTES.md", "op": "create", "content": "n\n"}], ["T0"], "cand/r/1/1", "proposal R")
    assert r.ok and repo.show_file(r.commit, "NOTES.md") == "n\n"


# ---------------------------------------------------------------- gate policy per kind of edit (defaults, environment overrides, audit)
from harness.selfimprove.policy import DEFAULTS, GatePolicy, PolicyError


def test_policy_defaults_are_the_ones_the_deployment_owner_chose():
    p = GatePolicy.resolve()
    assert [(t, p.gate_params(t)["alpha"], p.review(t)["mode"], p.canary_fraction(t)) for t in ("T0", "T1", "T2")] == [("T0", 0.05, "none", 0.0), ("T1", 0.05, "sampled", 0.0), ("T2", 0.01, "blocking", 0.1)]
    assert p.review("T1")["sample_rate"] == 0.2 and p.diff() == [] and p.relaxed() == []


def test_an_administrator_can_make_an_environment_stricter_or_leaner_and_every_deviation_is_listed_with_its_direction():
    cfg = {"tiers": {"T1": {"alpha": 0.04}}, "environments": {"prod": {"tiers": {"T1": {"alpha": 0.01, "review": {"mode": "blocking"}}, "T2": {"alpha": 0.001}}},
                                                                "dev": {"tiers": {"T1": {"alpha": 0.2, "review": {"mode": "none"}}, "T0": {"holm": False}}}}}
    base, prod, dev = GatePolicy.resolve(cfg), GatePolicy.resolve(cfg, "prod"), GatePolicy.resolve(cfg, "dev")
    assert base.gate_params("T1")["alpha"] == 0.04 and prod.gate_params("T1")["alpha"] == 0.01 and dev.gate_params("T1")["alpha"] == 0.2          # environment beats base beats default
    assert {(d["tier"], d["path"]): d["direction"] for d in prod.diff()} == {("T1", "alpha"): "stricter", ("T1", "review.mode"): "stricter", ("T2", "alpha"): "stricter"} and prod.relaxed() == []
    assert {(d["tier"], d["path"]): d["direction"] for d in dev.diff()} == {("T0", "holm"): "leaner", ("T1", "alpha"): "leaner", ("T1", "review.mode"): "leaner"} and len(dev.relaxed()) == 3
    assert len({base.fingerprint, prod.fingerprint, dev.fingerprint, GatePolicy.resolve().fingerprint}) == 4 and prod.fingerprint == GatePolicy.resolve(cfg, "prod").fingerprint


@pytest.mark.parametrize("cfg,env,code", [({"tiers": {"T1": {"alpha": 0.0}}}, None, "policy.invalid_value"), ({"tiers": {"T1": {"alpha": 0.5}}}, None, "policy.invalid_value"),
                                          ({"tiers": {"T2": {"review": {"mode": "sampled"}}}}, None, "policy.locked"), ({"tiers": {"T3": {}}}, None, "policy.unknown_tier"),
                                          ({"tiers": {"T1": {"alfa": 0.1}}}, None, "policy.unknown_key"), ({"gate": {}}, None, "policy.unknown_key"),
                                          ({"environments": {"a": {"tiers": {}}}}, "b", "policy.unknown_environment"), ({"tiers": {"T1": {"B": 10}}}, None, "policy.invalid_value"),
                                          ({"tiers": {"T1": {"review": {"sample_rate": 2}}}}, None, "policy.invalid_value")])
def test_policy_refuses_typos_out_of_range_values_and_attempts_to_relax_what_cannot_be_relaxed(cfg, env, code):
    with pytest.raises(PolicyError) as e: GatePolicy.resolve(cfg, env)
    assert e.value.code == code


def test_tiers_gate_at_their_own_alpha_so_the_same_evidence_can_pass_at_t1_and_fail_at_t2():
    def mk(sd): return {"proposal_id": "x", "tier": "T1", "models": {"main": {"variants": {v: {"target": {"b": arrays(40, 0.04, 0.15, sd)[0], "c": arrays(40, 0.04, 0.15, sd)[1], "direction": "maximize"}, "guardrails": []} for v in ("canonical", "format_varied")},
                                                                                   "heldout": {"target": {"b": [0.5], "c": [0.5], "direction": "maximize"}}}}}
    pol, P1, P2 = GatePolicy.resolve(), None, None
    P1, P2 = {**pol.gate_params("T1"), "B": 600}, {**pol.gate_params("T2"), "B": 600}
    found = next((sd for sd in range(1, 600) if G.decide_round("C", [mk(sd)], P1)[0]["accepted"] and not G.decide_round("C", [mk(sd)], P2)[0]["accepted"]), None)
    assert found is not None                                                                                       # evidence that clears alpha 0.05 but not 0.01 exists, and the policy keeps them apart
    lean = GatePolicy.resolve({"tiers": {"T2": {"alpha": 0.05}}})                                                  # an administrator relaxes T2 for this environment
    assert G.decide_round("C", [mk(found)], {**lean.gate_params("T2"), "B": 600})[0]["accepted"]
