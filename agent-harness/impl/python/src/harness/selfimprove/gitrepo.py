"""Thin wrapper over the pinned `git` CLI (decision P10). Deterministic: fixed identity and dates, so identical content on an
identical parent yields an identical commit id in every implementation."""
from __future__ import annotations
import hashlib, os, shutil, subprocess, tempfile
from pathlib import Path

FIXED_DATE = "2026-01-01T00:00:00 +0000"


class GitError(Exception):
    pass


class GitRepo:
    def __init__(self, path: str | Path):
        self.path = Path(path)

    def _env(self, extra: dict | None = None) -> dict:
        env = {"PATH": os.environ.get("PATH", "/usr/bin:/bin"), "HOME": tempfile.gettempdir(), "GIT_CONFIG_NOSYSTEM": "1", "GIT_CONFIG_GLOBAL": "/dev/null",
               "GIT_AUTHOR_NAME": "harness", "GIT_AUTHOR_EMAIL": "harness@localhost", "GIT_COMMITTER_NAME": "harness", "GIT_COMMITTER_EMAIL": "harness@localhost",
               "GIT_AUTHOR_DATE": FIXED_DATE, "GIT_COMMITTER_DATE": FIXED_DATE, "LC_ALL": "C"}
        env.update(extra or {})
        return env

    def git(self, *args: str, cwd: str | Path | None = None, check: bool = True, env: dict | None = None) -> str:
        r = subprocess.run(["git", "-c", "commit.gpgsign=false", "-c", "core.autocrlf=false", "-c", "core.fileMode=true", *args], cwd=str(cwd or self.path),
                           env=self._env(env), capture_output=True, text=True)
        if check and r.returncode != 0:
            raise GitError(f"git {' '.join(args)}: {r.stderr.strip()}")
        return r.stdout.strip() if check else (r.stdout.strip() if r.returncode == 0 else "")

    def ok(self, *args: str, cwd=None) -> bool:
        r = subprocess.run(["git", *args], cwd=str(cwd or self.path), env=self._env(), capture_output=True, text=True)
        return r.returncode == 0

    @classmethod
    def init(cls, path: str | Path, files: dict[str, str], message: str = "initial") -> "GitRepo":
        p = Path(path); p.mkdir(parents=True, exist_ok=True)
        repo = cls(p)
        repo.git("init", "-q", "-b", "main")
        for rel, content in files.items():
            f = p / rel; f.parent.mkdir(parents=True, exist_ok=True); f.write_text(content, encoding="utf-8")
        repo.git("add", "-A"); repo.git("commit", "-q", "--allow-empty", "-m", message)
        return repo

    def head(self, ref: str = "main") -> str:
        return self.git("rev-parse", "--verify", f"{ref}^{{commit}}")

    def exists(self, ref: str) -> bool:
        return self.ok("rev-parse", "--verify", "-q", f"{ref}^{{commit}}")

    def is_ancestor(self, a: str, b: str) -> bool:
        return self.ok("merge-base", "--is-ancestor", a, b)

    def tree_hash(self, ref: str = "main") -> str:
        return self.git("rev-parse", f"{ref}^{{tree}}")

    def worktree_hash(self) -> str:
        """Tree hash of the *working directory* (including uncommitted changes), computed in a throw-away index."""
        idx = tempfile.mktemp(prefix="idx-")
        try:
            self.git("add", "-A", env={"GIT_INDEX_FILE": idx})
            return self.git("write-tree", env={"GIT_INDEX_FILE": idx})
        finally:
            if os.path.exists(idx):
                os.unlink(idx)

    def show_file(self, ref: str, path: str) -> str | None:
        r = subprocess.run(["git", "show", f"{ref}:{path}"], cwd=str(self.path), env=self._env(), capture_output=True, text=True)
        return r.stdout if r.returncode == 0 else None

    def diff_names(self, a: str, b: str) -> list[str]:
        out = self.git("diff", "--name-only", a, b)
        return sorted(out.split("\n")) if out else []

    def changed_in(self, commit: str) -> list[str]:
        out = self.git("diff-tree", "--no-commit-id", "--name-only", "-r", "--root", commit)
        return sorted(out.split("\n")) if out else []

    def worktree_path(self, branch: str) -> Path:
        return self.path.parent / (self.path.name + "-wt") / branch.replace("/", "_")

    def add_worktree(self, branch: str, base: str) -> Path:
        wt = self.worktree_path(branch)
        self.remove_worktree(branch)                              # idempotent: a crashed attempt may have left it behind
        wt.parent.mkdir(parents=True, exist_ok=True)
        self.git("worktree", "add", "-q", "-B", branch, str(wt), base)
        return wt

    def remove_worktree(self, branch: str, delete_branch: bool = False) -> None:
        wt = self.worktree_path(branch)
        if wt.exists():
            self.git("worktree", "remove", "--force", str(wt), check=False)
            shutil.rmtree(wt, ignore_errors=True)
        self.git("worktree", "prune", check=False)
        if delete_branch and self.exists(f"refs/heads/{branch}"):
            self.git("branch", "-D", branch, check=False)

    def commit_all(self, wt: Path, message: str) -> str:
        self.git("add", "-A", cwd=wt)
        self.git("commit", "-q", "-m", message, cwd=wt)
        return self.git("rev-parse", "HEAD", cwd=wt)

    def ff_merge(self, commit: str) -> str:
        """Fast-forward `main` to `commit` (only legal if main is an ancestor of it)."""
        cur = self.git("rev-parse", "--abbrev-ref", "HEAD")
        if cur != "main":
            self.git("checkout", "-q", "main")
        self.git("merge", "--ff-only", "-q", commit)
        return self.head("main")

    def tag(self, name: str, ref: str) -> None:
        if not self.ok("rev-parse", "-q", "--verify", f"refs/tags/{name}"):
            self.git("tag", name, ref)

    def tag_exists(self, name: str) -> bool:
        return self.ok("rev-parse", "-q", "--verify", f"refs/tags/{name}")

    def revert(self, commit: str) -> str:
        self.git("revert", "--no-edit", commit)
        return self.head("main")
