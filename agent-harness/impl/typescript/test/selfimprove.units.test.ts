import fs from "node:fs";
import path from "node:path";
import { describe, expect, it } from "vitest";
import * as G from "../src/selfimprove/gate.js";
import * as S from "../src/selfimprove/search.js";
import { BlobStore } from "../src/blobs.js";
import { EditApplier } from "../src/selfimprove/edits.js";
import { EvalSetError, EvalSetRegistry, evalSeed, jaccard, shingleSet } from "../src/selfimprove/evalsets.js";
import { GitRepo } from "../src/selfimprove/gitrepo.js";
import { ComponentRegistry, RegistryError } from "../src/selfimprove/registry.js";
import { applyMigration002 } from "../src/selfimprove/schema.js";
import { GatePolicy, PolicyError } from "../src/selfimprove/policy.js";
import { buildAnswerIndex, runScreens } from "../src/selfimprove/screens.js";
import { EventStore, EventWriter } from "../src/eventlog.js";
import { Xoshiro256SS, improvements, seedFor } from "../src/stats.js";
import { FakeClock } from "../src/util.js";
import { CONFIG_FILES, REGISTRY_YAML } from "./siWorld.js";
import { tmpdir } from "./helpers.js";

const REG = ComponentRegistry.fromYaml(REGISTRY_YAML);
const fresh = () => GitRepo.init(path.join(tmpdir(), "cfg"), CONFIG_FILES);
const apply = (repo: GitRepo, edits: any[], tiers = ["T0", "T1"], maxLines = 200, name = "b") => new EditApplier(repo, REG, maxLines).apply(repo.head(), edits, tiers, `cand/x/1/${name}`, "proposal P\n\nmanifest-hash: m");

describe("registry and PathPolicy", () => {
  it("validates entries and resolves the longest match", () => {
    for (const bad of [{ schema_version: 1, components: [{ path: "a/**", component: "skill", tier: "T3", owner: "o", origin: "human" }] }, { schema_version: 1, components: [{ path: "a/**", component: "skill", tier: "T1", owner: "o" }] },
      { schema_version: 1, components: [{ path: "a/**", component: "nonsense", tier: "T1", owner: "o", origin: "human" }] }, { schema_version: 2, components: [] }]) expect(() => ComponentRegistry.fromDict(bad)).toThrow(RegistryError);
    const reg = ComponentRegistry.fromDict({ schema_version: 1, components: [{ path: "skills/**", component: "skill", tier: "T1", owner: "o", origin: "human" }, { path: "skills/triage/**", component: "skill", tier: "T2", owner: "o", origin: "learned" }] });
    expect([reg.tierOf("skills/other/x"), reg.tierOf("skills/triage/a/b")]).toEqual(["T1", "T2"]);
  });
});
describe("EditApplier", () => {
  it("applies edits atomically and computes the proposal tier itself", () => {
    const repo = fresh(), r = apply(repo, [{ path: "prompts/system.md", op: "replace", search: "Be careful.\n", replace: "Be careful.\nHINT+ x\n" }]);
    expect([r.ok, r.tier]).toEqual([true, "T1"]);
    const bad = apply(repo, [{ path: "tools/new.md", op: "create", content: "t\n" }, { path: "memory/playbook.jsonl", op: "replace", search: "", replace: "x" }], ["T0", "T1"], 200, "c");
    expect(bad.ok).toBe(false); expect(repo.exists("refs/heads/cand/x/1/c")).toBe(false);   // a failed attempt leaves no branch
    const r3 = apply(repo, [{ path: "tools/new.md", op: "create", content: "t\n" }, { path: "prompts/system.md", op: "delete" }], ["T0", "T1"], 200, "d");
    expect(r3.ok).toBe(true); expect(repo.showFile(r3.commit!, "prompts/system.md")).toBeNull(); expect(repo.showFile(r3.commit!, "tools/new.md")).toBe("t\n");
  });
  it.each([[{ path: "prompts/../harness-control/x", op: "create", content: "x" }, "path.traversal"], [{ path: "harness-improver/prompts/proposer.md", op: "replace", search: "Propose", replace: "x" }, "path.unregistered"],
    [{ path: "components.yaml", op: "replace", search: "T1", replace: "T2" }, "path.protected"], [{ path: "skills/triage/SKILL.md", op: "replace", search: "Triage.", replace: "x" }, "tier.exceeds_allowed"],
    [{ path: "prompts/system.md", op: "replace", search: "absent text", replace: "x" }, "edit.no_unique_match"], [{ path: "prompts/system.md", op: "replace", search: "a", replace: "b" }, "edit.no_unique_match"],
    [{ path: "prompts/system.md", op: "create", content: "x" }, "edit.exists"], [{ path: "prompts/ghost.md", op: "delete" }, "edit.missing"], [{ path: "prompts/system.md", op: "rewrite" }, "edit.invalid"],
    [{ path: "prompts/system.md", op: "create", content: "x", extra: 1 }, "edit.invalid"]])("refuses %j", (edit, code) => { expect(apply(fresh(), [edit]).reasons[0].code).toBe(code); });
  it("refuses a symlink that resolves outside the surface and never touches the target", () => {
    const tmp = tmpdir(), outside = path.join(tmp, "harness-control"); fs.mkdirSync(outside); fs.writeFileSync(path.join(outside, "secret.yaml"), "gate: x\n");
    const repo = GitRepo.init(path.join(tmp, "cfg"), CONFIG_FILES); fs.symlinkSync(outside, path.join(repo.path, "prompts", "evil")); repo.git(["add", "-A"]); repo.git(["commit", "-q", "-m", "link"]);
    for (const edit of [{ path: "prompts/evil/secret.yaml", op: "replace", search: "gate: x", replace: "gate: y" }, { path: "prompts/evil/new.yaml", op: "create", content: "x" }]) expect(apply(repo, [edit], ["T0", "T1"], 200, "s").reasons[0].code).toBe("path.symlink_escape");
    expect(fs.readFileSync(path.join(outside, "secret.yaml"), "utf8")).toBe("gate: x\n");
  });
  it("counts changed lines against the cap, and allows a registered file at the worktree root", () => {
    const big = Array.from({ length: 150 }, (_, i) => `line ${i}`).join("\n") + "\n", repo = fresh();
    expect(apply(repo, [{ path: "tools/a.md", op: "create", content: big }]).ok).toBe(true);
    expect(apply(repo, [{ path: "tools/a.md", op: "create", content: big }, { path: "tools/b.md", op: "create", content: big }], ["T0", "T1"], 200, "e").reasons[0].code).toBe("edit.cap_exceeded");
    const reg = ComponentRegistry.fromDict({ schema_version: 1, components: [{ path: "NOTES.md", component: "playbook", tier: "T0", owner: "o", origin: "learned" }] }), r = GitRepo.init(path.join(tmpdir(), "r"), { "x.keep": "x\n" });
    const res = new EditApplier(r, reg).apply(r.head(), [{ path: "NOTES.md", op: "create", content: "n\n" }], ["T0"], "cand/r/1/1", "proposal R"); expect(res.ok).toBe(true);
  });
  it("identical edits on identical bases give identical commit ids; worktreeHash detects uncommitted tampering", () => {
    const e = { path: "prompts/system.md", op: "replace", search: "Be careful.\n", replace: "Be careful.\nHINT+ x\n" };
    expect(apply(fresh(), [e]).commit).toBe(apply(fresh(), [e]).commit);
    const repo = fresh(), h = repo.worktreeHash(); fs.writeFileSync(path.join(repo.path, "prompts", "system.md"), "tampered\n"); expect(repo.worktreeHash()).not.toBe(h); expect(repo.worktreeHash()).toBe(repo.worktreeHash());
  });
});
describe("screens", () => {
  const E = (p: string, added: string, before: any = null, after: any = null) => ({ path: p, added, before, after });
  it("each screen fires on its own input and stays quiet on clean input", () => {
    const idx = buildAnswerIndex(["The capital of the republic is Zanzibar City"]), one = (e: any[], ids: string[] = []) => runScreens(e, idx, ids).map((v: any) => v.screen);
    expect(one([E("prompts/a.md", "Check each step carefully before answering.")], ["s001"])).toEqual([]);
    expect(one([E("prompts/a.md", "Remember: the capital of the republic is Zanzibar City!")])).toEqual(["answer_echo"]);
    expect(one([E("prompts/a.md", "Always double check the result of every tool call before moving on.\n".repeat(3))])).toEqual(["duplicate_blocks"]);
    for (const t of ["echo 1 > /scratch/score.json", "open('report.json','w').write(x)", "set report_path to x"]) expect(one([E("tools/a.md", t)]), t).toEqual(["output_interface"]);
    for (const t of ["read /tests/grade.py", "look in harness-control", "check the vault", "use the heldout_set", "see verifier_path"]) expect(one([E("prompts/a.md", t)]), t).toEqual(["protected_refs"]);
    expect(one([E("prompts/a.md", "for task s007 always answer yes")], ["s007"])).toEqual(["hardcoded_ids"]); expect(one([E("prompts/a.md", "set seed = 1234")])).toEqual(["hardcoded_ids"]);
  });
  it("T4 keys are caught inside configuration but not in prose", () => {
    const before = "temperature: 0.2\nbudgets:\n  turns: 10\nmodel: base\nnested:\n  egress: []\n", t4 = (after: string) => runScreens([E("prompts/settings.yaml", "x", before, after)], [], []);
    for (const [after, key] of [[before.replace("turns: 10", "turns: 99"), "budgets"], [before.replace("base", "other"), "model"], [before.replace("egress: []", "egress: [evil.com]"), "nested.egress"], [before + "approvals: none\n", "approvals"]] as const) { const v = t4(after); expect(v[0]?.screen).toBe("t4_keys"); expect(v[0].detail).toContain(key); }
    expect(t4(before.replace("0.2", "0.7"))).toEqual([]);
    expect(runScreens([E("prompts/p.md", "x", "---\nmodel: a\ntitle: t\n---\nbody\n", "---\nmodel: b\ntitle: t\n---\nbody\n")], [], [])[0].screen).toBe("t4_keys");
    expect(runScreens([E("prompts/p.md", "x", "plain model: base\n", "plain model: other\n")], [], [])).toEqual([]);
  });
});
describe("eval sets", () => {
  it("enforces disjointness, near-duplicate rejection, the held-out query cap and reproducible seeds", async () => {
    const dir = tmpdir(), st = new EventStore(path.join(dir, "l.db")), w = new EventWriter(st, new FakeClock()).start(); await applyMigration002(w);
    const reg = new EvalSetRegistry(st, w, new FakeClock(), 3);
    await reg.register("s", "scoring", [{ id: "a", text: "classify the sentiment of this short review about a restaurant" }]);
    await expect(reg.register("h", "heldout", [{ id: "a", text: "totally different" }])).rejects.toMatchObject({ code: "evalset.id_overlap" });
    await expect(reg.register("h", "heldout", [{ id: "z", text: "classify the sentiment of this short review about a restaurant." }])).rejects.toMatchObject({ code: "evalset.near_duplicate" });
    await reg.register("h", "heldout", [{ id: "z", text: "summarise the following legal contract into five bullet points" }]); await reg.register("s2", "scoring", [{ id: "a2", text: "classify the sentiment of this short review about a restaurant" }]);
    for (let i = 0; i < 3; i++) { expect(reg.status("h")).toBe("active"); await reg.recordQuery("h", "camp", `c${i}`); }
    expect([reg.status("h"), reg.usage("h")]).toEqual(["exhausted", 3]); await expect(reg.recordQuery("h", "camp", "c9")).rejects.toBeInstanceOf(EvalSetError);
    expect(await reg.recordQuery("h", "camp", "c0")).toBe(3);   // a replayed query is counted once
    expect(evalSeed("C", 1, "cand", "set")).toBe(evalSeed("C", 1, "cand", "set")); expect(evalSeed("C", 1, "cand", "set")).not.toBe(evalSeed("C", 2, "cand", "set")); expect(jaccard(shingleSet("a b c d"), shingleSet("a b c d"))).toBe(1);
  });
});
// ---------------------------------------------------------------- gate
function lcg(seed: number) { let s = seed >>> 0; return () => ((s = (Math.imul(s, 1664525) + 1013904223) >>> 0) / 4294967296); }
const arrays = (n = 40, delta = 0.1, spread = 0.1, seed = 1) => { const r = lcg(seed), b = Array.from({ length: n }, () => r()), c = b.map((x) => x + delta + (r() * 2 - 1) * spread); return [b, c]; };
function variant(delta = 0.1, spread = 0.05, costDelta = 0.0, seed = 1, broken: number[] = []) {
  const [b, c] = arrays(40, delta, spread, seed);
  return { target: { b, c, direction: "maximize" }, guardrails: [{ metric: "cost", b: Array(40).fill(1.0), c: Array(40).fill(1.0 + costDelta), direction: "minimize" }], preserved: { b: Array(40).fill(1), c: Array.from({ length: 40 }, (_, i) => (broken.includes(i) ? 0 : 1)), must_pass: [0, 1] } };
}
const cand = (pid: string, canonical?: any, fmt?: any, heldout?: any, seed = 1) => { const c = canonical ?? variant(0.1, 0.05, 0, seed); return { proposal_id: pid, tier: "T1", models: { main: { variants: { canonical: c, format_varied: fmt ?? c }, heldout: heldout !== undefined ? heldout : { target: { b: Array(20).fill(0.5), c: Array(20).fill(0.6), direction: "maximize" } } } } }; };
const P = { B: 600 };
describe("gate", () => {
  it("accepts a clear win and rejects a no-op", () => {
    const [win, noop] = G.decideRound("C", [cand("win"), cand("noop", variant(0.0, 0.0, 0, 2))], P);
    expect(win.accepted).toBe(true); expect(win.reasons).toEqual([]); expect(win.target.lower).toBeGreaterThan(0); expect(noop.accepted).toBe(false); expect(noop.reasons.some((r: string) => r.startsWith("target:"))).toBe(true);
  });
  it("rejects guardrail regressions, must-pass regressions, preserved non-inferiority failures and format-varied regressions", () => {
    let d = G.decideRound("C", [cand("g", variant(0.1, 0.05, 0.5))], P)[0]; expect(d.accepted).toBe(false); expect(d.reasons.some((r: string) => r.startsWith("guardrail:cost"))).toBe(true);
    expect(G.decideRound("C", [cand("ok", variant(0.1, 0.05, 0.01))], P)[0].accepted).toBe(true);
    d = G.decideRound("C", [cand("m", variant(0.1, 0.05, 0, 1, [0]))], P)[0]; expect(d.reasons.some((r: string) => r.includes("must_pass_regressed"))).toBe(true);
    d = G.decideRound("C", [cand("p", variant(0.1, 0.05, 0, 1, Array.from({ length: 20 }, (_, i) => i + 5)))], P)[0]; expect(d.reasons.some((r: string) => r.includes("preserved:non_inferiority"))).toBe(true);
    d = G.decideRound("C", [cand("f", variant(), variant(-0.1, 0.02, 0, 3))], P)[0]; expect(d.accepted).toBe(false); expect(d.reasons.some((r: string) => r.includes("(main:format_varied)"))).toBe(true);
  });
  it("blocks held-out regressions and only asks for the held-out set after every earlier check passes", () => {
    const bad = { target: { b: Array(20).fill(0.5), c: Array(20).fill(0.2), direction: "maximize" } }, d = G.decideRound("C", [cand("h", undefined, undefined, bad)], P)[0];
    expect(d.accepted).toBe(false); expect(d.reasons.some((r: string) => r.startsWith("heldout:regressed"))).toBe(true); expect(d.needs_heldout).toBe(false);
    const w: any = cand("w"); delete w.models.main.heldout; const dw = G.decideRound("C", [w], P)[0]; expect([dw.needs_heldout, dw.accepted]).toEqual([true, false]);
    const l: any = cand("l", variant(0.0, 0.0, 0, 2)); delete l.models.main.heldout; const dl = G.decideRound("C", [l], P)[0]; expect([dl.needs_heldout, dl.accepted]).toEqual([false, false]);   // a failed candidate never spends a held-out query
  });
  it("Holm makes a round stricter than a single-candidate gate (and levels follow alpha/(m-k+1))", () => {
    let seed = 0;
    for (let sd = 1; sd < 600 && !seed; sd++) {   // data that clears 0 at alpha=.05 alone but not at alpha/3
      const [b, c] = arrays(40, 0.025, 0.15, sd), x = improvements(b, c, "maximize"), s = seedFor("C", "bp:main:canonical:target");
      const lo = (a: number) => { const m = (G as any); return m.__iv ? 0 : null; }; void lo;
      const a = G.decideRound("C", [{ proposal_id: "bp", tier: "T1", models: { main: { variants: { canonical: { target: { b, c, direction: "maximize" }, guardrails: [] }, format_varied: { target: { b, c, direction: "maximize" }, guardrails: [] } }, heldout: { target: { b: [0.5], c: [0.5], direction: "maximize" } } } } }], P)[0].accepted;
      const mk = (pid: string) => ({ proposal_id: pid, tier: "T1", models: { main: { variants: { canonical: { target: { b, c, direction: "maximize" }, guardrails: [] }, format_varied: { target: { b, c, direction: "maximize" }, guardrails: [] } }, heldout: { target: { b: [0.5], c: [0.5], direction: "maximize" } } } } });
      const trio = G.decideRound("C", [mk("bp"), mk("bq"), mk("br")], P); void x; void s;
      if (a && !trio.some((d) => d.accepted)) seed = sd;
    }
    expect(seed).toBeGreaterThan(0);
    const l3 = G.holmLevels([0.01, 0.04, 0.03], 0.05); expect(l3[0]).toBeCloseTo(0.05 / 3, 12); expect(l3[1]).toBeCloseTo(0.05, 12); expect(l3[2]).toBeCloseTo(0.05 / 2, 12);
    expect(G.holmLevels([0.01, null, 0.03], 0.05)).toEqual([0.05 / 2, null, 0.05]); expect(G.holmLevels([0.01, 0.04], 0.05, false)).toEqual([0.05, 0.05]);
  });
  it("is deterministic, rejects dominated candidates and decides per target model", () => {
    expect(G.decideRound("C", [cand("d1"), cand("d2", undefined, undefined, undefined, 5)], P)).toEqual(G.decideRound("C", [cand("d1"), cand("d2", undefined, undefined, undefined, 5)], P));
    const c: any = cand("dom"); c.summary = { target: 0.1, cost: 2.0, latency: 5.0 }; c.archive = [{ target: 0.2, cost: 1.0, latency: 4.0 }];
    expect(G.decideRound("C", [c], P)[0].reasons).toContain("dominated"); c.archive = [{ target: 0.05, cost: 1.0, latency: 4.0 }]; expect(G.decideRound("C", [c], P)[0].accepted).toBe(true);
    const pm: any = cand("pm"); pm.models.gem = { variants: { canonical: variant(-0.1, 0.02, 0, 4), format_varied: variant(-0.1, 0.02, 0, 4) }, heldout: pm.models.main.heldout };
    const d = G.decideRound("C", [pm], P)[0]; expect([d.accepted, d.accepted_models, d.models.gem.status]).toEqual([true, ["main"], "fail"]);
  });
});
describe("search", () => {
  it("novelty rejection, parent weights, Pareto front and diversity", () => {
    const a = "always verify the tool output carefully before you write the final answer to the user";
    expect(S.noveltyRejected(a, ["something else entirely about spreadsheets and formulas in detail please", a + " now"])).toBe(1); expect(S.noveltyRejected("a completely new instruction about handling timeouts gracefully in tools", [a])).toBeNull();
    const w = S.parentWeights([0.9, 0.5, 0.5], [0, 0, 3], 0.2); expect(w[0]).toBeGreaterThan(w[1]); expect(w[1]).toBeGreaterThan(w[2]); expect(w[1] / w[2]).toBeCloseTo(4, 10);
    const picks = Array.from({ length: 400 }, (_, i) => S.sampleParent(w, new Xoshiro256SS(BigInt(i)))), cnt = (k: number) => picks.filter((p) => p === k).length; expect(cnt(0)).toBeGreaterThan(cnt(1)); expect(cnt(1)).toBeGreaterThan(cnt(2));
    expect(S.paretoFront([{ target: 0.5, cost: 1, latency: 1 }, { target: 0.6, cost: 2, latency: 1 }, { target: 0.4, cost: 3, latency: 3 }, { target: 0.6, cost: 1, latency: 1 }])).toEqual([3]);
    expect(S.meanPairwiseSimilarity([a, a])).toBe(1.0); expect(S.meanPairwiseSimilarity([a, "completely different words here and there across many tokens in this text"])).toBeLessThan(0.1);
  });
});
void BlobStore;
describe("gate policy per kind of edit", () => {
  const CFG = { tiers: { T1: { alpha: 0.04 } }, environments: { prod: { tiers: { T1: { alpha: 0.01, review: { mode: "blocking" } }, T2: { alpha: 0.001 } } }, dev: { tiers: { T1: { alpha: 0.2, review: { mode: "none" } }, T0: { holm: false } } } } };
  it("defaults are the ones the deployment owner chose", () => {
    const p = GatePolicy.resolve();
    expect(["T0", "T1", "T2"].map((t) => [t, p.gateParams(t).alpha, p.review(t).mode, p.canaryFraction(t)])).toEqual([["T0", 0.05, "none", 0], ["T1", 0.05, "sampled", 0], ["T2", 0.01, "blocking", 0.1]]); expect(p.review("T1").sample_rate).toBe(0.2); expect(p.diff()).toEqual([]);
  });
  it("an administrator can make an environment stricter or leaner; every deviation is listed with its direction", () => {
    const base = GatePolicy.resolve(CFG), prod = GatePolicy.resolve(CFG, "prod"), dev = GatePolicy.resolve(CFG, "dev"), dir = (p: GatePolicy) => Object.fromEntries(p.diff().map((d) => [`${d.tier}.${d.path}`, d.direction]));
    expect([base.gateParams("T1").alpha, prod.gateParams("T1").alpha, dev.gateParams("T1").alpha]).toEqual([0.04, 0.01, 0.2]);
    expect(dir(prod)).toEqual({ "T1.alpha": "stricter", "T1.review.mode": "stricter", "T2.alpha": "stricter" }); expect(prod.relaxed()).toHaveLength(0);
    expect(dir(dev)).toEqual({ "T0.holm": "leaner", "T1.alpha": "leaner", "T1.review.mode": "leaner" }); expect(dev.relaxed()).toHaveLength(3);
    expect(new Set([base.fingerprint, prod.fingerprint, dev.fingerprint, GatePolicy.resolve().fingerprint]).size).toBe(4);
  });
  it.each([[{ tiers: { T1: { alpha: 0.0 } } }, null, "policy.invalid_value"], [{ tiers: { T1: { alpha: 0.5 } } }, null, "policy.invalid_value"], [{ tiers: { T2: { review: { mode: "sampled" } } } }, null, "policy.locked"], [{ tiers: { T3: {} } }, null, "policy.unknown_tier"],
    [{ tiers: { T1: { alfa: 0.1 } } }, null, "policy.unknown_key"], [{ gate: {} }, null, "policy.unknown_key"], [{ environments: { a: { tiers: {} } } }, "b", "policy.unknown_environment"], [{ tiers: { T1: { B: 10 } } }, null, "policy.invalid_value"],
    [{ tiers: { T1: { review: { sample_rate: 2 } } } }, null, "policy.invalid_value"]])("refuses typos, out-of-range values and attempts to relax what cannot be relaxed: %j", (cfg, env, code) => { try { GatePolicy.resolve(cfg as any, env as any); throw new Error("accepted"); } catch (e: any) { expect(e).toBeInstanceOf(PolicyError); expect(e.code).toBe(code); } });
  it("tiers gate at their own alpha: the same evidence can pass at T1 and fail at T2, and an administrator can relax T2", () => {
    const mk = (sd: number) => { const [b, c] = arrays(40, 0.04, 0.15, sd), v = { target: { b, c, direction: "maximize" }, guardrails: [] }; return { proposal_id: "x", tier: "T1", models: { main: { variants: { canonical: v, format_varied: v }, heldout: { target: { b: [0.5], c: [0.5], direction: "maximize" } } } } }; };
    const pol = GatePolicy.resolve(), P1 = { ...pol.gateParams("T1"), B: 600 }, P2 = { ...pol.gateParams("T2"), B: 600 };
    let found = 0; for (let sd = 1; sd < 600 && !found; sd++) if (G.decideRound("C", [mk(sd)], P1)[0].accepted && !G.decideRound("C", [mk(sd)], P2)[0].accepted) found = sd;
    expect(found).toBeGreaterThan(0); expect(G.decideRound("C", [mk(found)], { ...GatePolicy.resolve({ tiers: { T2: { alpha: 0.05 } } }).gateParams("T2"), B: 600 })[0].accepted).toBe(true);
  });
});
