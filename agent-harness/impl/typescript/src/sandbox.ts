/** Sandbox plane: in-process executor (§10.4 method set), local backend (tier T-Dev), snapshots, egress policy, manager. */
import { spawn } from "node:child_process";
import fs from "node:fs";
import os from "node:os";
import path from "node:path";
import { canonicalize, sha256Hex, ulid } from "./util.js";
import { globMatch } from "./policy.js";
import { BlobStore } from "./blobs.js";
import net from "node:net";

const MAX_OUTPUT = 256 * 1024;
// Zero-credential sandboxing (H-4.2): sandbox processes get an allowlisted environment, never the daemon's.
export const SAFE_ENV_KEYS = ["PATH", "LANG", "LC_ALL", "LC_CTYPE", "TERM", "TZ"];
export function sandboxBaseEnv(home: string): Record<string, string> {
  const env: Record<string, string> = {};
  for (const k of SAFE_ENV_KEYS) if (process.env[k] !== undefined) env[k] = process.env[k]!;
  env.HOME = home; return env;
}
export class ExecError extends Error {
  constructor(public code: string, message: string, public category = "tool", public hint: string | null = null, public alternatives: string[] = [], public retryable = false) { super(message); }
  toDict() { return { code: this.code, category: this.category, message: this.message, retryable: this.retryable, hint: this.hint, alternatives: this.alternatives }; }
}
const real = (p: string) => fs.realpathSync(p);

export class LocalExecutor {
  roots: Record<string, string>; sessions = new Map<string, any>(); private execs = new Map<string, Promise<any>>(); private n = 0;
  live = new Set<number>();   // process groups started here and not yet finished
  constructor(roots: Record<string, string>, public users = ["agent", "candidate", "judge"], public limits: { cpu_s?: number; memory_mb?: number } = {}) { this.roots = Object.fromEntries(Object.entries(roots).map(([k, v]) => [k, real(v)])); }
  resolve(vpath: string, roots?: string[]): string {
    const allowed = roots ?? Object.keys(this.roots);
    if (!vpath.startsWith("/")) throw new ExecError("fs.path_not_absolute", `path must be absolute: ${vpath}`, "tool", "Use an absolute path under /workspace", [path.posix.join("/workspace", vpath)]);
    const norm = path.posix.normalize(vpath).replace(/(.)\/$/, "$1");
    for (const r of allowed) if (norm === r || norm.startsWith(r + "/")) {
      const joined = this.roots[r] + norm.slice(r.length);
      let rp = joined;
      try { rp = real(joined); } catch { // nonexistent target: resolve the deepest existing parent so symlinked parents are still checked
        let dir = path.dirname(joined), rest = path.basename(joined);
        while (!fs.existsSync(dir) && dir !== path.dirname(dir)) { rest = path.join(path.basename(dir), rest); dir = path.dirname(dir); }
        rp = path.join(real(dir), rest);
      }
      if (rp !== this.roots[r] && !rp.startsWith(this.roots[r] + path.sep)) throw new ExecError("fs.path_escape", `path escapes ${r}: ${vpath}`, "security");
      return rp;
    }
    throw new ExecError("fs.path_not_allowed", `path outside allowed roots ${JSON.stringify(allowed)}: ${vpath}`, "tool", "Use an absolute path under /workspace or /scratch", [path.posix.join("/workspace", path.posix.basename(norm))]);
  }
  v(realPath: string): string { for (const [k, r] of Object.entries(this.roots)) if (realPath === r || realPath.startsWith(r + path.sep)) return k + realPath.slice(r.length).split(path.sep).join("/"); return realPath; }
  fsRead(p: string, start?: number, end?: number, roots?: string[]) {
    const rp = this.resolve(p, roots);
    if (!fs.existsSync(rp) || !fs.statSync(rp).isFile()) throw new ExecError("fs.path_not_found", `not a file: ${p}`, "tool", "Check the path");
    const data = fs.readFileSync(rp); let text = data.toString("utf8");
    if (start !== undefined || end !== undefined) text = text.split("\n").slice((start ?? 1) - 1, end).join("\n");
    return { content: text, bytes: data.length };
  }
  fsWrite(p: string, content: string, roots?: string[]) { const rp = this.resolve(p, roots); fs.mkdirSync(path.dirname(rp), { recursive: true }); fs.writeFileSync(rp, content, "utf8"); return { bytes: Buffer.byteLength(content) }; }
  fsEdit(p: string, oldS: string, newS: string, roots?: string[]) {
    const rp = this.resolve(p, roots);
    if (!fs.existsSync(rp)) throw new ExecError("fs.path_not_found", `not a file: ${p}`);
    const s = fs.readFileSync(rp, "utf8"), count = oldS === "" ? 0 : s.split(oldS).length - 1;
    if (count !== 1) throw new ExecError("fs.edit_no_unique_match", `\`old\` must match exactly once (found ${count})`, "tool", "Include more surrounding context in `old`");
    fs.writeFileSync(rp, s.replace(oldS, () => newS), "utf8"); return { replaced: 1 };
  }
  fsStat(p: string) { const rp = this.resolve(p); if (!fs.existsSync(rp)) throw new ExecError("fs.path_not_found", `no such path: ${p}`); const st = fs.statSync(rp); return { size: st.size, is_dir: st.isDirectory(), mode: st.mode & 0o777 }; }
  private *walk(base: string): Generator<string> {
    const ents = fs.readdirSync(base, { withFileTypes: true }).sort((a, b) => (a.name < b.name ? -1 : 1));
    for (const e of ents) { const f = path.join(base, e.name); if (e.isDirectory()) yield* this.walk(f); else yield f; }
  }
  fsGlob(pattern: string, roots?: string[]) {
    const root = pattern.split("*")[0].replace(/\/[^/]*$/, "") || "/";
    const base = this.resolve(root !== "/" ? root : "/workspace", roots), out: string[] = [];
    const rx = new RegExp("^" + pattern.replace(/[.+^${}()|[\]\\]/g, "\\$&").replace(/\*/g, ".*").replace(/\?/g, ".") + "$", "s");
    if (fs.existsSync(base)) for (const f of this.walk(base)) { const vv = this.v(f); if (rx.test(vv)) out.push(vv); }
    return { paths: out.slice(0, 1000) };
  }
  fsGrep(pattern: string, p = "/workspace", roots?: string[], maxResults = 200) {
    const base = this.resolve(p, roots); let rx: RegExp;
    try { rx = new RegExp(pattern); } catch (e: any) { throw new ExecError("fs.bad_pattern", e.message); }
    const hits: any[] = [], files = fs.statSync(base).isFile() ? [base] : [...this.walk(base)];
    for (const f of files) {
      let txt: string; try { txt = fs.readFileSync(f, "utf8"); } catch { continue; }
      const lines = txt.split("\n");
      for (let i = 0; i < lines.length; i++) if (rx.test(lines[i])) { hits.push({ path: this.v(f), line: i + 1, text: lines[i].slice(0, 300) }); if (hits.length >= maxResults) return { matches: hits, truncated: true }; }
    }
    return { matches: hits, truncated: false };
  }
  workspaceManifest(root = "/workspace", exclude: string[] = []) {
    const base = this.resolve(root), files: any[] = [];
    for (const f of this.walk(base)) {
      const rel = path.relative(base, f).split(path.sep).join("/");
      if (exclude.some((e) => globMatch(e, rel)) || fs.lstatSync(f).isSymbolicLink()) continue;
      const data = fs.readFileSync(f); files.push({ path: rel, size: data.length, mode: fs.statSync(f).mode & 0o777, sha256: sha256Hex(data) });
    }
    return { files: files.sort((a, b) => (a.path < b.path ? -1 : a.path > b.path ? 1 : 0)) };
  }
  readReal(root: string, rel: string) { return fs.readFileSync(this.resolve(root + "/" + rel)); }
  translate(argv: string[]) { return argv.map((a) => { for (const [k, v] of Object.entries(this.roots)) if (a === k || a.startsWith(k + "/")) return v + a.slice(k.length); return a; }); }
  envForScripts() { return Object.fromEntries(Object.entries(this.roots).map(([k, v]) => ["HARNESS_" + k.replace(/^\//, "").toUpperCase(), v])); }
  private checkUser(user?: string | null) {
    if (user && !this.users.includes(user)) throw new ExecError("exec.unknown_user", `user ${JSON.stringify(user)} was not provisioned`, "security");
    return process.geteuid?.() === 0 && !!user;   // identity switching is only possible as root
  }
  /** Workflow-scoped cancellation: kill every process this sandbox still has running, so none is orphaned when the run ends. */
  killAll(): number { let n = 0; for (const pg of [...this.live]) { try { process.kill(-pg, "SIGKILL"); n++; } catch { /* gone */ } } this.live.clear(); return n; }
  private spawnProc(argv: string[], cwd: string, env: any, timeoutS: number, stdin?: string): Promise<any> {
    return new Promise((resolve) => {
      const { cpu_s: cpu, memory_mb: mem } = this.limits, wrapped = cpu || mem   // per-process CPU seconds and address-space limits (a safeguard while compute is unmetered, §7.4.4)
        ? ["sh", "-c", 'ulimit -t "$1"; ulimit -v "$2"; shift 2; exec "$@"', "sh", cpu ? String(cpu) : "unlimited", mem ? String(mem * 1024) : "unlimited", ...argv] : argv;
      const child = spawn(wrapped[0], wrapped.slice(1), { cwd, env, detached: true, stdio: ["pipe", "pipe", "pipe"] });
      if (child.pid) this.live.add(child.pid);
      let out = Buffer.alloc(0), err = Buffer.alloc(0), timedOut = false, total = [0, 0];
      child.stdout.on("data", (d) => { total[0] += d.length; if (out.length < MAX_OUTPUT) out = Buffer.concat([out, d]); });
      child.stderr.on("data", (d) => { total[1] += d.length; if (err.length < MAX_OUTPUT) err = Buffer.concat([err, d]); });
      const timer = setTimeout(() => { timedOut = true; try { process.kill(-child.pid!, "SIGKILL"); } catch { /* gone */ } }, timeoutS * 1000);
      child.on("error", (e) => { clearTimeout(timer); resolve({ exit_code: 127, timed_out: false, stdout: "", stderr: String(e), output_truncated: false }); });
      child.on("close", (code) => { clearTimeout(timer); if (child.pid) this.live.delete(child.pid); resolve({ exit_code: timedOut ? null : code, timed_out: timedOut, stdout: out.subarray(0, MAX_OUTPUT).toString("utf8"), stderr: err.subarray(0, MAX_OUTPUT).toString("utf8"), output_truncated: total[0] > MAX_OUTPUT || total[1] > MAX_OUTPUT }); });
      if (stdin !== undefined) child.stdin.end(stdin); else child.stdin.end();
    });
  }
  execStart(argv: string[], o: { cwd?: string; env?: any; user?: string | null; timeoutS?: number; stdin?: string } = {}) {
    const enforced = this.checkUser(o.user), id = `x${++this.n}`;
    const env = { ...sandboxBaseEnv(this.roots["/scratch"] ?? os.tmpdir()), ...(o.env ?? {}), ...this.envForScripts() };
    this.execs.set(id, this.spawnProc(this.translate(argv), this.resolve(o.cwd ?? "/workspace"), env, o.timeoutS ?? 120, o.stdin));
    return { exec_id: id, identity_enforced: enforced };
  }
  async execWait(id: string) { const p = this.execs.get(id)!; this.execs.delete(id); return p; }
  async execRun(argv: string[], o: any = {}) { const r = this.execStart(argv, o); const out = await this.execWait(r.exec_id); out.identity_enforced = r.identity_enforced; return out; }
  sessionOpen(o: { cwd?: string; env?: any; user?: string | null } = {}) { this.checkUser(o.user); const id = `s${++this.n}`; this.sessions.set(id, { cwd: o.cwd ?? "/workspace", env: { ...(o.env ?? {}) }, user: o.user }); return { session_id: id }; }
  async sessionRun(id: string, cmd: string, timeoutS = 120) {
    const s = this.sessions.get(id);
    if (!s) throw new ExecError("session.lost", "session no longer exists", "state", "Open a new session; shell state was lost");
    const marker = "__HARNESS_STATE__", script = `${cmd}\n__rc=$?\nprintf "\\n${marker}\\n%s\\n" "$PWD"\nenv -0 | base64 -w0\nexit $__rc`;
    const t0 = Date.now();
    const r = await this.spawnProc(["bash", "-c", script], this.resolve(s.cwd), { ...sandboxBaseEnv(this.roots["/scratch"] ?? os.tmpdir()), ...s.env }, timeoutS);
    const at = r.stdout.lastIndexOf("\n" + marker + "\n");
    if (at >= 0) {
      const tail = r.stdout.slice(at + marker.length + 2), nl = tail.indexOf("\n"), pwd = tail.slice(0, nl), envb = tail.slice(nl + 1);
      r.stdout = r.stdout.slice(0, at);
      try { const rp = real(pwd); if (Object.values(this.roots).some((x) => rp === x || rp.startsWith(x + path.sep))) s.cwd = this.v(rp); } catch { /* keep cwd */ }
      try {
        for (const kv of Buffer.from(envb, "base64").toString("utf8").split("\0")) {
          const i = kv.indexOf("="); if (i <= 0) continue;
          const k = kv.slice(0, i), val = kv.slice(i + 1);
          if (["PWD", "OLDPWD", "_", "SHLVL"].includes(k)) continue;
          if (process.env[k] !== val) s.env[k] = val;
        }
      } catch { /* ignore */ }
    }
    r.cwd = s.cwd; r.duration_ms = Date.now() - t0; return r;
  }
  sessionClose(id: string) { this.sessions.delete(id); return {}; }
}

export const TIER_RANK: Record<string, number> = { "T-Dev": 0, "T-Core": 1, "T-Std": 2, "T-High": 3 };
export const PROFILE_TIER: Record<string, string> = { core: "T-Core", standard: "T-Std", high: "T-High" };
export class SandboxHandle { paused = false; constructor(public sandbox_id: string, public role: string, public tier: string, public backend: string, public root: string, public executor: LocalExecutor, public labels: Record<string, string>) {} }
export class LocalProcessBackend {
  name = "local"; tier = "T-Dev"; hasNativeBinaries = false; base: string; live = new Map<string, SandboxHandle>();
  static DEFAULT_LIMITS = { cpu_s: 3600, memory_mb: 8192 }; limits: { cpu_s?: number; memory_mb?: number };
  constructor(base?: string, limits: { cpu_s?: number; memory_mb?: number } = {}) { this.limits = { ...LocalProcessBackend.DEFAULT_LIMITS, ...limits }; this.base = base ?? fs.mkdtempSync(path.join(os.tmpdir(), "harness-sbx-")); fs.mkdirSync(this.base, { recursive: true }); }
  create(spec: any): SandboxHandle {
    const sid = spec.sandbox_id ?? ulid(), root = path.join(this.base, sid);
    for (const d of ["workspace", "scratch"]) fs.mkdirSync(path.join(root, d), { recursive: true });
    const roots: any = { "/workspace": path.join(root, "workspace"), "/scratch": path.join(root, "scratch") };
    if (spec.role === "judge") { fs.mkdirSync(path.join(root, "tests"), { recursive: true }); fs.chmodSync(path.join(root, "tests"), 0o700); roots["/tests"] = path.join(root, "tests"); }
    const labels = { "harness.run_id": spec.run_id ?? "", "harness.role": spec.role ?? "work" };
    const h = new SandboxHandle(sid, spec.role ?? "work", this.tier, this.name, root, new LocalExecutor(roots, undefined, this.limits), labels);
    fs.writeFileSync(path.join(root, "labels.json"), JSON.stringify({ labels, role: h.role }));
    this.live.set(sid, h); return h;
  }
  start(_h: SandboxHandle) {} stop(_h: SandboxHandle) {}
  destroy(h: SandboxHandle) { h.executor.killAll(); fs.rmSync(h.root, { recursive: true, force: true }); this.live.delete(h.sandbox_id); }
  pause(h: SandboxHandle) { h.paused = true; } resume(h: SandboxHandle) { h.paused = false; }
  conformanceId() { return "local-dev-none"; }
  findByLabels(runId: string, role: string): SandboxHandle | null {
    for (const h of this.live.values()) if (h.labels["harness.run_id"] === runId && h.labels["harness.role"] === role) return h;
    for (const d of fs.existsSync(this.base) ? fs.readdirSync(this.base).sort() : []) {   // created by a previous daemon process: re-attach by label
      const lf = path.join(this.base, d, "labels.json"); if (!fs.existsSync(lf)) continue;
      const meta = JSON.parse(fs.readFileSync(lf, "utf8"));
      if (meta.labels["harness.run_id"] === runId && meta.labels["harness.role"] === role) {
        const root = path.join(this.base, d), roots: any = { "/workspace": path.join(root, "workspace"), "/scratch": path.join(root, "scratch") };
        if (fs.existsSync(path.join(root, "tests"))) roots["/tests"] = path.join(root, "tests");
        const h = new SandboxHandle(d, role, this.tier, this.name, root, new LocalExecutor(roots, undefined, this.limits), meta.labels); this.live.set(d, h); return h;
      }
    }
    return null;
  }
}

export class SnapshotIntegrityError extends Error {}
export function takeSnapshot(sb: SandboxHandle, blobs: BlobStore) {
  const manifest = sb.executor.workspaceManifest("/workspace", [".harness/tmp/*"]); let newBlobs = 0;
  for (const f of manifest.files) if (!blobs.has(f.sha256)) { blobs.put(sb.executor.readReal("/workspace", f.path)); newBlobs++; }
  const manifest_hash = blobs.put(canonicalize({ schema_version: 1, ...manifest }));
  return { manifest_hash, files: manifest.files.length, new_blobs: newBlobs };
}
export function restoreSnapshot(manifestHash: string, blobs: BlobStore, sb: SandboxHandle): number {
  const manifest = JSON.parse(blobs.get(manifestHash).toString("utf8"));
  if (sha256Hex(canonicalize(manifest)) !== manifestHash) throw new SnapshotIntegrityError("manifest hash mismatch");
  const root = sb.executor.roots["/workspace"];
  for (const f of manifest.files) {
    const data = blobs.get(f.sha256);
    if (sha256Hex(data) !== f.sha256) throw new SnapshotIntegrityError(`blob mismatch for ${f.path}`);
    const dest = path.resolve(root, f.path);
    if (!dest.startsWith(root + path.sep)) throw new SnapshotIntegrityError(`path escapes workspace: ${f.path}`);
    fs.mkdirSync(path.dirname(dest), { recursive: true }); fs.writeFileSync(dest, data); fs.chmodSync(dest, f.mode);
  }
  return manifest.files.length;
}

function ipv4(ip: string): number[] | null { const m = ip.match(/^(\d+)\.(\d+)\.(\d+)\.(\d+)$/); return m ? m.slice(1).map(Number) : null; }
export function isForbiddenIp(ip: string): boolean {
  const v4 = ipv4(ip);
  if (v4) { const [a, b] = v4; return a === 10 || a === 127 || a === 0 || (a === 172 && b >= 16 && b <= 31) || (a === 192 && b === 168) || (a === 169 && b === 254) || (a === 100 && b >= 64 && b <= 127) || a >= 224; }
  const l = ip.toLowerCase();
  return l === "::1" || l === "::" || l.startsWith("fe80") || l.startsWith("fc") || l.startsWith("fd") || l.startsWith("ff");
}
export function normalizeHost(h: string) { h = h.trim().toLowerCase().replace(/\.$/, ""); try { return new URL("http://" + h).hostname; } catch { return h; } }
export function egressDecide(host: string, port: number, grants: any[], resolved: string[] = [], method?: string) {
  const h = normalizeHost(host);
  for (const g of grants) {
    if (!globMatch(g.host_glob.toLowerCase(), h) || !g.ports.includes(port)) continue;
    if (g.methods && method && !g.methods.includes(method)) continue;
    if (resolved.some(isForbiddenIp) && !g.allow_private) return { decision: "deny", reason: "ssrf_blocked_address" };
    return { decision: "allow", reason: "grant", grant: g.host_glob };
  }
  return { decision: "deny", reason: "default_deny" };
}
void net;

export class TierError extends Error {}
export class SandboxManager {
  handles = new Map<string, SandboxHandle>();
  constructor(public backend: any, public allowDev = false) {}
  checkTier(profile: string) {
    const need = PROFILE_TIER[profile], have = this.backend.tier;
    if (have === "T-Dev" && this.allowDev && profile === "core") return;
    if (TIER_RANK[have] < TIER_RANK[need]) throw new TierError(`backend tier ${have} is weaker than ${need} required by profile ${profile}`);
  }
  async create(run: any, role: string, egressGrants: any[] = []) {
    this.checkTier(run.profile);
    const h: SandboxHandle = this.backend.create({ role, run_id: run.runId });
    this.handles.set(h.sandbox_id, h);
    await run.emit("sandbox.created", { sandbox_id: h.sandbox_id, role, backend: h.backend, tier: h.tier, image_digest: null, insecure: h.tier === "T-Dev" });
    this.backend.start(h);
    await run.emit("executor.ready", { sandbox_id: h.sandbox_id, protocol_version: 1, build_hash: "in-process" });
    await run.emit("grant.issued", { grant_id: ulid(), kind: "sandbox", spec: { sandbox_id: h.sandbox_id, role } });
    for (const g of egressGrants ?? []) await run.emit("grant.issued", { grant_id: ulid(), kind: "egress", spec: { ...g, sandbox_id: h.sandbox_id } });
    return h;
  }
  async destroy(run: any, h: SandboxHandle) {
    this.backend.stop(h); this.backend.destroy(h); this.handles.delete(h.sandbox_id);
    await run.emit("sandbox.destroyed", { sandbox_id: h.sandbox_id });
    for (const [gid, g] of Object.entries<any>(await run.proj("grants_active"))) if (g.spec?.sandbox_id === h.sandbox_id) await run.emit("grant.revoked", { grant_id: gid, kind: g.kind });
  }
  /** Idempotent: revokes every active grant, destroys remaining sandboxes, emits teardown events. */
  async teardown(run: any) {
    const grants = await run.proj("grants_active"), sbs = await run.proj("sandboxes");
    await run.emit("teardown.started", { grants: Object.keys(grants).length, sandboxes: Object.keys(sbs).length });
    for (const sid of Object.keys(sbs)) { const h = this.handles.get(sid); if (h) { this.backend.stop(h); this.backend.destroy(h); this.handles.delete(sid); } await run.emit("sandbox.destroyed", { sandbox_id: sid }); }
    for (const [gid, g] of Object.entries<any>(grants)) await run.emit("grant.revoked", { grant_id: gid, kind: g.kind });
    await run.emit("teardown.completed", { grants_revoked: Object.keys(grants).length, snapshots_deleted: 0 });
  }
}
