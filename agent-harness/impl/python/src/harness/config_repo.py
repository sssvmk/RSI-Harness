"""Configuration versioning in git (PRD §3.6 Configuration Versioning): control-plane policies, system instructions, context logic and tool definitions live in a git repository,
and a run is pinned to the commit it ran under. A dirty working tree is marked `+dirty`, which makes the run visibly non-comparable with any run on a clean commit."""
from __future__ import annotations
import subprocess
from pathlib import Path


class ConfigRepoError(RuntimeError):
    pass


def _git(path: str | Path, *args: str) -> str:
    r = subprocess.run(["git", *args], cwd=str(path), capture_output=True, text=True)
    if r.returncode != 0: raise ConfigRepoError(f"git {' '.join(args)}: {r.stderr.strip() or 'failed'}")
    return r.stdout.strip()


def resolve(path: str | Path) -> dict:
    """{commit, branch, dirty} of the configuration repository."""
    if not Path(path).is_dir(): raise ConfigRepoError(f"{path} is not a directory")
    try: _git(path, "rev-parse", "--git-dir")
    except ConfigRepoError: raise ConfigRepoError(f"{path} is not a git repository")
    try: commit = _git(path, "rev-parse", "--verify", "HEAD^{commit}")
    except ConfigRepoError: raise ConfigRepoError(f"{path} has no commits")
    branch = _git(path, "rev-parse", "--abbrev-ref", "HEAD")
    return {"commit": commit, "branch": branch, "dirty": bool(_git(path, "status", "--porcelain"))}


def version_of(path: str | Path) -> str:
    r = resolve(path)
    return r["commit"] + ("+dirty" if r["dirty"] else "")


def read_at(path: str | Path, commit: str, rel: str) -> str:
    """A configuration file exactly as it was at `commit` (what a pinned run used), independent of the working tree."""
    return _git(path, "show", f"{commit.split('+')[0]}:{rel}")
