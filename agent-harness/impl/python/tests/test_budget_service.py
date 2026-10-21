"""Budget service (design §7.4.4): reservations, unknown usage, hierarchy, concurrency, and unmetered safeguards."""
import asyncio, json, os
import pytest
from conftest import oa, events_of, VEC
from harness import budgets as B
from harness.budgets import BudgetController, ConcurrencyLimiter, DeadlineExceeded, QueueFull
from harness.gateway.gateway import GatewayError


async def make(mk, script=None, **budgets):
    h = mk(script or [oa("d")], budgets=budgets or {"turns": 10, "tokens": 1000})
    run, loop = await h.create_run("u", "go", budgets=budgets or {"turns": 10, "tokens": 1000})
    return h, run, BudgetController(run)


async def state(run): return await run.proj("budgets")


# ------------------------------------------------------------------ reserve, commit, release
async def test_a_reservation_holds_headroom_and_commit_replaces_the_estimate_with_actual_usage(mk):
    h, run, bc = await make(mk, turns=10, tokens=1000)
    rid, ex = await bc.reserve({"turns": 1, "tokens": 300}); assert ex is None and rid
    st = (await state(run))["agent"]; assert st["tokens"]["reserved"] == 300 and st["tokens"]["used"] == 0
    assert await bc.commit(rid, {"turns": 1, "tokens": 120}) is None
    st = (await state(run))["agent"]; assert (st["tokens"]["used"], st["tokens"]["reserved"], st["turns"]["used"]) == (120, 0, 1)        # the rest of the reservation went back
    rid2, _ = await bc.reserve({"tokens": 200}); await bc.release(rid2, "call did not run")
    assert (await state(run))["agent"]["tokens"]["reserved"] == 0 and not (await state(run))["reservations"]
    assert [e["type"] for e in h.store.events(run.run_id) if e["type"].startswith("budget.") and e["type"] != "budget.updated"] == ["budget.reserved", "budget.committed", "budget.reserved", "budget.released"]


async def test_a_reservation_that_would_exceed_the_limit_is_refused_and_names_the_dimension(mk):
    h, run, bc = await make(mk, turns=10, tokens=1000)
    r1, _ = await bc.reserve({"tokens": 700})
    rid, ex = await bc.reserve({"tokens": 400}); assert rid is None and ex == "tokens"                                                 # 700 held + 400 asked > 1000, though nothing is spent yet
    ev = [e for e in events_of(h, run, "budget.exhausted")]; assert ev and ev[0]["payload"]["dimension"] == "tokens" and ev[0]["payload"]["reserved"] == 700
    await bc.release(r1, "x"); rid, ex = await bc.reserve({"tokens": 400}); assert rid and ex is None


async def test_concurrent_callers_cannot_reserve_the_same_remainder(mk):
    h, run, bc = await make(mk, turns=100, tokens=1000)
    results = await asyncio.gather(*[BudgetController(run).reserve({"tokens": 130}) for _ in range(20)])      # 20 callers, room for 7
    ok = [r for r, ex in results if r]; assert len(ok) == 7 and sum(1 for r, ex in results if ex == "tokens") == 13
    st = (await state(run))["agent"]["tokens"]; assert st["reserved"] == 910 and st["reserved"] + st["used"] <= st["limit"]


async def test_usage_that_is_unknown_is_charged_at_the_estimate_flagged_and_replaced_when_known(mk):
    h, run, bc = await make(mk, turns=10, tokens=1000)
    rid, _ = await bc.reserve({"turns": 1, "tokens": 400})
    await bc.commit(rid, {"turns": 1, "tokens": 400}, unreconciled=True)                                                              # a timeout after the request may have been sent
    st = await state(run); assert st["agent"]["tokens"]["used"] == 400 and st["agent"]["tokens"]["unreconciled"] == 400 and rid in st["unreconciled"]
    assert events_of(h, run, "budget.unreconciled")[0]["payload"]["reservation_id"] == rid
    await bc.commit(rid, {"tokens": 90}, reconciles=True)                                                                            # the provider's usage report arrives
    assert (await state(run))["agent"]["tokens"]["unreconciled"] == 0 and not (await state(run))["unreconciled"]


async def test_the_model_call_reserves_the_worst_case_commits_actual_usage_and_charges_a_turn_even_when_the_call_fails(mk):
    h = mk([oa("d")], budgets={"turns": 10, "tokens": 100000}); run = await h.run_to_end("u", "go")
    ev = [e for e in h.store.events(run.run_id) if e["type"] in ("budget.reserved", "budget.committed")]
    assert [e["type"] for e in ev[:2]] == ["budget.reserved", "budget.committed"] and ev[0]["payload"]["deltas"]["tokens"] >= 4096                # input estimate plus max_output_tokens
    assert ev[1]["payload"]["actual"]["tokens"] < ev[0]["payload"]["deltas"]["tokens"] and (await state(run))["agent"]["tokens"]["reserved"] == 0
    from harness.gateway.gateway import ScriptedTransport
    for code, flagged in (("gateway.connection", True), ("gateway.http_400", False)):                                                  # only an uncertain failure is charged at the estimate
        h2 = mk([oa("d")], budgets={"turns": 10, "tokens": 100000})
        async def boom(*a, **k): raise GatewayError(code, "x")
        h2.gateway.call = boom; run2 = await h2.run_to_end("u", "go"); c = events_of(h2, run2, "budget.committed")[0]["payload"]
        assert c["unreconciled"] is flagged and c["actual"]["turns"] == 1 and ("tokens" in c["actual"]) is flagged
        assert (await state(run2))["agent"]["turns"]["used"] == 1


# ------------------------------------------------------------------ hierarchy
async def test_an_allocation_is_guaranteed_to_its_agent_and_never_exceeds_what_the_workflow_has_left(mk):
    h, run, bc = await make(mk, turns=100, tokens=1000)
    assert await bc.allocate("research", {"tokens": 600}) is None
    assert await bc.allocate("verify", {"tokens": 500}) == "tokens"                                                                    # only 400 is left after research's guarantee
    assert await bc.allocate("verify", {"tokens": 300}) is None
    rid, ex = await bc.reserve({"tokens": 200}); assert ex == "tokens"                                                                 # an unallocated caller cannot take what was promised (1000 - 600 - 300 = 100 left)
    rid, ex = await bc.reserve({"tokens": 100}); assert rid
    r, ex = await bc.reserve({"tokens": 650}, agent_id="research"); assert ex == "tokens"                                              # beyond its own allocation
    r, ex = await bc.reserve({"tokens": 600}, agent_id="research"); assert r and not ex
    await bc.commit(r, {"tokens": 250}); st = await state(run); assert st["allocations"]["research"]["dims"]["tokens"]["used"] == 250
    await bc.end_allocation("research"); g = (await state(run))["agent"]["tokens"]
    nr, ex = await bc.reserve({"tokens": 200}); assert nr and not ex                                                                   # the unspent 350 returned to the workflow


async def test_total_consumption_of_all_agents_never_exceeds_the_global_limit(mk):
    h, run, bc = await make(mk, turns=100, tokens=1000)
    await bc.allocate("a", {"tokens": 500}); await bc.allocate("b", {"tokens": 500})
    for agent in ("a", "b", "a", "b", "a", "b"):
        r, ex = await bc.reserve({"tokens": 200}, agent_id=agent)
        if r: await bc.commit(r, {"tokens": 200})
    st = (await state(run))["agent"]["tokens"]; assert st["used"] <= st["limit"] == 1000 and st["used"] == 800                         # 2 x 400: a third reservation per agent would pass 500


# ------------------------------------------------------------------ pure accounting vectors, shared with TypeScript
def test_budget_vectors():
    v = json.loads((VEC / "budget-vectors.json").read_text())
    for c in v["available"]: assert list(B.available(c["state"], "agent", c["dim"], c.get("agent_id"))) == c["expect"], c["name"]
    for c in v["check_reserve"]: assert B.check_reserve(c["state"], "agent", c["deltas"], c.get("agent_id")) == c["expect"], c["name"]
    for c in v["check_allocation"]: assert B.check_allocation(c["state"], "agent", c["limits"]) == c["expect"], c["name"]
    for c in v["reserve_cost"]: assert B.reserve_cost(c["in"], c["out"], c["price"], c["margin"]) == c["expect"], c["name"]


# ------------------------------------------------------------------ concurrency
async def test_the_limiter_bounds_simultaneous_work_queues_the_rest_and_pushes_back_when_full():
    lim = ConcurrencyLimiter(workers=2, queue=1); gate = asyncio.Event(); running = []
    async def work(i):
        async with lim.slot(): running.append(i); await gate.wait()
    tasks = [asyncio.ensure_future(work(i)) for i in range(3)]; await asyncio.sleep(0.05)
    assert lim.in_flight == 2 and lim.waiting == 1 and sorted(running) == [0, 1]
    with pytest.raises(QueueFull):
        async with lim.slot(): pass                                                                                                    # 2 running and 1 queued: backpressure
    gate.set(); await asyncio.gather(*tasks); assert lim.in_flight == 0 and lim.waiting == 0 and lim.peak == 2 and lim.rejected == 1


async def test_permits_are_released_on_failure_cancellation_and_deadline():
    lim = ConcurrencyLimiter(workers=1, queue=5)
    with pytest.raises(RuntimeError):
        async with lim.slot(): raise RuntimeError("tool failed")
    assert lim.in_flight == 0
    hold = asyncio.Event()
    async def blocker():
        async with lim.slot(): await hold.wait()
    t = asyncio.ensure_future(blocker()); await asyncio.sleep(0.02)
    with pytest.raises(DeadlineExceeded):
        async with lim.slot(timeout_s=0.05): pass                                                                                      # a queued task respects its deadline
    assert lim.waiting == 0
    t.cancel()
    with pytest.raises(asyncio.CancelledError): await t
    assert lim.in_flight == 0
    async with lim.slot(): pass                                                                                                        # and the pool still works
    with pytest.raises(ValueError): ConcurrencyLimiter(workers=0)


# ------------------------------------------------------------------ unmetered dimensions
def test_an_unmetered_dimension_is_declared_never_zero_and_needs_independent_safeguards():
    assert B.unmetered({"turns": 5}) == ["compute"] and B.unmetered({}, metered=("compute",)) == []
    assert B.safeguard_problems({"turns": 20, "time_s": 300}, 4, 100) == []
    probs = B.safeguard_problems({}, 0, -1); assert len(probs) == 4 and any("turn" in p for p in probs) and any("wall-clock" in p for p in probs)
    assert B.safeguard_problems({"turns": 20, "time_ms": 1}, 4, 0) == []


# ------------------------------------------------------------------ nothing is left holding budget
async def test_a_run_that_ends_holds_no_budget_and_crash_recovery_settles_orphaned_reservations(mk):
    h, run, bc = await make(mk, turns=10, tokens=100000)
    await bc.reserve({"turns": 1, "tokens": 500}); await bc.reserve({"tokens": 200})
    assert len((await state(run))["reservations"]) == 2 and (await bc.release_open("run ended")) == 2
    assert (await state(run))["agent"]["tokens"]["reserved"] == 0 and not (await state(run))["reservations"]
    # a crash that left three reservations open: two followed by a model request that was never answered, and one reserved after it
    h2 = mk([oa("d")], budgets={"turns": 10, "tokens": 100000}); run2, loop2 = await h2.create_run("u", "go", budgets={"turns": 10, "tokens": 100000}); bc2 = BudgetController(run2)
    r_a, _ = await bc2.reserve({"turns": 1, "tokens": 100})
    r_sent, _ = await bc2.reserve({"turns": 1, "tokens": 300})
    await run2.emit("model.requested", {"request_ref": "x", "model_ref": "m", "provider": "p", "est_input_tokens": 1, "turn": 1})
    r_later, _ = await bc2.reserve({"tokens": 50})
    how = dict(await __import__("harness.recovery", fromlist=["RecoveryManager"]).RecoveryManager(h2)._settle_reservations(run2, h2.store.events(run2.run_id)))
    assert how[r_a] == "charged_estimate" and how[r_sent] == "charged_estimate" and how[r_later] == "released"      # sent and unanswered may have been charged; one that never reached a request is given back
    st = await state(run2); assert st["agent"]["tokens"]["reserved"] == 0 and st["agent"]["tokens"]["unreconciled"] == 400 and not st["reservations"]


# ------------------------------------------------------------------ independent safeguards while compute is unmetered
import time
from harness.sandbox.executor import LocalExecutor
from harness.sandbox.backend import LocalProcessBackend
from taskkit import solution
from test_eval import FIN, eval_run


def executor(tmp_path, **limits):
    (tmp_path / "w").mkdir(exist_ok=True); (tmp_path / "s").mkdir(exist_ok=True)
    return LocalExecutor({"/workspace": str(tmp_path / "w"), "/scratch": str(tmp_path / "s")}, limits=limits)


async def test_a_runaway_process_is_killed_by_its_cpu_limit_before_the_wall_clock_deadline(tmp_path):
    ex = executor(tmp_path, cpu_s=1); t0 = time.time()
    r = await ex.exec_run(["python3", "-c", "while True: pass"], timeout_s=20)
    assert not r["timed_out"] and r["exit_code"] != 0 and time.time() - t0 < 10                                                        # SIGXCPU, not the 20 s timeout
    ok = await executor(tmp_path, cpu_s=5).exec_run(["python3", "-c", "print(sum(range(10**6)))"], timeout_s=20); assert ok["exit_code"] == 0 and "499999500000" in ok["stdout"]


async def test_a_process_that_exceeds_its_memory_limit_is_stopped(tmp_path):
    r = await executor(tmp_path, memory_mb=400).exec_run(["python3", "-c", "x = bytearray(1500 * 1024 * 1024); print('allocated')"], timeout_s=20)
    assert r["exit_code"] != 0 and "allocated" not in r["stdout"] and "MemoryError" in r["stderr"]
    assert (await executor(tmp_path).exec_run(["python3", "-c", "x = bytearray(100 * 1024 * 1024); print('allocated')"], timeout_s=20))["stdout"].strip() == "allocated"   # no limit set: unconstrained


def test_the_local_backend_applies_default_limits_that_the_configuration_can_change_or_switch_off():
    assert LocalProcessBackend().limits == {"cpu_s": 3600, "memory_mb": 8192}
    assert LocalProcessBackend(limits={"cpu_s": 60}).limits == {"cpu_s": 60, "memory_mb": 8192}
    assert executor(__import__("pathlib").Path("/tmp"), cpu_s=0, memory_mb=0)._apply_limits() is None                                  # 0 switches a limit off


async def test_ending_a_run_kills_what_it_left_running_so_nothing_is_orphaned(tmp_path):
    ex = executor(tmp_path); r = ex.exec_start(["sleep", "30"], timeout_s=60); await asyncio.sleep(0.2)
    assert len(ex._live) == 1; pg = next(iter(ex._live)); t0 = time.time()
    assert ex.kill_all() == 1
    out = await ex.exec_wait(r["exec_id"]); assert out["exit_code"] in (-9, 137) and time.time() - t0 < 5 and not ex._live
    with pytest.raises(ProcessLookupError): os.kill(pg, 0)
    be = LocalProcessBackend(base_dir=str(tmp_path / "b")); h = be.create({"role": "work"}); r2 = h.executor.exec_start(["sleep", "30"], timeout_s=60); await asyncio.sleep(0.2)
    be.destroy(h); out2 = await h.executor.exec_wait(r2["exec_id"]); assert out2["exit_code"] in (-9, 137)                           # destroying the sandbox is enough


async def test_tool_calls_pass_through_the_bounded_pool_and_a_full_queue_is_reported_as_a_retryable_error(mk):
    script = [oa(calls=[("fs.glob", {"pattern": "/workspace/*"})]), oa("done")]
    h = mk(script, budgets={"turns": 10, "tokens": 100000, "workers": 1, "queue": 0}); assert (h.runtime.limiter.workers, h.runtime.limiter.queue) == (1, 0)
    run = await h.run_to_end("u", "go"); assert h.runtime.limiter.peak == 1 and h.runtime.limiter.in_flight == 0
    class Full:
        def slot(self, *a, **k): raise QueueFull("1 running and 0 queued")
    h2 = mk(script, budgets={"turns": 10, "tokens": 100000}); h2.runtime.limiter = Full(); run2 = await h2.run_to_end("u", "go")
    f = events_of(h2, run2, "tool.call.failed")[0]["payload"]["error"]; assert f["code"] == "tool.queue_full" and f["retryable"] is True


# ------------------------------------------------------------------ submissions are counted before the job is launched
async def test_a_submission_is_reserved_and_recorded_before_scoring_and_a_crash_in_between_still_counts_it(mk, tmp_path):
    h, run, _ = await eval_run(mk, tmp_path, [oa(calls=[solution(0.8)[0]]), oa(calls=[solution(0.8)[1]]), FIN()])
    seq = [e["type"] for e in h.store.events(run.run_id) if e["type"] in ("budget.reserved", "submission.created", "budget.committed", "judge.started")]
    first = seq.index("submission.created"); assert "budget.reserved" in seq[:first] and seq[first + 1] == "budget.committed" and seq.index("judge.started") > first + 1
    # a crash after the submission was recorded but before it was committed
    h2 = mk([oa("d")], budgets={"turns": 10, "tokens": 100000, "submissions": 3}); run2, loop2 = await h2.create_run("u", "go", budgets={"turns": 10, "tokens": 100000, "submissions": 3}); bc = BudgetController(run2)
    r1, _ = await bc.reserve({"submissions": 1}); await run2.emit("submission.created", {"submission_id": "S1", "snapshot_id": None, "note": None})
    r2, _ = await bc.reserve({"submissions": 1})                                                                                      # reserved, but the crash came before it was recorded
    from harness.recovery import RecoveryManager
    how = dict(await RecoveryManager(h2)._settle_reservations(run2, h2.store.events(run2.run_id)))
    assert how[r1] == "submission_committed" and how[r2] == "released" and (await state(run2))["agent"]["submissions"]["used"] == 1


# ------------------------------------------------------------------ preflight and the manifest
async def test_preflight_requires_the_safeguards_that_bound_an_unmetered_dimension_and_the_run_records_what_is_unmetered(mk):
    failed = lambda h: {c["id"] for c in h.preflight()["failed"]}
    assert "PF-B01" not in failed(mk([oa("d")]))
    h0 = mk([oa("d")]); h0.config["budgets"] = {"tokens": 1000}; assert "PF-B01" in failed(h0)                                         # no turn limit and no wall-clock limit
    msg = next(c["message"] for c in h0.preflight()["checks"] if c["id"] == "PF-B01"); assert "turn" in msg and "wall-clock" in msg
    h = mk([oa("d")]); run = await h.run_to_end("u", "go"); assert events_of(h, run, "run.created")[0]["payload"]["provenance"]["unmetered"] == ["compute"]
