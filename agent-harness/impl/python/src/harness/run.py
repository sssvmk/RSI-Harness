"""Run model: services, run handle, state machine (§6), RunManager lifecycle."""
from __future__ import annotations
import asyncio, copy, re
from dataclasses import dataclass, field
from typing import Any
from .budgets import BudgetController, normalize_budgets, REQUIRED
from .canonical import sha256_hex, canonicalize
from .eventlog.projections import ACTIVE_STATES, TERMINAL_STATES, ProjectionManager
from .eventlog.store import EventStore, EventWriter
from .blobs import BlobStore
from .clock import Clock
from .failure import classify, load_rules
from .ids import ulid

STATES = {"CREATED", "PROVISIONING", "READY", "RUNNING", "AWAITING_APPROVAL", "SCORING", "AWAITING_HUMAN", "PAUSED",
          "COMPLETING", "TERMINATING", "COMPLETED", "TERMINATED", "FAILED_INTEGRITY"}
NON_TERMINAL = STATES - TERMINAL_STATES
EXPLICIT = {
    ("CREATED", "PROVISIONING"), ("PROVISIONING", "READY"), ("READY", "RUNNING"),
    ("RUNNING", "AWAITING_APPROVAL"), ("AWAITING_APPROVAL", "RUNNING"), ("RUNNING", "SCORING"), ("SCORING", "RUNNING"),
    ("RUNNING", "COMPLETING"), ("COMPLETING", "COMPLETED"), ("COMPLETING", "RUNNING"),
    ("RUNNING", "AWAITING_HUMAN"), ("AWAITING_APPROVAL", "AWAITING_HUMAN"), ("SCORING", "AWAITING_HUMAN"),
    ("PAUSED", "RUNNING"), ("TERMINATING", "TERMINATED"),
}
TERMINATION_REASONS = {"budget_exhausted", "stuck_loop", "policy_violation", "operator_kill", "fatal_error",
                       "verification_failed_limit", "timeout", "integrity_failure", "recovery_uncertain_abandoned"}


class TransitionError(Exception):
    pass


def transition_allowed(frm: str, to: str) -> bool:
    if (frm, to) in EXPLICIT:
        return True
    if frm in NON_TERMINAL and to == "PAUSED":
        return True
    if frm in NON_TERMINAL and frm != "TERMINATING" and to == "TERMINATING":
        return True
    if to == "FAILED_INTEGRITY" and frm in NON_TERMINAL:
        return True
    return False


@dataclass
class Services:
    store: EventStore
    writer: EventWriter
    blobs: BlobStore
    clock: Clock
    config: dict
    projections: ProjectionManager = None  # type: ignore
    plugins: Any = None   # the PluginHost of the deployment (design §18.11)
    handles: dict = field(default_factory=dict)

    def __post_init__(self):
        self.projections = ProjectionManager(self.store, self.writer)


class Run:
    """Run-scoped handle: emit events, read projections."""

    def __init__(self, svc: Services, run_id: str, profile: str, owner: str):
        self.svc, self.run_id, self.profile, self.owner = svc, run_id, profile, owner
        self.resume_event = asyncio.Event()
        self.resume_event.set()
        self.approval_waiters: dict[str, asyncio.Future] = {}
        self._pins = None

    @property
    def pins(self):
        """The definitions this run is pinned to (design §13.9): loaded from its manifest, so a restarted process sees exactly what the run began with."""
        if self._pins is None:
            from .definitions import Pins
            self._pins = Pins.load(self.svc, self.run_id)
        return self._pins

    async def emit(self, type: str, payload: dict, actor: dict | None = None, causation_seq: int | None = None,
                   correlation_id: str | None = None) -> dict:
        return await self.svc.writer.submit({"run_id": self.run_id, "type": type, "payload": payload,
                                             "actor": actor or {"kind": "control_plane", "id": "harness"},
                                             "causation_seq": causation_seq, "correlation_id": correlation_id})

    async def proj(self, name: str) -> Any:
        return await self.svc.projections.get(self.run_id, name)

    async def state(self) -> str:
        return (await self.proj("run_state"))["state"]

    async def transition(self, to: str, reason: str | None = None, causation_seq: int | None = None) -> dict:
        rs = await self.proj("run_state")
        frm = rs["state"]
        if frm == to:
            return {}
        if not transition_allowed(frm, to):
            raise TransitionError(f"illegal transition {frm} -> {to}")
        if frm == "COMPLETING" and to == "COMPLETED":
            if rs["verified_seq"] is None or rs["entered_completing_seq"] is None or rs["verified_seq"] < rs["entered_completing_seq"]:
                raise TransitionError("COMPLETED requires recorded verification evidence (H-3.3-evidence-based-completion)")
        ev = await self.emit("run.state_changed", {"from": frm, "to": to, "reason": reason}, causation_seq=causation_seq)
        if to == "PAUSED":
            self.resume_event.clear()
        elif frm == "PAUSED":
            self.resume_event.set()
        return ev


class RunManager:
    def __init__(self, svc: Services, sandboxes, failure_rules: dict | None = None):
        self.svc, self.sandboxes = svc, sandboxes
        self.rules = failure_rules or load_rules()
        self.runs: dict[str, Run] = {}

    def get(self, run_id: str) -> Run:
        if run_id not in self.runs:
            row = self.svc.store.run_row(run_id)
            if row is None:
                raise KeyError(run_id)
            self.runs[run_id] = Run(self.svc, run_id, row["profile"], row["owner"])
        return self.runs[run_id]

    async def create_run(self, owner: str, profile: str, instruction: str, agent: dict, task_id: str | None = None,
                         config_commit: str = "local", budgets: dict | None = None, evaluator_budgets: dict | None = None,
                         provenance: dict | None = None, preflight: dict | None = None, egress: list | None = None, manifest: dict | None = None) -> Run:
        cfg_budgets = budgets if budgets is not None else self.svc.config.get("budgets", {})
        run_id = ulid()
        run = Run(self.svc, run_id, profile, owner)
        self.runs[run_id] = run
        await run.emit("run.created", {
            "owner": owner, "profile": profile, "task_id": task_id, "config_commit": config_commit,
            "agent": agent, "budgets": normalize_budgets(cfg_budgets), "evaluator_budgets": normalize_budgets(evaluator_budgets or {}),
            "provenance": provenance or {}, **({"manifest": {**manifest, "run_id": run_id}} if manifest else {})}, actor={"kind": "user", "id": owner})
        if preflight is not None and not preflight["ok"]:
            await self.terminate(run, "fatal_error", "preflight failed: " + ", ".join(c["id"] for c in preflight["failed"]))
            return run
        await run.transition("PROVISIONING", "run.created accepted")
        try:
            await self.sandboxes.create(run, "work", egress)
        except Exception as e:
            await self.terminate(run, "fatal_error", f"provisioning failed: {e}")
            return run
        await run.transition("READY", "executor.ready")
        await run.emit("user.message", {"message": {"role": "user", "parts": [{"type": "text", "text": instruction}]},
                                        "trust": "user", "search_text": instruction}, actor={"kind": "user", "id": owner})
        return run

    async def start(self, run: Run) -> None:
        await run.transition("RUNNING", "start")

    async def pause(self, run: Run, who: str) -> None:
        await run.emit("operator.action", {"action": "pause", "who": who}, actor={"kind": "user", "id": who})
        await run.transition("PAUSED", "operator pause")

    async def resume(self, run: Run, who: str) -> None:
        await run.emit("operator.action", {"action": "resume", "who": who}, actor={"kind": "user", "id": who})
        await run.transition("RUNNING", "operator resume")

    async def kill(self, run: Run, who: str) -> None:
        await self.terminate(run, "operator_kill", f"killed by {who}")

    async def terminate(self, run: Run, reason: str, detail: str = "") -> None:
        base = reason.split(":")[0]
        if base not in TERMINATION_REASONS:
            raise ValueError(f"unknown termination reason {reason}")
        state = await run.state()
        if state in TERMINAL_STATES:
            return
        if state != "TERMINATING":
            await run.transition("TERMINATING", reason)  # recorded before any action takes effect (§3.3 rule 5)
        for fut in list(run.approval_waiters.values()):
            if not fut.done():
                fut.cancel()
        await self.sandboxes.teardown(run)
        await BudgetController(run).release_open("run ended")      # a run that ends holds no budget (§7.4.4)
        m = re.match(r"^([a-z_]+\.[a-z_0-9]+):", detail or "")   # e.g. "gateway.http_500: ..." carries its own error code
        evs = self.svc.store.events(run.run_id, types=["tool.call.failed"])
        last_err = m.group(1) if m else (((evs[-1]["payload"].get("error") or {}).get("code")) if evs else None)
        cls = classify(reason, last_err, self.rules, [(e["payload"].get("error") or {}).get("code") for e in evs if (e["payload"].get("error") or {}).get("code")])
        if (await run.proj("run_state"))["terminal_reason"] is None:
            await run.emit("run.terminated", {"reason": reason, "detail": detail})
            if cls:
                await run.emit("failure.classified", cls)
        await self._finish(run, "TERMINATED")
        await run.transition("TERMINATED", reason)
        run.resume_event.set()

    async def _finish(self, run: Run, state: str) -> None:
        """`run.finished` carries the manifest's final fields and its hash (design §13.9). A run created without a manifest has nothing to finish."""
        from . import manifest as M
        created = self.svc.store.events(run.run_id, types=["run.created"])
        start = (created[0]["payload"].get("manifest") if created else None)
        if not start: return
        models = {m["ref"]: m for m in (self.svc.config or {}).get("models", [])}
        final = await M.final_fields(self.svc, run, models, state)
        await run.emit("run.finished", {"fields": final, "manifest_hash": M.manifest_hash(M.merged(start, final))})

    async def complete(self, run: Run, evidence_ref: str | None) -> None:
        await run.emit("run.completed", {"evidence_ref": evidence_ref})
        await self._finish(run, "COMPLETED")
        await run.transition("COMPLETED", "verified")
        await self.sandboxes.teardown(run)
