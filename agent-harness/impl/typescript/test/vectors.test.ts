/** Shared conformance vectors (design §19.3): the TypeScript implementation must match the Python-generated and hand-written vectors bit-for-bit. */
import fs from "node:fs";
import path from "node:path";
import { describe, expect, it } from "vitest";
import { computeHash, genesisHash } from "../src/util.js";
import { decide } from "../src/policy.js";
import { estimateRequest } from "../src/tokens.js";
import { ADAPTERS } from "../src/adapters.js";
import { Xoshiro256SS, bootstrapCi, gate, seedFor, splitmix64 } from "../src/stats.js";
import * as P from "../src/projections.js";

export const VEC = path.resolve(import.meta.dirname, "../../../spec/vectors");
const load = (f: string) => JSON.parse(fs.readFileSync(path.join(VEC, f), "utf8"));
const clone = <T>(x: T): T => JSON.parse(JSON.stringify(x));

describe("shared vectors", () => {
  it("hash chain (incl. UTF-16 key ordering, float formatting, unicode escapes)", () => {
    const v = load("hash-vectors.json");
    expect(genesisHash(v.run_id)).toBe(v.genesis);
    let prev = v.genesis;
    for (const e of v.events) { expect(e.prev_hash).toBe(prev); expect(computeHash(prev, e), e.type).toBe(e.hash); prev = e.hash; }
  });
  it("policy vectors", () => {
    const lines = fs.readFileSync(path.join(VEC, "policy-vectors.jsonl"), "utf8").split("\n").filter((l) => l.trim()).map((l) => JSON.parse(l));
    expect(lines.length).toBeGreaterThanOrEqual(30);
    for (const c of lines) {
      const got = decide(c.input.ruleset, c.input.call);
      expect({ d: got.decision, m: got.matched_rule_ids, u: got.default_used }, c.name).toEqual({ d: c.expected.decision, m: c.expected.matched_rule_ids, u: c.expected.default_used });
    }
  });
  it("estimator vectors", () => {
    for (const c of load("estimator-vectors.json")) expect(estimateRequest(c.input.messages, c.input.tools, c.input.divisor ?? 3), c.name).toBe(c.expected);
  });
  it("adapter vectors (request and response, both directions)", () => {
    const v = load("adapter-vectors.json");
    for (const c of v.request_cases) {
      const a: any = new ADAPTERS[c.provider]();
      expect(a.toWire(clone(c.request), c.model_id, c.id_map), c.name).toEqual(c.expected_wire);
      if (c.expected_downgrades) expect(a.lastDowngrades).toEqual(c.expected_downgrades);
    }
    for (const c of v.response_cases) {
      const ids = [...c.ids]; const a = new ADAPTERS[c.provider](() => ids.shift()!);
      const r: any = a.fromWire(c.wire); r.id_map = r.provider_meta.id_map; delete r.provider_meta;
      expect(r, c.name).toEqual(c.expected);
    }
  });
  it("statistics: SplitMix64 reference, xoshiro256**, seeds, bootstrap CIs, gates", () => {
    const v = load("stats-vectors.json");
    expect("0x" + (splitmix64(0n).next().value as bigint).toString(16)).toBe("0xe220a8397b1dcdaf");
    expect(v.splitmix64_seed0_first).toBe("0xe220a8397b1dcdaf");
    for (const c of v.xoshiro) { const r = new Xoshiro256SS(BigInt(c.seed)); expect(Array.from({ length: 5 }, () => r.next().toString())).toEqual(c.first); expect(Array.from({ length: 5 }, () => r.index(10))).toEqual(c.index_10); }
    for (const c of v.seeds) expect(seedFor(c.run_id, c.gate_id).toString()).toBe(c.seed);
    for (const c of v.bootstrap) expect(bootstrapCi(c.x, BigInt(c.seed), c.confidence, c.B)).toEqual(c.expected);
    for (const g of v.gates) expect(gate(g.run_id, g.gate_id, [g.target.baseline, g.target.candidate, g.target.direction], g.guardrails, g.confidence, g.B)).toEqual(g.expected);
  });
  it("bootstrap index arithmetic matches the spec worked example (250 / 9749)", () => {
    const ci = bootstrapCi([0.1, 0.2, 0.3], 1n, 0.95, 10000);
    expect([ci.lower_index, ci.upper_index]).toEqual([250, 9749]);
  });
  it("projection folds match the Python implementation event-for-event", () => {
    const v = load("projection-vectors.json");
    expect(v.versions).toEqual(P.VERSIONS);
    for (const n of P.NAMES) expect(P.foldAll(n, clone(v.events)), n).toEqual(v.expected[n]);
  });
});
