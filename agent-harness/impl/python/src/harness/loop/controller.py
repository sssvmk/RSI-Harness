"""LoopController: the agent turn (design §7.2)."""
from __future__ import annotations
import asyncio, json, re
from pathlib import Path
from ..plugins import PluginUnavailable
from ..reconcile import UNVERIFIED
from ..definitions import DefinitionMismatch
from ..budgets import BudgetController, cost_of, reserve_cost
from ..canonical import canonicalize, sha256_hex
from ..context.assembler import build_context
from ..context.compaction import Compactor
from ..eventlog.projections import active_time_ms
from ..gateway.canonical import message_text, tool_calls_of
from ..gateway.gateway import GatewayError
from ..policy import glob_match
from ..stuck import call_signature, stuck_action, turn_signature
from ..tools.builtin import ALWAYS_LOADED
from ..tools.runtime import ToolContext

NUDGE = "Your last response contained no tool call and no completion request. Use a tool to make progress, or call finish(summary) when done."


class LoopController:
    def __init__(self, run, rm, gateway, runtime, registry, agent: dict, models: dict, config: dict, verifier=None, hooks: dict | None = None,
                 config_dir: str | None = None, memory_guard=None, price_lookup=None):
        self.run, self.rm, self.gateway, self.runtime, self.registry = run, rm, gateway, runtime, registry
        self.agent, self.models, self.config, self.verifier = agent, models, config, verifier
        self.config_dir, self.memory_guard = config_dir, memory_guard
        self.svc = run.svc
        self.budgets = BudgetController(run)
        self.model = models[agent["model_ref"]]
        self.completion_summary: str | None = None
        self.skills = self._skills()
        self.tctx_state: dict = {}
        self.hooks = hooks or {}
        self.hooks["finish"] = self._finish_hook
        lc = config.get("loop", {})
        self.max_idle = lc.get("max_idle_turns", 3)
        self.stuck_threshold = lc.get("stuck", {}).get("threshold", 4)
        self.stuck_extra = lc.get("stuck", {}).get("extra_repeats", 2)
        self.max_verif_failures = lc.get("max_verification_failures", 3)
        self.divisor = config.get("tokens", {}).get("divisor", 3.0)
        self.compactor = Compactor(run, gateway, config.get("context", {}), agent["model_ref"], self.svc.blobs.get, self.budgets, self.model.get("price"))

    # ---------- helpers ----------
    def _skills(self) -> list[tuple[str, str]]:
        out = []
        for name in self.agent.get("skills", []):
            desc = name
            if self.run.pins.active and self.run.pins.has(f"skill.{name}"):      # skills come from the manifest, not from whatever is on disk now
                m = re.search(r"^description:\s*(.+)$", self.run.pins.resolve(f"skill.{name}"), re.M)
                out.append((name, m.group(1).strip() if m else name)); continue
            if self.config_dir:
                p = Path(self.config_dir) / "skills" / name / "SKILL.md"
                if p.is_file():
                    m = re.search(r"^description:\s*(.+)$", p.read_text(), re.M)
                    desc = m.group(1).strip() if m else name
            out.append((name, desc))
        return out

    def sandbox(self):
        for h in self.rm.sandboxes.handles.values():
            if h.labels.get("harness.run_id") == self.run.run_id and h.role == "work":
                return h
        raise RuntimeError("no work sandbox")

    async def _finish_hook(self, summary: str) -> None:
        self.completion_summary = summary
        await self.run.emit("completion.proposed", {"summary": summary, "via": "finish"})   # durable, so a crash cannot lose it

    def _tctx(self) -> ToolContext:
        return ToolContext(run=self.run, svc=self.svc, sandbox=self.sandbox(), registry=self.registry, agent=self.agent,
                           hooks=self.hooks, state=self.tctx_state, config_dir=self.config_dir, memory_guard=self.memory_guard)

    async def _build(self):
        conv = await self.run.proj("conversation")
        eph = await self.run.proj("ephemeral")
        ws = await self.run.proj("working_state")
        lp = await self.run.proj("loop")
        names = self.config.get("context", {}).get("working_state", ["*"])
        working = {n: self.svc.blobs.get(v["hash"]).decode() for n, v in ws.items() if any(glob_match(g, n) for g in names)}
        tools = self.registry.exposed(self.agent["tool_allowlist"], ALWAYS_LOADED, lp["exposed_tools"], self.config.get("tools", {}).get("dynamic_threshold", 20))
        pins = self.run.pins
        if pins.active:                                             # definitions are resolved through the manifest and verified before use (§13.9)
            prompt = pins.resolve("prompt.system")
            for t in tools: pins.check_hash(f"tool.{t['name']}", t)
        else: prompt = self.agent["system_prompt"]
        return build_context(system_prompt=prompt, profile=self.run.profile, tools=[t for t in tools], skills=self.skills,
                             conversation=conv, working_state=working, guidance=eph["guidance"], resolve=self.svc.blobs.get,
                             divisor=self.divisor, window=self.model["window"])

    @staticmethod
    def _searchable(msg: dict, calls: list[dict]) -> str:
        """History index text: the prose plus what the agent *did* (tool names and arguments)."""
        parts = [message_text(msg)] + [f"{c['name']} {json.dumps(c['arguments'], sort_keys=True)}" for c in calls]
        return "\n".join(p for p in parts if p)[:8000]

    async def _checkpoint(self) -> bool:
        """Cooperative cancellation point: True if the loop may keep going."""
        while True:
            st = await self.run.state()
            if st == "PAUSED":
                await self.run.resume_event.wait()
                continue
            return st in ("RUNNING", "COMPLETING")

    # ---------- main ----------
    async def _definition_failure(self, e: DefinitionMismatch) -> str:
        await self.run.emit("definition.mismatch", {"id": e.def_id, "expected": e.expected, "observed": e.observed, "reason": e.reason})
        await self.rm.terminate(self.run, "fatal_error", f"definition.mismatch: {e}")      # controlled, and nothing is substituted
        return "TERMINATED"

    async def run_loop(self) -> str:
        pins = self.run.pins
        if pins.active:                                                # verify every pinned definition when the run starts, and again when it resumes
            try:
                ok = pins.verify_all(); host = getattr(self.svc, "plugins", None)
                for pid in [i for i in pins.by_id if i.startswith("plugin.")]:       # a plugin that was swapped or removed since the run was pinned is a mismatch
                    try: pins.check_hash(pid, host.definition(pid[7:]))
                    except PluginUnavailable: raise DefinitionMismatch(pid, pins.by_id[pid]["sha256"], None, "the plugin is no longer loaded")
            except DefinitionMismatch as e: return await self._definition_failure(e)
            await self.run.emit("definition.verified", {"count": len(ok), "definitions": ok})
        while True:
            if not await self._checkpoint():
                return await self.run.state()
            if await self.run.state() != "RUNNING":
                return await self.run.state()
            try:
                out = await self.turn()
            except DefinitionMismatch as e:
                return await self._definition_failure(e)
            except asyncio.CancelledError:
                raise
            except Exception as e:  # a bug must not leave the run dangling
                await self.rm.terminate(self.run, "fatal_error", f"{type(e).__name__}: {e}")
                return "TERMINATED"
            if out in ("done", "terminated", "halted"):
                return await self.run.state()

    async def turn(self) -> str:
        run = self.run
        lp0 = await run.proj("loop")
        turn_no = lp0["turn"] + 1
        compacted = await self.compactor.maybe_compact(self._build, self.model["window"])
        if compacted:
            await self.compactor.hydrate(compacted["event"]["payload"]["from_seq"], compacted["event"]["payload"]["to_seq"])
        ctx = await self._build()
        await run.emit("context.assembled", {"turn": turn_no, "segment_hashes": ctx["segment_hashes"], "est_tokens": ctx["est_tokens"], "window": ctx["window"]})
        max_out = self.agent.get("max_output_tokens", 4096)
        margin = ((self.svc.config or {}).get("budgets") or {}).get("safety_margin", 0.05)
        rid, ex = await self.budgets.reserve({"turns": 1, "tokens": ctx["est_tokens"] + max_out, "cost": reserve_cost(ctx["est_tokens"], max_out, self.model.get("price"), margin)})   # worst case, reserved before the call (§7.4.4)
        if ex:
            await self.rm.terminate(run, f"budget_exhausted:{ex}", f"before turn {turn_no}")
            return "terminated"
        request = {"model_ref": self.agent["model_ref"], "messages": ctx["messages"], "tools": ctx["tools"],
                   "params": {"max_output_tokens": self.agent.get("max_output_tokens", 4096)}, "metadata": {"run_id": run.run_id, "turn": turn_no}}
        req_ref = self.svc.blobs.put(canonicalize(request))
        await run.emit("model.requested", {"request_ref": req_ref, "model_ref": self.agent["model_ref"], "provider": self.model["provider"],
                                           "est_input_tokens": ctx["est_tokens"], "turn": turn_no})
        rs = await run.proj("run_state")
        tlim = (await run.proj("budgets"))["agent"].get("time_ms")
        remaining = (tlim["limit"] - active_time_ms(rs, self.svc.clock.now_ms())) if tlim else None

        async def on_fail(info):
            await run.emit("model.failed", info)
        try:
            resp = await self.gateway.call(request, ctx["id_map"], on_fail, remaining)
        except GatewayError as e:
            if e.code == "gateway.connection":                  # the request may have reached the provider: charge the estimate and flag it until the real usage is known
                await self.budgets.commit(rid, {"turns": 1, "tokens": ctx["est_tokens"] + max_out, "cost": reserve_cost(ctx["est_tokens"], max_out, self.model.get("price"), margin)}, unreconciled=True)
            else:                                               # the request provably did not run: the turn still counts, the rest is released
                await self.budgets.commit(rid, {"turns": 1})
            await self.rm.terminate(run, "fatal_error", f"{e.code}: {e}")
            return "terminated"
        except Exception:                                       # anything else between reserve and commit: the turn counts, the rest of the reservation is given back
            await self.budgets.commit(rid, {"turns": 1}); raise
        msg = resp["message"]
        calls = tool_calls_of(msg)
        stored = self.svc.blobs.inline_or_ref(msg, "message", "message_ref")
        await run.emit("model.responded", {**stored, "call_ids": [c["id"] for c in calls], "finish_reason": resp["finish_reason"],
                                           "usage": resp["usage"], "latency_ms": resp.get("latency_ms", 0), "attempt": resp.get("attempt", 1),
                                           "id_map": resp["provider_meta"].get("id_map", {}), "search_text": self._searchable(msg, calls)})
        ex = await self.budgets.commit(rid, {"turns": 1, "tokens": resp["usage"]["input_tokens"] + resp["usage"]["output_tokens"], "cost": cost_of(resp["usage"], self.model.get("price"))})
        await self.budgets.snapshot_time()
        if ex:
            await self.rm.terminate(run, f"budget_exhausted:{ex}", f"after turn {turn_no}")
            return "terminated"

        sigs: list[str] = []
        idle = False
        if calls:
            results = await self._execute_calls(calls)
            for r in results:
                if r.get("terminate"):
                    await self.rm.terminate(run, r["terminate"], f"turn {turn_no}")
                    return "terminated"
            sigs = [call_signature(c["name"], c["arguments"], r["digest"]) for c, r in zip(calls, results)]
            if not await self._checkpoint():
                return "halted"
        elif self.verifier is not None and resp["finish_reason"] == "stop":
            self.completion_summary = message_text(msg) or "(end of turn)"
            await run.emit("completion.proposed", {"summary": self.completion_summary, "via": "end_turn"})
        else:
            idle = True
            if resp["finish_reason"] == "length":
                await run.emit("guidance.injected", {"kind": "policy", "text": "Your response was cut off by the output limit. Respond more briefly and use tools."})
        await run.emit("turn.completed", {"turn": turn_no, "signature": turn_signature(sigs), "idle": idle})

        if self.completion_summary is not None:
            summary, self.completion_summary = self.completion_summary, None
            return await self._complete(summary)
        lp = await run.proj("loop")
        if idle:
            if lp["idle_streak"] > self.max_idle:
                await self.rm.terminate(run, "verification_failed_limit", f"{lp['idle_streak']} consecutive idle turns")
                return "terminated"
            await run.emit("guidance.injected", {"kind": "policy", "text": NUDGE})
            return "continue"
        act = stuck_action(lp, self.stuck_threshold, self.stuck_extra)
        if act == "guidance":
            await run.emit("stuck_loop.detected", {"signature": lp["last_signature"], "count": lp["repeat_count"], "action": "guidance"})
            await run.emit("guidance.injected", {"kind": "stuck_loop", "text": "You are repeating the same actions with the same results. Change your approach."})
        elif act == "terminate":
            high = run.profile == "high"
            await run.emit("stuck_loop.detected", {"signature": lp["last_signature"], "count": lp["repeat_count"], "action": "escalate" if high else "terminate"})
            if high:
                await run.transition("AWAITING_HUMAN", "stuck loop")
                return "halted"
            await self.rm.terminate(run, "stuck_loop", f"signature repeated {lp['repeat_count']} times")
            return "terminated"
        return "continue"

    async def _execute_calls(self, calls: list[dict]) -> list[dict]:
        tctx = self._tctx()
        if len(calls) > 1 and all((self.registry.get(c["name"]) and self.registry.get(c["name"]).manifest["parallel_safe"]) for c in calls):
            return list(await asyncio.gather(*(self.runtime.execute(self.run, c, tctx) for c in calls)))
        out = []
        for c in calls:
            if not await self._checkpoint():
                break
            out.append(await self.runtime.execute(self.run, c, tctx))
        while len(out) < len(calls):
            out.append({"digest": "", "ok": False})
        return out

    async def resume(self, plan) -> str:
        """Continue after RecoveryManager.recover(): finish interrupted tool calls, then re-enter the loop."""
        run = self.run
        st = await run.state()
        if plan.halted:
            return st
        if st == "TERMINATING":
            ev = [e for e in self.svc.store.events(run.run_id, types=["run.state_changed"]) if e["payload"]["to"] == "TERMINATING"][-1]
            await self.rm.terminate(run, ev["payload"]["reason"], "completed teardown after crash")
            return await run.state()
        if st == "COMPLETING":
            await self._complete("(recovered after crash)")
            return await run.state()
        if st in ("PROVISIONING", "READY", "CREATED"):
            await self.rm.terminate(run, "fatal_error", f"crashed during {st}")
            return await run.state()
        if plan.pending_calls:
            results = await self._execute_calls(plan.pending_calls)
            for r in results:
                if r.get("terminate"):
                    await self.rm.terminate(run, r["terminate"], "after recovery")
                    return "TERMINATED"
            await run.emit("turn.completed", {"turn": (await run.proj("loop"))["turn"] + 1, "signature": None, "idle": False})
        pending = (await run.proj("loop"))["completion_pending"]
        if pending is not None:                       # the proposal was durable; verification never ran
            self.completion_summary = None
            await self._complete(pending)
            if await run.state() != "RUNNING":
                return await run.state()
        return await self.run_loop()

    async def _complete(self, summary: str) -> str:
        run = self.run
        await run.transition("COMPLETING", "agent proposed completion")
        unverified = {k: v for k, v in (await run.proj("reconcile"))["subjects"].items() if v["state"] in UNVERIFIED}
        if unverified:                                                              # a run is not complete while required effects remain unverified (§5.13)
            res = {"passed": False, "detail": "required effects remain unverified: " + ", ".join(f"{k} ({v['state']})" for k, v in sorted(unverified.items())), "evidence": {"unverified": unverified}}
        elif self.verifier is None:
            res = {"passed": False, "detail": "no verifier configured: the agent's claim of success is not evidence", "evidence": {}}
        else:
            res = await self.verifier.verify(run, self.sandbox())
        ref = self.svc.blobs.put_json({"summary": summary, **res})
        await run.emit("verification.completed", {"passed": res["passed"], "detail": res["detail"], "evidence_ref": ref})
        if res["passed"]:
            await self.rm.complete(run, ref)
            return "done"
        rs = await run.proj("run_state")
        if rs["verification_failures"] >= self.max_verif_failures:
            await self.rm.terminate(run, "verification_failed_limit", res["detail"])
            return "terminated"
        await run.transition("RUNNING", "verification failed")
        await run.emit("guidance.injected", {"kind": "policy", "text": f"Verification failed: {res['detail']}. Fix the problem before finishing."})
        return "continue"
