/** Coeffect isolation and interception (paper §3.2.3), realised at the value-access layer: derived contexts that change nothing in the shared table. */
import { PreconditionFailed, World } from "./core.js";
export function mergeMeta(a: Record<string, any>, b: Record<string, any>): Record<string, any> {
  const out = { ...a };
  for (const [k, v] of Object.entries(b)) out[k] = Array.isArray(v) && Array.isArray(a[k]) ? [...a[k], ...v.filter((x: any) => !a[k].includes(x))] : v;
  return out;
}
export class Scope {
  constructor(public world: World, public realms: Record<string, string> = {}, public meta: Record<string, Record<string, any>> = {}) {}
  isolate(key: string, realm: string) { return new Scope(this.world, { ...this.realms, [key]: realm }, this.meta); }
  intercept(key: string, meta: Record<string, any>) { return new Scope(this.world, this.realms, { ...this.meta, [key]: mergeMeta(this.meta[key] ?? {}, meta) }); }
  get(key: string, declaredMeta: Record<string, any> = {}): any {
    const realm = this.realms[key] ?? key, sig = this.world.sigma(); if (!sig.has(realm)) throw new PreconditionFailed(`'${key}' (realm '${realm}') is not bound`);
    const m = mergeMeta(declaredMeta, this.meta[key] ?? {}); for (const hook of m.on_access ?? []) hook(key, realm);
    const value = sig.get(realm)![1]; return typeof value === "function" ? value(m) : value;
  }
}
