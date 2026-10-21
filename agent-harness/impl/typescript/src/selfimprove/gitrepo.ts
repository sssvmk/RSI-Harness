/** Thin wrapper over the pinned `git` CLI (decision P10). Deterministic: fixed identity and dates, so identical content on an identical parent
 *  yields an identical commit id in every implementation. */
import { spawnSync } from "node:child_process";
import fs from "node:fs";
import os from "node:os";
import path from "node:path";
export const FIXED_DATE = "2026-01-01T00:00:00 +0000";
export class GitError extends Error {}
export class GitRepo {
  path: string;
  constructor(p: string) { this.path = p; }
  private env(extra: Record<string, string> = {}) {
    return { PATH: process.env.PATH ?? "/usr/bin:/bin", HOME: os.tmpdir(), GIT_CONFIG_NOSYSTEM: "1", GIT_CONFIG_GLOBAL: "/dev/null", GIT_AUTHOR_NAME: "harness", GIT_AUTHOR_EMAIL: "harness@localhost",
      GIT_COMMITTER_NAME: "harness", GIT_COMMITTER_EMAIL: "harness@localhost", GIT_AUTHOR_DATE: FIXED_DATE, GIT_COMMITTER_DATE: FIXED_DATE, LC_ALL: "C", ...extra };
  }
  git(args: string[], o: { cwd?: string; check?: boolean; env?: Record<string, string> } = {}): string {
    const r = spawnSync("git", ["-c", "commit.gpgsign=false", "-c", "core.autocrlf=false", "-c", "core.fileMode=true", ...args], { cwd: o.cwd ?? this.path, env: this.env(o.env), encoding: "utf8" });
    const check = o.check ?? true;
    if (check && r.status !== 0) throw new GitError(`git ${args.join(" ")}: ${(r.stderr ?? "").trim()}`);
    return r.status === 0 ? (r.stdout ?? "").trim() : "";
  }
  ok(args: string[], cwd?: string): boolean { return spawnSync("git", args, { cwd: cwd ?? this.path, env: this.env(), encoding: "utf8" }).status === 0; }
  static init(p: string, files: Record<string, string>, message = "initial"): GitRepo {
    fs.mkdirSync(p, { recursive: true }); const repo = new GitRepo(p);
    repo.git(["init", "-q", "-b", "main"]);
    for (const [rel, content] of Object.entries(files)) { const f = path.join(p, rel); fs.mkdirSync(path.dirname(f), { recursive: true }); fs.writeFileSync(f, content, "utf8"); }
    repo.git(["add", "-A"]); repo.git(["commit", "-q", "--allow-empty", "-m", message]); return repo;
  }
  head(ref = "main") { return this.git(["rev-parse", "--verify", `${ref}^{commit}`]); }
  exists(ref: string) { return this.ok(["rev-parse", "--verify", "-q", `${ref}^{commit}`]); }
  isAncestor(a: string, b: string) { return this.ok(["merge-base", "--is-ancestor", a, b]); }
  treeHash(ref = "main") { return this.git(["rev-parse", `${ref}^{tree}`]); }
  /** Tree hash of the *working directory* (including uncommitted changes), computed in a throw-away index. */
  worktreeHash(): string {
    const idx = path.join(os.tmpdir(), `idx-${process.pid}-${Math.random().toString(36).slice(2)}`);
    try { this.git(["add", "-A"], { env: { GIT_INDEX_FILE: idx } }); return this.git(["write-tree"], { env: { GIT_INDEX_FILE: idx } }); }
    finally { if (fs.existsSync(idx)) fs.unlinkSync(idx); }
  }
  showFile(ref: string, p: string): string | null { const r = spawnSync("git", ["show", `${ref}:${p}`], { cwd: this.path, env: this.env(), encoding: "utf8" }); return r.status === 0 ? r.stdout : null; }
  changedIn(commit: string): string[] { const out = this.git(["diff-tree", "--no-commit-id", "--name-only", "-r", "--root", commit]); return out ? out.split("\n").sort() : []; }
  worktreePath(branch: string) { return path.join(path.dirname(this.path), path.basename(this.path) + "-wt", branch.replaceAll("/", "_")); }
  addWorktree(branch: string, base: string): string {
    const wt = this.worktreePath(branch); this.removeWorktree(branch);   // idempotent: a crashed attempt may have left it behind
    fs.mkdirSync(path.dirname(wt), { recursive: true }); this.git(["worktree", "add", "-q", "-B", branch, wt, base]); return wt;
  }
  removeWorktree(branch: string, deleteBranch = false) {
    const wt = this.worktreePath(branch);
    if (fs.existsSync(wt)) { this.git(["worktree", "remove", "--force", wt], { check: false }); fs.rmSync(wt, { recursive: true, force: true }); }
    this.git(["worktree", "prune"], { check: false });
    if (deleteBranch && this.exists(`refs/heads/${branch}`)) this.git(["branch", "-D", branch], { check: false });
  }
  commitAll(wt: string, message: string): string { this.git(["add", "-A"], { cwd: wt }); this.git(["commit", "-q", "-m", message], { cwd: wt }); return this.git(["rev-parse", "HEAD"], { cwd: wt }); }
  ffMerge(commit: string): string {
    if (this.git(["rev-parse", "--abbrev-ref", "HEAD"]) !== "main") this.git(["checkout", "-q", "main"]);
    this.git(["merge", "--ff-only", "-q", commit]); return this.head("main");
  }
  tag(name: string, ref: string) { if (!this.ok(["rev-parse", "-q", "--verify", `refs/tags/${name}`])) this.git(["tag", name, ref]); }
  tagExists(name: string) { return this.ok(["rev-parse", "-q", "--verify", `refs/tags/${name}`]); }
  revert(commit: string): string { this.git(["revert", "--no-edit", commit]); return this.head("main"); }
}
