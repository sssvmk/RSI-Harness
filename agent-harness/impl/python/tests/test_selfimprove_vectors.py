"""Shared self-improvement vectors (path policy, screens, gate, search, deterministic git). The TypeScript suite checks the same files."""
import copy, json, tempfile
from pathlib import Path
import pytest
from conftest import VEC
from harness.selfimprove import gate as G, search as S
from harness.selfimprove.edits import EditApplier
from harness.selfimprove.evalsets import eval_seed, jaccard, shingle_set
from harness.selfimprove.gitrepo import GitRepo
from harness.selfimprove.promotion import promotion_id, unit_interval
from harness.selfimprove.registry import ComponentRegistry
from harness.selfimprove.screens import build_answer_index, run_screens
from harness.stats import Xoshiro256SS

load = lambda n: json.loads((VEC / n).read_text())


def test_pathpolicy_vectors():
    v = load("selfimprove-pathpolicy.json"); reg = ComponentRegistry.from_yaml(v["registry_yaml"])
    for c in v["cases"]:
        assert reg.check(c["path"]) == (c["tier"], c["reason"]), c["path"]


def test_screen_vectors():
    v = load("selfimprove-screens.json")
    assert build_answer_index(v["answers"]) == v["answer_index"]
    for c in v["cases"]:
        assert [x["screen"] for x in run_screens(copy.deepcopy(c["edits"]), v["answer_index"], v["instance_ids"])] == c["screens"], c["name"]


def test_gate_vectors():
    v = load("selfimprove-gate.json")
    for c in v["cases"]:
        assert G.decide_round(c["run_id"], copy.deepcopy(c["candidates"]), c["params"]) == c["expected"], c["name"]
    for h in v["holm_levels"]:
        assert G.holm_levels(h["p"], h["alpha"], h["holm"]) == h["levels"]
    by = {c["name"]: c["expected"] for c in v["cases"]}                      # the vectors encode the behaviours the design requires, not just whatever we computed
    assert by["clear win and no-op"][0]["accepted"] and not by["clear win and no-op"][1]["accepted"]
    assert by["holm alone (borderline passes)"][0]["accepted"] and not any(d["accepted"] for d in by["holm among three (all fail)"]) and by["holm with a clear winner"][0]["accepted"]
    assert any(d["accepted"] for d in by["holm disabled"]) and by["needs heldout"][0]["needs_heldout"] and "dominated" in by["dominated"][0]["reasons"]


def test_search_vectors():
    v = load("selfimprove-search.json")
    for c in v["novelty"]: assert S.novelty_rejected(c["edit"], c["archive"]) == c["expected"]
    w = v["weights"]; got = S.parent_weights(w["scores"], w["children"], w["tau"])
    assert got == pytest.approx(w["expected"], rel=1e-12)
    for p in v["picks"]: assert S.sample_parent(w["expected"], Xoshiro256SS(int(p["seed"]))) == p["expected"]
    assert S.pareto_front(v["pareto"]["points"]) == v["pareto"]["expected"]
    for s in v["similarity"]: assert S.mean_pairwise_similarity(s["texts"]) == pytest.approx(s["expected"], rel=1e-12)
    for e in v["eval_seeds"]: assert eval_seed(*e["args"]) == e["expected"]
    for j in v["jaccard"]: assert jaccard(shingle_set(j["a"]), shingle_set(j["b"])) == j["expected"]
    assert [promotion_id(x["campaign"], x["candidate"]) for x in v["promotion"]["ids"]] == [x["expected"] for x in v["promotion"]["ids"]]
    for u in v["promotion"]["unit"]: assert unit_interval(u["s"]) == u["expected"]


def test_deterministic_git_vector():
    v = load("selfimprove-git.json"); d = Path(tempfile.mkdtemp())
    repo = GitRepo.init(d / "cfg", v["files"]); assert repo.head() == v["base_commit"]           # same files => same initial commit id as the reference
    reg = ComponentRegistry.from_yaml(v["files"]["components.yaml"])
    r = EditApplier(repo, reg).apply(repo.head(), [v["edit"]], ["T0", "T1"], "cand/x/1/1", v["message"])
    e = v["expected"]
    assert (r.commit, r.tier, r.changed_lines) == (e["commit"], e["tier"], e["changed_lines"]) and repo.changed_in(r.commit) == e["changed"] and repo.git("rev-parse", f"{r.commit}^{{tree}}") == e["tree"]


async def test_golden_campaign_log_is_reproduced_hash_for_hash():
    """The same campaign (id, scripted proposer, simulated environment) must give an identical event hash chain and the same main commit in every implementation."""
    from si_world import World, proposal, edit_hint
    v = load("selfimprove-campaign.json")
    w = World(Path(tempfile.mkdtemp()), {(1, 1): proposal(edit_hint("HINT+ verify tool output carefully")), (1, 2): proposal(edit_hint("HINT- skip verification entirely")),
                                         (2, 1): proposal(edit_hint("HINT+ plan the steps ahead of time")), (2, 2): proposal(edit_hint("OVERFIT memorise the examples")), "default": None}, cid=v["campaign_id"])
    await w.setup(); s = await w.controller().run(); ev = w.store.events(w.cid)
    assert s["stopped"] == v["stopped"] and w.config.head() == v["main_head"]
    assert [{"seq": e["seq"], "type": e["type"], "hash": e["hash"]} for e in ev] == v["events"]
    w.close()


def test_policy_vectors():
    from harness.selfimprove.policy import GatePolicy, PolicyError
    v = load("selfimprove-policy.json")
    for c in v["cases"]:
        q = GatePolicy.resolve(c["config"], c["environment"])
        assert q.fingerprint == c["fingerprint"] and q.diff() == c["diff"] and len(q.relaxed()) == c["relaxed"], c["name"]
        assert {t: q.gate_params(t) for t in ("T0", "T1", "T2")} == c["gate_params"] and {t: q.review(t) for t in ("T0", "T1", "T2")} == c["review"] and {t: q.canary_fraction(t) for t in ("T0", "T1", "T2")} == c["canary"]
    for e in v["errors"]:
        with pytest.raises(PolicyError) as ex: GatePolicy.resolve(e["config"], e["environment"])
        assert ex.value.code == e["code"], e["name"]
