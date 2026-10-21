"""Verifiers produce the evidence required for COMPLETED (§6.3, H-3.3-evidence-based-completion)."""
from __future__ import annotations
from typing import Awaitable, Callable


class Verifier:
    async def verify(self, run, sandbox) -> dict:  # -> {passed, detail, evidence}
        raise NotImplementedError


class CommandVerifier(Verifier):
    """Task-declared verifier: a command that must exit 0, run in the work sandbox."""

    def __init__(self, argv: list[str], cwd: str = "/workspace", timeout_s: int = 300, user: str = "judge"):
        self.argv, self.cwd, self.timeout_s, self.user = argv, cwd, timeout_s, user

    async def verify(self, run, sandbox) -> dict:
        r = await sandbox.executor.exec_run(self.argv, cwd=self.cwd, timeout_s=self.timeout_s, user=self.user)
        return {"passed": r["exit_code"] == 0 and not r["timed_out"], "detail": f"exit {r['exit_code']}",
                "evidence": {"argv": self.argv, "exit_code": r["exit_code"], "stdout": r["stdout"][-2000:], "stderr": r["stderr"][-2000:]}}


class CallableVerifier(Verifier):
    def __init__(self, fn: Callable[..., Awaitable[dict]]):
        self.fn = fn

    async def verify(self, run, sandbox) -> dict:
        return await self.fn(run, sandbox)
