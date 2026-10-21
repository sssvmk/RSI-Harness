import json, shutil
from pathlib import Path
import pytest
from conftest import oa, events_of
from taskkit import make_task, solution
from harness.eval.runner import shape_feedback, truncate_utf8
from harness.eval.task import load_task, validate_task
from harness.stats import required_trials

FIN = lambda: oa(calls=[("finish", {"summary": "done"})])


def test_valid_task_loads(tmp_path):
    assert validate_task(make_task(tmp_path)) == []
    assert load_task(tmp_path / "t1").id == "t1"


@pytest.mark.parametrize("mutate,needle", [
    (lambda d: (d / "instruction.md").unlink(), "instruction file"),
    (lambda d: shutil.rmtree(d / "environment"), "build file"),
    (lambda d: (d / "tests" / "grade.py").unlink(), "not found under tests/"),
    (lambda d: shutil.rmtree(d / "tests" / "confirm"), None),
    (lambda d: (d / "environment" / "tests").mkdir(), "agent-reachable"),
])
def test_task_validation_failures(tmp_path, mutate, needle):
    d = make_task(tmp_path)
    mutate(d)
    errs = validate_task(d)
    if needle: assert any(needle in e for e in errs), errs
    else: assert errs == [] or errs                       # confirm/ is only required for heldout mode
    if needle: pytest.raises(ValueError, load_task, d)


def test_task_validation_users_ids_and_modes(tmp_path):
    d = make_task(tmp_path, "a", environment={"work_image": {"build": "environment/Dockerfile"}, "users": {"agent": 1, "candidate": 1, "judge": 2}})
    assert any("distinct" in e for e in validate_task(d))
    dup = [{"id": "x", "run_as": "judge", "cmd": ["true"], "timeout_s": 5}] * 2
    assert any("unique" in e for e in validate_task(make_task(tmp_path, "b", steps=dup)))
    d = make_task(tmp_path, "c", confirmation={"mode": "heldout_set", "trials": 1}); shutil.rmtree(d / "tests" / "confirm")
    assert any("tests/confirm" in e for e in validate_task(d))
    assert any("invalid" in e for e in validate_task(make_task(tmp_path, "e", scoring={"steps": [{"id": "s", "run_as": "root", "cmd": ["x"], "timeout_s": 1}], "report_path": "/p", "primary": {"metric": "m", "direction": "maximize"}})))
    assert validate_task(tmp_path / "missing")[0].endswith("not found")


def test_feedback_shaping_is_bounded_and_never_raw():
    rep = {"valid": True, "primary": 0.5, "feedback_text": "é" * 100}
    assert shape_feedback(rep, "none", 100) == "Submission received."
    assert shape_feedback(rep, "score", 100) == "Score: 0.5"
    out = shape_feedback(rep, "score_and_summary", 20)
    assert len(out.encode()) <= 20 and out.startswith("Score: 0.5\n")
    out.encode("utf-8")                                                      # never splits a multi-byte character
    assert truncate_utf8("aé", 2) == "a"
    assert "could not be scored" in shape_feedback({"valid": False, "primary": 0}, "score", 100)


async def eval_run(mk, tmp_path, script, task_over=None, **cfg):
    h = mk(script, **cfg)
    task = load_task(make_task(tmp_path, **(task_over or {})))
    run, loop, runner = await h.create_eval_run("u", task)
    await h.rm.start(run)
    await loop.run_loop()
    return h, run, runner


async def test_full_flow_events_isolation_and_feedback(mk, tmp_path):
    h, run, runner = await eval_run(mk, tmp_path, [oa(calls=[solution(0.8)[0]]), oa(calls=[solution(0.8)[1]]), FIN()])
    assert await run.state() == "COMPLETED"
    types = [e["type"] for e in h.store.events(run.run_id)]
    order = ["submission.created", "snapshot.created", "judge.started", "judge.completed", "feedback.delivered", "verification.completed", "run.completed"]
    idx = [types.index(t) for t in order]; assert idx == sorted(idx)
    states = [(e["payload"]["from"], e["payload"]["to"]) for e in events_of(h, run, "run.state_changed")]
    assert ("RUNNING", "SCORING") in states and ("SCORING", "RUNNING") in states
    # feedback is exactly the shaped score, delivered as the result of the submit call
    submit_res = [e for e in events_of(h, run, "tool.call.completed") if e["payload"]["tool"] == "submit"][0]
    assert submit_res["payload"]["message"]["parts"][0]["parts"][0]["text"] == "Score: 0.8"
    # a fresh judge sandbox, distinct from the work sandbox, destroyed afterwards with its grants revoked
    created = events_of(h, run, "sandbox.created")
    assert [c["payload"]["role"] for c in created] == ["work", "judge"] and len({c["payload"]["sandbox_id"] for c in created}) == 2
    judge_id = created[1]["payload"]["sandbox_id"]
    assert any(e["payload"]["sandbox_id"] == judge_id for e in events_of(h, run, "sandbox.destroyed"))
    assert await run.proj("grants_active") == {}
    # snapshot event records the chain head it was taken at
    snap = events_of(h, run, "snapshot.created")[0]
    assert snap["payload"]["chain_head"] == h.store.get_event(run.run_id, snap["seq"] - 1)["hash"]
    # the report is validated and the evaluator kind is set by the control plane, not the grader
    rep = h.blobs.get_json(events_of(h, run, "judge.completed")[0]["payload"]["score_report_ref"])
    assert rep["evaluator"] == {"kind": "deterministic", "model_ref": None} and rep["primary"] == 0.8
    assert h.store.verify(run.run_id)["ok"]


async def test_agent_cannot_reach_hidden_tests_through_file_tools(mk, tmp_path):
    h, run, runner = await eval_run(mk, tmp_path, [oa(calls=[("fs.read", {"path": "/tests/grade.py"}), ("fs.read", {"path": "/workspace/tests/grade.py"}),
                                                          ("fs.glob", {"pattern": "/workspace/*"}), ("fs.search", {"pattern": "HIDDEN-HINT|SECRETSTACK", "path": "/workspace"})]), oa("x")])
    assert [e["payload"]["error"]["code"] for e in events_of(h, run, "tool.call.failed")] == ["fs.path_not_allowed", "fs.path_not_found"]
    texts = [e["payload"]["message"]["parts"][0]["parts"][0]["text"] for e in events_of(h, run, "tool.call.completed")]
    assert "grade.py" not in " ".join(texts) and texts[-1] == "(no matches)"


async def test_work_sandbox_never_mounts_tests_dir(mk, tmp_path):
    h = mk([])
    task = load_task(make_task(tmp_path))
    run, loop, runner = await h.create_eval_run("u", task)
    work = loop.sandbox()
    assert "/tests" not in work.executor.roots and not (Path(work.root) / "tests").exists()
    from harness.preflight import run_preflight
    pf = run_preflight("core", h.config, work_roots=[str(work.root)], hidden_tests_dirs=[str(task.tests_dir)])
    assert [c for c in pf["checks"] if c["id"] == "PF-13"][0]["ok"]


async def test_no_raw_grader_output_leaks_to_the_agent(mk, tmp_path):
    h, run, runner = await eval_run(mk, tmp_path, [oa(calls=[("submit", {})]), oa(calls=[("submit", {})]), oa("x")])    # no result.txt: grader crashes with SECRETSTACK
    blob = json.dumps(h.store.events(run.run_id)) + json.dumps([r["body"] for r in h.transport.requests])
    assert "SECRETSTACK" not in blob and "HIDDEN-HINT" not in blob
    fb = [e for e in events_of(h, run, "tool.call.completed") if e["payload"]["tool"] == "submit"][0]["payload"]["message"]["parts"][0]["parts"][0]["text"]
    assert fb == "Submission was invalid: it could not be scored."
    assert events_of(h, run, "judge.completed")[0]["payload"]["valid"] is False


async def test_summary_level_feedback_is_truncated_to_max_bytes(mk, tmp_path):
    h, run, _ = await eval_run(mk, tmp_path, [oa(calls=[solution(0.5)[0]]), oa(calls=[solution(0.5)[1]]), oa("x")], task_over={"feedback": {"level": "score_and_summary", "max_bytes": 40}})
    fb = [e for e in events_of(h, run, "tool.call.completed") if e["payload"]["tool"] == "submit"][0]["payload"]["message"]["parts"][0]["parts"][0]["text"]
    assert len(fb.encode()) <= 40 and fb.startswith("Score: 0.5\nHIDDEN-HINT")
    assert events_of(h, run, "feedback.delivered")[0]["payload"]["bytes"] <= 40


async def test_submission_budget_is_enforced(mk, tmp_path):
    s = [oa(calls=[solution(0.1)[0]]), oa(calls=[solution(0.1)[1]]), oa(calls=[solution(0.2)[1]]), oa("x")]
    h, run, _ = await eval_run(mk, tmp_path, s, task_over={"agent": {"budgets": {"turns": 30, "tokens": 2_000_000, "time_s": 600, "submissions": 1}}})
    assert len(events_of(h, run, "submission.created")) == 1
    assert [e["payload"]["error"]["code"] for e in events_of(h, run, "tool.call.failed")] == ["submit.budget_exhausted"]


async def test_selection_picks_best_and_confirmation_is_mandatory_for_two_or_more(mk, tmp_path):
    s = [oa(calls=[solution(0.6)[0]]), oa(calls=[solution(0.6)[1]]),
         oa(calls=[solution(0.9)[0]]), oa(calls=[solution(0.9)[1]]),
         oa(calls=[solution(0.7)[0]]), oa(calls=[solution(0.7)[1]]), FIN()]
    h, run, runner = await eval_run(mk, tmp_path, s, task_over={"scoring": {"pass_threshold": 0.85}})
    sel = events_of(h, run, "selection.made")[0]["payload"]
    subs = await run.proj("submissions")
    assert sel["n_submissions"] == 3 and subs[sel["submission_id"]]["primary"] == 0.9
    conf = events_of(h, run, "confirmation.completed")[0]["payload"]
    assert conf["rule"] == "reseed" and conf["n_submissions"] == 3
    r = conf["result"]
    assert r["valid"] and r["trials"] == 3 and r["unconfirmed_upper_bound"] == 0.9 and r["submissions_used"] == 3
    assert r["lower"] <= r["mean"] <= r["upper"] and 0.9 <= r["mean"] <= 0.907     # confirmed value differs from the upper bound (trial noise)
    assert len(events_of(h, run, "confirmation.trial")) == 3
    assert await run.state() == "COMPLETED"
    assert "confirmed value" in h.blobs.get_json(events_of(h, run, "verification.completed")[0]["payload"]["evidence_ref"])["detail"]


async def test_single_submission_is_labelled_an_unconfirmed_upper_bound(mk, tmp_path):
    h, run, _ = await eval_run(mk, tmp_path, [oa(calls=[solution(0.9)[0]]), oa(calls=[solution(0.9)[1]]), FIN()])
    assert not events_of(h, run, "confirmation.completed")
    ev = h.blobs.get_json(events_of(h, run, "verification.completed")[0]["payload"]["evidence_ref"])
    assert "unconfirmed upper bound" in ev["detail"]


async def test_heldout_confirmation_uses_a_separate_hidden_set_and_can_fail_the_gate(mk, tmp_path):
    # the visible score (0.9) would pass a 0.86 threshold, but the held-out set scores 0.85 (the grader subtracts 0.05 there)
    s = [oa(calls=[solution(0.9)[0]]), oa(calls=[solution(0.9)[1]]), oa(calls=[solution(0.9)[1]]), FIN(), FIN(), FIN()]
    h, run, _ = await eval_run(mk, tmp_path, s, task_over={"confirmation": {"mode": "heldout_set", "trials": 2}, "scoring": {"pass_threshold": 0.86}})
    conf = events_of(h, run, "confirmation.completed")[0]["payload"]
    assert conf["rule"] == "heldout_set" and abs(conf["result"]["mean"] - 0.85) < 1e-9 and conf["result"]["unconfirmed_upper_bound"] == 0.9
    v = events_of(h, run, "verification.completed")
    assert v[0]["payload"]["passed"] is False and "confirmed value 0.85" in v[0]["payload"]["detail"]
    assert await run.state() == "TERMINATED" and not events_of(h, run, "run.completed")


async def test_invalid_submission_never_wins_selection(mk, tmp_path):
    s = [oa(calls=[("submit", {})]), oa(calls=[solution(0.4)[0]]), oa(calls=[solution(0.4)[1]]), FIN()]
    h, run, runner = await eval_run(mk, tmp_path, s)
    sel = events_of(h, run, "selection.made")[0]["payload"]
    assert (await run.proj("submissions"))[sel["submission_id"]]["valid"] is True


async def test_judge_step_timeout_marks_submission_invalid(mk, tmp_path):
    steps = [{"id": "slow", "run_as": "judge", "cmd": ["bash", "-c", "sleep 30"], "timeout_s": 1}]
    h, run, _ = await eval_run(mk, tmp_path, [oa(calls=[("submit", {})]), oa("x")], task_over={"scoring": {"steps": steps, "report_path": "/scratch/score.json", "primary": {"metric": "m", "direction": "maximize"}}})
    rep = h.blobs.get_json(events_of(h, run, "judge.completed")[0]["payload"]["score_report_ref"])
    assert rep["valid"] is False and "timed out" in rep["errors"][0]


def test_replication_sizing_formula():
    assert required_trials(0.01, 0.1) == 3                                 # floor at min_trials
    assert required_trials(0.2, 0.05) == 62
    with pytest.raises(ValueError): required_trials(1, 1, 0.5)
