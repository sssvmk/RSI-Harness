"""Shared conformance vectors (design §19.3): every implementation must pass these bit-for-bit."""
import copy, json
from conftest import VEC
from harness.canonical import compute_hash, genesis_hash
from harness.eventlog import projections as P
from harness.gateway.adapters import ADAPTERS
from harness.policy import decide
from harness.stats import Xoshiro256SS, bootstrap_ci, gate, seed_for, splitmix64_stream
from harness.tokens import estimate_request


def test_hash_vectors():
    v = json.loads((VEC / "hash-vectors.json").read_text())
    assert genesis_hash(v["run_id"]) == v["genesis"]
    prev = v["genesis"]
    for e in v["events"]:
        assert e["prev_hash"] == prev
        assert compute_hash(prev, e) == e["hash"], e["type"]
        prev = e["hash"]


def test_policy_vectors():
    lines = [json.loads(l) for l in (VEC / "policy-vectors.jsonl").read_text().splitlines() if l.strip()]
    assert len(lines) >= 30
    for c in lines:
        got = decide(c["input"]["ruleset"], c["input"]["call"])
        exp = c["expected"]
        assert (got["decision"], got["matched_rule_ids"], got["default_used"]) == (exp["decision"], exp["matched_rule_ids"], exp["default_used"]), c["name"]


def test_estimator_vectors():
    for c in json.loads((VEC / "estimator-vectors.json").read_text()):
        i = c["input"]
        assert estimate_request(i["messages"], i.get("tools"), i.get("divisor", 3.0)) == c["expected"], c["name"]


def test_adapter_vectors():
    v = json.loads((VEC / "adapter-vectors.json").read_text())
    for c in v["request_cases"]:
        a = ADAPTERS[c["provider"]]()
        assert a.to_wire(copy.deepcopy(c["request"]), c["model_id"], c["id_map"]) == c["expected_wire"], c["name"]
        if "expected_downgrades" in c:
            assert a.last_downgrades == c["expected_downgrades"]
    for c in v["response_cases"]:
        ids = list(c["ids"])
        a = ADAPTERS[c["provider"]](lambda: ids.pop(0))
        r = a.from_wire(c["wire"])
        r["id_map"] = r.pop("provider_meta")["id_map"]
        assert r == c["expected"], c["name"]


def test_stats_vectors():
    v = json.loads((VEC / "stats-vectors.json").read_text())
    assert hex(next(splitmix64_stream(0))) == "0xe220a8397b1dcdaf" == v["splitmix64_seed0_first"]  # published reference value
    for c in v["xoshiro"]:
        r = Xoshiro256SS(int(c["seed"]))
        assert [str(r.next()) for _ in range(5)] == c["first"]
        assert [r.index(10) for _ in range(5)] == c["index_10"]
    for c in v["seeds"]:
        assert str(seed_for(c["run_id"], c["gate_id"])) == c["seed"]
    for c in v["bootstrap"]:
        assert bootstrap_ci(c["x"], int(c["seed"]), c["confidence"], c["B"]) == c["expected"]
    for g in v["gates"]:
        got = gate(g["run_id"], g["gate_id"], (g["target"]["baseline"], g["target"]["candidate"], g["target"]["direction"]), g["guardrails"], g["confidence"], g["B"])
        assert got == g["expected"]


def test_bootstrap_index_arithmetic_matches_spec():
    ci = bootstrap_ci([0.1, 0.2, 0.3], 1, 0.95, 10000)
    assert (ci["lower_index"], ci["upper_index"]) == (250, 9749)  # §13.7 worked example


def test_projection_vectors():
    v = json.loads((VEC / "projection-vectors.json").read_text())
    assert v["versions"] == P.VERSIONS
    for name in P.NAMES:
        assert P.fold_all(name, copy.deepcopy(v["events"])) == v["expected"][name], name
