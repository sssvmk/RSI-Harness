/** Shared self-improvement vectors: the TypeScript implementation must reproduce what the Python implementation computes (and the hand-authored cases). */
import fs from "node:fs";
import path from "node:path";
import { describe, expect, it } from "vitest";
import * as G from "../src/selfimprove/gate.js";
import * as S from "../src/selfimprove/search.js";
import { EditApplier } from "../src/selfimprove/edits.js";
import { evalSeed, jaccard, shingleSet } from "../src/selfimprove/evalsets.js";
import { GitRepo } from "../src/selfimprove/gitrepo.js";
import { ComponentRegistry } from "../src/selfimprove/registry.js";
import { GatePolicy, PolicyError } from "../src/selfimprove/policy.js";
import { buildAnswerIndex, runScreens } from "../src/selfimprove/screens.js";
import { Xoshiro256SS } from "../src/stats.js";
import { tmpdir } from "./helpers.js";
import { World, editHint, proposal } from "./siWorld.js";

const VEC = path.resolve(import.meta.dirname, "../../../spec/vectors");
const load = (n: string) => JSON.parse(fs.readFileSync(path.join(VEC, n), "utf8"));
const clone = <T>(x: T): T => JSON.parse(JSON.stringify(x));

describe("self-improvement vectors", () => {
  it("path policy (hand-authored): longest match, traversal, symlink-free normalisation, protected registry", () => {
    const v = load("selfimprove-pathpolicy.json"), reg = ComponentRegistry.fromYaml(v.registry_yaml);
    for (const c of v.cases) expect(reg.check(c.path), c.path).toEqual([c.tier, c.reason]);
  });
  it("screens (hand-authored): every screen fires on its own input and only there; the answer index hashes match", () => {
    const v = load("selfimprove-screens.json");
    expect(buildAnswerIndex(v.answers)).toEqual(v.answer_index);
    for (const c of v.cases) expect(runScreens(clone(c.edits), v.answer_index, v.instance_ids).map((x: any) => x.screen), c.name).toEqual(c.screens);
  });
  it("gate: every decision, interval and Holm level is identical to the Python implementation", () => {
    const v = load("selfimprove-gate.json");
    for (const c of v.cases) expect(G.decideRound(c.run_id, clone(c.candidates), c.params), c.name).toEqual(c.expected);
    for (const h of v.holm_levels) expect(G.holmLevels(h.p, h.alpha, h.holm)).toEqual(h.levels);
  });
  it("search: novelty, parent weights and sampling, Pareto front, diversity, seeds, Jaccard", () => {
    const v = load("selfimprove-search.json");
    for (const c of v.novelty) expect(S.noveltyRejected(c.edit, c.archive)).toBe(c.expected);
    const w = v.weights, got = S.parentWeights(w.scores, w.children, w.tau);
    got.forEach((x, i) => expect(Math.abs(x - w.expected[i]) / w.expected[i]).toBeLessThan(1e-12));   // Math.exp may differ from libm by an ulp
    for (const p of v.picks) expect(S.sampleParent(w.expected, new Xoshiro256SS(BigInt(p.seed))), `seed ${p.seed}`).toBe(p.expected);
    expect(S.paretoFront(v.pareto.points)).toEqual(v.pareto.expected);
    for (const s of v.similarity) expect(Math.abs(S.meanPairwiseSimilarity(s.texts) - s.expected)).toBeLessThan(1e-12);
    for (const e of v.eval_seeds) expect(evalSeed(e.args[0], e.args[1], e.args[2], e.args[3])).toBe(e.expected);
    for (const j of v.jaccard) expect(jaccard(shingleSet(j.a), shingleSet(j.b))).toBe(j.expected);
  });
  it("deterministic git: the same files and the same edit give the same commit and tree ids as the Python implementation", () => {
    const v = load("selfimprove-git.json"), repo = GitRepo.init(path.join(tmpdir(), "cfg"), v.files);
    expect(repo.head()).toBe(v.base_commit);
    const r = new EditApplier(repo, ComponentRegistry.fromYaml(v.files["components.yaml"])).apply(repo.head(), [v.edit], ["T0", "T1"], "cand/x/1/1", v.message), e = v.expected;
    expect([r.commit, r.tier, r.changed_lines]).toEqual([e.commit, e.tier, e.changed_lines]); expect(repo.changedIn(r.commit!)).toEqual(e.changed); expect(repo.git(["rev-parse", `${r.commit}^{tree}`])).toBe(e.tree);
  });
  it("golden campaign: the same campaign gives a byte-identical event hash chain and the same main commit as the Python implementation", async () => {
    const v = load("selfimprove-campaign.json"), w = new World(tmpdir(), { "1,1": proposal(editHint("HINT+ verify tool output carefully")), "1,2": proposal(editHint("HINT- skip verification entirely")),
      "2,1": proposal(editHint("HINT+ plan the steps ahead of time")), "2,2": proposal(editHint("OVERFIT memorise the examples")), default: null }, { cid: v.campaign_id });
    await w.setup(); const s = await w.controller().run(), ev = w.store.events(w.cid);
    expect([s.stopped, w.config.head()]).toEqual([v.stopped, v.main_head]); expect(ev.map((e: any) => ({ seq: e.seq, type: e.type, hash: e.hash }))).toEqual(v.events); w.close();
  });
  it("gate policy: resolution, audit diff, fingerprint and refusals are identical to the Python implementation", () => {
    const v = load("selfimprove-policy.json");
    for (const c of v.cases) {
      const q = GatePolicy.resolve(c.config, c.environment), t = ["T0", "T1", "T2"];
      expect([q.fingerprint, q.diff(), q.relaxed().length], c.name).toEqual([c.fingerprint, c.diff, c.relaxed]);
      expect(Object.fromEntries(t.map((x) => [x, q.gateParams(x)])), c.name).toEqual(c.gate_params); expect(Object.fromEntries(t.map((x) => [x, q.review(x)]))).toEqual(c.review); expect(Object.fromEntries(t.map((x) => [x, q.canaryFraction(x)]))).toEqual(c.canary);
    }
    for (const e of v.errors) { let code = ""; try { GatePolicy.resolve(e.config, e.environment); } catch (err: any) { if (!(err instanceof PolicyError)) throw err; code = err.code; } expect(code, e.name).toBe(e.code); }
  });
});
