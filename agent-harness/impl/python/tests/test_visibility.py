"""Declared test visibility (design §13.1) and the evaluation-run manifest: policy and verified runtime state are separate, contradictions are refused, and public-test results never authorize a generalization claim."""
import os
import pytest
from conftest import events_of, oa
from taskkit import make_task, solution
from test_eval import FIN, eval_run
from harness import manifest as M
from harness.eval import visibility as V
from harness.eval.outcome import contract_for, contract_hash
from harness.eval.task import Task, validate_task

SCRIPT = lambda: [oa(calls=[solution(0.8)[0]]), oa(calls=[solution(0.8)[1]]), FIN(), FIN()]
PUBLIC = {"visibility": "public", "purpose": "development"}


def test_a_task_without_a_full_declaration_gets_a_conservative_one_and_never_a_generalization_claim():
    assert V.declaration_for({"visibility": "hidden"}) == {"purpose": "evaluation", "held_out_use": False, "generalization_claim": False, "public_tests": {"mount_mode": "read_only"}}
    assert V.declaration_for({"visibility": "hidden", "confirmation": {"mode": "heldout_set"}})["held_out_use"] is True
    assert V.declaration_for({"visibility": "public"}) == {"purpose": "development_only", "held_out_use": False, "generalization_claim": False, "public_tests": {"mount_mode": "read_only"}}
    given = {"visibility": "hidden", "test_visibility": {"purpose": "evaluation", "held_out_use": True, "generalization_claim": True}}; assert V.declaration_for(given)["generalization_claim"] is True and V.validate_declaration(given) == []


def test_missing_invalid_and_contradictory_declarations_are_rejected(tmp_path):
    bad = {"public + a generalization claim": {"visibility": "public", "purpose": "development", "test_visibility": {"purpose": "development_only", "held_out_use": False, "generalization_claim": True}},
           "development_only + held-out use": {"visibility": "hidden", "test_visibility": {"purpose": "development_only", "held_out_use": True, "generalization_claim": False}},
           "public + held-out use": {"visibility": "public", "purpose": "development", "test_visibility": {"purpose": "development_only", "held_out_use": True, "generalization_claim": False}},
           "public declared for evaluation": {"visibility": "public", "purpose": "development", "test_visibility": {"purpose": "evaluation", "held_out_use": False, "generalization_claim": False}}}
    for name, over in bad.items():
        errs = validate_task(make_task(tmp_path, name.replace(" ", "_").replace("+", "p"), **over)); assert any("test_visibility" in e for e in errs), (name, errs)
    for name, tv in {"missing field": {"purpose": "evaluation", "held_out_use": False}, "unknown purpose": {"purpose": "fun", "held_out_use": False, "generalization_claim": False},
                     "writable mount": {"purpose": "evaluation", "held_out_use": False, "generalization_claim": False, "public_tests": {"mount_mode": "read_write"}}}.items():
        assert validate_task(make_task(tmp_path, name.replace(" ", "_"), test_visibility=tv)), name
    assert validate_task(make_task(tmp_path, "fine", test_visibility={"purpose": "evaluation", "held_out_use": False, "generalization_claim": False})) == []


async def test_a_contradictory_declaration_also_refuses_the_run(mk, tmp_path):
    d = make_task(tmp_path, "t", test_visibility={"purpose": "development_only", "held_out_use": True, "generalization_claim": False})
    h = mk([oa("d")])
    with pytest.raises(ValueError, match="held-out"): await h.create_eval_run("u", Task(d, __import__("yaml").safe_load((d / "task.yaml").read_text())))


async def fresh(mk, tmp_path, **over):
    import yaml
    d = make_task(tmp_path, "t1", **over); h = mk([oa("d")]); run, loop, runner = await h.create_eval_run("u", Task(d, yaml.safe_load((d / "task.yaml").read_text()))); return h, run, loop


async def test_a_hidden_tasks_tests_are_verified_not_to_be_in_the_agent_workspace(mk, tmp_path):
    h, run, loop = await fresh(mk, tmp_path)                                                                                                                  # inspected while the sandbox still exists
    v = events_of(h, run, "test_visibility.verified")[0]["payload"]; assert v["state"] == {"available": False, "mounted_in_agent_workspace": False, "mount_mode": "read_only", "read_only_enforced": False} and not events_of(h, run, "test.exposed")
    assert not os.path.exists(loop.sandbox().executor.resolve("/workspace") + "/tests")                                                                       # nothing of the hidden tests is in the agent's workspace


async def test_a_public_tasks_tests_are_mounted_read_only_and_the_exposure_is_recorded_without_claiming_enforcement(mk, tmp_path):
    h, run, loop = await fresh(mk, tmp_path, **PUBLIC)
    ex = events_of(h, run, "test.exposed")[0]["payload"]; v = events_of(h, run, "test_visibility.verified")[0]["payload"]
    assert "grade.py" in ex["test_ids"] and not any(i.startswith("confirm/") for i in ex["test_ids"]) and ex["mount_mode"] == "read_only"                      # the held-out confirmation set is never shown
    assert v["state"]["mounted_in_agent_workspace"] is True and v["state"]["available"] is True and v["state"]["read_only_enforced"] is False                 # a file permission on the dev tier, and it says so
    ex_ = loop.sandbox().executor; real = ex_.resolve("/workspace/tests/grade.py")
    assert os.path.isfile(real) and (os.stat(real).st_mode & 0o222) == 0 and v["declaration"]["generalization_claim"] is False and v["declaration"]["purpose"] == "development_only"
    assert not os.path.exists(ex_.resolve("/workspace") + "/tests/confirm")


def test_the_grader_hash_follows_the_tests(tmp_path):
    d = make_task(tmp_path, "g"); a = V.tests_hash(d / "tests"); assert a == V.tests_hash(d / "tests") and len(a) == 64
    (d / "tests" / "grade.py").write_text("print('changed')"); assert V.tests_hash(d / "tests") != a
    (d / "tests" / "new.py").write_text("x"); assert V.tests_hash(d / "tests") != a


async def test_the_manifest_of_an_evaluation_run_records_the_task_grader_rubric_and_verified_visibility(mk, tmp_path):
    h, run, _ = await eval_run(mk, tmp_path, SCRIPT(), task_over=PUBLIC)
    start = events_of(h, run, "run.created")[0]["payload"]["manifest"]; f = start["fields"]
    task = f["task"]["value"]; assert (task["id"], task["version"]) == ("t1", "1.0.0") and len(task["grader_sha256"]) == 64 and M.validate(start, "core") == []
    rubric = next(d for d in f["definitions"]["value"] if d["id"] == "evaluator_rubric"); assert run.pins.resolve("evaluator_rubric")["scoring"]["primary"]["metric"] == "accuracy" and rubric["version"] == 1
    ev = events_of(h, run, "outcome.evaluated")[0]["payload"]; assert task["outcome_contract_sha256"] == ev["contract_hash"]                                        # the contract in the manifest is the one that judged the run
    fin = events_of(h, run, "run.finished")[0]["payload"]; assert fin["fields"]["test_visibility"]["value"]["state"]["mounted_in_agent_workspace"] is True and fin["fields"]["outcome"]["value"]["verdict"] == "SUCCESS"
    assert fin["manifest_hash"] == M.manifest_hash(M.merged(start, fin["fields"]))


def test_declarations_and_the_grader_hash_match_the_shared_vectors(tmp_path):
    import json
    from conftest import VEC
    v = json.loads((VEC / "visibility-vectors.json").read_text())
    for c in v["declarations"]: assert (V.declaration_for(c["spec"]), V.validate_declaration(c["spec"])) == (c["declaration"], c["errors"]), c["name"]
    for k, content in v["tests_dir"]["files"].items(): (tmp_path / k).parent.mkdir(parents=True, exist_ok=True); (tmp_path / k).write_text(content)
    assert V.tests_hash(tmp_path) == v["tests_dir"]["tests_hash"] and [str(p.relative_to(tmp_path)) for p in V.public_files(tmp_path)] == v["tests_dir"]["public_files"]
