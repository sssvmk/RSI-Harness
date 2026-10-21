"""Gate policy per kind of edit (design §17.8-§17.10, decision made by the deployment owner): sensible defaults, and an administrator may make any environment stricter or leaner.

Defaults: T0 passes the statistical gate at alpha 0.05 and is promoted immediately (no human review, no canary); T1 alpha 0.05 with a sampled human review;
T2 alpha 0.01 with a blocking approval by an independent approver and a canary. T3/T4 (optimizer code and above) are never promoted by the loop.

An override can move a value in either direction, but it is validated (so a typo such as alpha 0 is refused), every deviation from the defaults is listed with its direction,
and the resolved policy is fingerprinted and recorded with the campaign. Two things cannot be relaxed: T2 always needs a blocking approval, and tiers above T2 are not promotable."""
from __future__ import annotations
import copy
from ..canonical import canonicalize, sha256_hex

TIERS = ("T0", "T1", "T2")
REVIEW_MODES = ("none", "sampled", "blocking")
_COMMON = {"B": 10000, "holm": True, "margins": {"default": 0.02}, "heldout_margin": 0.02, "preserved_margin": 0.02}
DEFAULTS = {"tiers": {
    "T0": {"alpha": 0.05, **copy.deepcopy(_COMMON), "review": {"mode": "none", "sample_rate": 0.0, "window_h": 72}, "canary_fraction": 0.0},
    "T1": {"alpha": 0.05, **copy.deepcopy(_COMMON), "review": {"mode": "sampled", "sample_rate": 0.2, "window_h": 72}, "canary_fraction": 0.0},
    "T2": {"alpha": 0.01, **copy.deepcopy(_COMMON), "review": {"mode": "blocking", "sample_rate": 1.0, "window_h": 72}, "canary_fraction": 0.1}}}
GATE_KEYS = ("alpha", "B", "holm", "margins", "heldout_margin", "preserved_margin")
_TIER_KEYS = set(GATE_KEYS) | {"review", "canary_fraction"}
_REVIEW_KEYS = {"mode", "sample_rate", "window_h"}


class PolicyError(ValueError):
    def __init__(self, code: str, msg: str):
        super().__init__(f"{code}: {msg}")
        self.code = code


def _merge(base: dict, over: dict, where: str) -> dict:
    out = copy.deepcopy(base)
    for k, v in over.items():
        if isinstance(v, dict) and isinstance(out.get(k), dict) and k != "margins" or (k == "margins" and isinstance(v, dict)):
            out[k] = {**out.get(k, {}), **v} if k == "margins" else _merge(out[k], v, f"{where}.{k}")
        else:
            out[k] = v
    return out


def _num(x) -> bool:
    return isinstance(x, (int, float)) and not isinstance(x, bool)


def _validate(tier: str, t: dict) -> None:
    if set(t) - _TIER_KEYS: raise PolicyError("policy.unknown_key", f"{tier}: unknown setting(s) {sorted(set(t) - _TIER_KEYS)}")
    if not _num(t["alpha"]) or not (1e-6 <= t["alpha"] <= 0.2): raise PolicyError("policy.invalid_value", f"{tier}.alpha must be between 0.000001 and 0.2 (got {t['alpha']!r}); 0 would make the gate unpassable")
    if not isinstance(t["B"], int) or isinstance(t["B"], bool) or not (200 <= t["B"] <= 100000): raise PolicyError("policy.invalid_value", f"{tier}.B must be an integer between 200 and 100000")
    if not isinstance(t["holm"], bool): raise PolicyError("policy.invalid_value", f"{tier}.holm must be true or false")
    for k in ("heldout_margin", "preserved_margin"):
        if not _num(t[k]) or not (0 <= t[k] <= 0.5): raise PolicyError("policy.invalid_value", f"{tier}.{k} must be between 0 and 0.5")
    m = t["margins"]
    if not isinstance(m, dict) or "default" not in m or any(not _num(v) or not (0 <= v <= 0.5) for v in m.values()): raise PolicyError("policy.invalid_value", f"{tier}.margins needs a default and values between 0 and 0.5")
    r = t["review"]
    if set(r) - _REVIEW_KEYS: raise PolicyError("policy.unknown_key", f"{tier}.review: unknown setting(s) {sorted(set(r) - _REVIEW_KEYS)}")
    if r["mode"] not in REVIEW_MODES: raise PolicyError("policy.invalid_value", f"{tier}.review.mode must be one of {REVIEW_MODES}")
    if not _num(r["sample_rate"]) or not (0 <= r["sample_rate"] <= 1): raise PolicyError("policy.invalid_value", f"{tier}.review.sample_rate must be between 0 and 1")
    if not _num(r["window_h"]) or r["window_h"] <= 0: raise PolicyError("policy.invalid_value", f"{tier}.review.window_h must be positive")
    if not _num(t["canary_fraction"]) or not (0 <= t["canary_fraction"] <= 1): raise PolicyError("policy.invalid_value", f"{tier}.canary_fraction must be between 0 and 1")
    if tier == "T2" and r["mode"] != "blocking": raise PolicyError("policy.locked", "T2 always requires a blocking approval by an independent approver; it cannot be relaxed")


def _direction(path: str, default, value) -> str:
    lower_stricter = path in ("alpha", "heldout_margin", "preserved_margin") or path.startswith("margins.")
    if lower_stricter: return "stricter" if value < default else "leaner"
    if path in ("B", "review.sample_rate"): return "stricter" if value > default else "leaner"
    if path == "holm": return "stricter" if value else "leaner"
    if path == "review.mode": return "stricter" if REVIEW_MODES.index(value) > REVIEW_MODES.index(default) else "leaner"
    return "changed"


def _flatten(d: dict, prefix: str = "") -> dict:
    out = {}
    for k, v in d.items():
        p = f"{prefix}{k}"
        out.update(_flatten(v, p + ".") if isinstance(v, dict) else {p: v})
    return out


class GatePolicy:
    def __init__(self, tiers: dict, environment: str | None = None, validated: bool = True):
        self.tiers, self.environment = tiers, environment
        if validated:
            for t in TIERS: _validate(t, tiers[t])

    @classmethod
    def resolve(cls, config: dict | None = None, environment: str | None = None) -> "GatePolicy":
        """Defaults, then the base `tiers` overrides, then the named environment's overrides. Anything unknown or out of range is refused."""
        cfg = config or {}
        if set(cfg) - {"tiers", "environments", "environment"}: raise PolicyError("policy.unknown_key", f"unknown top-level setting(s) {sorted(set(cfg) - {'tiers', 'environments', 'environment'})}")
        env = environment if environment is not None else cfg.get("environment")
        tiers = copy.deepcopy(DEFAULTS["tiers"])
        layers = [("tiers", cfg.get("tiers") or {})]
        if env is not None:
            envs = cfg.get("environments") or {}
            if env not in envs: raise PolicyError("policy.unknown_environment", f"environment {env!r} is not defined (defined: {sorted(envs)})")
            if set(envs[env]) - {"tiers"}: raise PolicyError("policy.unknown_key", f"environment {env!r}: unknown setting(s) {sorted(set(envs[env]) - {'tiers'})}")
            layers.append((f"environments.{env}", envs[env].get("tiers") or {}))
        for where, layer in layers:
            for tier, over in layer.items():
                if tier not in TIERS: raise PolicyError("policy.unknown_tier", f"{where}: only {TIERS} can be configured (T3 and above are never promoted by the loop), got {tier!r}")
                tiers[tier] = _merge(tiers[tier], over, f"{where}.{tier}")
        return cls(tiers, env)

    @classmethod
    def uniform(cls, params: dict | None = None) -> "GatePolicy":
        """Backward compatible: one gate parameter set for every tier (the form the campaign controller took before policies existed)."""
        tiers = copy.deepcopy(DEFAULTS["tiers"])
        for t in TIERS:
            for k, v in (params or {}).items():
                if k in GATE_KEYS: tiers[t][k] = copy.deepcopy(v) if k != "margins" else {**tiers[t]["margins"], **v}
        return cls(tiers, None, validated=False)

    # ---- use
    def gate_params(self, tier: str) -> dict:
        t = self.tiers[tier]
        return {k: copy.deepcopy(t[k]) for k in GATE_KEYS}

    def review(self, tier: str) -> dict: return dict(self.tiers[tier]["review"])
    def canary_fraction(self, tier: str) -> float: return self.tiers[tier]["canary_fraction"]

    # ---- audit
    @property
    def resolved(self) -> dict: return {"tiers": copy.deepcopy(self.tiers)}

    @property
    def fingerprint(self) -> str: return sha256_hex(canonicalize(self.resolved))

    def diff(self) -> list[dict]:
        out = []
        for tier in TIERS:
            d, v = _flatten(DEFAULTS["tiers"][tier]), _flatten(self.tiers[tier])
            for path in sorted(set(d) | set(v)):
                if d.get(path) != v.get(path):
                    out.append({"tier": tier, "path": path, "default": d.get(path), "value": v.get(path), "direction": _direction(path, d[path], v[path]) if path in d and path in v else "changed"})
        return out

    def relaxed(self) -> list[dict]: return [x for x in self.diff() if x["direction"] == "leaner"]
