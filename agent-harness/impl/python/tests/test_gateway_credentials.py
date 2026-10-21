import asyncio, random
import pytest
from harness.clock import FakeClock
from harness.credentials import DictSecretProvider, Redactor, TokenSource, CredentialUnavailable, Secret
from harness.gateway.gateway import ModelGateway, GatewayError, ScriptedTransport, Middleware, Block
from conftest import oa

MODELS = {"main": {"ref": "main", "provider": "openai", "model_id": "m", "window": 1000, "secret": "k"},
          "claude": {"ref": "claude", "provider": "anthropic", "model_id": "c", "window": 1000, "secret": "k"}}
REQ = {"model_ref": "main", "messages": [{"role": "user", "parts": [{"type": "text", "text": "hi"}]}], "params": {"max_output_tokens": 100}, "metadata": {"run_id": "R", "turn": 1}}


def gw(script, **kw):
    t = ScriptedTransport(script)
    clock = kw.pop("clock", FakeClock())
    return ModelGateway(MODELS, t, clock, DictSecretProvider({"k": "sk-abc"}), rng=random.Random(1), **kw), t, clock


async def test_success_and_auth_headers():
    g, t, _ = gw([oa("hello")])
    r = await g.call(REQ)
    assert r["message"]["parts"][0]["text"] == "hello" and r["usage"]["input_tokens"] == 10
    assert t.requests[0]["headers"]["authorization"] == "Bearer sk-abc"
    r2 = await gw([(200, {"content": [{"type": "text", "text": "x"}], "stop_reason": "end_turn", "usage": {"input_tokens": 1, "output_tokens": 1}})])[0].call({**REQ, "model_ref": "claude"})
    assert r2["finish_reason"] == "stop"


async def test_transient_errors_retry_with_full_jitter_backoff_then_succeed():
    g, t, clock = gw([(429, {}), (503, {}), ConnectionResetError("rst"), oa("ok")])
    fails = []
    async def on_fail(i): fails.append(i)
    r = await g.call(REQ, on_failure=on_fail)
    assert r["attempt"] == 4 and len(t.requests) == 4
    assert [f["attempt"] for f in fails] == [1, 2, 3]
    caps = [1000, 2000, 4000]   # base 1 s doubling; full jitter draws uniformly from [0, cap]
    assert all(0 <= s <= c for s, c in zip(clock.slept, caps)) and len(clock.slept) == 3


async def test_backoff_cap_is_30s():
    assert max(ModelGateway.backoff_ms(20, random.Random(i)) for i in range(50)) <= 30000


async def test_gives_up_after_max_attempts_and_reports_last_error():
    g, t, _ = gw([(500, {})] * 4)
    with pytest.raises(GatewayError) as e:
        await g.call(REQ)
    assert e.value.code == "gateway.http_500" and len(t.requests) == 4


@pytest.mark.parametrize("status", [400, 401, 403, 404])
async def test_non_transient_errors_do_not_retry(status):
    g, t, _ = gw([(status, {"error": "bad"})])
    with pytest.raises(GatewayError) as e:
        await g.call(REQ)
    assert not e.value.retryable and len(t.requests) == 1


async def test_request_validation_requires_max_output_tokens():
    g, _, _ = gw([oa("x")])
    with pytest.raises(ValueError):
        await g.call({**REQ, "params": {}})


class Screen(Middleware):
    name, optional = "screen", False
    async def pre_request(self, req):
        if "attack" in str(req): raise Block("injection")
        return req


class Flaky(Middleware):
    name, optional = "flaky", True
    async def pre_request(self, req): raise RuntimeError("boom")


class Broken(Middleware):
    name, optional = "broken-safety", False
    async def pre_request(self, req): raise RuntimeError("boom")


async def test_safety_hook_blocks_and_fails_closed_but_optional_hook_is_skipped():
    g, t, _ = gw([oa("x")], middleware=[Screen()])
    bad = {**REQ, "messages": [{"role": "user", "parts": [{"type": "text", "text": "attack"}]}]}
    with pytest.raises(GatewayError) as e: await g.call(bad)
    assert e.value.code == "gateway.blocked" and not t.requests
    g2, _, _ = gw([oa("x")], middleware=[Flaky()])
    r = await g2.call(REQ)
    assert any(h["hook"] == "flaky" and not h["ok"] for h in r["provider_meta"]["hooks"])
    g3, t3, _ = gw([oa("x")], middleware=[Broken()])
    with pytest.raises(GatewayError) as e: await g3.call(REQ)
    assert e.value.code == "gateway.hook_failed" and not t3.requests


async def test_missing_secret_is_structured_error():
    t = ScriptedTransport([oa("x")])
    g = ModelGateway(MODELS, t, FakeClock(), DictSecretProvider({}))
    with pytest.raises(GatewayError) as e: await g.call(REQ)
    assert e.value.code == "gateway.credential_unavailable"


def test_secret_never_reprs():
    assert "sk-abc" not in repr(Secret("sk-abc")) and "sk-abc" not in str(Secret("sk-abc"))


def test_redactor_known_values_and_shapes():
    r = Redactor(["hunter2-secret-value"])
    out = r.redact("pw=hunter2-secret-value key=sk-ABCDEFGHIJKLMNOP12345 Authorization: Bearer abcdefghijklmnopqrstu aws AKIAABCDEFGHIJKLMNOP")
    assert "hunter2" not in out and "sk-ABC" not in out and "abcdefghijklmnopqrstu" not in out and "AKIA" not in out
    assert r.redact_obj({"a": ["x sk-ABCDEFGHIJKLMNOP12345"]}) == {"a": ["x [REDACTED]"]}


def test_token_source_refreshes_at_half_life_and_keeps_leased_tokens():
    clock, n = FakeClock(), [0]
    def fetch():
        n[0] += 1
        return f"tok{n[0]}", 100
    ts = TokenSource(fetch, clock)
    l1 = ts.lease(); assert l1[0] == "tok1"
    clock.advance(49_000); l2 = ts.lease(); assert l2[0] == "tok1"           # before 50%: reuse
    clock.advance(2_000); l3 = ts.lease(); assert l3[0] == "tok2"            # after 50%: proactive refresh
    assert ts.revocable() == []                                              # tok1 still has live leases
    ts.release(l1)
    assert ts.revocable() == []                                              # l2 still holds tok1
    ts.release(l2)
    assert ts.revocable() == ["tok1"] and ts.revocable() == []               # revocable exactly once, only when idle


def test_token_refresh_failure_fails_fast_after_expiry():
    clock = FakeClock(); calls = [0]
    def fetch():
        calls[0] += 1
        if calls[0] > 1: raise RuntimeError("idp down")
        return "tok", 100
    ts = TokenSource(fetch, clock); ts.lease()
    clock.advance(60_000); assert ts.lease()[0] == "tok"                      # refresh failed but token still valid
    clock.advance(50_000)
    with pytest.raises(CredentialUnavailable): ts.lease()                     # expired and refresh keeps failing: fail fast
