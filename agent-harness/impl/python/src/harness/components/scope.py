"""Coeffect isolation and interception (paper §3.2.3, Definitions 24-27), realised at the value-access layer.
Both give *derived* contexts: nothing in the shared table changes, so no effect is tracked and no component is reloaded or re-wired."""
from __future__ import annotations
from typing import Any, Callable
from .core import PreconditionFailed, World


def merge_meta(a: dict, b: dict) -> dict:
    """The per-key monoid (+): scalar fields are overwritten (right-biased), list-valued fields are unioned in order. {} is the identity."""
    out = dict(a)
    for k, v in b.items():
        out[k] = ([*a[k], *[x for x in v if x not in a[k]]] if isinstance(v, list) and isinstance(a.get(k), list) else v)
    return out


class Scope:
    def __init__(self, world: World, realms: dict[str, str] | None = None, meta: dict[str, dict] | None = None):
        self.world, self.realms, self.meta = world, dict(realms or {}), {k: dict(v) for k, v in (meta or {}).items()}

    def isolate(self, key: str, realm: str) -> "Scope":
        """rho[k -> r]: in the derived scope `key` resolves to the binding stored under realm `realm`; a key outside rho resolves to its own realm."""
        return Scope(self.world, {**self.realms, key: realm}, self.meta)

    def intercept(self, key: str, meta: dict) -> "Scope":
        """iota[k -> iota(k) (+) nu]: install cross-cutting behaviour (policy checks, logging, rate limits) on a key."""
        return Scope(self.world, self.realms, {**self.meta, key: merge_meta(self.meta.get(key, {}), meta)})

    def get(self, key: str, declared_meta: dict | None = None) -> Any:
        realm = self.realms.get(key, key)
        sig = self.world.sigma()
        if realm not in sig: raise PreconditionFailed(f"{key!r} (realm {realm!r}) is not bound")
        m = merge_meta(declared_meta or {}, self.meta.get(key, {}))        # the context-carried metadata has priority
        for hook in m.get("on_access", []): hook(key, realm)               # may raise to deny
        value = sig[realm][1]
        return value(m) if callable(value) else value
