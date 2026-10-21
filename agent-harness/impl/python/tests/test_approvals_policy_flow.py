import asyncio
import pytest
from harness.clock import FakeClock
from conftest import oa, events_of
from harness.approvals import ApprovalError

SHELL = {"schema_version": 1, "rules": [{"id": "R-shell", "action": "require_approval", "match": {"tool": ["shell.run"]},
                                         "approval": {"ttl_s": 3600, "max_count": 1, "allow_standing": True}}]}


async def start(h, instr="go"):
    run, loop = await h.create_run("owner", instr)
    await h.rm.start(run)
    return run, loop, asyncio.ensure_future(loop.run_loop())


async def wait_for(pred, t=3.0):
    for _ in range(int(t / 0.01)):
        if pred(): return
        await asyncio.sleep(0.01)
    raise AssertionError("timeout")


async def pending(h, run):
    return list((await run.proj("approvals_open")))


async def test_require_approval_pauses_run_then_grant_executes(mk):
    h = mk([oa(calls=[("shell.run", {"cmd": "echo hi"})]), oa("done")], profile="standard", ruleset=SHELL,
           approvals={"approvers": ["boss"], "channels": ["cli"]})
    run, loop, task = await start(h)
    await wait_for(lambda: h.store.run_row(run.run_id)["state"] == "AWAITING_APPROVAL")
    assert not events_of(h, run, "tool.call.started")                       # nothing executes before the decision
    aid = events_of(h, run, "approval.requested")[0]["payload"]["approval_id"]
    assert await h.approvals.decide(run, aid, "boss", ["approver"], True) == "granted"
    await wait_for(lambda: events_of(h, run, "tool.call.completed"))
    await wait_for(lambda: h.store.run_row(run.run_id)["state"] == "RUNNING" or task.done())
    task.cancel()
    types = [e["type"] for e in h.store.events(run.run_id)]
    assert types.index("policy.decided") < types.index("approval.requested") < types.index("approval.decided") < types.index("tool.call.started")


async def test_denied_approval_becomes_an_observation(mk):
    h = mk([oa(calls=[("shell.run", {"cmd": "rm -rf /"})]), oa("done")], profile="standard", ruleset=SHELL, approvals={"approvers": ["boss"]})
    run, loop, task = await start(h)
    await wait_for(lambda: events_of(h, run, "approval.requested"))
    aid = events_of(h, run, "approval.requested")[0]["payload"]["approval_id"]
    await h.approvals.decide(run, aid, "boss", ["approver"], False)
    await wait_for(lambda: events_of(h, run, "tool.call.failed"))
    assert events_of(h, run, "tool.call.failed")[0]["payload"]["error"]["code"] == "approval.denied"
    assert not events_of(h, run, "tool.call.started")
    task.cancel()


async def test_only_approvers_and_never_the_owner(mk):
    h = mk([oa(calls=[("shell.run", {"cmd": "x"})]), oa("d")], profile="standard", ruleset=SHELL, approvals={"approvers": ["boss"]})
    run, loop, task = await start(h)
    await wait_for(lambda: events_of(h, run, "approval.requested"))
    aid = events_of(h, run, "approval.requested")[0]["payload"]["approval_id"]
    with pytest.raises(ApprovalError) as e: await h.approvals.decide(run, aid, "mallory", ["viewer"], True)
    assert e.value.code == "approvals.forbidden"
    with pytest.raises(ApprovalError) as e: await h.approvals.decide(run, aid, "owner", ["approver"], True)
    assert e.value.code == "approvals.self_approval"
    assert (await pending(h, run)) == [aid]
    task.cancel()


async def test_replayed_decision_has_no_second_effect(mk):
    h = mk([oa(calls=[("shell.run", {"cmd": "x"})]), oa("d")], profile="standard", ruleset=SHELL, approvals={"approvers": ["boss"]})
    run, loop, task = await start(h)
    await wait_for(lambda: events_of(h, run, "approval.requested"))
    aid = events_of(h, run, "approval.requested")[0]["payload"]["approval_id"]
    await h.approvals.decide(run, aid, "boss", ["approver"], True)
    with pytest.raises(ApprovalError) as e: await h.approvals.decide(run, aid, "boss", ["approver"], False)
    assert e.value.code == "approvals.already_decided"
    assert len(events_of(h, run, "approval.decided")) == 1
    task.cancel()


async def test_approval_expires_and_is_recorded(mk):
    rules = {"schema_version": 1, "rules": [{"id": "R", "action": "require_approval", "match": {"tool": ["shell.run"]}, "approval": {"ttl_s": 60}}]}
    clock = FakeClock()
    h = mk([oa(calls=[("shell.run", {"cmd": "x"})]), oa("d")], profile="standard", ruleset=rules, clock=clock)
    run, loop, task = await start(h)
    await wait_for(lambda: events_of(h, run, "approval.requested"))
    assert await h.approvals.expire_due(run) == 0                        # not yet due
    clock.advance(61_000)
    assert await h.approvals.expire_due(run) == 1
    d = events_of(h, run, "approval.decided")[0]["payload"]
    assert d["status"] == "expired" and d["decided_by"] == "system:expiry"
    await wait_for(lambda: events_of(h, run, "tool.call.failed"))
    assert events_of(h, run, "tool.call.failed")[0]["payload"]["error"]["code"] == "approval.expired"
    assert not events_of(h, run, "tool.call.started")                       # an expired approval never executes the tool
    task.cancel()


async def test_standing_approval_is_scoped_counted_and_atomic(mk):
    h = mk([oa(calls=[("shell.run", {"cmd": "echo a"})]), oa(calls=[("shell.run", {"cmd": "echo b"})]), oa(calls=[("shell.run", {"cmd": "echo c"})]), oa("d")],
           profile="standard", ruleset=SHELL, approvals={"approvers": ["boss"]})
    with pytest.raises(ApprovalError): await h.approvals.create_standing("x", ["viewer"], "shell.run", None, 60, 2)
    await h.approvals.create_standing("boss", ["approver"], "shell.run", '*"echo*', 3600, 2)
    run, loop, task = await start(h)
    await wait_for(lambda: len(events_of(h, run, "tool.call.completed")) == 2)   # two standing uses...
    await wait_for(lambda: events_of(h, run, "approval.requested") and len(events_of(h, run, "approval.requested")) == 3)
    await wait_for(lambda: h.store.run_row(run.run_id)["state"] == "AWAITING_APPROVAL")   # ...the third needs a human
    used = h.store.conn().execute("SELECT used FROM standing_approvals").fetchone()["used"]
    assert used == 2
    task.cancel()


async def test_allow_standing_false_forces_per_instance_approval(mk):
    rules = {"schema_version": 1, "rules": [{"id": "R", "action": "require_approval", "match": {"tool": ["shell.run"]}, "approval": {"allow_standing": False}}]}
    h = mk([oa(calls=[("shell.run", {"cmd": "echo a"})]), oa("d")], profile="standard", ruleset=rules, approvals={"approvers": ["boss"]})
    await h.approvals.create_standing("boss", ["approver"], "shell.run", None, 3600, 5)
    run, loop, task = await start(h)
    await wait_for(lambda: events_of(h, run, "approval.requested"))
    assert not events_of(h, run, "tool.call.started")
    task.cancel()


async def test_high_profile_irreversible_never_uses_standing(mk):
    h = mk([oa(calls=[("shell.run", {"cmd": "echo a"})]), oa("d")], profile="high", ruleset={"schema_version": 1, "rules": []},
           approvals={"approvers": ["boss"]}, sandbox={"backend": "local", "allow_dev_tier": True})
    # high requires a T-High backend; this exercises the approval layer directly instead of via run creation
    from harness.run import Run
    run = Run(h.svc, "01HIGH0000000000000000000A", "high", "owner")
    sid = await h.approvals.create_standing("boss", ["approver"], "shell.run", None, 3600, 5)
    assert await h.approvals._try_standing(run, "shell.run", {"cmd": "x"}) == sid           # the standing approval exists and matches...
    h.writer.execute_sync("INSERT INTO runs(run_id,created_at,owner,profile,config_commit,state) VALUES(?,?,?,?,?,?)", (run.run_id, "t", "owner", "high", "c", "RUNNING"))
    r = await h.approvals.request(run, "C1", "shell.run", {"cmd": "x"}, "R", None, "irreversible")
    assert r["status"] == "pending"                                                         # ...but is not honored for irreversible actions in High
    r2 = await h.approvals.request(run, "C2", "shell.run", {"cmd": "x"}, "R", None, "reversible_write")   # same tool, lesser effect class
    assert r2["status"] == "granted"
    h.approvals._timers and [t.cancel() for t in h.approvals._timers.values()]


async def test_deny_rule_blocks_without_asking(mk):
    rules = {"schema_version": 1, "rules": [{"id": "R-no-etc", "action": "deny", "match": {"tool": ["fs.*"], "args": [{"path": "$.path", "op": "glob", "value": "/workspace/secrets/*"}]}}]}
    h = mk([oa(calls=[("fs.write", {"path": "/workspace/secrets/k", "content": "x"})]), oa("d")], ruleset=rules)
    run = await h.run_to_end("u", "go")
    f = events_of(h, run, "tool.call.failed")[0]["payload"]["error"]
    assert f["code"] == "policy.denied" and f["category"] == "security"
    assert not events_of(h, run, "approval.requested") and not events_of(h, run, "tool.call.started")
    assert events_of(h, run, "policy.decided")[0]["payload"]["matched_rule_ids"] == ["R-no-etc"]
