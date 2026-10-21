/** Configuration versioning in git (PRD §3.6): a run is pinned to the commit it ran under; a dirty tree is marked `+dirty`, so the run is visibly non-comparable with a clean one. */
import { spawnSync } from "node:child_process";
import fs from "node:fs";
export class ConfigRepoError extends Error {}
function git(p: string, ...args: string[]): string { const r = spawnSync("git", args, { cwd: p, encoding: "utf8" }); if (r.status !== 0) throw new ConfigRepoError(`git ${args.join(" ")}: ${(r.stderr ?? "").trim() || "failed"}`); return (r.stdout ?? "").trim(); }
export function resolve(p: string): { commit: string; branch: string; dirty: boolean } {
  if (!fs.existsSync(p) || !fs.statSync(p).isDirectory()) throw new ConfigRepoError(`${p} is not a directory`);
  try { git(p, "rev-parse", "--git-dir"); } catch { throw new ConfigRepoError(`${p} is not a git repository`); }
  let commit: string; try { commit = git(p, "rev-parse", "--verify", "HEAD^{commit}"); } catch { throw new ConfigRepoError(`${p} has no commits`); }
  return { commit, branch: git(p, "rev-parse", "--abbrev-ref", "HEAD"), dirty: git(p, "status", "--porcelain").length > 0 };
}
export function versionOf(p: string): string { const r = resolve(p); return r.commit + (r.dirty ? "+dirty" : ""); }
/** A configuration file exactly as it was at `commit` (what a pinned run used), independent of the working tree. */
export const readAt = (p: string, commit: string, rel: string): string => git(p, "show", `${commit.split("+")[0]}:${rel}`);
