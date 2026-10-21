"""Crash recovery (design §5.9, §19.5): kill at event boundaries, restart on the same files, verify invariants."""
import asyncio, itertools, json, sqlite3
from pathlib import Path
import pytest
from conftest import oa, events_of, make_config
from taskkit import make_task, solution
from harness.app import Harness
from harness.clock import FakeClock
from harness.config import load_config
from harness.credentials import DictSecretProvider
from harness.eval.task import load_task
from harness.gateway.gateway import ScriptedTransport
from harness.loop.verifier import CallableVerifier
from harness.sandbox.backend import LocalProcessBackend
from harness.tools.runtime import ToolError


class Crash(BaseException):
    """Simulated SIGKILL: not an Exception, so no handler in the code under test can swallow it."""


async def _ok(run, sb): return {"passed": True, "detail": "ok", "evidence": {}}
PASS = lambda: CallableVerifier(_ok)
FIN = lambda: oa(calls=[("finish", {"summary": "done"})])
def SCRIPT(tmp=None):
    """The shell command appends to a *host-side* file, which (unlike the sandbox) survives teardown, so executions can be counted."""
    path = f"{tmp}/execs.log" if tmp else "/workspace/log.txt"
    return [oa(calls=[("fs.write", {"path": "/workspace/a.txt", "content": "A"})]), oa(calls=[("shell.run", {"cmd": f"echo x >> {path}"})]), FIN()]


def executions(tmp) -> int:
    p = Path(tmp) / "execs.log"
    return len(p.read_text().splitlines()) if p.exists() else 0


class World:
    """A 'machine' that can be killed and rebooted on the same disk."""
    def __init__(self, tmp, script, profile="core", clock=None, **cfg_over):
        self.tmp, self.script, self.cfg_over, self.clock = tmp, script, cfg_over, clock or FakeClock()
        self.gen = itertools.count(1)
        self.boot(script)

    def boot(self, script):
        self.cfg = make_config(self.tmp, "core", **self.cfg_over)
        self.transport = ScriptedTransport(script)
        base = len(list(getattr(self, "ids_used", []))) if False else 0
        self.h = Harness(self.cfg, self.transport, DictSecretProvider({"k": "sk-test-0000000000000000"}), backend=LocalProcessBackend(str(self.tmp / "sbx")),
                         clock=self.clock, id_factory=lambda g=self.gen: f"ID{next(g)}")

    def arm(self, pred):
        orig = self.h.writer.submit
        async def submit(d):
            ev = await orig(d)
            if pred(ev): raise Crash(ev["type"])
            return ev
        self.h.writer.submit = submit

    def kill(self, run_id):
        for t in self.h.approvals._timers.values(): t.cancel()
        self.h.close()
        responded = sum(1 for e in self.h.store.events(run_id, types=["model.responded"]))
        return responded

    def reboot(self, run_id, responded):
        self.boot(self.script[responded:])


def after(typ, nth=1):
    c = itertools.count(1)
    return lambda ev: ev["type"] == typ and next(c) == nth


async def crash_run(w, pred, verifier=None, instruction="go", **kw):
    w.arm(pred)
    run_id = None
    run, loop = await w.h.create_run("u", instruction, verifier=verifier, **kw) if False else (None, None)
    try:
        await w.h.run_to_end("u", instruction, verifier=verifier, **kw)
    except Crash:
        pass
    run_id = w.h.store.list_runs()[0]["run_id"]
    return run_id


def check_log_invariants(store, run_id):
    assert store.verify(run_id)["ok"]
    done = [e["payload"]["tool_call_id"] for e in store.events(run_id, types=["tool.call.completed"])]
    assert len(done) == len(set(done)), "a tool call completed twice"


async def test_non_idempotent_call_in_flight_is_never_retried(tmp_path):
    w = World(tmp_path, SCRIPT(tmp_path))
    rid = await crash_run(w, lambda e: e["type"] == "tool.call.started" and e["payload"]["tool"] == "shell.run", PASS())
    n = w.kill(rid); w.reboot(rid, n)
    plan, final = await w.h.resume_run(rid, PASS())
    assert final == "AWAITING_HUMAN" and plan.halted == "recovery_uncertain"
    u = events_of(w.h, w.h.rm.get(rid), "recovery.uncertain")[0]["payload"]
    assert u["tool"] == "shell.run" and "unknown" in u["reason"]
    assert executions(tmp_path) == 0                                                                        # the command was NOT run again
    assert len(events_of(w.h, w.h.rm.get(rid), "tool.call.started")) == 2                                 # fs.write + the single shell.run start
    assert (await w.h.rm.get(rid).proj("run_state"))["state"] == "AWAITING_HUMAN"
    check_log_invariants(w.h.store, rid)
    w.h.close()


async def test_idempotent_call_in_flight_is_rerun_and_the_run_completes(tmp_path):
    w = World(tmp_path, SCRIPT(tmp_path))
    rid = await crash_run(w, lambda e: e["type"] == "tool.call.started" and e["payload"]["tool"] == "fs.write", PASS())
    n = w.kill(rid); w.reboot(rid, n)
    plan, final = await w.h.resume_run(rid, PASS())
    assert final == "COMPLETED"
    run = w.h.rm.get(rid)
    assert [e["payload"]["attempt"] for e in events_of(w.h, run, "tool.call.started") if e["payload"]["tool"] == "fs.write"] == [1, 1]   # re-executed
    assert len(events_of(w.h, run, "tool.call.completed")) == 3 and len(events_of(w.h, run, "recovery.uncertain")) == 0
    check_log_invariants(w.h.store, rid)
    w.h.close()


async def test_model_call_in_flight_is_reissued_and_counts_against_budgets(tmp_path):
    w = World(tmp_path, SCRIPT(tmp_path))
    rid = await crash_run(w, after("model.requested", 2), PASS())
    run0 = w.h.rm.get(rid)
    turns_before = (await run0.proj("budgets"))["agent"]["turns"]["used"]
    n = w.kill(rid); w.reboot(rid, n)
    plan, final = await w.h.resume_run(rid, PASS())
    assert plan.reissue_model and final == "COMPLETED"
    run = w.h.rm.get(rid)
    b = (await run.proj("budgets"))["agent"]["turns"]
    assert turns_before == 1 and b["used"] == 4                  # the interrupted turn is a reservation at the crash and is charged at its estimate by recovery (§7.4.4); its re-issue counts too (never reset)
    assert (await run.proj("budgets"))["agent"]["tokens"]["unreconciled"] > 0 and not (await run.proj("budgets"))["reservations"]   # flagged until real usage is known, and nothing is left held
    check_log_invariants(w.h.store, rid)
    w.h.close()


@pytest.mark.parametrize("k", list(range(1, 62, 3)))
async def test_chaos_sweep_crash_at_every_few_event_boundaries(tmp_path, k):
    """Kill after the k-th event, reboot, resume: whatever the boundary, invariants hold and the run reaches a legal outcome."""
    w = World(tmp_path, SCRIPT(tmp_path))
    cnt = itertools.count(1)
    rid = await crash_run(w, lambda e: next(cnt) == k, PASS())
    if w.h.store.run_row(rid)["state"] in ("COMPLETED", "TERMINATED"):
        w.h.close(); return                                          # crash point was after the run finished
    turns_before = (await w.h.rm.get(rid).proj("budgets"))["agent"]["turns"]["used"] if w.h.store.last_seq(rid) > 1 else 0
    tokens_before = (await w.h.rm.get(rid).proj("budgets"))["agent"]["tokens"]["used"] if w.h.store.last_seq(rid) > 1 else 0
    n = w.kill(rid); w.reboot(rid, n)
    plan, final = await w.h.resume_run(rid, PASS())
    run = w.h.rm.get(rid)
    assert final in ("COMPLETED", "AWAITING_HUMAN", "TERMINATED"), final
    if final == "TERMINATED":                                        # only legal when the crash hit before the agent loop existed
        assert "crashed during" in events_of(w.h, run, "run.terminated")[0]["payload"]["detail"]
    if final == "AWAITING_HUMAN":                                    # only legal for an unresolved non-idempotent call
        assert events_of(w.h, run, "recovery.uncertain")[0]["payload"]["tool"] == "shell.run"
    b = (await run.proj("budgets"))["agent"]
    assert b["turns"]["used"] >= turns_before and b["tokens"]["used"] >= tokens_before          # counters never reset
    assert executions(tmp_path) <= 1                                                                 # the shell command never ran twice
    check_log_invariants(w.h.store, rid)
    w.h.close()


async def test_corrupted_log_is_detected_at_recovery(tmp_path):
    w = World(tmp_path, SCRIPT(tmp_path))
    rid = await crash_run(w, after("tool.call.completed", 1), PASS())
    n = w.kill(rid)
    c = sqlite3.connect(w.h.store.path); c.execute("DROP TRIGGER events_no_update")
    c.execute("UPDATE events SET payload='{\"x\":1}' WHERE run_id=? AND seq=7", (rid,)); c.commit(); c.close()
    w.reboot(rid, n)
    plan, final = await w.h.resume_run(rid, PASS())
    assert not plan.ok and plan.halted == "integrity_failure" and final == "FAILED_INTEGRITY"
    assert events_of(w.h, w.h.rm.get(rid), "integrity.alert")[0]["payload"]["bad_seq"] == 7
    w.h.close()


async def test_downtime_is_excluded_only_for_harness_failures_and_only_up_to_the_cap(tmp_path):
    for cause, downtime_s, expect_ms in [("harness_failure", 100, 100_000), ("harness_failure", 900, 300_000), ("operator", 100, 0)]:
        sub = tmp_path / f"{cause}{downtime_s}"; sub.mkdir()
        w = World(sub, SCRIPT())
        rid = await crash_run(w, after("tool.call.completed", 1), PASS())
        w.kill(rid); w.clock.advance(downtime_s * 1000); w.reboot(rid, 1)
        await w.h.resume_run(rid, PASS(), downtime_cause=cause)
        rec = events_of(w.h, w.h.rm.get(rid), "run.recovered")[0]["payload"]
        assert rec["downtime_ms"] >= downtime_s * 1000 and rec["excluded_ms"] == expect_ms and rec["downtime_cause"] == cause
        rs = await w.h.rm.get(rid).proj("run_state")
        assert rs["excluded_ms"] == expect_ms
        w.h.close()


async def test_open_approval_survives_restart_and_executes_exactly_once(tmp_path):
    rules = {"schema_version": 1, "rules": [{"id": "R", "action": "require_approval", "match": {"tool": ["shell.run"]}}]}
    w = World(tmp_path, SCRIPT(tmp_path), approvals={"approvers": ["boss"]})
    w.h.runtime.ruleset = rules
    rid = await crash_run(w, after("approval.requested"), PASS())
    n = w.kill(rid); w.reboot(rid, n); w.h.runtime.ruleset = rules
    run = w.h.rm.get(rid)
    task = asyncio.ensure_future(w.h.resume_run(rid, PASS()))
    for _ in range(300):
        if (await run.proj("approvals_open")) and await run.state() == "AWAITING_APPROVAL": break
        await asyncio.sleep(0.01)
    open_ = await run.proj("approvals_open")
    assert len(open_) == 1 and len(events_of(w.h, run, "approval.requested")) == 1                      # still the one original request
    await w.h.approvals.decide(run, list(open_)[0], "boss", ["approver"], True)
    plan, final = await asyncio.wait_for(task, 10)
    assert final == "COMPLETED"
    assert len([e for e in events_of(w.h, run, "tool.call.started") if e["payload"]["tool"] == "shell.run"]) == 1
    assert executions(tmp_path) == 1
    w.h.close()


async def test_crash_in_the_middle_of_termination_finishes_teardown_once(tmp_path):
    w = World(tmp_path, [oa(calls=[("fs.glob", {"pattern": "/workspace/*"})])] * 5, budgets={"turns": 1})
    rid = await crash_run(w, after("teardown.started"))
    assert w.h.store.run_row(rid)["state"] == "TERMINATING"
    n = w.kill(rid); w.reboot(rid, n)
    plan, final = await w.h.resume_run(rid)
    run = w.h.rm.get(rid)
    assert final == "TERMINATED" and await run.proj("grants_active") == {} and await run.proj("sandboxes") == {}
    assert len(events_of(w.h, run, "run.terminated")) == 1                                              # no duplicate terminal record
    check_log_invariants(w.h.store, rid)
    w.h.close()


@pytest.mark.parametrize("crash_at", ["submission.created", "snapshot.created", "judge.started", "judge.completed", "feedback.delivered"])
async def test_crash_anywhere_in_the_submit_sequence_recovers_without_a_second_submission(tmp_path, crash_at):
    task_dir = make_task(tmp_path, "t")
    script = [oa(calls=[solution(0.7)[0]]), oa(calls=[solution(0.7)[1]]), FIN()]
    w = World(tmp_path, script)
    task = load_task(task_dir)
    w.arm(after(crash_at))
    try:
        run, loop, runner = await w.h.create_eval_run("u", task)
        await w.h.rm.start(run); await loop.run_loop()
    except Crash:
        pass
    rid = w.h.store.list_runs()[0]["run_id"]
    n = w.kill(rid); w.reboot(rid, n)
    plan, final = await w.h.resume_run(rid, task=task)
    run = w.h.rm.get(rid)
    assert final == "COMPLETED", (final, plan)
    assert len(events_of(w.h, run, "submission.created")) == 1 and len(events_of(w.h, run, "judge.completed")) == 1   # scored once
    assert len(events_of(w.h, run, "feedback.delivered")) == 1
    res = [e for e in events_of(w.h, run, "tool.call.completed") if e["payload"]["tool"] == "submit"]
    assert len(res) == 1 and res[0]["payload"]["message"]["parts"][0]["parts"][0]["text"] == "Score: 0.7"       # feedback still delivered as submit's result
    assert (await run.proj("sandboxes")) == {} and await run.proj("grants_active") == {}
    check_log_invariants(w.h.store, rid)
    w.h.close()
