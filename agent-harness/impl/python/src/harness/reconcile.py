"""Lifecycle reconciliation and the pre-action gate (design §5.13). The question 'does the world match what the harness believes?' matters throughout a run, so it is a runtime service in the tool-call pipeline
and not a hook the agent must remember to call. Authorization and consistency are separate checks, and both must pass. A timeout is read from authoritative state before any retry."""
from __future__ import annotations
import asyncio, hashlib
from typing import Any, Awaitable, Callable

STATES = ("consistent", "drift_detected", "reconciling", "blocked", "unknown", "recovery_required")
UNVERIFIED = ("drift_detected", "reconciling", "unknown", "recovery_required")        # effects that are not established: a run is not complete while any remain
EVENTS = ("verified", "mismatch", "start_reconcile", "denied", "unreadable", "repair_failed")
RISKS = ("low", "medium", "high", "critical")
DEFAULT_RISK = {"read_only": "low", "reversible_write": "medium", "irreversible": "high", "external_facing": "high"}
# (state, event) -> state. A missing pair is an illegal transition. `None` is a subject never seen before.
TABLE = {
    (None, "verified"): "consistent", (None, "mismatch"): "drift_detected", (None, "unreadable"): "unknown", (None, "denied"): "blocked",
    ("consistent", "verified"): "consistent", ("consistent", "mismatch"): "drift_detected", ("consistent", "unreadable"): "unknown", ("consistent", "denied"): "blocked",
    ("drift_detected", "start_reconcile"): "reconciling", ("drift_detected", "denied"): "blocked", ("drift_detected", "unreadable"): "unknown", ("drift_detected", "mismatch"): "drift_detected",
    ("reconciling", "verified"): "consistent", ("reconciling", "mismatch"): "drift_detected", ("reconciling", "unreadable"): "unknown", ("reconciling", "repair_failed"): "recovery_required",
    ("blocked", "verified"): "consistent", ("blocked", "mismatch"): "drift_detected", ("blocked", "unreadable"): "unknown", ("blocked", "denied"): "blocked",
    ("unknown", "verified"): "consistent", ("unknown", "mismatch"): "drift_detected", ("unknown", "unreadable"): "unknown", ("unknown", "start_reconcile"): "reconciling", ("unknown", "denied"): "blocked",
    ("recovery_required", "start_reconcile"): "reconciling", ("recovery_required", "verified"): "consistent", ("recovery_required", "mismatch"): "recovery_required", ("recovery_required", "unreadable"): "recovery_required",
}


class ReconcileError(ValueError):
    pass


def next_state(state: str | None, event: str, authoritative: bool = False) -> str | None:
    """The next state. An `unknown` (or `recovery_required`, or `reconciling`) subject becomes `consistent` only on `verified` evidence that came from an authoritative read; without it the state stays where it is."""
    if (state, event) not in TABLE: raise ReconcileError(f"illegal transition: {state} on {event}")
    nxt = TABLE[(state, event)]
    if event == "verified" and state in ("unknown", "recovery_required", "reconciling", "blocked") and not authoritative: return state
    return nxt


def apply_event(state: str | None, event: str, authoritative: bool = False) -> str | None:
    """Apply an event to a subject's state the way the log does. A verified authoritative read of a subject in `drift_detected` passes through `reconciling` on its way to `consistent`; an event that does not
    apply to the state (a denied action on a subject that is merely drifting) changes nothing."""
    if (state, event) in TABLE: return next_state(state, event, authoritative)
    if event == "verified" and (state, "start_reconcile") in TABLE: return next_state(next_state(state, "start_reconcile"), "verified", authoritative)
    return state


def requirements(risk: str) -> dict:
    """What an action of this risk needs. The risk follows the potential impact of the action, never the agent or tool that invoked it."""
    if risk not in RISKS: raise ReconcileError(f"unknown risk {risk!r}")
    i = RISKS.index(risk)
    return {"world_read": i >= 1, "preconditions": i >= 1, "version_check": i >= 1, "readback": i >= 1, "fresh_state": i >= 2, "current_approval": i >= 2, "idempotency_key": i >= 2, "fail_closed": i >= 2, "human": i >= 3}


def risk_of(effect_class: str | None, declared: str | None = None, critical: bool = False) -> str:
    return "critical" if critical else (declared or DEFAULT_RISK.get(effect_class or "read_only", "medium"))


class VersionConflict(Exception):
    """The resource changed between the check and the action."""


class WorldAdapter:
    """What a gate needs from a kind of resource: authoritative state, a version-conditional execute, and a check that the expected effects hold. read_state returns {version, value}, or None when it cannot be read."""
    async def read_state(self, resource: str, ctx: Any = None) -> dict | None: raise NotImplementedError
    async def check_preconditions(self, action: dict, actual: dict) -> bool: return True
    async def effects_hold(self, action: dict, before: dict, after: dict, result: Any) -> bool: return True


def approval_current(approval: dict | None, now_s: float, max_age_s: float = 900) -> bool:
    """An approval is current if it was granted, has not been revoked, and is not older than the freshness window [proposed P81]."""
    return bool(approval) and approval.get("status") == "granted" and not approval.get("revoked") and (now_s - approval.get("decided_at_s", now_s)) <= max_age_s


class Gate:
    """All consequential actions go through one gateway in ToolRuntime, which the agent cannot skip."""
    def __init__(self, adapters: dict[str, WorldAdapter] | None = None, approval_max_age_s: float = 900):
        self.adapters, self.approval_max_age_s = adapters or {}, approval_max_age_s
        self._locks: dict[tuple, asyncio.Lock] = {}

    def lock(self, adapter: str, resource: str) -> asyncio.Lock:
        return self._locks.setdefault((adapter, resource), asyncio.Lock())

    async def pre(self, emit: Callable[..., Awaitable], action: dict, ctx: Any = None, now_s: float = 0.0) -> dict:
        """Everything before the action. Returns {status: ok|blocked, reason, actual}. Each step that cannot be verified blocks a medium-or-higher action: it does not run."""
        req, aid = requirements(action["risk"]), action["id"]
        async def block(reason: str, ev: str = "denied"):
            await emit("action.blocked", {"action_id": aid, "reason": reason, "risk": action["risk"]}); await emit("reconcile.checked", {"action_id": aid, "risk": action["risk"], "result": "blocked", "reason": reason})
            await self._state(emit, action, ev, False); return {"status": "blocked", "reason": reason, "actual": None}
        if not action.get("authorized", True): return await block("authorization_denied")
        if not req["world_read"]:
            await emit("reconcile.checked", {"action_id": aid, "risk": action["risk"], "result": "internal_only"}); return {"status": "ok", "reason": None, "actual": None}
        ad = self.adapters.get(action.get("adapter"))
        if ad is None: return await block("world_state_unavailable", "unreadable")
        try: actual = await ad.read_state(action["resource"], ctx)
        except Exception: actual = None
        if actual is None: return await block("world_state_unavailable", "unreadable")
        if req["preconditions"] and not await ad.check_preconditions(action, actual): return await block("precondition_failed", "mismatch")
        if req["version_check"] and action.get("expected_version") is not None and action["expected_version"] != actual["version"]: return await block("stale_state", "mismatch")
        if req["current_approval"] and action.get("approval_required") and not approval_current(action.get("approval"), now_s, self.approval_max_age_s): return await block("approval_stale_or_revoked")
        if req["human"] and not action.get("human_approved"): return await block("human_intervention_required")
        await emit("reconcile.checked", {"action_id": aid, "risk": action["risk"], "result": "ok_to_execute"}); return {"status": "ok", "reason": None, "actual": actual}

    async def guarded(self, action: dict, actual: dict | None, do: Callable[[], Awaitable[Any]], ctx: Any = None) -> Any:
        """Run `do` with concurrency protection: under the resource's lock, the state is read again and must still be the state that was checked. A pre-action read alone cannot guarantee that state is unchanged."""
        if actual is None: return await do()
        ad = self.adapters[action["adapter"]]
        async with self.lock(action["adapter"], action["resource"]):
            cur = await ad.read_state(action["resource"], ctx)
            if cur is None or cur["version"] != actual["version"]: raise VersionConflict(action["resource"])
            return await do()

    async def post(self, emit: Callable[..., Awaitable], action: dict, actual: dict | None, result: Any, ctx: Any = None) -> dict:
        """Read the state back and compare it with the expected effect; the tool's acknowledgment alone is not trusted. Returns {status: verified_success|pending_reconciliation|reconciliation_required|not_checked}."""
        req, aid = requirements(action["risk"]), action["id"]
        if not req["readback"]: return {"status": "not_checked", "observed": None}
        ad = self.adapters.get(action.get("adapter"))
        try: observed = await ad.read_state(action["resource"], ctx) if ad else None
        except Exception: observed = None
        if observed is None:
            await emit("reconcile.checked", {"action_id": aid, "risk": action["risk"], "result": "outcome_unverified"}); await self._state(emit, action, "unreadable", False); return {"status": "pending_reconciliation", "reason": "outcome_unverified", "observed": None}
        if not await ad.effects_hold(action, actual or {}, observed, result):
            await emit("reconcile.checked", {"action_id": aid, "risk": action["risk"], "result": "effect_mismatch"}); await self._state(emit, action, "mismatch", True); return {"status": "reconciliation_required", "reason": "effect_mismatch", "observed": observed}
        await emit("reconcile.checked", {"action_id": aid, "risk": action["risk"], "result": "verified"}); await self._state(emit, action, "verified", True); return {"status": "verified_success", "observed": observed}

    async def establish_outcome(self, emit: Callable[..., Awaitable], action: dict, before: dict | None, ctx: Any = None) -> dict:
        """After a timeout or interruption: did the operation complete? Read authoritative state, never assume. Returns {outcome: applied|not_applied|unknown}. `unknown` means do not retry."""
        ad = self.adapters.get(action.get("adapter"))
        try: now = await ad.read_state(action["resource"], ctx) if ad else None
        except Exception: now = None
        if now is None:
            await self._state(emit, action, "unreadable", False); return {"outcome": "unknown", "observed": None}
        applied = await ad.effects_hold(action, before or {}, now, None)
        await emit("reconcile.checked", {"action_id": action["id"], "risk": action["risk"], "result": "applied_before_retry" if applied else "not_applied", "lifecycle": "retry"})
        await self._state(emit, action, "verified" if applied else "mismatch", True)
        return {"outcome": "applied" if applied else "not_applied", "observed": now}

    async def _state(self, emit, action: dict, event: str, authoritative: bool) -> None:
        subject = f"{action.get('adapter')}:{action.get('resource')}"
        await emit("reconcile.state_changed", {"subject": subject, "action_id": action["id"], "event": event, "authoritative": authoritative})


class WorkspaceFileAdapter(WorldAdapter):
    """Files in the agent's workspace. The version of a file is the hash of its content, or `absent`."""
    async def read_state(self, resource: str, ctx: Any = None) -> dict | None:
        ex = ctx.sandbox.executor
        try: content = ex.fs_read(resource)["content"]
        except Exception as e:
            if getattr(e, "code", "") == "fs.path_not_found" or isinstance(e, FileNotFoundError): return {"version": "absent", "value": None}      # a file that does not exist is a readable state
            return None                                                                                                                           # anything else means the state cannot be read
        return {"version": hashlib.sha256(content.encode("utf-8")).hexdigest(), "value": content}

    async def check_preconditions(self, action: dict, actual: dict) -> bool:
        return all(c != "exists" or actual["version"] != "absent" for c in action.get("preconditions", [])) and all(c != "absent" or actual["version"] == "absent" for c in action.get("preconditions", []))

    async def effects_hold(self, action: dict, before: dict, after: dict, result: Any) -> bool:
        want = (action.get("args") or {}).get("content")
        return all(e != "content_equals" or after["value"] == want for e in action.get("expected_effects", []))
