"""Outcome contract and verdicts (design §13.11): the verdict comes from evidence against a contract fixed before execution, never from the agent's claim."""
import json
import pytest
from conftest import VEC, events_of, oa
from taskkit import make_task, solution
from test_eval import FIN, eval_run
from harness.eval import outcome as O
from harness.eval.task import validate_task

V = json.loads((VEC / "outcome-vectors.json").read_text())


def ctx_of(c):
    files = c.get("files")
    def read(p):
        if p not in files: raise KeyError(p)
        return files[p]
    return {**{k: v for k, v in c.items() if k != "files"}, "fs_read": read if files is not None else None}


def test_verdict_precedence_matches_the_shared_vectors():
    for c in V["derive"]: assert O.derive_verdict(c["results"]) == c["expect"], c["name"]
    assert {c["expect"] for c in V["derive"]} == set(O.VERDICTS)                                                                      # every verdict is reachable


def test_evidence_that_is_missing_stale_conflicting_or_from_a_failing_adapter_can_never_make_a_success():
    for status in ("unavailable", "stale", "conflict", "error", "anything else"):
        assert O.derive_verdict([{"id": "a", "required": True, "status": "pass"}, {"id": "b", "required": True, "status": status}]) == "INCONCLUSIVE"
    O.register_verifier("authoritative_lookup", lambda p, c: (_ for _ in ()).throw(RuntimeError("system down")))
    try:
        con = {"acceptance_criteria": [{"id": "r", "required": True, "verification": "authoritative_lookup"}], "success_policy": "all_required_criteria_pass", "inconclusive_policy": "do_not_report_success"}
        assert O.evaluate_contract(con, {})["verdict"] == "INCONCLUSIVE"                                                               # an adapter that fails proves nothing
        O.register_verifier("authoritative_lookup", lambda p, c: {"status": "pass", "ref": "record:42"})
        assert O.evaluate_contract(con, {})["verdict"] == "SUCCESS" and O.evaluate_contract(con, {})["criteria"][0]["evidence_ref"] == "record:42"
    finally: O.VERIFIERS.pop("authoritative_lookup", None)
    assert "authoritative_lookup" not in O.VERIFIERS


def test_contracts_are_evaluated_identically_to_the_vectors():
    for c in V["evaluate"]:
        r = O.evaluate_contract(c["contract"], ctx_of(c["ctx"]))
        assert (r["verdict"], [x["status"] for x in r["criteria"]], [x["evidence_ref"] for x in r["criteria"]], r["contract_hash"]) == (c["verdict"], c["statuses"], c["refs"], c["contract_hash"]), c["name"]


def test_invalid_contracts_are_rejected_before_anything_runs():
    for c in V["invalid"]:
        assert O.validate_contract(c["contract"]), c["name"]
        with pytest.raises(O.ContractError): O.evaluate_contract(c["contract"], {})


def test_money_is_compared_as_exact_decimals_not_floats():
    mk = lambda a, e: {"acceptance_criteria": [{"id": "amt", "required": True, "verification": "exact_decimal_comparison", "params": {"actual": a, "expected": e}}], "success_policy": "all_required_criteria_pass", "inconclusive_policy": "do_not_report_success"}
    assert O.evaluate_contract(mk("0.1000000000000000055511151231257827", "0.1"), {})["verdict"] == "FAILURE"                          # a float 0.1 is not the decimal 0.1
    assert O.evaluate_contract(mk("0.10", "0.1"), {})["verdict"] == "SUCCESS" and O.evaluate_contract(mk("not a number", "1"), {})["verdict"] == "INCONCLUSIVE"


def test_the_contract_hash_follows_the_contract():
    a = V["evaluate"][1]["contract"]; b = json.loads(json.dumps(a)); assert O.contract_hash(a) == O.contract_hash(b)
    b["acceptance_criteria"][0]["params"]["value"] = 0.9; assert O.contract_hash(a) != O.contract_hash(b)


def test_outcome_metrics_use_every_evaluated_task_unless_an_exclusion_is_documented():
    m = O.outcome_metrics(V["metrics"]["records"]); assert m == V["metrics"]["all"] and m["denominator"] == "all evaluated tasks"
    assert m["overall"]["n"] == 5 and m["overall"]["success_rate"] == 0.4 and m["overall"]["inconclusive_rate"] == 0.2 and m["overall"]["audited"] == 2 and m["overall"]["evaluation_error_rate"] == 0.5
    assert set(m["groups"]) == {"code|low", "db|high"}
    ex = O.outcome_metrics(V["metrics"]["records"], "tasks without an authoritative system", lambda r: r["category"] == "db"); assert ex == V["metrics"]["excluding"] and ex["overall"]["n"] == 3
    with pytest.raises(O.ContractError, match="documented exclusion policy"): O.outcome_metrics(V["metrics"]["records"], None, lambda r: r["category"] == "db")     # no silent survivorship bias


# ------------------------------------------------------------------ in a real evaluation run
CONTRACT = lambda *c: {"acceptance_criteria": list(c), "success_policy": "all_required_criteria_pass", "inconclusive_policy": "do_not_report_success"}
SCRIPT = lambda: [oa(calls=[solution(0.8)[0]]), oa(calls=[solution(0.8)[1]]), FIN(), FIN(), FIN()]


async def test_every_evaluation_run_records_a_verdict_even_without_an_explicit_contract(mk, tmp_path):
    h, run, _ = await eval_run(mk, tmp_path, SCRIPT())
    ev = events_of(h, run, "outcome.evaluated")[0]["payload"]
    assert ev["verdict"] == "SUCCESS" and ev["criteria"][0]["id"] == "valid_submission" and len(ev["contract_hash"]) == 64 and await run.state() == "COMPLETED"


async def test_a_contract_the_work_does_not_meet_fails_the_run_with_a_failure_verdict(mk, tmp_path):
    con = CONTRACT({"id": "score", "required": True, "verification": "test_result", "params": {"metric": "value", "op": ">=", "value": 0.95}})
    h, run, _ = await eval_run(mk, tmp_path, SCRIPT(), task_over={"outcome": con})
    assert events_of(h, run, "outcome.evaluated")[0]["payload"]["verdict"] == "FAILURE" and await run.state() != "COMPLETED"


async def test_a_criterion_with_no_adapter_is_inconclusive_so_the_run_cannot_claim_success(mk, tmp_path):
    con = CONTRACT({"id": "score", "required": True, "verification": "test_result", "params": {"metric": "value", "op": ">=", "value": 0.5}},
                   {"id": "record_updated", "required": True, "verification": "authoritative_lookup"})
    h, run, _ = await eval_run(mk, tmp_path, SCRIPT(), task_over={"outcome": con})
    ev = events_of(h, run, "outcome.evaluated")[0]["payload"]
    assert ev["verdict"] == "INCONCLUSIVE" and [c["status"] for c in ev["criteria"]] == ["pass", "unavailable"] and await run.state() != "COMPLETED"        # the score passed, but success was not established


async def test_an_artifact_criterion_checks_the_final_workspace(mk, tmp_path):
    ok = CONTRACT({"id": "script", "required": True, "verification": "artifact_check", "params": {"path": "/workspace/run.sh"}})
    h, run, _ = await eval_run(mk, tmp_path, SCRIPT(), task_over={"outcome": ok}); assert events_of(h, run, "outcome.evaluated")[0]["payload"]["verdict"] == "SUCCESS"
    missing = CONTRACT({"id": "report", "required": True, "verification": "artifact_check", "params": {"path": "/workspace/report.md"}})
    h2, run2, _ = await eval_run(mk, tmp_path / "b", SCRIPT(), task_over={"outcome": missing}); assert events_of(h2, run2, "outcome.evaluated")[0]["payload"]["verdict"] == "FAILURE"


def test_task_validation_rejects_a_bad_contract(tmp_path):
    bad = CONTRACT({"id": "a", "required": True, "verification": "test_result"}, {"id": "a", "required": True, "verification": "test_result"})
    assert any("used twice" in e for e in validate_task(make_task(tmp_path, "bad", outcome=bad)))
    assert validate_task(make_task(tmp_path, "good", outcome=CONTRACT({"id": "a", "required": True, "verification": "test_result"}))) == []


async def test_every_evaluation_run_records_its_trajectory_metrics_apart_from_the_outcome(mk, tmp_path):
    from harness import telemetry as T
    h, run, _ = await eval_run(mk, tmp_path, SCRIPT())
    tr = events_of(h, run, "trajectory.analyzed")[0]; out = events_of(h, run, "outcome.evaluated")[0]; m = tr["payload"]["metrics"]
    assert tr["seq"] < out["seq"] and m["analyzer_version"] == 1 and m["steps"] >= 2 and m["tool_calls"] >= 2 and m["tool_errors"] == 0                                  # the transcript's record comes first, and is separate
    assert m == T.trajectory_metrics([e for e in h.store.events(run.run_id) if e["seq"] < tr["seq"]]) and "verdict" not in m                                       # a pure function of the log up to that point; no outcome mixed in
    ev = json.loads(h.blobs.get(events_of(h, run, "verification.completed")[0]["payload"]["evidence_ref"])); assert ev["evidence"]["trajectory"] == m and ev["evidence"]["outcome"]["verdict"] == "SUCCESS"                       # both appear in the evaluation report
