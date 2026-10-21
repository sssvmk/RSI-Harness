/** Edits and EditApplier (§17.2.3, §17.6): the only way a proposal touches configuration, applied by the control plane to a candidate worktree. */
import fs from "node:fs";
import path from "node:path";
import { GitRepo } from "./gitrepo.js";
import { ComponentRegistry, tierRank } from "./registry.js";
const EDIT_KEYS = new Set(["path", "op", "search", "replace", "content"]);
export const countLines = (s?: string | null) => (!s ? 0 : s.split("\n").length);
export interface ApplyResult { ok: boolean; commit: string | null; tier: string | null; changed_lines: number; reasons: any[]; added: any[] }
const reject = (code: string, p: string | null = null, detail = "") => ({ code, path: p, detail });
export class EditApplier {
  constructor(public repo: GitRepo, public registry: ComponentRegistry, public maxLines = 200) {}
  private symlinkChain(wt: string, rel: string): boolean {
    let cur = wt;
    for (const part of rel.split("/")) { cur = path.join(cur, part); try { if (fs.lstatSync(cur).isSymbolicLink()) return true; } catch { break; } }
    return false;
  }
  apply(baseCommit: string, edits: any, tiersAllowed: string[], branch: string, message: string): ApplyResult {
    const res: ApplyResult = { ok: false, commit: null, tier: null, changed_lines: 0, reasons: [], added: [] };
    if (!Array.isArray(edits) || !edits.length) { res.reasons.push(reject("edit.invalid", null, "edits must be a non-empty list")); return res; }
    const tiers: string[] = [];
    for (const e of edits) {   // pass 1: pure checks, no filesystem writes
      if (e === null || typeof e !== "object" || Object.keys(e).some((k) => !EDIT_KEYS.has(k)) || !["replace", "create", "delete"].includes(e.op) || typeof e.path !== "string") { res.reasons.push(reject("edit.invalid", e?.path ?? null, "malformed edit")); continue; }
      const [tier, why] = this.registry.check(e.path);
      if (why) { res.reasons.push(reject(why, e.path)); continue; }
      if (!tiersAllowed.includes(tier!)) { res.reasons.push(reject("tier.exceeds_allowed", e.path, `${tier} not in ${JSON.stringify(tiersAllowed)}`)); continue; }
      tiers.push(tier!);
    }
    if (res.reasons.length) return res;
    res.tier = tiers.reduce((a, b) => (tierRank(b) > tierRank(a) ? b : a));
    const wt = this.repo.addWorktree(branch, baseCommit);   // pass 2: apply in a candidate worktree
    try {
      const realWt = fs.realpathSync(wt); let lines = 0;
      for (const e of edits) {
        const rel = path.posix.normalize(e.path), full = path.join(wt, rel), exists = fs.existsSync(full);
        const probe = exists ? full : path.dirname(full);
        let realProbe = probe; try { realProbe = fs.realpathSync(probe); } catch { /* parent missing: containment checked below */ }
        if (this.symlinkChain(wt, rel) || !(realProbe + path.sep).startsWith(realWt + path.sep) && realProbe !== realWt) { res.reasons.push(reject("path.symlink_escape", e.path)); break; }
        const before = exists && fs.statSync(full).isFile() ? fs.readFileSync(full, "utf8") : null;
        let after: string | null, added: string;
        if (e.op === "replace") {
          if (before === null || typeof e.search !== "string" || !e.search || typeof e.replace !== "string") { res.reasons.push(reject("edit.invalid", e.path, "replace needs an existing file, non-empty search and a replace string")); break; }
          const n = before.split(e.search).length - 1;
          if (n !== 1) { res.reasons.push(reject("edit.no_unique_match", e.path, `search matched ${n} times`)); break; }
          after = before.replace(e.search, () => e.replace); added = e.replace; lines += countLines(e.search) + countLines(e.replace);
        } else if (e.op === "create") {
          if (before !== null || exists) { res.reasons.push(reject("edit.exists", e.path)); break; }
          if (typeof e.content !== "string") { res.reasons.push(reject("edit.invalid", e.path, "create needs content")); break; }
          fs.mkdirSync(path.dirname(full), { recursive: true }); after = e.content; added = e.content; lines += countLines(e.content);
        } else {
          if (before === null) { res.reasons.push(reject("edit.missing", e.path)); break; }
          after = null; added = ""; lines += countLines(before);
        }
        if (lines > this.maxLines) { res.reasons.push(reject("edit.cap_exceeded", e.path, `${lines} > ${this.maxLines} changed lines`)); break; }
        if (after === null) fs.unlinkSync(full); else fs.writeFileSync(full, after, "utf8");
        res.added.push({ path: e.path, added, before, after });
      }
      res.changed_lines = lines;
      if (res.reasons.length) return res;
      res.commit = this.repo.commitAll(wt, message); res.ok = true; return res;
    } finally { this.repo.removeWorktree(branch, !res.ok); }
  }
}
