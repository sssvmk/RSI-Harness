"""Declared test visibility (design §13.1, PRD §3.6).

The task declares what its tests are for: `purpose`, whether they may serve as held-out data, and whether their results may support a generalization claim. That is *policy*. What the environment actually
does (are public tests mounted? are they read-only?) is *runtime state*, recorded separately from what was verified, and the two are never confused. Public-test results never authorize a generalization claim."""
from __future__ import annotations
import hashlib, os
from pathlib import Path

PURPOSES = ("development_only", "evaluation")


def declaration_for(spec: dict) -> dict:
    """The task's declaration. A task that predates the full form gets one derived conservatively from `visibility` and `purpose`: no held-out use is claimed unless the task confirms on a held-out set."""
    if spec.get("test_visibility") is not None:
        d = spec["test_visibility"]; return {"purpose": d["purpose"], "held_out_use": d["held_out_use"], "generalization_claim": d["generalization_claim"], "public_tests": {"mount_mode": "read_only"}}
    public = spec.get("visibility", "hidden") == "public"
    return {"purpose": "development_only" if public else "evaluation", "held_out_use": (not public) and spec.get("confirmation", {}).get("mode") == "heldout_set", "generalization_claim": False, "public_tests": {"mount_mode": "read_only"}}


def validate_declaration(spec: dict) -> list[str]:
    """Contradictory declarations are rejected by `harness task validate` and again at run start."""
    d, public, errs = declaration_for(spec), spec.get("visibility", "hidden") == "public", []
    if d["purpose"] == "development_only" and d["held_out_use"]: errs.append("test_visibility: development_only tests cannot be used as held-out data (held_out_use must be false)")
    if public and d["held_out_use"]: errs.append("test_visibility: public tests cannot be used as held-out data")
    if public and d["generalization_claim"]: errs.append("test_visibility: public-test results cannot authorize a generalization claim (generalization_claim must be false)")
    if public and d["purpose"] != "development_only": errs.append("test_visibility: public tests are development aids and must declare purpose development_only")
    if not public and d["purpose"] == "development_only" and spec.get("test_visibility") is not None and d["generalization_claim"]: errs.append("test_visibility: development_only tests cannot support a generalization claim")
    return errs


def tests_hash(tests_dir: Path) -> str:
    """The grader's identity: every file under tests/, by relative path and content."""
    h = hashlib.sha256()
    for p in sorted(Path(tests_dir).rglob("*")):
        if p.is_file(): h.update(str(p.relative_to(tests_dir)).encode() + b"\0" + hashlib.sha256(p.read_bytes()).digest())
    return h.hexdigest()


def public_files(tests_dir: Path) -> list[Path]:
    """What a public task shows the agent: its tests, but never the held-out confirmation set."""
    return [p for p in sorted(Path(tests_dir).rglob("*")) if p.is_file() and p.relative_to(tests_dir).parts[0] != "confirm"]


def mount_public_tests(tests_dir: Path, executor) -> dict:
    """Mount a public task's tests into the agent workspace, read-only, and verify what happened. Returns the verified runtime state, not the intent.
    On the development tier read-only is a file permission, which the agent's own identity can change, so `read_only_enforced` is false there and that is said plainly."""
    ids = []
    for p in public_files(tests_dir):
        rel = str(p.relative_to(tests_dir)); vpath = f"/workspace/tests/{rel}"; executor.fs_write(vpath, p.read_text()); real = executor.resolve(vpath); os.chmod(real, 0o444); ids.append(rel)
    mounted = bool(ids) and all(os.path.isfile(executor.resolve(f"/workspace/tests/{i}")) and (os.stat(executor.resolve(f"/workspace/tests/{i}")).st_mode & 0o222) == 0 for i in ids)   # the permission bits, which hold even for a privileged process
    return {"available": bool(ids), "mounted_in_agent_workspace": mounted, "mount_mode": "read_only", "read_only_enforced": False, "test_ids": ids}


def hidden_state() -> dict:
    return {"available": False, "mounted_in_agent_workspace": False, "mount_mode": "read_only", "read_only_enforced": False, "test_ids": []}
