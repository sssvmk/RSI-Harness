import fs from "node:fs";
import path from "node:path";
import { createHash } from "node:crypto";
import { describe, expect, it } from "vitest";
import { BlobStore } from "../src/blobs.js";
import { ExecError, LocalProcessBackend, SandboxManager, TierError, egressDecide, restoreSnapshot, takeSnapshot } from "../src/sandbox.js";
import { runPreflight } from "../src/preflight.js";
import { eventsOf, mk, tmpdir } from "./helpers.js";

const fresh = () => { const b = new LocalProcessBackend(path.join(tmpdir(), "sbx")), h = b.create({ role: "work", run_id: "R" }); return { b, h, ex: h.executor }; };
const code = (f: () => any) => { try { f(); } catch (e: any) { return e.code; } return null; };

describe("executor", () => {
  it("edit requires a unique match", () => {
    const { ex } = fresh(); ex.fsWrite("/workspace/a.py", "x = 1\nx = 1\ny = 2\n");
    expect(code(() => ex.fsEdit("/workspace/a.py", "x = 1", "x = 9"))).toBe("fs.edit_no_unique_match"); expect(ex.fsEdit("/workspace/a.py", "y = 2", "y = 3")).toEqual({ replaced: 1 });
    expect(ex.fsRead("/workspace/a.py").content).toContain("y = 3"); expect(code(() => ex.fsEdit("/workspace/a.py", "absent", "z"))).toBe("fs.edit_no_unique_match");
  });
  it("roots and traversal", () => {
    const { ex } = fresh(); for (const bad of ["/etc/passwd", "/workspace/../etc/passwd", "/workspacex/a", "rel/path", "/tests/x"]) expect(() => ex.fsRead(bad)).toThrow(ExecError);
    ex.fsWrite("/scratch/t.txt", "ok"); expect(ex.fsRead("/scratch/t.txt").content).toBe("ok"); expect(ex.fsGlob("/workspace/*").paths).toEqual([]);
  });
  it("grep, glob, stat", () => {
    const { ex } = fresh(); ex.fsWrite("/workspace/src/a.py", "def foo():\n    pass\n"); ex.fsWrite("/workspace/src/b.txt", "foo bar");
    expect(ex.fsGrep("foo", "/workspace").matches.map((m: any) => m.path)).toEqual(["/workspace/src/a.py", "/workspace/src/b.txt"]); expect(ex.fsGlob("/workspace/src/*.py").paths).toEqual(["/workspace/src/a.py"]); expect(ex.fsStat("/workspace/src").is_dir).toBe(true); expect(() => ex.fsGrep("(", "/workspace")).toThrow(ExecError);
  });
  it("sessions persist cwd and exports, and report loss", async () => {
    const { ex } = fresh(); ex.fsWrite("/workspace/sub/f.txt", "x"); const sid = ex.sessionOpen({ user: "agent" }).session_id;
    let r = await ex.sessionRun(sid, "cd sub && export FOO=bar && pwd"); expect([r.exit_code, r.cwd]).toEqual([0, "/workspace/sub"]);
    r = await ex.sessionRun(sid, "echo $FOO; ls"); expect(r.stdout.split(/\s+/).filter(Boolean)).toEqual(["bar", "f.txt"]); r = await ex.sessionRun(sid, "exit 3"); expect(r.exit_code).toBe(3);
    ex.sessionClose(sid); await expect(ex.sessionRun(sid, "true")).rejects.toMatchObject({ code: "session.lost" });
  });
  it("timeouts kill the process group; unprovisioned users are refused; scripts see real roots via env", async () => {
    const { ex } = fresh(); const r = await ex.execRun(["bash", "-c", "sleep 30"], { timeoutS: 1 }); expect([r.timed_out, r.exit_code]).toEqual([true, null]);
    expect(code(() => ex.execStart(["true"], { user: "root" }))).toBe("exec.unknown_user"); expect((await ex.execRun(["bash", "-c", "echo $HARNESS_WORKSPACE"], { user: "candidate" })).stdout.trim()).toBe(ex.roots["/workspace"]);
  });
});
describe("zero-credential sandboxing (H-4.2)", () => {
  it("host secrets never reach sandbox processes, sessions or interpreters, while session state persists", async () => {
    process.env.ANTHROPIC_API_KEY = "sk-ant-LEAKCHECK-0000000000000000"; process.env.AWS_SECRET_ACCESS_KEY = "LEAKCHECK-aws";
    try {
      const { ex } = fresh(); let r = await ex.execRun(["bash", "-c", "env"], { user: "candidate" });
      expect(r.stdout).not.toContain("LEAKCHECK"); expect(r.stdout).toContain("PATH="); expect(r.stdout).toContain("HARNESS_WORKSPACE=");
      const sid = ex.sessionOpen({ user: "agent" }).session_id; r = await ex.sessionRun(sid, "export FOO=bar; env; echo key=$ANTHROPIC_API_KEY");
      expect(r.stdout).not.toContain("LEAKCHECK"); expect(r.stdout).toContain("key=\n"); expect((await ex.sessionRun(sid, "echo $FOO")).stdout.trim()).toBe("bar");
      expect((await ex.execRun(["python3", "-c", "import os;print(any('LEAKCHECK' in v for v in os.environ.values()))"])).stdout.trim()).toBe("False");
    } finally { delete process.env.ANTHROPIC_API_KEY; delete process.env.AWS_SECRET_ACCESS_KEY; }
  });
});
describe("snapshots", () => {
  it("round trip is content-addressed, deduplicated and deterministic", () => {
    const { b, h, ex } = fresh(), blobs = new BlobStore(tmpdir()); ex.fsWrite("/workspace/a.txt", "alpha"); ex.fsWrite("/workspace/d/b.txt", "beta");
    const s1 = takeSnapshot(h, blobs); expect([s1.files, s1.new_blobs]).toEqual([2, 2]); ex.fsWrite("/workspace/c.txt", "alpha");
    const s2 = takeSnapshot(h, blobs); expect(s2.new_blobs).toBe(0); expect(s2.manifest_hash).not.toBe(s1.manifest_hash); expect(takeSnapshot(h, blobs).manifest_hash).toBe(s2.manifest_hash);
    const h2 = b.create({ role: "judge", run_id: "R" }); expect(restoreSnapshot(s1.manifest_hash, blobs, h2)).toBe(2); expect(h2.executor.fsRead("/workspace/d/b.txt").content).toBe("beta"); expect(fs.existsSync(h2.executor.roots["/workspace"] + "/c.txt")).toBe(false);
  });
  it("restore detects blob tampering", () => {
    const { b, h, ex } = fresh(), blobs = new BlobStore(tmpdir()); ex.fsWrite("/workspace/a.txt", "alpha"); const s = takeSnapshot(h, blobs);
    fs.writeFileSync(blobs.p(createHash("sha256").update("alpha").digest("hex")), "EVIL"); expect(() => restoreSnapshot(s.manifest_hash, blobs, b.create({ role: "judge", run_id: "R" }))).toThrow(/integrity/);
  });
});
describe("egress and tiers", () => {
  it("default deny, normalization and SSRF defence", () => {
    const g = [{ host_glob: "*.pypi.org", ports: [443] }, { host_glob: "internal.corp", ports: [443], allow_private: true }];
    expect(egressDecide("files.pypi.org", 443, g, ["151.101.1.1"]).decision).toBe("allow"); expect(egressDecide("files.pypi.org", 80, g).reason).toBe("default_deny"); expect(egressDecide("evil.com", 443, g).reason).toBe("default_deny");
    expect(egressDecide("FILES.PyPI.org.", 443, g, ["151.101.1.1"]).decision).toBe("allow");
    for (const ip of ["10.0.0.5", "192.168.1.1", "127.0.0.1", "169.254.169.254", "::1", "fd00:ec2::254"]) expect(egressDecide("files.pypi.org", 443, g, [ip]).reason, ip).toBe("ssrf_blocked_address");
    expect(egressDecide("internal.corp", 443, g, ["10.1.2.3"]).decision).toBe("allow");
  });
  it("tier enforcement refuses a weak backend", () => {
    const m = new SandboxManager(new LocalProcessBackend(tmpdir()), true); m.checkTier("core"); for (const p of ["standard", "high"]) expect(() => m.checkTier(p)).toThrow(TierError); expect(() => new SandboxManager(new LocalProcessBackend(tmpdir())).checkTier("core")).toThrow(TierError);
  });
  it("a Standard run on the dev backend is blocked by preflight PF-02", async () => {
    const h = mk([], { profile: "standard", tier: "T-Dev" }), { run } = await h.createRun("u", "x");
    expect(eventsOf(h, run, "run.terminated")[0].payload.detail).toContain("PF-02"); expect(await run.state()).toBe("TERMINATED");
  });
  it("teardown is idempotent and revokes every grant", async () => {
    const h = mk([], { egress: { default: "deny", grants: [{ host_glob: "pypi.org", ports: [443] }] } }), { run } = await h.createRun("u", "x");
    expect(Object.keys(await run.proj("grants_active"))).toHaveLength(2); await h.sandboxes.teardown(run); await h.sandboxes.teardown(run);
    expect(await run.proj("grants_active")).toEqual({}); expect(await run.proj("sandboxes")).toEqual({}); expect(eventsOf(h, run, "grant.revoked")).toHaveLength(2);
  });
});
describe("preflight", () => {
  const pf = (profile: string, cfg: any = {}, o: any = {}) => Object.fromEntries(runPreflight(profile, cfg, o).checks.map((c: any) => [c.id, c]));
  it("individual checks", () => {
    const h = mk([]), base = h.config, tmp = tmpdir();
    expect(pf("bogus")["PF-01"].ok).toBe(false); expect(pf("core", { ...base, egress: { default: "allow" } })["PF-03"].ok).toBe(false); expect(pf("core", { ...base, egress: { default: "deny", grants: [{ host_glob: "x", ports: ["443"] }] } })["PF-03"].ok).toBe(false);
    expect(pf("standard", { budgets: { turns: 1, tokens: 1 } })["PF-04"].ok).toBe(false); expect(pf("core", { budgets: { turns: 1, tokens: 1, time_s: 1 } })["PF-04"].ok).toBe(true);
    const leak = pf("core", base, { knownSecrets: ["s3cr3t-value"], sandboxEnvs: [{ A: "ok" }, { B: "x s3cr3t-value y" }] }); expect(leak["PF-05"].ok).toBe(false); expect(leak["PF-05"].message).toContain("#1");
    const exe = path.join(tmp, "exec.bin"); fs.writeFileSync(exe, "binary"); const cfg = { ...base, sandbox: { ...base.sandbox, executor_sha256: createHash("sha256").update("binary").digest("hex") } };
    expect(pf("core", cfg, { executorPath: exe })["PF-06"].ok).toBe(true); fs.writeFileSync(exe, "tampered"); expect(pf("core", cfg, { executorPath: exe })["PF-06"].ok).toBe(false);
    expect(pf("core", base, { ruleset: { schema_version: 1, rules: [{ id: "a", action: "allow" }, { id: "a", action: "deny" }] } })["PF-07"].ok).toBe(false); expect(pf("core", base, { ruleset: { schema_version: 1, rules: [{ id: "a", action: "maybe" }] } })["PF-07"].ok).toBe(false);
    expect(pf("core", base, { ruleset: { schema_version: 1, rules: [{ id: "a", action: "deny", match: { args: [{ path: "$.x", op: "regex", value: "(?=x)" }] } }] } })["PF-07"].ok).toBe(false);
    const reg = { warnings: [1] }; expect(pf("standard", base, { registry: reg })["PF-08"].ok).toBe(false); expect(pf("core", base, { registry: reg })["PF-08"].severity).toBe("warn");
    expect(pf("core", { mcp: { servers: [{ tools: { t: "short" } }] } })["PF-09"].ok).toBe(false); expect(pf("standard", { approvals: { channels: ["cli"], approvers: [] } })["PF-10"].ok).toBe(false); expect(pf("core", base, { store: h.store })["PF-11"].ok).toBe(true);
    expect(pf("standard", { evaluator: { independence: "same_model_low_independence" } })["PF-12"].ok).toBe(false); expect(pf("core", { evaluator: { independence: "same_model_low_independence" } })["PF-12"].ok).toBe(true);
    expect(pf("core", {}, { workRoots: ["/sbx/work"], hiddenTestsDirs: ["/sbx/work/tests"] })["PF-13"].ok).toBe(false); expect(pf("standard", { cache: { semantic: "on" } })["PF-14"].ok).toBe(false);
    expect(pf("standard", {})["PF-15"].ok).toBe(false); expect(pf("standard", {})["PF-16"].ok).toBe(false); expect(pf("standard", { retention: { events_days: 1, blobs_days: 1 }, auth: { oidc: { issuer: "i", audience: "a" } } })["PF-16"].ok).toBe(true);
  });
});
