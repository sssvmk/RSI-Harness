/** Static anti-gaming screens (§17.7): declarative rules in spec/screens.json, shared with the Python implementation. */
import fs from "node:fs";
import path from "node:path";
import YAML from "yaml";
import { canonicalize, sha256Hex } from "../util.js";
import { SPEC_DIR } from "../failure.js";
export const loadRules = (p = path.join(SPEC_DIR, "screens.json")) => JSON.parse(fs.readFileSync(p, "utf8"));
const rx = (p: any) => new RegExp(p.re, (p.flags ?? "").includes("i") ? "i" : "");
export const tokens = (text: string): string[] => text.toLowerCase().match(/[a-z0-9]+/g) ?? [];
export function ngramHashes(text: string, n: number): Set<string> {
  const t = tokens(text), out = new Set<string>();
  for (let i = 0; i + n <= t.length; i++) out.add(sha256Hex(t.slice(i, i + n).join(" ")));
  return out;
}
/** Control-plane side: only hashes ever leave it. */
export function buildAnswerIndex(answers: string[], n = 5): string[] { const all = new Set<string>(); for (const a of answers) for (const h of ngramHashes(a, n)) all.add(h); return [...all].sort(); }
function docOf(p: string, text: string | null): any {
  if (text === null || text === undefined) return {};
  try {
    if (p.endsWith(".yaml") || p.endsWith(".yml") || p.endsWith(".json")) return YAML.parse(text) ?? {};
    if (p.endsWith(".md") && text.startsWith("---\n")) { const end = text.indexOf("\n---", 4); return end > 0 ? YAML.parse(text.slice(4, end)) ?? {} : {}; }
  } catch { return null; }
  return {};
}
function t4Entries(node: any, keys: Set<string>, prefix = ""): Record<string, string> {
  const out: Record<string, string> = {};
  if (node && typeof node === "object" && !Array.isArray(node)) {
    for (const [k, v] of Object.entries(node)) { const kp = prefix ? `${prefix}.${k}` : String(k); if (keys.has(String(k).toLowerCase())) out[kp] = canonicalize(v); Object.assign(out, t4Entries(v, keys, kp)); }
  } else if (Array.isArray(node)) node.forEach((v, i) => Object.assign(out, t4Entries(v, keys, `${prefix}[${i}]`)));
  return out;
}
export function runScreens(edits: any[], answerIndex: string[], instanceIds: string[], rules?: any): any[] {
  const R = rules ?? loadRules(), idx = new Set(answerIndex), ids = instanceIds.filter(Boolean), v: any[] = [], keys = new Set<string>(R.t4_keys.map((k: string) => k.toLowerCase()));
  for (const e of edits) if (idx.size && [...ngramHashes(e.added ?? "", R.ngram_n)].some((h) => idx.has(h))) v.push({ screen: "answer_echo", path: e.path, detail: "edit contains a span matching a known answer" });
  for (const e of edits) {
    const lines = (e.added ?? "").split("\n").map((l: string) => l.trim()).filter(Boolean), w = R.duplicate_block.window_lines, seen = new Map<string, number>();
    for (let i = 0; i + w <= lines.length; i++) { const blk = lines.slice(i, i + w).join("\n"); if (blk.length >= R.duplicate_block.min_chars) seen.set(blk, (seen.get(blk) ?? 0) + 1); }
    if ([...seen.values()].some((n) => n >= 2)) v.push({ screen: "duplicate_blocks", path: e.path, detail: "a block of lines is repeated" });
  }
  for (const e of edits) if (R.output_interface.some((p: any) => rx(p).test(e.added ?? ""))) v.push({ screen: "output_interface", path: e.path, detail: "edit writes to a score or report path" });
  for (const e of edits) { const hit = R.protected_refs.find((p: any) => rx(p).test(e.added ?? "")); if (hit) v.push({ screen: "protected_refs", path: e.path, detail: `references a protected location (${hit.re})` }); }
  for (const e of edits) { const t = e.added ?? ""; if (ids.some((i) => t.includes(i)) || R.hardcoded_seed.some((p: any) => rx(p).test(t))) v.push({ screen: "hardcoded_ids", path: e.path, detail: "hard-coded instance id or seed" }); }
  for (const e of edits) {
    const b = docOf(e.path, e.before), a = docOf(e.path, e.after);
    if (b === null || a === null) continue;
    const eb = t4Entries(b, keys), ea = t4Entries(a, keys), changed = [...new Set([...Object.keys(eb), ...Object.keys(ea)])].filter((k) => eb[k] !== ea[k]).sort();
    if (changed.length) v.push({ screen: "t4_keys", path: e.path, detail: "changes T4-owned keys: " + changed.join(", ") });
  }
  return v;
}
