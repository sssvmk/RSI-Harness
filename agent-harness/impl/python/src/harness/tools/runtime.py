"""ToolRuntime: validate -> policy -> approval -> execute -> shape (design §9.4, §9.7, §9.8)."""
from __future__ import annotations
import asyncio, json
from dataclasses import dataclass, field
from typing import Any
import jsonschema
from ..clock import parse_ts
from ..reconcile import Gate, VersionConflict, WorkspaceFileAdapter, risk_of
from ..selection import SelectionMonitor, rules as selection_rules, unsafe_execution_alert
from ..budgets import BudgetController, ConcurrencyLimiter, QueueFull
from ..canonical import sha256_hex
from ..policy import decide, glob_match
from ..sandbox.executor import ExecError
from .registry import ToolRegistry

MAX_RETRY_ATTEMPTS = 3


class ToolError(Exception):
    def __init__(self, code: str, category: str, message: str, retryable: bool = False, hint: str | None = None, alternatives: list | None = None):
        super().__init__(message)
        self.code, self.category, self.retryable, self.hint, self.alternatives = code, category, retryable, hint, alternatives or []

    def to_dict(self) -> dict:
        return {"code": self.code, "category": self.category, "message": str(self), "retryable": self.retryable,
                "hint": self.hint, "alternatives": self.alternatives}


@dataclass
class ToolResult:
    text: str
    trust: str | None = None
    extra: dict = field(default_factory=dict)


@dataclass
class ToolContext:
    run: Any
    svc: Any
    sandbox: Any
    registry: ToolRegistry
    agent: dict
    hooks: dict = field(default_factory=dict)
    state: dict = field(default_factory=dict)
    config_dir: str | None = None
    memory_guard: Any = None


@dataclass
class ToolMetrics:
    calls: int = 0
    selection_errors: int = 0

    def rate(self) -> float:
        return self.selection_errors / self.calls if self.calls else 0.0


class ToolRuntime:
    def __init__(self, registry: ToolRegistry, ruleset: dict, approvals, svc, redactor=None, divisor: float = 3.0,
                 selection_error_alert: float = 0.05):
        self.registry, self.ruleset, self.approvals, self.svc = registry, ruleset, approvals, svc
        self.redactor, self.divisor = redactor, divisor
        self.metrics, self.alert_threshold = ToolMetrics(), selection_error_alert
        self.alert_cfg = selection_rules((getattr(svc, "config", None) or {}).get("alerts", {}))      # invalid alert rules are refused at startup
        self.monitors: dict = {}
        rcfg = (getattr(svc, "config", None) or {}).get("reconcile") or {}
        self.gate = Gate({"workspace_file": WorkspaceFileAdapter()}, rcfg.get("approval_max_age_s", 900)); self.critical_tools = set(rcfg.get("critical_tools") or [])   # the pre-action gate (§5.13)
        bcfg = (getattr(svc, "config", None) or {}).get("budgets") or {}
        self.limiter = ConcurrencyLimiter(bcfg.get("workers", 4), bcfg.get("queue", 100))      # B_concurrency: a bounded pool and a bounded queue (§7.4.4)

    async def _observe(self, run, error: bool, tool: str) -> None:
        """Feed one selection outcome to the run's monitor; a transition (raise, escalate, resolve) becomes an event with the tool but never the arguments."""
        mon = self.monitors.get(run.run_id)
        if mon is None: mon = self.monitors[run.run_id] = SelectionMonitor.from_events(self.svc.store.events(run.run_id), {k: v for k, v in self.alert_cfg.items()})
        t = mon.observe(error, {"tool": tool})
        if t: await run.emit(t["event"], t["payload"])

    def world_action(self, run, cid: str, name: str, args: dict, m: dict, decision: str = "allow", status: str = "granted") -> dict | None:
        """The action as the gate sees it. The risk follows the potential impact of the effect and not the tool's name or the agent that called it; a policy can mark a tool critical."""
        w = m.get("world")
        if not w: return None
        approval = None
        if decision == "require_approval":
            evs = self.svc.store.events(run.run_id, types=["approval.requested", "approval.decided", "approval.revoked"]); aid = next((e["payload"]["approval_id"] for e in reversed(evs) if e["type"] == "approval.requested" and e["payload"]["tool_call_id"] == cid), None)
            dec = next((e for e in reversed(evs) if e["type"] == "approval.decided" and e["payload"]["approval_id"] == aid), None)
            if dec: approval = {"status": dec["payload"]["status"], "decided_at_s": parse_ts(dec["ts"]) / 1000, "revoked": any(e["type"] == "approval.revoked" and e["payload"]["approval_id"] == aid for e in evs), "by": dec["payload"].get("decided_by", "")}
        risk = risk_of(m.get("effect_class"), w.get("risk"), name in self.critical_tools)
        return {"id": cid, "tool": name, "risk": risk, "adapter": w["adapter"], "resource": str(args.get(w["resource_arg"], "")), "args": args, "preconditions": w.get("preconditions", []), "expected_effects": w.get("expected_effects", []),
                "expected_version": args.get("if_version"), "authorized": True, "approval_required": decision == "require_approval", "approval": approval, "human_approved": bool(approval and approval["status"] == "granted" and not str(approval["by"]).startswith("standing:"))}

    def _redact(self, s: str) -> str:
        return self.redactor.redact(s) if self.redactor else s

    async def _fail(self, run, cid, tool, err: ToolError, trust: str, req_seq: int | None) -> dict:
        text = f"Error [{err.code}]: {err}" + (f"\nHint: {err.hint}" if err.hint else "") + (f"\nAlternatives: {', '.join(err.alternatives)}" if err.alternatives else "")
        text = self._redact(text)
        msg = {"role": "tool", "parts": [{"type": "tool_result", "call_id": cid, "parts": [{"type": "text", "text": text}], "is_error": True}],
               "provenance": {"source": tool, "trust": trust}}
        await run.emit("tool.call.failed", {"tool_call_id": cid, "tool": tool, "error": err.to_dict(), "message": msg, "trust": trust,
                                            "search_text": text[:2000]}, causation_seq=req_seq, correlation_id=cid)
        return {"ok": False, "digest": sha256_hex(text), "text": text, "error": err.to_dict()}

    async def execute(self, run, call: dict, tctx: ToolContext) -> dict:
        cid, name, args = call["id"], call["name"], call["arguments"]
        agent, bc = tctx.agent, BudgetController(run)
        self.metrics.calls += 1
        tool = self.registry.get(name)
        allowed = tool is not None and any(glob_match(g, name) for g in agent["tool_allowlist"])
        m = tool.manifest if tool else {}
        args_payload = self.svc.blobs.inline_or_ref(args, "args", "args_ref")
        req = await run.emit("tool.call.requested", {"tool_call_id": cid, "tool": name, "effect_class": m.get("effect_class"),
                                                     "idempotency": m.get("idempotency"), **args_payload}, correlation_id=cid)
        if not allowed:
            self.metrics.selection_errors += 1; await self._observe(run, True, name)
            return await self._fail(run, cid, name, ToolError("tool.unknown", "tool", f"unknown or unavailable tool {name!r}",
                                    hint="Use tool.search to discover tools", alternatives=self.registry.allowed(agent["tool_allowlist"])[:5]), "tool_trusted", req["seq"])
        v = jsonschema.Draft202012Validator(m["input_schema"])
        errs = sorted(v.iter_errors(args), key=lambda e: list(e.path))
        if errs:
            self.metrics.selection_errors += 1; await self._observe(run, True, name)
            e = errs[0]
            where = "/".join(str(p) for p in e.path) or "(root)"
            return await self._fail(run, cid, name, ToolError("tool.invalid_arguments", "tool", f"{where}: {e.message}",
                                    hint=f"Arguments must satisfy: {json.dumps(m['input_schema'])[:300]}"), "tool_trusted", req["seq"])
        d = decide(self.ruleset, {"tool": name, "effect_class": m.get("effect_class"), "profile": run.profile, "actor_role": "agent", "args": args})
        await run.emit("policy.decided", {"tool_call_id": cid, "decision": d["decision"], "matched_rule_ids": d["matched_rule_ids"],
                                          "default_used": d["default_used"]}, causation_seq=req["seq"], correlation_id=cid)
        if d["decision"] == "deny":
            await self._observe(run, True, name)
            return await self._fail(run, cid, name, ToolError("policy.denied", "security", f"policy denied {name}",
                                    hint=f"Rules: {', '.join(d['matched_rule_ids'])}"), "tool_trusted", req["seq"])
        status = "granted"
        if d["decision"] == "require_approval":
            open_ = await run.proj("approvals_open")
            existing = next((aid for aid, a in open_.items() if a["tool_call_id"] == cid), None)
            r = ({"approval_id": existing, "status": "pending"} if existing else
                 await self.approvals.request(run, cid, name, args, d["winning_rule_id"], d["approval"], m.get("effect_class"), req["seq"]))
            status = r["status"]
            if status == "pending":
                if await run.state() == "RUNNING":
                    await run.transition("AWAITING_APPROVAL", f"approval {r['approval_id']}")
                status = await self.approvals.wait(run, r["approval_id"])
                if not (await run.proj("approvals_open")) and await run.state() == "AWAITING_APPROVAL":
                    await run.transition("RUNNING", "approval decided")
            if status != "granted":
                return await self._fail(run, cid, name, ToolError(f"approval.{status}", "security", f"approval {status} for {name}",
                                        hint="Choose a different approach or ask the operator"), "tool_trusted", req["seq"])
        exhausted = await bc.precheck({"tools": 1})
        if exhausted:
            return {"ok": False, "terminate": f"budget_exhausted:{exhausted}", "digest": "", "text": ""}
        await self._observe(run, False, name)
        world = None
        action = self.world_action(run, cid, name, args, m, d["decision"], status)
        if action is not None:                                                      # the single gateway for consequential actions: the agent cannot skip it
            emit = lambda t, p: run.emit(t, p, causation_seq=req["seq"], correlation_id=cid)
            pre = await self.gate.pre(emit, action, tctx, self.svc.clock.now_ms() / 1000)
            if pre["status"] == "blocked":
                sec = pre["reason"] in ("authorization_denied", "approval_stale_or_revoked", "human_intervention_required")
                return await self._fail(run, cid, name, ToolError("action.blocked", "security" if sec else "environment", f"{name} did not run: {pre['reason']}", hint="Its preconditions, the state it assumed, or its approval no longer hold; re-check the resource and try again"), "tool_trusted", req["seq"])
            world = (action, pre["actual"], emit)
        return await self._run_with_retries(run, cid, name, args, tool, m, tctx, req["seq"], decision=d["decision"], approved=(d["decision"] != "require_approval" or status == "granted"), world=world)

    async def _run_with_retries(self, run, cid, name, args, tool, m, tctx, req_seq, decision: str = "allow", approved: bool = True, world=None) -> dict:
        if decision == "deny" or not approved:     # unreachable by construction; if it is ever reached, nothing executes and it is an immediate alert, whatever the error rate (§9.13)
            a = unsafe_execution_alert(name, decision if decision == "deny" else "unapproved"); await run.emit(a["event"], a["payload"])
            return await self._fail(run, cid, name, ToolError("policy.violation", "security", f"{name} was not authorized to run"), "tool_trusted", req_seq)
        bc = BudgetController(run)
        attempt, err, result = 0, None, None
        while True:
            attempt += 1
            await run.emit("tool.call.started", {"tool_call_id": cid, "tool": name, "attempt": attempt,
                                                 "idempotency_key": cid if m["idempotency"] == "keyed" else None}, causation_seq=req_seq, correlation_id=cid)
            await bc.add({"tools": 1})
            try:
                async with self.limiter.slot():
                    if world: raw = await self.gate.guarded(world[0], world[1], lambda: asyncio.wait_for(tool.handler(args, tctx), m["timeout_s"]), tctx)      # version-conditional: a change since the check is not missed
                    else: raw = await asyncio.wait_for(tool.handler(args, tctx), m["timeout_s"])
                result = raw if isinstance(raw, ToolResult) else ToolResult(raw if isinstance(raw, str) else json.dumps(raw, sort_keys=True))
                err = None
            except VersionConflict:
                await world[2]("action.blocked", {"action_id": cid, "reason": "stale_state", "risk": world[0]["risk"]}); await self.gate._state(world[2], world[0], "mismatch", True)
                err = ToolError("action.blocked", "environment", f"{name} did not run: stale_state (the resource changed after it was checked)", retryable=False, hint="Read the resource again and decide whether the action still applies")
            except QueueFull as e:
                err = ToolError("tool.queue_full", "environment", str(e), retryable=True, hint="Too many tool calls are running; retry shortly")
            except ToolError as e:
                err = e
            except ExecError as e:
                err = ToolError(e.code, e.category, str(e), e.retryable, e.hint, e.alternatives)
            except asyncio.TimeoutError:
                err = ToolError("tool.timeout", "environment", f"{name} exceeded {m['timeout_s']}s", retryable=True)
            except Exception as e:  # tool bugs are observations, never crashes
                err = ToolError("tool.internal", "tool", f"{type(e).__name__}: {e}")
            if err is None:
                break
            if world and err.code == "tool.timeout":                                 # a timeout is not a failure: what actually happened is read from state before anything is retried
                est = await self.gate.establish_outcome(world[2], world[0], world[1], tctx)
                if est["outcome"] == "applied": result, err = ToolResult("(the action had already taken effect before it timed out; verified from the resource's state, so it was not repeated)"), None; break
                if est["outcome"] == "unknown": err = ToolError("reconcile.unknown", "environment", f"{name} timed out and its outcome could not be established from state; it was not retried", retryable=False, hint="Check the resource yourself before trying again"); break
            if err.retryable and m["idempotency"] in ("idempotent", "keyed") and attempt < MAX_RETRY_ATTEMPTS:
                continue  # §9.8: never auto-retry non_idempotent tools
            break
        trust_default = "tool_trusted" if m["location"] == "control_plane" else "untrusted"
        if err is None and world and not (result.text or "").startswith("(the action had already"):
            post = await self.gate.post(world[2], world[0], world[1], result, tctx)         # read the result back; the tool's acknowledgment alone is not trusted
            if post["status"] == "reconciliation_required": err = ToolError("reconcile.effect_mismatch", "environment", f"{name} reported success but the state read back does not match the expected effect", retryable=False, hint="Read the resource and repeat or repair the change")
            elif post["status"] == "pending_reconciliation": result = ToolResult(result.text + "\n[the harness could not read the result back; it is recorded as unverified and the run cannot complete until it is verified]", result.trust, result.extra)
        if err is not None:
            return await self._fail(run, cid, name, err, trust_default, req_seq)
        return await self._complete(run, cid, name, result, trust_default, req_seq)

    async def _complete(self, run, cid, name, result: ToolResult, trust_default: str, req_seq) -> dict:
        full = self._redact(result.text)
        digest = sha256_hex(full)
        cap_tokens = self.registry.get(name).manifest["result_cap_tokens"]
        cap_bytes = int(cap_tokens * self.divisor)
        raw = full.encode("utf-8")
        payload: dict = {"truncated": False}
        text = full
        if len(raw) > cap_bytes:
            ref = self.svc.blobs.put(raw)
            text = raw[:cap_bytes].decode("utf-8", "ignore")
            tot = -(-len(raw) // int(self.divisor)); shown = -(-cap_bytes // int(self.divisor))
            text += f"\n[truncated: {shown} of {tot} tokens; fetch more with history.get(tool_call_id=\"{cid}\", offset={len(text.encode())})]"
            payload.update({"truncated": True, "result_ref": ref})
        trust = result.trust or trust_default
        msg = {"role": "tool", "parts": [{"type": "tool_result", "call_id": cid, "parts": [{"type": "text", "text": text}], "is_error": False}],
               "provenance": {"source": name, "trust": trust}}
        await run.emit("tool.call.completed", {"tool_call_id": cid, "tool": name, "message": msg, "trust": trust,
                                               "search_text": text[:8000], **payload, **result.extra}, causation_seq=req_seq, correlation_id=cid)
        return {"ok": True, "digest": digest, "text": text}
