"""Lifecycle reconciliation and the pre-action gate (design §5.13): state machine, risk tiers, the gate, retries after a timeout, completion, and recovery against the world's actual state."""
import asyncio, json
import pytest
from conftest import VEC, events_of, oa
from reconcile_kit import run_scenario
from test_recovery import PASS, SCRIPT, World, check_log_invariants, crash_run
from harness import reconcile as R
from harness.tools.runtime import ToolResult

V = json.loads((VEC / "reconcile-vectors.json").read_text())
WF = lambda h: h.runtime.gate.adapters["workspace_file"]
WRITE = lambda c="A", p="/workspace/a.txt": ("fs.write", {"path": p, "content": c})


# ------------------------------------------------------------------ the pure parts, against the shared vectors
def test_every_state_event_pair_matches_the_vectors_and_unknown_never_becomes_consistent_without_evidence():
    for c in V["transitions"]:
        try: nxt = R.next_state(c["state"], c["event"], c["authoritative"])
        except R.ReconcileError: nxt = "ILLEGAL"
        assert nxt == c["next"] and R.apply_event(c["state"], c["event"], c["authoritative"]) == c["apply"], c
    assert R.next_state("unknown", "verified", False) == "unknown" and R.next_state("unknown", "verified", True) == "consistent"
    for s in ("unknown", "recovery_required", "reconciling", "blocked"): assert R.apply_event(s, "verified", False) == s                              # no evidence, no change
    assert R.apply_event("drift_detected", "verified", True) == "consistent" and R.apply_event("drift_detected", "verified", False) == "reconciling"        # through reconciling
    assert R.apply_event("consistent", "start_reconcile") == "consistent" and R.apply_event("blocked", "repair_failed") == "blocked"                    # an event that does not apply to the state changes nothing
    assert set(R.STATES) == {"consistent", "drift_detected", "reconciling", "blocked", "unknown", "recovery_required"}


def test_risk_tiers_follow_the_impact_of_the_action_and_add_requirements_as_they_rise():
    assert {r: R.requirements(r) for r in R.RISKS} == V["requirements"]
    for c in V["risk_of"]: assert R.risk_of(c["effect_class"], c["declared"], c["critical"]) == c["risk"]
    low, med, high, crit = (R.requirements(r) for r in R.RISKS)
    assert not any(low.values()) and med["readback"] and med["version_check"] and not med["current_approval"] and high["fresh_state"] and high["current_approval"] and high["idempotency_key"] and high["fail_closed"] and not high["human"] and crit["human"]
    for a, b in zip(R.RISKS, R.RISKS[1:]): assert all(R.requirements(b)[k] or not v for k, v in R.requirements(a).items())                           # each tier needs at least what the one below needs
    with pytest.raises(R.ReconcileError): R.requirements("catastrophic")


def test_approval_freshness_matches_the_vectors():
    for c in V["approval"]: assert R.approval_current(c["approval"], c["now_s"], c["max_age_s"]) == c["current"], c


async def test_the_gate_scenarios_match_the_vectors_exactly_including_the_events_emitted():
    for c in V["scenarios"]: assert json.loads(json.dumps(await run_scenario(c))) == c["expect"], c["name"]


# ------------------------------------------------------------------ through the real tool pipeline
async def run_with(mk, script, patch=None, manifest=None, verifier=PASS, **kw):
    h = mk(script, **kw)
    if manifest: manifest(h.registry.get("fs.write"))
    if patch: patch(h)
    return h, await h.run_to_end("u", "go", verifier=verifier())


async def test_a_verified_write_is_checked_before_and_read_back_after_and_leaves_the_subject_consistent(mk):
    h, run = await run_with(mk, [oa(calls=[WRITE()]), oa("d")])
    ev = [(e["type"], e["payload"].get("result")) for e in h.store.events(run.run_id) if e["type"].startswith("reconcile.")]
    assert ev == [("reconcile.checked", "ok_to_execute"), ("reconcile.checked", "verified"), ("reconcile.state_changed", None)]
    st = (await run.proj("reconcile"))["subjects"]; assert {k: v["state"] for k, v in st.items()} == {"workspace_file:/workspace/a.txt": "consistent"}
    c = events_of(h, run, "reconcile.checked")[0]["payload"]; assert c["risk"] == "medium" and c["action_id"] == "ID1"


async def test_a_tool_that_says_it_succeeded_is_not_believed_when_the_state_disagrees(mk):
    async def never(*a, **k): return False
    h, run = await run_with(mk, [oa(calls=[WRITE()]), oa(calls=[("finish", {"summary": "done"})]), oa(calls=[("finish", {"summary": "done"})]), oa(calls=[("finish", {"summary": "done"})])], patch=lambda h: setattr(WF(h), "effects_hold", never))
    f = events_of(h, run, "tool.call.failed")[0]["payload"]["error"]; assert f["code"] == "reconcile.effect_mismatch" and "does not match" in f["message"]
    assert (await run.proj("reconcile"))["subjects"]["workspace_file:/workspace/a.txt"]["state"] == "drift_detected"
    v = events_of(h, run, "verification.completed")[0]["payload"]; assert v["passed"] is False and "required effects remain unverified" in v["detail"] and "drift_detected" in v["detail"]      # not complete, though the verifier would pass
    assert await run.state() == "TERMINATED"


async def test_a_run_completes_once_the_unverified_effect_is_verified_by_a_later_action(mk):
    calls, orig = {"n": 0}, R.WorkspaceFileAdapter.effects_hold
    async def flaky(self, action, before, after, result):
        calls["n"] += 1; return calls["n"] > 1                                                                                                             # the first read-back disagrees, the second agrees
    try: h, run = await run_with(mk, [oa(calls=[WRITE()]), oa(calls=[WRITE()]), oa(calls=[("finish", {"summary": "done"})])], patch=lambda h: setattr(R.WorkspaceFileAdapter, "effects_hold", flaky))
    finally: R.WorkspaceFileAdapter.effects_hold = orig
    assert await run.state() == "COMPLETED" and (await run.proj("reconcile"))["subjects"]["workspace_file:/workspace/a.txt"]["state"] == "consistent"
    assert [e["payload"]["result"] for e in events_of(h, run, "reconcile.checked") if e["payload"]["result"] in ("effect_mismatch", "verified")] == ["effect_mismatch", "verified"]


async def test_an_unreadable_result_is_recorded_unknown_and_blocks_completion_until_verified(mk):
    st = {"n": 0}
    orig = R.WorkspaceFileAdapter.read_state
    async def flaky(self, resource, ctx=None):
        st["n"] += 1
        return None if st["n"] == 3 else await orig(self, resource, ctx)                                                                                  # pre, the guard, then the read-back fails
    try:
        h, run = await run_with(mk, [oa(calls=[WRITE()]), oa(calls=[("finish", {"summary": "done"})]), oa(calls=[WRITE()]), oa(calls=[("finish", {"summary": "done"})])], patch=lambda h: setattr(R.WorkspaceFileAdapter, "read_state", flaky))
    finally: R.WorkspaceFileAdapter.read_state = orig
    done = events_of(h, run, "tool.call.completed")[0]["payload"]["message"]["parts"][0]["parts"][0]["text"]; assert "recorded as unverified" in done
    assert "required effects remain unverified: workspace_file:/workspace/a.txt (unknown)" in events_of(h, run, "verification.completed")[0]["payload"]["detail"]
    assert await run.state() == "COMPLETED" and (await run.proj("reconcile"))["subjects"]["workspace_file:/workspace/a.txt"]["state"] == "consistent"          # the second, verified write resolves it


async def test_a_resource_that_changes_between_the_check_and_the_action_blocks_the_action(mk, tmp_path):
    st = {"n": 0}; orig = R.WorkspaceFileAdapter.read_state
    async def moved(self, resource, ctx=None):
        st["n"] += 1; r = await orig(self, resource, ctx)
        return {"version": "someone-else-wrote-it", "value": "x"} if st["n"] == 2 and r else r                                                           # the guard's re-read sees a different version
    async def seed(h): pass
    try:
        h, run = await run_with(mk, [oa(calls=[WRITE("B")]), oa("d")], patch=lambda h: setattr(R.WorkspaceFileAdapter, "read_state", moved), verifier=PASS)
    finally: R.WorkspaceFileAdapter.read_state = orig
    # the first read of a new file is "absent", which has a version, so the changed second read conflicts
    b = events_of(h, run, "action.blocked"); f = events_of(h, run, "tool.call.failed")[0]["payload"]["error"]
    assert (st["n"], f["code"]) == (2, "action.blocked") or (b and b[0]["payload"]["reason"] == "stale_state")
    assert not events_of(h, run, "tool.call.completed") and (await run.proj("reconcile"))["subjects"]["workspace_file:/workspace/a.txt"]["state"] == "drift_detected"


# ------------------------------------------------------------------ a timeout is not a failure
def slow_handler(write: bool, calls: list):
    async def handler(a, t):
        calls.append(1)
        if write: t.sandbox.executor.fs_write(a["path"], a["content"])
        await asyncio.sleep(2)
        return "wrote"
    return handler


async def test_a_timed_out_action_that_took_effect_is_verified_from_state_and_never_repeated(mk):
    calls = []
    def patch(h): h.registry.get("fs.write").handler = slow_handler(True, calls)
    h, run = await run_with(mk, [oa(calls=[WRITE()]), oa("d")], patch=patch, manifest=lambda t: t.manifest.__setitem__("timeout_s", 0.2))
    text = events_of(h, run, "tool.call.completed")[0]["payload"]["message"]["parts"][0]["parts"][0]["text"]
    assert len(calls) == 1 and "had already taken effect" in text and "was not repeated" in text and not events_of(h, run, "tool.call.failed")                  # one execution, not two
    assert "applied_before_retry" in [e["payload"]["result"] for e in events_of(h, run, "reconcile.checked")] and len(events_of(h, run, "tool.call.started")) == 1


async def test_a_timed_out_action_that_did_not_take_effect_is_retried(mk):
    calls = []
    async def handler(a, t):
        calls.append(1)
        if len(calls) == 1: await asyncio.sleep(2)                                                                                                         # the first attempt hangs without writing
        t.sandbox.executor.fs_write(a["path"], a["content"]); return "wrote"
    h, run = await run_with(mk, [oa(calls=[WRITE()]), oa("d")], patch=lambda h: setattr(h.registry.get("fs.write"), "handler", handler), manifest=lambda t: t.manifest.__setitem__("timeout_s", 0.2))
    assert len(calls) == 2 and [e["payload"]["attempt"] for e in events_of(h, run, "tool.call.started")] == [1, 2] and "not_applied" in [e["payload"]["result"] for e in events_of(h, run, "reconcile.checked")]


async def test_a_timed_out_action_whose_state_cannot_be_read_is_not_retried_and_stays_unknown(mk):
    calls, st = [], {"n": 0}; orig = R.WorkspaceFileAdapter.read_state
    async def flaky(self, resource, ctx=None):
        st["n"] += 1; return None if st["n"] >= 3 else await orig(self, resource, ctx)                                                                      # unreadable once the action has timed out
    try: h, run = await run_with(mk, [oa(calls=[WRITE()]), oa("d")], patch=lambda h: (setattr(h.registry.get("fs.write"), "handler", slow_handler(True, calls)), setattr(R.WorkspaceFileAdapter, "read_state", flaky)), manifest=lambda t: t.manifest.__setitem__("timeout_s", 0.2))
    finally: R.WorkspaceFileAdapter.read_state = orig
    f = events_of(h, run, "tool.call.failed")[0]["payload"]["error"]; assert f["code"] == "reconcile.unknown" and f["retryable"] is False and len(calls) == 1
    assert (await run.proj("reconcile"))["subjects"]["workspace_file:/workspace/a.txt"]["state"] == "unknown"


# ------------------------------------------------------------------ blocking, and tiers by impact
async def test_an_action_that_cannot_be_verified_does_not_run(mk):
    orig = R.WorkspaceFileAdapter.read_state
    async def dead(self, resource, ctx=None): return None
    try: h, run = await run_with(mk, [oa(calls=[WRITE()]), oa("d")], patch=lambda h: setattr(R.WorkspaceFileAdapter, "read_state", dead))
    finally: R.WorkspaceFileAdapter.read_state = orig
    f = events_of(h, run, "tool.call.failed")[0]["payload"]["error"]; assert f["code"] == "action.blocked" and "world_state_unavailable" in f["message"] and not events_of(h, run, "tool.call.started")
    b = events_of(h, run, "action.blocked")[0]["payload"]; assert (b["reason"], b["risk"]) == ("world_state_unavailable", "medium")


async def test_the_risk_follows_the_impact_and_a_policy_can_mark_a_tool_critical(mk):
    h = mk([oa("d")], reconcile={"critical_tools": ["fs.write"]}); run, loop = await h.create_run("u", "go")
    m = h.registry.get("fs.write").manifest; assert h.runtime.world_action(run, "c", "fs.write", {"path": "/workspace/x", "content": "y"}, m)["risk"] == "critical"
    h2 = mk([oa("d")]); r2, _ = await h2.create_run("u", "go"); assert h2.runtime.world_action(r2, "c", "fs.write", {"path": "/workspace/x", "content": "y"}, h2.registry.get("fs.write").manifest)["risk"] == "medium"      # from its effect class
    assert h2.runtime.world_action(r2, "c", "fs.read", {"path": "/x"}, h2.registry.get("fs.read").manifest) is None                                       # read-only tools carry no world block
    h3 = mk([oa(calls=[WRITE()]), oa("d")], reconcile={"critical_tools": ["fs.write"]}); r3 = await h3.run_to_end("u", "go", verifier=PASS())
    assert events_of(h3, r3, "action.blocked")[0]["payload"]["reason"] == "human_intervention_required" and events_of(h3, r3, "tool.call.failed")[0]["payload"]["error"]["category"] == "security"


# ------------------------------------------------------------------ recovery against the world's actual state
async def test_recovery_does_not_repeat_an_action_whose_effect_is_already_in_the_world(tmp_path):
    w = World(tmp_path, SCRIPT(tmp_path))
    rid = await crash_run(w, lambda e: e["type"] == "tool.call.started" and e["payload"]["tool"] == "fs.write", PASS())
    n = w.kill(rid); w.reboot(rid, n)
    (next((tmp_path / "sbx").glob("*/workspace")) / "a.txt").write_text("A")                                                                                 # the action took effect before the crash
    plan, final = await w.h.resume_run(rid, PASS()); run = w.h.rm.get(rid)
    assert final == "COMPLETED" and len([e for e in events_of(w.h, run, "tool.call.started") if e["payload"]["tool"] == "fs.write"]) == 1                          # not run a second time
    assert any("after recovery" in json.dumps(e["payload"]) for e in events_of(w.h, run, "tool.call.completed")) and any("from state" in n for n in plan.notes)
    check_log_invariants(w.h.store, rid); w.h.close()


async def test_recovery_halts_rather_than_guess_when_the_resources_state_cannot_be_read(tmp_path):
    w = World(tmp_path, SCRIPT(tmp_path))
    rid = await crash_run(w, lambda e: e["type"] == "tool.call.started" and e["payload"]["tool"] == "fs.write", PASS())
    n = w.kill(rid); w.reboot(rid, n)
    async def dead(resource, ctx=None): return None
    w.h.runtime.gate.adapters["workspace_file"].read_state = dead
    plan, final = await w.h.resume_run(rid, PASS()); run = w.h.rm.get(rid)
    assert final == "AWAITING_HUMAN" and plan.halted == "recovery_uncertain" and "not known whether" in events_of(w.h, run, "recovery.uncertain")[0]["payload"]["reason"]
    assert len([e for e in events_of(w.h, run, "tool.call.started") if e["payload"]["tool"] == "fs.write"]) == 1; w.h.close()
