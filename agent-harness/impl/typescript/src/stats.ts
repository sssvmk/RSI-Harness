/** Statistics (design §13.7): xoshiro256** + SplitMix64 with BigInt, paired bootstrap, gates. Left-to-right sums only. */
import { sha256Hex } from "./util.js";
const M64 = (1n << 64n) - 1n;
export function* splitmix64(seed: bigint) {
  let s = seed & M64;
  while (true) {
    s = (s + 0x9e3779b97f4a7c15n) & M64;
    let z = s;
    z = ((z ^ (z >> 30n)) * 0xbf58476d1ce4e5b9n) & M64;
    z = ((z ^ (z >> 27n)) * 0x94d049bb133111ebn) & M64;
    yield z ^ (z >> 31n);
  }
}
const rotl = (x: bigint, k: bigint) => ((x << k) | (x >> (64n - k))) & M64;
export class Xoshiro256SS {
  s: bigint[];
  constructor(seed: bigint) { const sm = splitmix64(seed); this.s = [0, 1, 2, 3].map(() => sm.next().value as bigint); }
  next(): bigint {
    const s = this.s;
    const result = (rotl((s[1] * 5n) & M64, 7n) * 9n) & M64;
    const t = (s[1] << 17n) & M64;
    s[2] ^= s[0]; s[3] ^= s[1]; s[1] ^= s[2]; s[0] ^= s[3]; s[2] ^= t; s[3] = rotl(s[3], 45n);
    return result;
  }
  index(n: number): number { return Number((this.next() * BigInt(n)) >> 64n); }
}
export const seedFor = (runId: string, gateId: string): bigint => BigInt("0x" + sha256Hex(`${runId}:${gateId}`).slice(0, 16));
export function improvements(b: number[], c: number[], dir: string): number[] {
  if (b.length !== c.length) throw new Error("paired samples must have equal length");
  return b.map((bi, i) => (dir === "maximize" ? c[i] - bi : bi - c[i]));
}
export function mean(x: number[]): number { let s = 0.0; for (const v of x) s += v; return s / x.length; }
/** The sorted bootstrap distribution of the mean (one resample set can serve several confidence levels). */
export function bootstrapMeans(x: number[], seed: bigint, B = 10000): Float64Array {
  const n = x.length;
  if (!n) throw new Error("empty sample");
  const rng = new Xoshiro256SS(seed), means = new Float64Array(B);
  for (let b = 0; b < B; b++) { let s = 0.0; for (let j = 0; j < n; j++) s += x[rng.index(n)]; means[b] = s / n; }
  means.sort();   // typed-array sort is numeric ascending
  return means;
}
export function intervalFromSorted(means: Float64Array, confidence: number): [number, number, number, number] {
  const B = means.length, a = 1 - confidence, lo = Math.floor((a / 2) * B), hi = Math.ceil((1 - a / 2) * B) - 1;
  return [means[lo], means[hi], lo, hi];
}
export function bootstrapCi(x: number[], seed: bigint, confidence = 0.95, B = 10000) {
  const means = bootstrapMeans(x, seed, B), [lower, upper, lo, hi] = intervalFromSorted(means, confidence);
  return { mean: mean(x), lower, upper, lower_index: lo, upper_index: hi };
}
/** Plain left-to-right float addition (matches the Python implementation, whose sum() is compensated). */
export function lsum(xs: number[]): number { let t = 0.0; for (const x of xs) t += x; return t; }
export function gate(runId: string, gateId: string, target: [number[], number[], string], guardrails: any[] = [], confidence = 0.95, B = 10000) {
  const seed = seedFor(runId, gateId);
  const t: any = bootstrapCi(improvements(...target), seed, confidence, B);
  t.pass = t.lower > 0;
  const gr = guardrails.map((g) => { const ci: any = bootstrapCi(improvements(g.baseline, g.candidate, g.direction), seed, confidence, B); ci.metric = g.metric; ci.margin = g.margin; ci.pass = ci.lower >= -g.margin; return ci; });
  return { target: t, guardrails: gr, accepted: t.pass && gr.every((g) => g.pass), seed: seed.toString() };
}
const Z: Record<number, number> = { 0.8: 1.2816, 0.9: 1.6449, 0.95: 1.96, 0.99: 2.5758 };
export function requiredTrials(sd: number, effect: number, confidence = 0.95, minTrials = 3): number {
  if (!(confidence in Z)) throw new Error("confidence must be one of 0.80, 0.90, 0.95, 0.99");
  if (effect <= 0) throw new Error("effect must be positive");
  return Math.max(minTrials, Math.ceil((Z[confidence] * sd / effect) ** 2));
}
