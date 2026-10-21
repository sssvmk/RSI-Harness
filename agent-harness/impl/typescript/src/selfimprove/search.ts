/** Archive and search strategy (§17.9, decision P19). */
import { Xoshiro256SS, lsum } from "../stats.js";
export function shingles(text: string, k = 5): Set<string> {
  const t = text.toLowerCase().match(/[a-z0-9]+/g) ?? [], out = new Set<string>();
  if (!t.length) return out;
  for (let i = 0; i < Math.max(1, t.length - k + 1); i++) out.add(t.slice(i, i + k).join(" "));
  return out;
}
export function jaccard(a: Set<string>, b: Set<string>): number { if (!a.size && !b.size) return 1.0; let n = 0; for (const x of a) if (b.has(x)) n++; return n / (a.size + b.size - n); }
/** Index of the first archived candidate the edit is too similar to (Jaccard >= threshold), else null. */
export function noveltyRejected(editText: string, archive: string[], k = 5, threshold = 0.8): number | null {
  const s = shingles(editText, k);
  for (let i = 0; i < archive.length; i++) if (jaccard(s, shingles(archive[i], k)) >= threshold) return i;
  return null;
}
/** w_i = softmax(score_i / tau) / (1 + children_i), max-shifted for stability. */
export function parentWeights(scores: number[], children: number[], tau: number): number[] {
  const z = scores.map((s) => s / tau), m = Math.max(...z), e = z.map((v) => Math.exp(v - m)), tot = lsum(e);
  return e.map((v, i) => (v / tot) / (1 + children[i]));
}
export function sampleParent(weights: number[], rng: Xoshiro256SS): number {
  const u = Number(rng.next() >> 11n) / 9007199254740992 * lsum(weights);   // 53-bit uniform, exact in binary64
  let acc = 0.0; for (let i = 0; i < weights.length; i++) { acc += weights[i]; if (u < acc) return i; }
  return weights.length - 1;
}
/** Non-dominated indices over (target maximised, cost minimised, latency minimised). */
export function paretoFront(points: any[]): number[] {
  const out: number[] = [];
  points.forEach((p, i) => {
    const dom = points.some((q, j) => i !== j && q.target >= p.target && q.cost <= p.cost && q.latency <= p.latency && (q.target > p.target || q.cost < p.cost || q.latency < p.latency));
    if (!dom) out.push(i);
  });
  return out;
}
export function meanPairwiseSimilarity(texts: string[], k = 5): number {
  const sh = texts.map((t) => shingles(t, k)), vals: number[] = [];
  for (let i = 0; i < sh.length; i++) for (let j = i + 1; j < sh.length; j++) vals.push(jaccard(sh[i], sh[j]));
  return vals.length ? lsum(vals) / vals.length : 0.0;
}
