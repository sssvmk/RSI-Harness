/** Foundation: clock, ULIDs, RFC 8785 canonical JSON, hash chain, timestamps (design §4, §5.2, App. A.0). */
import { createHash, randomBytes } from "node:crypto";
import canonicalizeLib from "canonicalize";

export interface Clock { nowMs(): number; monoMs(): number; sleep(ms: number): Promise<void>; }
export class SystemClock implements Clock {
  nowMs() { return Date.now(); }
  monoMs() { return performance.now(); }
  sleep(ms: number) { return new Promise<void>((r) => setTimeout(r, ms)); }
}
export class FakeClock implements Clock {
  slept: number[] = [];
  constructor(public t = 1_792_400_000_000) {}
  nowMs() { return this.t; }
  monoMs() { return this.t; }
  advance(ms: number) { this.t += Math.trunc(ms); }
  async sleep(ms: number) { this.slept.push(ms); this.t += Math.trunc(ms); }
}

export function formatTs(ms: number): string { return new Date(ms).toISOString(); }  // always RFC 3339 UTC with milliseconds
export function parseTs(s: string): number { return Date.parse(s); }

const ENC = "0123456789ABCDEFGHJKMNPQRSTVWXYZ";
export function ulid(nowMs = Date.now(), rand?: bigint): string {
  const r = rand ?? BigInt("0x" + randomBytes(10).toString("hex"));
  let n = (BigInt(nowMs) << 80n) | r;
  let out = "";
  for (let i = 0; i < 26; i++) { out = ENC[Number(n & 31n)] + out; n >>= 5n; }
  return out;
}

export const MAX_SAFE_INT = 2 ** 53;
export function canonicalize(o: unknown): string { return (canonicalizeLib as any)(o) as string; }
export function sha256Hex(d: string | Uint8Array): string { return createHash("sha256").update(d).digest("hex"); }
export function assertSafeInts(o: any, path = "$"): void {
  if (typeof o === "number") { if (Number.isInteger(o) && Math.abs(o) > MAX_SAFE_INT) throw new Error(`integer at ${path} exceeds +-2^53; encode as a decimal string`); }
  else if (Array.isArray(o)) o.forEach((v, i) => assertSafeInts(v, `${path}[${i}]`));
  else if (o && typeof o === "object") for (const [k, v] of Object.entries(o)) assertSafeInts(v, `${path}.${k}`);
}

export const ENVELOPE_FIELDS = ["run_id", "seq", "ts", "type", "actor", "causation_seq", "correlation_id", "schema_version", "payload"] as const;
export function genesisHash(runId: string): string { return sha256Hex("harness:run:" + runId); }
export function eventMaterial(ev: any): string {
  const m: any = {};
  for (const k of ENVELOPE_FIELDS) m[k] = ev[k] ?? null;
  return canonicalize(m);
}
export function computeHash(prev: string, ev: any): string { return sha256Hex(Buffer.concat([Buffer.from(prev, "ascii"), Buffer.from("\n"), Buffer.from(eventMaterial(ev), "utf8")])); }

export const sleepReal = (ms: number) => new Promise<void>((r) => setTimeout(r, ms));
export function deepClone<T>(o: T): T { return JSON.parse(JSON.stringify(o)); }
