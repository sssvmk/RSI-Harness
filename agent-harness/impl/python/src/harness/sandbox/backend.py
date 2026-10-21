"""SandboxBackend interface (§10.1) and the LocalProcessBackend (tier T-Dev: NO isolation; development only)."""
from __future__ import annotations
import json, shutil, tempfile, os
from dataclasses import dataclass, field
from pathlib import Path
from ..ids import ulid
from .executor import LocalExecutor

TIER_RANK = {"T-Dev": 0, "T-Core": 1, "T-Std": 2, "T-High": 3}
PROFILE_TIER = {"core": "T-Core", "standard": "T-Std", "high": "T-High"}


@dataclass
class SandboxHandle:
    sandbox_id: str
    role: str
    tier: str
    backend: str
    root: Path
    executor: LocalExecutor
    paused: bool = False
    labels: dict = field(default_factory=dict)


class SandboxBackend:
    name = "abstract"
    tier = "T-Dev"
    has_native_binaries = True

    def create(self, spec: dict) -> SandboxHandle: raise NotImplementedError
    def start(self, h): ...
    def stop(self, h): ...
    def destroy(self, h): raise NotImplementedError
    def pause(self, h): h.paused = True
    def resume(self, h): h.paused = False
    def stats(self, h) -> dict: return {"cpu_s": 0.0, "mem_peak": 0}
    def conformance_id(self) -> str: raise NotImplementedError


class LocalProcessBackend(SandboxBackend):
    name = "local"
    tier = "T-Dev"
    has_native_binaries = False

    DEFAULT_LIMITS = {"cpu_s": 3600, "memory_mb": 8192}

    def __init__(self, base_dir: str | None = None, limits: dict | None = None):
        self.limits = {**self.DEFAULT_LIMITS, **(limits or {})}        # a value of 0 turns that limit off
        self.base = Path(base_dir or tempfile.mkdtemp(prefix="harness-sbx-"))
        self.live: dict[str, SandboxHandle] = {}

    def create(self, spec: dict) -> SandboxHandle:
        sid = spec.get("sandbox_id") or ulid()
        root = self.base / sid
        for d in ("workspace", "scratch"):
            (root / d).mkdir(parents=True, exist_ok=True)
        roots = {"/workspace": str(root / "workspace"), "/scratch": str(root / "scratch")}
        if spec.get("role") == "judge":
            (root / "tests").mkdir(exist_ok=True)
            os.chmod(root / "tests", 0o700)
            roots["/tests"] = str(root / "tests")
        h = SandboxHandle(sid, spec.get("role", "work"), self.tier, self.name, root, LocalExecutor(roots, limits=self.limits),
                          labels={"harness.run_id": spec.get("run_id", ""), "harness.role": spec.get("role", "work")})
        (root / "labels.json").write_text(json.dumps({"labels": h.labels, "role": h.role}))   # lets a restarted daemon re-attach by label
        self.live[sid] = h
        return h

    def destroy(self, h: SandboxHandle) -> None:
        if hasattr(h.executor, "kill_all"): h.executor.kill_all()          # nothing the run started outlives it
        shutil.rmtree(h.root, ignore_errors=True)
        self.live.pop(h.sandbox_id, None)

    def find_by_labels(self, run_id: str, role: str) -> SandboxHandle | None:
        for h in self.live.values():
            if h.labels.get("harness.run_id") == run_id and h.labels.get("harness.role") == role:
                return h
        for d in sorted(self.base.glob("*/labels.json")):  # not in memory: a previous daemon process created it
            meta = json.loads(d.read_text())
            if meta["labels"].get("harness.run_id") == run_id and meta["labels"].get("harness.role") == role:
                root = d.parent
                roots = {"/workspace": str(root / "workspace"), "/scratch": str(root / "scratch")}
                if (root / "tests").exists():
                    roots["/tests"] = str(root / "tests")
                h = SandboxHandle(root.name, role, self.tier, self.name, root, LocalExecutor(roots, limits=self.limits), labels=meta["labels"])
                self.live[h.sandbox_id] = h
                return h
        return None

    def conformance_id(self) -> str:
        return "local-dev-none"
