import json, sys
from pathlib import Path
import pytest

ROOT = Path(__file__).resolve().parents[3]
VEC = ROOT / "spec" / "vectors"

from harness.app import Harness
from harness.clock import FakeClock
from harness.config import load_config
from harness.credentials import DictSecretProvider
from harness.gateway.gateway import ScriptedTransport


def oa(content=None, calls=None, fr=None, usage=(10, 5)):
    """Build an OpenAI-shaped wire response. calls: [(tool_name, args_dict)]"""
    m = {"role": "assistant", "content": content}
    if calls:
        m["tool_calls"] = [{"id": f"call_{i}", "type": "function", "function": {"name": n.replace(".", "-"), "arguments": json.dumps(a)}} for i, (n, a) in enumerate(calls)]
    fr = fr or ("tool_calls" if calls else "stop")
    return (200, {"choices": [{"message": m, "finish_reason": fr}], "usage": {"prompt_tokens": usage[0], "completion_tokens": usage[1]}})


class TierDouble:
    """Test double: a LocalProcessBackend that *claims* a stronger tier so strict-profile logic can be exercised without a container runtime."""
    @staticmethod
    def make(tier):
        from harness.sandbox.backend import LocalProcessBackend
        return type("TierDouble", (LocalProcessBackend,), {"tier": tier})


STRICT = {"evaluator": {"independence": "deterministic"}, "retention": {"events_days": 365, "blobs_days": 90},
          "auth": {"oidc": {"issuer": "https://idp", "audience": "harness"}}, "approvals": {"channels": ["cli"], "approvers": ["boss"]}}


def make_config(tmp, profile="core", **over):
    cfg = {"profile": profile, "storage": {"event_log": {"kind": "sqlite", "path": str(tmp / "log.db")}, "blobs": {"kind": "fs", "root": str(tmp / "blobs")}},
           "models": [{"ref": "main", "provider": "openai", "model_id": "m", "window": 100000, "secret": "k", "price": {"in": 1.0, "out": 2.0}}],
           "sandbox": {"backend": "local", "allow_dev_tier": True},
           "budgets": {"turns": 30, "tokens": 1_000_000, "time_s": 3600, "cost": 100, "tools": 500}}
    if profile != "core":
        cfg.update({k: dict(v) for k, v in STRICT.items()})
    for k, v in over.items():
        cfg[k] = {**cfg.get(k, {}), **v} if isinstance(v, dict) and isinstance(cfg.get(k), dict) else v
    return load_config(cfg)


@pytest.fixture
def mk(tmp_path):
    made = []
    import itertools
    counter = itertools.count(1)

    def _mk(script=None, profile="core", clock=None, ruleset=None, config_dir=None, tier="auto", **over):
        t = ScriptedTransport(script or [])
        backend = None
        if tier == "auto" and profile != "core":
            backend = TierDouble.make({"standard": "T-Std", "high": "T-High"}[profile])(str(tmp_path / "sbx"))
        elif tier not in ("auto", "T-Dev"):
            backend = TierDouble.make(tier)(str(tmp_path / "sbx"))
        h = Harness(make_config(tmp_path, profile, **over), t, backend=backend, secrets= DictSecretProvider({"k": "sk-test-0000000000000000"}), clock=clock or FakeClock(),
                    ruleset=ruleset, config_dir=config_dir, id_factory=lambda: f"ID{next(counter)}")
        made.append(h)
        h.transport = t
        return h
    yield _mk
    for h in made:
        h.close()


def events_of(h, run, *types):
    return h.store.events(run.run_id, types=list(types) or None)
