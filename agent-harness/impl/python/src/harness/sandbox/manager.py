"""SandboxManager (§3.2, §10): tier enforcement, lifecycle events, grants, idempotent teardown."""
from __future__ import annotations
from ..ids import ulid
from .backend import PROFILE_TIER, TIER_RANK, SandboxBackend


class TierError(Exception):
    pass


class SandboxManager:
    def __init__(self, backend: SandboxBackend, allow_dev_tier: bool = False):
        self.backend, self.allow_dev = backend, allow_dev_tier
        self.handles: dict[str, object] = {}

    def check_tier(self, profile: str) -> None:
        need = PROFILE_TIER[profile]
        have = self.backend.tier
        if have == "T-Dev" and self.allow_dev and profile == "core":
            return  # explicitly allowed insecure dev mode, recorded as insecure
        if TIER_RANK[have] < TIER_RANK[need]:
            raise TierError(f"backend tier {have} is weaker than {need} required by profile {profile}")

    async def create(self, run, role: str, egress_grants: list[dict] | None = None):
        self.check_tier(run.profile)
        h = self.backend.create({"role": role, "run_id": run.run_id})
        self.handles[h.sandbox_id] = h
        await run.emit("sandbox.created", {"sandbox_id": h.sandbox_id, "role": role, "backend": h.backend, "tier": h.tier,
                                          "image_digest": None, "insecure": h.tier == "T-Dev"})
        self.backend.start(h)
        await run.emit("executor.ready", {"sandbox_id": h.sandbox_id, "protocol_version": 1, "build_hash": "in-process"})
        gid = ulid()
        await run.emit("grant.issued", {"grant_id": gid, "kind": "sandbox", "spec": {"sandbox_id": h.sandbox_id, "role": role}})
        for g in egress_grants or []:
            await run.emit("grant.issued", {"grant_id": ulid(), "kind": "egress", "spec": {**g, "sandbox_id": h.sandbox_id}})
        return h

    async def destroy(self, run, h) -> None:
        self.backend.stop(h)
        self.backend.destroy(h)
        self.handles.pop(h.sandbox_id, None)
        await run.emit("sandbox.destroyed", {"sandbox_id": h.sandbox_id})
        for gid, g in (await run.proj("grants_active")).items():
            if (g.get("spec") or {}).get("sandbox_id") == h.sandbox_id:
                await run.emit("grant.revoked", {"grant_id": gid, "kind": g["kind"]})

    async def teardown(self, run) -> None:
        """Idempotent: revokes every active grant, destroys remaining sandboxes, emits teardown events."""
        grants = await run.proj("grants_active")
        sandboxes = await run.proj("sandboxes")
        await run.emit("teardown.started", {"grants": len(grants), "sandboxes": len(sandboxes)})
        for sid in list(sandboxes):
            h = self.handles.get(sid)
            if h is not None:
                self.backend.stop(h); self.backend.destroy(h); self.handles.pop(sid, None)
            await run.emit("sandbox.destroyed", {"sandbox_id": sid})
        for gid, g in grants.items():
            await run.emit("grant.revoked", {"grant_id": gid, "kind": g["kind"]})
        await run.emit("teardown.completed", {"grants_revoked": len(grants), "snapshots_deleted": 0})
