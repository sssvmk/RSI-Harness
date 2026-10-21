/** The plugin interface (design §18.11). Mirrors plugins.py: the harness defines the contract (`spec/plugins/kinds.json`), verifies a plugin when it is loaded, and calls it only through a handle that validates,
 *  times, budgets, audits and fails closed. A plugin is also a component of the component runtime. */
import { spawn, ChildProcess } from "node:child_process";
import fs from "node:fs";
import path from "node:path";
import { Component, Provide, World } from "./components/core.js";
import { canonicalize, sha256Hex } from "./util.js";
import { loadSchema, newAjv } from "./registry.js";

export const KINDS: Record<string, any> = JSON.parse(fs.readFileSync(path.resolve(import.meta.dirname, "../../../spec/plugins/kinds.json"), "utf8")).kinds;
export const RANK: Record<string, number> = { core: 0, standard: 1, high: 2 };
export const TRUST: Record<string, string> = { untrusted_output: "untrusted", trusted_output: "tool_trusted" };
export class PluginError extends Error { code = "plugin.error"; }
export class ManifestInvalid extends PluginError { code = "plugin.manifest_invalid"; }
export class PolicyDenied extends PluginError { code = "plugin.policy_denied"; }
export class ConformanceFailed extends PluginError { code = "plugin.conformance_failed"; }
export class PluginUnavailable extends PluginError { code = "plugin.unavailable"; }
export class ContractViolation extends PluginError { code = "plugin.contract_violation"; }
export class PluginTimeout extends PluginError { code = "plugin.timeout"; }
export class PluginFailed extends PluginError { code = "plugin.failed"; }
export class PluginBudgetExhausted extends PluginError { code = "plugin.budget_exhausted"; }
export const ERROR_CODES = ["invalid_argument", "not_found", "permission_denied", "conflict", "unsupported", "unavailable", "rate_limited"];
export const RETRYABLE = new Set(["unavailable", "rate_limited"]);
/** Thrown by a plugin for a failure that the contract declares for the method (an invalid argument, a missing session, ...). `code` is one of ERROR_CODES. */
export class PluginRejection extends Error { constructor(public code: string, message = "") { super(message || code); } }
/** The plugin refused a call with one of the failures its contract declares for the method. `reason` is the contract's error code and `retryable` says whether the same call may succeed later. */
export class PluginRejected extends PluginError { code = "plugin.rejected"; reason: string; retryable: boolean; constructor(reason: string, message = "") { super(message && message !== reason ? `${reason}: ${message}` : reason); this.reason = reason; this.retryable = RETRYABLE.has(reason); } }

const ajv = newAjv();
const checkers = new Map<any, any>();
export function schemaErrors(schema: any, value: any): string[] {
  let v = checkers.get(schema); if (!v) { v = ajv.compile(schema); checkers.set(schema, v); }
  return v(value) ? [] : [...(v.errors ?? [])].map((e: any) => `${(e.instancePath || "").replace(/^\//, "").replaceAll("/", "/") || "(root)"}: ${e.message}`);
}
/** Everything that can be decided from the manifest alone. */
export function validateManifest(m: any): string[] {
  const errs = schemaErrors(loadSchema("plugin.manifest.v1.json"), m); if (errs.length) return errs;
  const k = KINDS[m.kind]; if (!k) return [`unknown plugin kind '${m.kind}' (known: ${Object.keys(KINDS).sort().join(", ")})`];
  const out: string[] = [];
  if (m.interface_version.split(".")[0] !== k.interface_version.split(".")[0]) out.push(`interface ${m.interface_version} is not compatible with ${m.kind} interface ${k.interface_version}`);
  const extra = (m.grants ?? []).filter((g: string) => !k.grants.includes(g)); if (extra.length) out.push(`grants ${JSON.stringify(extra)} are not allowed for a ${m.kind} plugin (allowed: ${JSON.stringify(k.grants)})`);
  const ev = m.evaluator;
  if (m.kind === "evaluator") {   // an evaluator declares its type, and a model-based one declares the model and credential it uses
    if (!ev) out.push("an evaluator plugin must declare evaluator.type (code, llm_judge, or typesafe_ai)");
    else if (ev.type === "code" && (ev.model_ref || ev.credential_ref)) out.push("a code evaluator uses no model, so it must not declare model_ref or credential_ref");
    else if (ev.type !== "code" && !(ev.model_ref && ev.credential_ref)) out.push(`a ${ev.type} evaluator must declare the model_ref and credential_ref it uses, so that its independence can be checked`);
  } else if (ev !== undefined) out.push("only an evaluator plugin may declare an evaluator block");
  if (m.trust === "in_process" && !(m.entry.class || (m.entry.module && m.entry.factory))) out.push("an in_process plugin needs entry.module and entry.factory, or entry.class (module:ClassName)");
  if (m.trust === "subprocess" && !m.entry.command) out.push("a subprocess plugin needs entry.command");
  return out;
}
export function policyErrors(m: any, profile: string, trusted: Set<string>): string[] {
  const k = KINDS[m.kind], out: string[] = [];
  if (RANK[profile] < RANK[k.min_profile]) out.push(`a ${m.kind} plugin needs a ${k.min_profile} deployment or stricter; this one is ${profile}`);
  if (m.trust === "in_process" && RANK[profile] >= RANK.standard && !trusted.has(m.id)) out.push(`${m.id}: standard and high deployments run plugins out of process unless the operator lists them as trusted`);
  return out;
}
/** The independence of an evaluator from the agent under test, derived and never declared (design §13.4). A `code` evaluator is deterministic. A model-based one is independent only if its model, its context and its
 *  credential all differ from the agent's; the dimensions it shares are recorded. */
export function independenceLevel(manifest: any, agent: { model_ref?: string; credential_ref?: string; context?: string }) {
  const ev = manifest.evaluator; if (ev.type === "code") return { level: "deterministic", type: "code", shared: [] as string[] };
  const shared = ([["model", ev.model_ref === agent.model_ref], ["credential", ev.credential_ref === agent.credential_ref], ["context", (ev.context ?? "shared") === "shared"]] as [string, boolean][]).filter(([, same]) => same).map(([d]) => d);
  return { level: shared.length ? "same_model_low_independence" : "independent_model", type: ev.type as string, shared };
}
/** Whether an evaluation at this independence level may gate a promotion, a release or a claim. Core labels and allows; Standard needs a deterministic or independent evaluator; High also needs a claim that rests on an
 *  independent model to be backed by a deterministic check or a second independent evaluator. */
export function mayGate(level: string, profile: string, materialClaim = false, backed = false): [boolean, string] {
  if (profile === "core") return [true, "core labels the level and does not restrict"];
  if (level === "same_model_low_independence") return [false, "an evaluator that shares the agent's model, context or credential is advisory in standard and high deployments"];
  if (profile === "high" && materialClaim && level === "independent_model" && !backed) return [false, "a material claim resting on an independent model needs a deterministic check or a second independent evaluator behind it"];
  return [true, "independent enough to gate"];
}
/** What the harness may claim about exfiltration monitoring (design §10.6). The core monitors network activity and claims partial conformance. It claims payload inspection only when the isolation plugin's verified settings contain an
 *  `egress_inspection` record that names a verified inspection point, says the policy is applied and the outcomes are recorded for audit, and documents its coverage gaps and bypass conditions. TLS interception alone is not enough. */
export function exfiltrationClaim(observed: any) {
  const eg = observed?.egress_inspection;
  if (!eg || typeof eg !== "object" || Array.isArray(eg)) return { claim: "partial", reasons: ["the isolation plugin reported no payload inspection point"], coverage_gaps: [] as string[], bypass_conditions: [] as string[] };
  const why: string[] = [];
  if (!(typeof eg.inspection_point === "string" && eg.inspection_point.trim())) why.push("no inspection point is named");
  if (eg.policy_applied !== true) why.push("the exfiltration policy is not applied");
  if (eg.audit_records !== true) why.push("inspection outcomes are not recorded for audit");
  if (!Array.isArray(eg.coverage_gaps)) why.push("coverage gaps are not documented");
  if (!Array.isArray(eg.bypass_conditions)) why.push("bypass conditions are not documented");
  if (eg.verified !== true) why.push("the inspection point has not been verified to be effective");
  return { claim: why.length ? "partial" : "payload_inspection", reasons: why, coverage_gaps: Array.isArray(eg.coverage_gaps) ? eg.coverage_gaps : [], bypass_conditions: Array.isArray(eg.bypass_conditions) ? eg.bypass_conditions : [] };
}
export const manifestHash = (m: any) => sha256Hex(canonicalize(m));
export const fileSha256 = (p: string) => sha256Hex(fs.readFileSync(p));
export interface PluginResult { value: any; trust: string; plugin_id: string; method: string }
interface Impl { rpc(method: string, params: any, timeoutS?: number): Promise<any>; close(): Promise<void> }
/** Adapts an object whose methods are named like the contract's methods (sync or async) to the one `rpc` shape the host uses. */
export class InProcessPlugin implements Impl {
  constructor(public obj: any) {}
  async rpc(method: string, params: any) { const fn = this.obj?.[method]; if (typeof fn !== "function") throw new PluginFailed(`the plugin has no method ${method}`); return fn.call(this.obj, params); }
  async close() {}
}
/** A plugin in its own process, speaking newline-delimited JSON over stdio. A call that times out kills the process (fail closed), and the next call starts a fresh one. */
export class SubprocessPlugin implements Impl {
  proc: ChildProcess | null = null; private n = 0; private buf = ""; private waiting: ((l: string) => void)[] = []; private lines: string[] = [];
  constructor(public command: string[], public env?: NodeJS.ProcessEnv, public cwd?: string) {}
  private start() {
    if (this.proc && this.proc.exitCode === null && !this.proc.killed) return;
    this.buf = ""; this.lines = []; this.waiting = []; const p = spawn(this.command[0], this.command.slice(1), { stdio: ["pipe", "pipe", "ignore"], env: this.env, cwd: this.cwd }); this.proc = p;
    p.stdout!.on("data", (d: Buffer) => { this.buf += d.toString("utf8"); let i; while ((i = this.buf.indexOf("\n")) >= 0) { const l = this.buf.slice(0, i); this.buf = this.buf.slice(i + 1); const w = this.waiting.shift(); if (w) w(l); else this.lines.push(l); } });
    p.on("close", () => { for (const w of this.waiting.splice(0)) w(""); });
  }
  private line(): Promise<string> { const l = this.lines.shift(); return l !== undefined ? Promise.resolve(l) : new Promise((res) => this.waiting.push(res)); }
  async rpc(method: string, params: any, timeoutS = 30) {
    this.start(); const rid = ++this.n; let timer: NodeJS.Timeout | undefined;
    const go = (async () => { this.proc!.stdin!.write(JSON.stringify({ id: rid, method, params }) + "\n"); const l = await this.line(); if (!l) throw new PluginFailed("the plugin process closed its output"); return JSON.parse(l); })();
    const resp: any = await Promise.race([go, new Promise((_, rej) => { timer = setTimeout(() => rej(Object.assign(new Error("timeout"), { isTimeout: true })), timeoutS * 1000); })]).catch(async (e) => { if (e?.isTimeout) await this.close(); throw e; }).finally(() => clearTimeout(timer));
    if (resp.id !== rid) throw new PluginFailed("the plugin answered a different request");
    if (resp.error) {
      if (ERROR_CODES.includes(resp.error.code)) throw new PluginRejection(resp.error.code, resp.error.message ?? "");   // a contract error crosses the process boundary as its code
      throw new PluginFailed(`${resp.error.code ?? "error"}: ${resp.error.message ?? ""}`);
    }
    return resp.result;
  }
  async close() { const p = this.proc; this.proc = null; if (p && p.exitCode === null) { await new Promise<void>((res) => { p.once("close", () => res()); p.kill("SIGKILL"); }); } }
}
const withTimeout = <T>(p: Promise<T>, s: number): Promise<T> => new Promise((res, rej) => { const t = setTimeout(() => rej(Object.assign(new Error("timeout"), { isTimeout: true })), s * 1000); p.then((v) => { clearTimeout(t); res(v); }, (e) => { clearTimeout(t); rej(e); }); });
/** The only way the harness calls a plugin: input and output are validated against the kind's contract, every call has a timeout, and repeated failures quarantine the plugin. */
export class PluginHandle {
  id: string; kind: string; state = "active"; failures = 0;
  constructor(public host: PluginHost, public manifest: any, public impl: Impl, public fiber: string, public codeSha256: string | null) { this.id = manifest.id; this.kind = manifest.kind; }
  async call(method: string, args: any = {}, run: any = null, budget = true): Promise<PluginResult> {
    const spec = KINDS[this.kind].methods[method]; if (!spec) throw new ContractViolation(`${this.kind} has no method '${method}'`);
    const bad = schemaErrors(spec.input, args); if (bad.length) throw new ContractViolation(`${this.id}.${method} input: ${bad[0]}`);
    const f = this.host.world.fibers.get(this.fiber); if (this.state !== "active" || !f || f.state !== "ACTIVE") throw new PluginUnavailable(`plugin ${this.id} is ${this.state}`);
    let rid: string | null = null, bc: any = null;
    if (run && budget) { const { BudgetController } = await import("./budgets.js"); bc = new BudgetController(run); const [r, ex] = await bc.reserve({ tools: 1 }); if (ex) throw new PluginBudgetExhausted(`the ${ex} budget cannot cover a plugin call`); rid = r; }
    const t0 = Date.now(); let code: string | null = null;
    try {
      let out: any;
      try { out = await withTimeout(this.impl.rpc(method, args, spec.timeout_s), spec.timeout_s); }
      catch (e: any) {
        if (e?.isTimeout) { code = "timeout"; throw new PluginTimeout(`${this.id}.${method} exceeded ${spec.timeout_s}s`); }
        if (e instanceof PluginRejection) {   // a refusal the contract may declare; anything undeclared is a contract violation
          if (!(spec.errors ?? []).some((x: any) => x.code === e.code)) { code = "contract_violation"; throw new ContractViolation(`${this.id}.${method} signalled '${e.code}', which the contract does not declare for this method`); }
          code = `rejected.${e.code}`; throw new PluginRejected(e.code, e.message);
        }
        if (e instanceof PluginError) { code = e.code; throw e; } code = "failed"; throw new PluginFailed(`${this.id}.${method}: ${e?.constructor?.name ?? "Error"}`); }
      const bad2 = schemaErrors(spec.output, out); if (bad2.length) { code = "contract_violation"; throw new ContractViolation(`${this.id}.${method} output: ${bad2[0]}`); }
      this.failures = 0;
      if (run) { await run.emit("plugin.called", { plugin_id: this.id, kind: this.kind, method, ok: true, duration_ms: Date.now() - t0, trust: TRUST[KINDS[this.kind].trust] }); if (rid) await bc.commit(rid, { tools: 1 }); }
      return { value: out, trust: TRUST[KINDS[this.kind].trust], plugin_id: this.id, method };
    } catch (e) {
      if (!(e instanceof PluginError)) throw e;
      if (e instanceof PluginRejected) {   // a correct refusal is not a plugin fault, except that a backend that is down is
        const counts = e.reason === "unavailable"; if (counts) this.failures++;
        if (run) {
          await run.emit("plugin.called", { plugin_id: this.id, kind: this.kind, method, ok: false, error: e.reason, retryable: e.retryable, duration_ms: Date.now() - t0, trust: TRUST[KINDS[this.kind].trust] }); if (rid) await bc.commit(rid, { tools: 1 });
          if (counts && this.failures >= this.host.maxFailures && this.state === "active") await run.emit("plugin.quarantined", { plugin_id: this.id, kind: this.kind, after_failures: this.failures });
        }
        if (counts && this.failures >= this.host.maxFailures) this.state = "quarantined";
        throw e;
      }
      this.failures++;
      if (run) {
        await run.emit("plugin.failed", { plugin_id: this.id, kind: this.kind, method, error: code, consecutive_failures: this.failures }); if (rid) await bc.commit(rid, { tools: 1 });   // a failed call still cost a call
        if (this.failures >= this.host.maxFailures && this.state === "active") await run.emit("plugin.quarantined", { plugin_id: this.id, kind: this.kind, after_failures: this.failures });
      }
      if (this.failures >= this.host.maxFailures) this.state = "quarantined";
      throw e;
    }
  }
}
export class PluginHost {
  world = new World(); handles = new Map<string, PluginHandle>(); history: any[] = []; trusted: Set<string>;
  constructor(public profile = "core", trusted: Iterable<string> = [], public maxFailures = 3, public verifyErrors = false) { if (!(profile in RANK)) throw new Error(`unknown profile '${profile}'`); this.trusted = new Set(trusted); }
  /** Verify a plugin and make it available. Nothing is partly loaded: if any check fails the plugin is not registered. */
  async load(manifest: any, impl: any = null, codePath: string | null = null): Promise<PluginHandle> {
    let errs = validateManifest(manifest); if (errs.length) { this.note("plugin.rejected", manifest, errs); throw new ManifestInvalid(errs.join("; ")); }
    errs = policyErrors(manifest, this.profile, this.trusted); if (errs.length) { this.note("plugin.rejected", manifest, errs); throw new PolicyDenied(errs.join("; ")); }
    if (this.handles.has(manifest.id)) throw new ManifestInvalid(`plugin ${manifest.id} is already loaded`);
    if ([...this.handles.values()].some((h) => h.kind === manifest.kind && h.state !== "unloaded")) throw new ManifestInvalid(`a ${manifest.kind} plugin is already loaded; unload it first`);
    const code = codePath ? fileSha256(codePath) : manifest.entry.code_sha256 ?? null;
    if (codePath && manifest.entry.code_sha256 && manifest.entry.code_sha256 !== code) { this.note("plugin.rejected", manifest, ["code hash mismatch"]); throw new ManifestInvalid("the plugin's code does not match the hash in its manifest"); }
    if (impl === null) impl = await this.build(manifest); else if (typeof impl.rpc !== "function") impl = new InProcessPlugin(impl);
    const problems = await this.conformance(manifest, impl); if (problems.length) { await impl.close(); this.note("plugin.rejected", manifest, problems); throw new ConformanceFailed(problems.join("; ")); }
    const key = `plugin.${manifest.kind}`, comp = new Component(`plugin.${manifest.id}`, () => [new Provide(key, manifest.id)], { provides: { [key]: manifest.version }, version: manifest.version });
    const fiber = this.world.insert(comp); this.world.settle(); const h = new PluginHandle(this, manifest, impl, fiber, code); this.handles.set(manifest.id, h); this.note("plugin.loaded", manifest, []); return h;
  }
  private async build(m: any): Promise<Impl> {
    if (m.trust === "subprocess") return new SubprocessPlugin(m.entry.command);
    if (m.entry.class) {   // an implementation class named in the deployment configuration, like a Java class named in a config file
      const i = m.entry.class.lastIndexOf(":"), modName = m.entry.class.slice(0, i), clsName = m.entry.class.slice(i + 1), reject = (why: string, msg: string) => { this.note("plugin.rejected", m, [why]); return new ManifestInvalid(msg); };
      let mod: any; try { mod = await import(modName); } catch (x: any) { throw reject(`cannot load ${m.entry.class}`, `cannot load ${m.entry.class}: ${x?.message}`); }
      const cls = mod[clsName]; if (typeof cls !== "function" || !cls.prototype) throw reject("not a class", `cannot load ${m.entry.class}: ${clsName} is not a class in that module`);
      const missing = Object.keys(KINDS[m.kind].methods).filter((n) => typeof cls.prototype[n] !== "function");
      if (missing.length) throw reject("does not implement the interface", `${m.entry.class} must be a class that implements the ${m.kind} interface: it has no ${missing.join(", ")} (abstract methods left unimplemented)`);
      let obj: any; try { obj = new cls(m.entry.config ?? {}); } catch (x: any) { throw reject(String(x?.message), `${m.entry.class} cannot be created with its configuration: ${x?.message}`); }
      return new InProcessPlugin(obj);
    }
    const mod = await import(m.entry.module); return new InProcessPlugin(await mod[m.entry.factory](m));
  }
  /** Call every method of the contract with its sample input and check what comes back. `describe` must say what the manifest says, and `health` must be ok. */
  private async conformance(m: any, impl: Impl): Promise<string[]> {
    const out: string[] = [], made: Record<string, any> = {};   // ids the plugin created, which the calls that follow must use
    for (const [name, spec] of Object.entries<any>(KINDS[m.kind].methods)) {
      const args = { ...spec.sample, ...Object.fromEntries(Object.entries(made).filter(([k]) => k in spec.input.properties)) };
      let r: any; try { r = await withTimeout(impl.rpc(name, args, spec.timeout_s), spec.timeout_s); } catch (e: any) { out.push(e?.isTimeout ? `${name}: timed out` : `${name}: ${e?.constructor?.name}: ${e?.message}`); continue; }
      const bad = schemaErrors(spec.output, r); if (bad.length) { out.push(`${name}: output ${bad[0]}`); continue; }
      for (const [creator, key] of [["open", "session"], ["fork", "fork_id"], ["provision", "sandbox_id"]]) if (name === creator && r && key in r) made[key] = r[key];
      if (name === "describe" && (r.id !== m.id || r.kind !== m.kind || r.version !== m.version)) out.push(`describe says ${r.id}/${r.kind}/${r.version}, the manifest says ${m.id}/${m.kind}/${m.version}`);
      if (name === "health" && !r.ok) out.push("health is not ok");
    }
    if (this.verifyErrors) out.push(...(await this.probes(m, impl)));
    return out;
  }
  /** Run each declared error's probe, a safe input that must raise exactly that error (design §18.11). A plugin that does not signal what its contract says it signals is not loaded. */
  private async probes(m: any, impl: Impl): Promise<string[]> {
    const out: string[] = [];
    for (const [name, spec] of Object.entries<any>(KINDS[m.kind].methods)) for (const e of spec.errors ?? []) {
      if (e.probe == null) continue;
      try { await withTimeout(impl.rpc(name, e.probe, spec.timeout_s), spec.timeout_s); out.push(`${name}: the ${e.code} probe did not raise`); }
      catch (x: any) { if (x instanceof PluginRejection) { if (x.code !== e.code) out.push(`${name}: the ${e.code} probe raised ${x.code}`); } else if (x?.isTimeout) out.push(`${name}: the ${e.code} probe timed out`); else out.push(`${name}: the ${e.code} probe raised ${x?.constructor?.name}, not a contract error`); }
    }
    return out;
  }
  async unload(id: string) {
    const h = this.handles.get(id); if (!h) throw new PluginUnavailable(`plugin ${id} is not loaded`);
    h.state = "unloaded"; this.world.retire(h.fiber); this.world.settle(); this.world.sweep(); await h.impl.close(); this.handles.delete(id); this.note("plugin.unloaded", h.manifest, []);
  }
  async replace(manifest: any, impl: any = null, codePath: string | null = null) { const old = [...this.handles.values()].find((h) => h.kind === manifest.kind); if (old) await this.unload(old.id); return this.load(manifest, impl, codePath); }
  async close() { for (const id of [...this.handles.keys()]) await this.unload(id); }
  has(kind: string) { return [...this.handles.values()].some((h) => h.kind === kind && h.state === "active"); }
  /** The active plugin of a kind. A feature that needs a plugin that is not there does not run (fail closed). */
  get(kind: string): PluginHandle { const h = [...this.handles.values()].find((x) => x.kind === kind && x.state === "active"); if (!h) throw new PluginUnavailable(`no active ${kind} plugin is loaded`); return h; }
  definition(id: string) { const h = this.handles.get(id); if (!h) throw new PluginUnavailable(`plugin ${id} is not loaded`); return { manifest: h.manifest, code_sha256: h.codeSha256 }; }
  /** What a run pins: the manifest and code hash of every loaded plugin, so a plugin swapped during a run is a definition mismatch. */
  definitions(): Record<string, any> { return Object.fromEntries([...this.handles.keys()].sort().map((id) => [`plugin.${id}`, this.definition(id)])); }
  requirementsMet(kinds: string[]): string[] { return kinds.filter((k) => !this.has(k)).map((k) => `no active ${k} plugin is loaded`); }
  private note(event: string, m: any, problems: string[]) { this.history.push({ event, plugin_id: m.id, kind: m.kind, version: m.version, problems }); }
}
