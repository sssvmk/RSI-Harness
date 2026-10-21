/** Credentials and secrets (design §12). */
import fs from "node:fs";
import path from "node:path";
import { Clock, SystemClock } from "./util.js";
export class Secret { constructor(private v: string) {} reveal() { return this.v; } toString() { return "Secret(***)"; } toJSON() { return "Secret(***)"; } }
export interface SecretProvider { get(name: string): Secret; }
export class DictSecretProvider implements SecretProvider {
  constructor(public d: Record<string, string>) {}
  get(name: string) { if (!(name in this.d)) throw new KeyError(`secret ${name} unavailable`); return new Secret(this.d[name]); }
}
export class EnvSecretProvider implements SecretProvider {
  get(name: string) { const v = process.env[name.toUpperCase().replace("/", "_")]; if (v === undefined) throw new KeyError(`secret ${name} unavailable`); return new Secret(v); }
}
export class FileSecretProvider implements SecretProvider { constructor(public root: string) {} get(name: string) { return new Secret(fs.readFileSync(path.join(this.root, name), "utf8").trim()); } }
export class KeyError extends Error {}
const SHAPES = [/sk-[A-Za-z0-9_\-]{16,}/g, /AKIA[0-9A-Z]{16}/g, /bearer\s+[A-Za-z0-9._\-]{16,}/gi, /eyJ[A-Za-z0-9_\-]{10,}\.[A-Za-z0-9_\-]{10,}\.[A-Za-z0-9_\-]{10,}/g];
export class Redactor {
  known: string[];
  constructor(known: string[] = []) { this.known = known.filter(Boolean); }
  add(v: string) { if (v) this.known.push(v); }
  redact(text: string): string {
    for (const k of [...this.known].sort((a, b) => b.length - a.length)) text = text.split(k).join("[REDACTED]");
    for (const rx of SHAPES) text = text.replace(rx, "[REDACTED]");
    return text;
  }
  redactObj(o: any): any {
    if (typeof o === "string") return this.redact(o);
    if (Array.isArray(o)) return o.map((v) => this.redactObj(v));
    if (o && typeof o === "object") return Object.fromEntries(Object.entries(o).map(([k, v]) => [k, this.redactObj(v)]));
    return o;
  }
}
export class CredentialUnavailable extends Error { code = "gateway.credential_unavailable"; }
/** Refreshes proactively at 50% of lifetime; never revokes a token that still has live leases (§12.3). */
export class TokenSource {
  cur: any[] | null = null; old: any[][] = []; failed = false;
  constructor(public fetch: () => [string, number], public clock: Clock = new SystemClock()) {}
  private refresh() {
    let r: [string, number];
    try { r = this.fetch(); } catch { this.failed = true; return; }
    this.failed = false; if (this.cur) this.old.push(this.cur);
    this.cur = [r[0], this.clock.nowMs(), r[1] * 1000, 0];
  }
  lease() {
    const now = this.clock.nowMs();
    if (this.cur === null || now - this.cur[1] >= this.cur[2] * 0.5) this.refresh();
    if (this.cur === null || (this.failed && now - this.cur[1] >= this.cur[2])) throw new CredentialUnavailable("token refresh failed");
    this.cur[3] += 1; return this.cur;
  }
  release(l: any[]) { l[3] -= 1; }
  revocable(): string[] { const free = this.old.filter((o) => o[3] <= 0); this.old = this.old.filter((o) => o[3] > 0); return free.map((o) => o[0]); }
}
