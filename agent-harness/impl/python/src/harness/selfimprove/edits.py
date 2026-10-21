"""Edits and EditApplier (§17.2.3, §17.6): the only way a proposal touches configuration, applied by the control plane to a candidate worktree."""
from __future__ import annotations
import os
from dataclasses import dataclass, field
from pathlib import Path
from .gitrepo import GitRepo
from .registry import ComponentRegistry, tier_rank

EDIT_KEYS = {"path", "op", "search", "replace", "content"}


def count_lines(s: str | None) -> int:
    return 0 if not s else s.count("\n") + 1


@dataclass
class ApplyResult:
    ok: bool
    commit: str | None = None
    tier: str | None = None
    changed_lines: int = 0
    reasons: list[dict] = field(default_factory=list)
    added: list[dict] = field(default_factory=list)          # [{path, added, before, after}] for the screens


class EditApplier:
    def __init__(self, repo: GitRepo, registry: ComponentRegistry, max_lines: int = 200):
        self.repo, self.registry, self.max_lines = repo, registry, max_lines

    def _reject(self, code: str, path: str | None = None, detail: str = "") -> dict:
        return {"code": code, "path": path, "detail": detail}

    def _symlink_chain(self, wt: Path, rel: str) -> bool:
        cur = wt
        for part in rel.split("/"):
            cur = cur / part
            if cur.is_symlink():
                return True
            if not cur.exists():
                break
        return False

    def apply(self, base_commit: str, edits: list[dict], tiers_allowed: list[str], branch: str, message: str) -> ApplyResult:
        res = ApplyResult(ok=False)
        if not isinstance(edits, list) or not edits:
            res.reasons.append(self._reject("edit.invalid", None, "edits must be a non-empty list")); return res
        # --- pass 1: pure checks (no filesystem writes) ---
        tiers = []
        for e in edits:
            if not isinstance(e, dict) or set(e) - EDIT_KEYS or e.get("op") not in ("replace", "create", "delete") or not isinstance(e.get("path"), str):
                res.reasons.append(self._reject("edit.invalid", e.get("path") if isinstance(e, dict) else None, "malformed edit")); continue
            tier, why = self.registry.check(e["path"])
            if why:
                res.reasons.append(self._reject(why, e["path"])); continue
            if tier not in tiers_allowed:
                res.reasons.append(self._reject("tier.exceeds_allowed", e["path"], f"{tier} not in {tiers_allowed}")); continue
            tiers.append(tier)
        if res.reasons:
            return res
        res.tier = max(tiers, key=tier_rank)
        # --- pass 2: apply in a candidate worktree ---
        wt = self.repo.add_worktree(branch, base_commit)
        try:
            real_wt = os.path.realpath(wt)
            lines = 0
            for e in edits:
                rel = os.path.normpath(e["path"]).replace(os.sep, "/")
                full = wt / rel
                probe = os.path.realpath(full.parent if not full.exists() else full)
                if self._symlink_chain(wt, rel) or not (probe == real_wt or probe.startswith(real_wt + os.sep)):
                    res.reasons.append(self._reject("path.symlink_escape", e["path"])); break
                before = full.read_text(encoding="utf-8") if full.is_file() else None
                op = e["op"]
                if op == "replace":
                    s, r = e.get("search"), e.get("replace")
                    if before is None or not isinstance(s, str) or not s or not isinstance(r, str):
                        res.reasons.append(self._reject("edit.invalid", e["path"], "replace needs an existing file, non-empty search and a replace string")); break
                    n = before.count(s)
                    if n != 1:
                        res.reasons.append(self._reject("edit.no_unique_match", e["path"], f"search matched {n} times")); break
                    after, added = before.replace(s, r, 1), r
                    lines += count_lines(s) + count_lines(r)
                elif op == "create":
                    c = e.get("content")
                    if before is not None or full.exists():
                        res.reasons.append(self._reject("edit.exists", e["path"])); break
                    if not isinstance(c, str):
                        res.reasons.append(self._reject("edit.invalid", e["path"], "create needs content")); break
                    full.parent.mkdir(parents=True, exist_ok=True)
                    after, added = c, c
                    lines += count_lines(c)
                else:
                    if before is None:
                        res.reasons.append(self._reject("edit.missing", e["path"])); break
                    after, added = None, ""
                    lines += count_lines(before)
                if lines > self.max_lines:
                    res.reasons.append(self._reject("edit.cap_exceeded", e["path"], f"{lines} > {self.max_lines} changed lines")); break
                if after is None:
                    full.unlink()
                else:
                    full.write_text(after, encoding="utf-8")
                res.added.append({"path": e["path"], "added": added, "before": before, "after": after})
            res.changed_lines = lines
            if res.reasons:
                return res
            res.commit = self.repo.commit_all(wt, message)
            res.ok = True
            return res
        finally:
            self.repo.remove_worktree(branch, delete_branch=not res.ok)
