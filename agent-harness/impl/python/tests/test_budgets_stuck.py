import pytest
from conftest import oa, events_of
from harness.budgets import normalize_budgets, cost_of, REQUIRED
from harness.stuck import call_signature, turn_signature, stuck_action


def test_normalize_budgets_converts_time_and_sets_warn_threshold():
    n = normalize_budgets({"turns": 10, "time_s": 5, "bogus": 1})
    assert n == {"turns": {"limit": 10, "warn_at": 8.0}, "time_ms": {"limit": 5000, "warn_at": 4000.0}}


def test_cost_uses_price_per_million():
    assert cost_of({"input_tokens": 1_000_000, "output_tokens": 500_000}, {"in": 3.0, "out": 15.0}) == 10.5
    assert cost_of({"input_tokens": 5}, None) == 0.0


def test_profiles_require_more_dimensions():
    assert set(REQUIRED["core"]) < set(REQUIRED["standard"]) < set(REQUIRED["high"])


def test_stuck_signature_is_stable_and_order_sensitive():
    a = call_signature("fs.read", {"b": 1, "a": 2}, "d")
    assert a == call_signature("fs.read", {"a": 2, "b": 1}, "d")       # canonical args
    assert a != call_signature("fs.read", {"a": 2, "b": 1}, "other")   # result digest matters
    assert turn_signature([]) is None
    assert turn_signature(["x", "y"]) != turn_signature(["y", "x"])


def test_stuck_action_thresholds():
    st = lambda n, w: {"repeat_count": n, "stuck_warned": w}
    assert stuck_action(st(3, False), 4, 2) is None
    assert stuck_action(st(4, False), 4, 2) == "guidance"
    assert stuck_action(st(5, True), 4, 2) is None
    assert stuck_action(st(6, True), 4, 2) == "terminate"


async def test_turn_budget_is_a_hard_stop_with_warning_first(mk):
    script = [oa(calls=[("fs.glob", {"pattern": f"/workspace/{i}*"})]) for i in range(10)]
    h = mk(script, budgets={"turns": 5})
    run = await h.run_to_end("u", "go")
    assert await run.state() == "TERMINATED"
    term = events_of(h, run, "run.terminated")[0]["payload"]
    assert term["reason"] == "budget_exhausted:turns"
    types = [e["type"] for e in h.store.events(run.run_id)]
    assert types.index("budget.warning") < types.index("budget.exhausted")
    assert types.count("model.requested") == 5                      # never a 6th call
    assert events_of(h, run, "failure.classified")[0]["payload"]["code"] == "arch.budget_exhausted"
    assert any(e["payload"]["kind"] == "budget" for e in events_of(h, run, "guidance.injected"))


async def test_token_budget_uses_provider_reported_usage(mk):
    h = mk([oa(calls=[("fs.glob", {"pattern": "/workspace/*"})], usage=(15000, 6000))], budgets={"tokens": 20000})
    run = await h.run_to_end("u", "go")
    assert events_of(h, run, "run.terminated")[0]["payload"]["reason"] == "budget_exhausted:tokens"
    b = (await run.proj("budgets"))["agent"]["tokens"]
    assert b["used"] == 21000 and b["exhausted"]  # provider-reported usage replaces the estimate


async def test_tool_call_budget(mk):
    h = mk([oa(calls=[("fs.glob", {"pattern": "/workspace/*"})] * 4)], budgets={"tools": 2})
    run = await h.run_to_end("u", "go")
    assert events_of(h, run, "run.terminated")[0]["payload"]["reason"] == "budget_exhausted:tools"
    assert len(events_of(h, run, "tool.call.started")) == 2


async def test_cost_budget_counts_price(mk):
    # 900k input tokens at $1/M = $0.90 > $0.50 limit, while tokens stay under their own limit
    h = mk([oa(calls=[("fs.glob", {"pattern": "/workspace/*"})], usage=(900_000, 0))], budgets={"cost": 0.5})
    run = await h.run_to_end("u", "go")
    assert events_of(h, run, "run.terminated")[0]["payload"]["reason"] == "budget_exhausted:cost"
