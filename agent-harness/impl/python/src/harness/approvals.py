"""Approvals (design §11.4): pending -> granted|denied|expired|cancelled; standing approvals with atomic use counts."""
from __future__ import annotations
import asyncio, json
from .canonical import canonicalize
from .clock import format_ts, parse_ts
from .ids import ulid
from .policy import glob_match


class ApprovalError(Exception):
    def __init__(self, code: str, msg: str):
        super().__init__(msg)
        self.code = code


class ApprovalService:
    def __init__(self, svc, redactor=None):
        self.svc, self.redactor = svc, redactor
        self.cfg = svc.config.get("approvals", {})
        self._timers: dict[str, asyncio.Task] = {}

    # ---- standing approvals ----
    async def create_standing(self, who: str, roles: list[str], tool: str, args_pattern: str | None, ttl_s: int, max_count: int,
                              run_id: str | None = None) -> str:
        if "approver" not in roles:
            raise ApprovalError("approvals.forbidden", "approver role required")
        sid = ulid()
        exp = format_ts(self.svc.clock.now_ms() + ttl_s * 1000)
        await self.svc.writer.execute("INSERT INTO standing_approvals(id,run_id,tool,args_pattern,expires_at,max_count,used,granted_by) VALUES(?,?,?,?,?,?,0,?)",
                                      (sid, run_id, tool, args_pattern, exp, max_count, who))
        return sid

    async def _try_standing(self, run, tool: str, args: dict) -> str | None:
        now = format_ts(self.svc.clock.now_ms())
        rows = self.svc.store.conn().execute(
            "SELECT * FROM standing_approvals WHERE (run_id IS NULL OR run_id=?) AND expires_at>? AND used<max_count", (run.run_id, now)).fetchall()
        args_json = canonicalize(args).decode()
        for r in rows:
            if not glob_match(r["tool"], tool):
                continue
            if r["args_pattern"] and not glob_match(r["args_pattern"], args_json):
                continue
            n = await self.svc.writer.execute("UPDATE standing_approvals SET used=used+1 WHERE id=? AND used<max_count", (r["id"],))
            if n == 1:  # atomic increment won
                return r["id"]
        return None

    # ---- requests ----
    async def request(self, run, tool_call_id: str, tool: str, args: dict, rule_id: str | None, approval_cfg: dict | None,
                      effect_class: str | None, causation_seq: int | None = None) -> dict:
        cfg = approval_cfg or {}
        per_instance = (cfg.get("allow_standing") is False) or (run.profile == "high" and effect_class == "irreversible")
        if not per_instance:
            sid = await self._try_standing(run, tool, args)
            if sid:
                aid = ulid()
                await run.emit("approval.requested", {"approval_id": aid, "tool_call_id": tool_call_id, "tool": tool, "rule_id": rule_id, "scope": {"standing": sid}}, causation_seq=causation_seq)
                await run.emit("approval.decided", {"approval_id": aid, "status": "granted", "decided_by": f"standing:{sid}"})
                return {"approval_id": aid, "status": "granted", "standing": sid}
        aid = ulid()
        ttl = cfg.get("ttl_s", self.cfg.get("expiry_s", 3600))
        now = self.svc.clock.now_ms()
        scope = cfg.get("scope") or {"by": ["tool", "args_pattern"]}
        shown = self.redactor.redact_obj(args) if self.redactor else args
        await self.svc.writer.execute(
            "INSERT INTO approvals(approval_id,run_id,tool_call_id,rule_id,status,requested_at,expires_at,scope) VALUES(?,?,?,?,'pending',?,?,?)",
            (aid, run.run_id, tool_call_id, rule_id, format_ts(now), format_ts(now + ttl * 1000), json.dumps(scope)))
        await run.emit("approval.requested", {"approval_id": aid, "tool_call_id": tool_call_id, "tool": tool, "rule_id": rule_id,
                                              "scope": scope, "args": shown, "expires_at": format_ts(now + ttl * 1000)}, causation_seq=causation_seq)
        run.approval_waiters[aid] = asyncio.get_event_loop().create_future()
        self._arm(run, aid, ttl * 1000)
        return {"approval_id": aid, "status": "pending"}

    def _arm(self, run, aid: str, ms: float) -> None:
        async def expire():
            await asyncio.sleep(ms / 1000)   # real timer; the durable expires_at stays the source of truth
            await self._finalize(run, aid, "expired", "system:expiry")
        self._timers[aid] = asyncio.ensure_future(expire())

    async def expire_due(self, run) -> int:
        """Expire every pending approval whose durable expires_at has passed (timer fallback and restart path)."""
        n = 0
        for r in self.svc.store.conn().execute("SELECT * FROM approvals WHERE run_id=? AND status='pending'", (run.run_id,)).fetchall():
            if parse_ts(r["expires_at"]) <= self.svc.clock.now_ms():
                n += await self._finalize(run, r["approval_id"], "expired", "system:expiry")
        return n

    async def rearm(self, run) -> None:
        """After restart the durable expires_at is the source of truth (§23)."""
        for r in self.svc.store.conn().execute("SELECT * FROM approvals WHERE run_id=? AND status='pending'", (run.run_id,)).fetchall():
            run.approval_waiters.setdefault(r["approval_id"], asyncio.get_event_loop().create_future())
            self._arm(run, r["approval_id"], max(0, parse_ts(r["expires_at"]) - self.svc.clock.now_ms()))
        await self.expire_due(run)

    async def _finalize(self, run, aid: str, status: str, by: str) -> bool:
        n = await self.svc.writer.execute("UPDATE approvals SET status=?, decided_by=?, decided_at=? WHERE approval_id=? AND status='pending'",
                                          (status, by, format_ts(self.svc.clock.now_ms()), aid))
        if n != 1:
            return False  # already decided: replays have no further effect (exactly-once)
        await run.emit("approval.decided", {"approval_id": aid, "status": status, "decided_by": by})
        t = self._timers.pop(aid, None)
        if t and t is not asyncio.current_task():
            t.cancel()
        fut = run.approval_waiters.get(aid)
        if fut and not fut.done():
            fut.set_result(status)
        return True

    async def decide(self, run, aid: str, who: str, roles: list[str], grant: bool) -> str:
        if "approver" not in roles:
            raise ApprovalError("approvals.forbidden", "approver role required")
        if who == run.owner and not self.cfg.get("allow_owner_approval", False):
            raise ApprovalError("approvals.self_approval", "the run owner cannot approve their own run")
        row = self.svc.store.conn().execute("SELECT * FROM approvals WHERE approval_id=?", (aid,)).fetchone()
        if row is None or row["run_id"] != run.run_id:
            raise ApprovalError("approvals.not_found", aid)
        status = "granted" if grant else "denied"
        if not await self._finalize(run, aid, status, who):
            cur = self.svc.store.conn().execute("SELECT status FROM approvals WHERE approval_id=?", (aid,)).fetchone()["status"]
            raise ApprovalError("approvals.already_decided", f"approval already {cur}")
        return status

    async def wait(self, run, aid: str) -> str:
        row = self.svc.store.conn().execute("SELECT status FROM approvals WHERE approval_id=?", (aid,)).fetchone()
        if row and row["status"] != "pending":
            return row["status"]
        fut = run.approval_waiters.setdefault(aid, asyncio.get_event_loop().create_future())
        try:
            return await fut
        except asyncio.CancelledError:
            await self._finalize(run, aid, "cancelled", "system:cancel")
            return "cancelled"
