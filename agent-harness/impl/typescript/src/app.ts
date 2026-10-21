/** Harness: wires services into a runnable daemon-lite (no HTTP layer). */
import { normalizeBudgets, unmetered } from "./budgets.js";
import { DefinitionStore } from "./definitions.js";
import { PluginHost } from "./plugins.js";
import { UNSET, declare as declareProject } from "./project.js";
import { contractFor, contractHash } from "./outcome.js";
import { declarationFor, hiddenState, mountPublicTests, testsHash, validateDeclaration } from "./visibility.js";
import * as M from "./manifest.js";
import fs from "node:fs";
import path from "node:path";
import { ApprovalService } from "./approvals.js";
import { BlobStore } from "./blobs.js";
import { MemoryGuard, registerBuiltins } from "./builtin.js";
import { configCommit } from "./config.js";
import { DictSecretProvider, Redactor } from "./credentials.js";
import { EventStore, EventWriter } from "./eventlog.js";
import { attachEval, Task } from "./eval.js";
import { HttpTransport, ModelGateway } from "./gateway.js";
import { LoopController } from "./loop.js";
import { runPreflight } from "./preflight.js";
import { RecoveryManager } from "./recovery.js";
import { RunManager, Services } from "./run.js";
import { LocalProcessBackend, SandboxManager } from "./sandbox.js";
import { ToolRegistry } from "./registry.js";
import { ToolRuntime } from "./runtime.js";
import { SystemClock, sha256Hex } from "./util.js";
const DEFAULT_SYSTEM = "You are an autonomous software agent. Work in /workspace using the provided tools. Finish with the finish tool when the task is complete.";
const CORE_RULES = { schema_version: 1, rules: [{ id: "R-core-irreversible-in-containment", description: "Core profile: no external effects, so irreversible in-sandbox actions are allowed (§9.6, §16.2)", match: { profile: ["core"], effect_class: ["irreversible"] }, action: "allow" }] };
export const defaultRuleset = (profile: string) => (profile === "core" ? CORE_RULES : { schema_version: 1, rules: [] });

export class Harness {
  plugins: PluginHost; pluginsLoaded = false;
  store: EventStore; writer: EventWriter; blobs: BlobStore; svc: Services; clock: any; secrets: any; redactor = new Redactor(); backend: any; sandboxes: SandboxManager; rm: RunManager;
  registry = new ToolRegistry(); ruleset: any; approvals: ApprovalService; models: Record<string, any>; gateway: ModelGateway; runtime: ToolRuntime; memoryGuard: MemoryGuard; loops = new Map<string, LoopController>(); configDir: string | null; closed = false;
  constructor(public config: any, o: { transport?: any; secrets?: any; backend?: any; clock?: any; ruleset?: any; configDir?: string | null; rng?: () => number; idFactory?: () => string } = {}) {
    this.clock = o.clock ?? new SystemClock();
    fs.mkdirSync(path.dirname(config.storage.event_log.path), { recursive: true });
    this.store = new EventStore(config.storage.event_log.path); this.writer = new EventWriter(this.store, this.clock).start();
    this.blobs = new BlobStore(config.storage.blobs.root); this.svc = new Services(this.store, this.writer, this.blobs, this.clock, config);
    this.secrets = o.secrets ?? new DictSecretProvider({}); this.backend = o.backend ?? new LocalProcessBackend(undefined, config.sandbox?.limits);
    this.sandboxes = new SandboxManager(this.backend, config.sandbox?.allow_dev_tier ?? false); this.rm = new RunManager(this.svc, this.sandboxes);
    registerBuiltins(this.registry); this.ruleset = o.ruleset ?? defaultRuleset(config.profile);
    this.approvals = new ApprovalService(this.svc, this.redactor);
    this.models = Object.fromEntries(config.models.map((m: any) => [m.ref, m]));
    const pc = config.plugins ?? {}; this.plugins = new PluginHost(config.profile, pc.trusted_in_process ?? [], pc.max_failures ?? 3, Boolean(pc.verify_errors)); this.svc.plugins = this.plugins;
    this.gateway = new ModelGateway(this.models, o.transport ?? new HttpTransport(), this.clock, this.secrets, [], { maxAttempts: config.gateway.max_attempts, timeoutS: config.gateway.timeout_s, rng: o.rng, idFactory: o.idFactory });
    this.runtime = new ToolRuntime(this.registry, this.ruleset, this.approvals, this.svc, this.redactor, config.tokens?.divisor ?? 3, config.tools.selection_error_alert);
    this.memoryGuard = new MemoryGuard(this.svc); this.configDir = o.configDir ?? null;
  }
  /** Load the plugins listed in `plugins.load`. A plugin that fails verification stops startup instead of being skipped. */
  async loadPlugins() { if (this.pluginsLoaded) return; this.pluginsLoaded = true; for (const m of this.config.plugins?.load ?? []) await this.plugins.load(m); }
  preflight(profile?: string, extra: any = {}) { return runPreflight(profile ?? this.config.profile, this.config, { store: this.store, backend: this.backend, registry: this.registry, ruleset: this.ruleset, plugin_problems: this.plugins.requirementsMet(this.config.plugins?.required ?? []), ...extra }); }
  defaultAgent(over: any = {}) { return { agent_id: "agent", model_ref: this.config.models[0].ref, system_prompt: DEFAULT_SYSTEM, tool_allowlist: ["*"], skills: [], max_output_tokens: 4096, ...over }; }
  private skillText(name: string): string { const p = this.configDir ? path.join(this.configDir, "skills", name, "SKILL.md") : null; return p && fs.existsSync(p) ? fs.readFileSync(p, "utf8") : name; }
  /** Publish the definitions this run will use as immutable versions and build its provenance manifest (design §13.9). A required field that cannot be recorded refuses the run. */
  private async manifestFor(profile: string, instruction: string, agent: any, budgets: any, extra: any) {
    const ds = new DefinitionStore(this.svc), pins: any[] = []; extra = extra ?? {};
    pins.push(await ds.publish("prompt.system", agent.system_prompt));
    for (const name of agent.skills ?? []) pins.push(await ds.publish(`skill.${name}`, this.skillText(name)));
    for (const name of this.registry.allowed(agent.tool_allowlist)) pins.push(await ds.publish(`tool.${name}`, this.registry.get(name)!.manifest));
    const cfgBudgets = budgets ?? this.config.budgets ?? {};
    pins.push(await ds.publish("budget_policy", normalizeBudgets(cfgBudgets)));
    const { system_prompt: _sp, ...agentRest } = agent;
    pins.push(await ds.publish("workflow", { agent: agentRest, loop: this.config.loop ?? {}, context: this.config.context ?? {} }));
    const tier = { name: this.backend.tier, backend: this.backend.name, profile, limits: this.backend.limits ?? null }, tp = await ds.publish("tier_policy", tier); pins.push(tp);
    for (const [pid, content] of Object.entries<any>(extra.definitions ?? {})) pins.push(await ds.publish(pid, content));
    for (const [pid, content] of Object.entries<any>(this.plugins.definitions())) pins.push(await ds.publish(pid, content));   // the plugins a run uses are pinned like any other definition
    pins.sort((a, b) => (a.id < b.id ? -1 : a.id > b.id ? 1 : 0));
    const m = this.models[agent.model_ref], local = this.backend.name === "local";
    const fields: any = {
      models: M.rec([{ role: "agent", provider: m.provider, model_id: m.model_id, version: m.version ?? m.model_id, snapshot: Boolean(m.snapshot ?? false) }]),
      harness_version: M.rec("0.1.0"), config_commit: M.rec(configCommit(this.config)), profile: M.rec(profile),
      limits: M.rec({ agent: { window: m.window, max_output_tokens: agent.max_output_tokens ?? 4096 } }), definitions: M.rec(pins),
      tier_policy: M.rec({ name: tier.name, version: tp.version, sha256: tp.sha256 }),
      image_digests: local ? M.na("the local backend runs processes, not images") : M.unavailable("this backend does not report image digests"),
      build_hashes: !this.backend.hasNativeBinaries ? M.na("no native executor or egress binaries are used by this backend") : M.unavailable("not recorded"),
      task: extra.task ?? M.na("not an evaluation task"),
      inputs: M.rec({ instruction_sha256: sha256Hex(Buffer.from(instruction, "utf8")), dataset_versions: [], retrieval_index_version: null }, "no datasets or retrieval index are used"),
      budgets: M.rec({ limits: normalizeBudgets(cfgBudgets), unmetered: unmetered(cfgBudgets) }),
      test_visibility: extra.test_visibility ?? M.na("not an evaluation task"), parent_run_id: M.na("a top-level run"), project: extra.project ? M.rec(extra.project) : M.na("project memory is not enabled and no project was declared"), ...M.pendingFinal() };
    const man = { manifest_schema_version: M.SCHEMA_VERSION, run_id: "", fields }, errs = M.validate({ ...man, run_id: "pending" }, profile);
    if (errs.length) throw new M.ManifestError("the run manifest is incomplete: " + errs.join("; "));
    return man;
  }
  async createRun(owner: string, instruction: string, o: { agent?: any; verifier?: any; taskId?: string | null; budgets?: any; evaluatorBudgets?: any; hooks?: any; profile?: string; preflightExtra?: any; manifestExtra?: any; project?: any; askProject?: (q: string) => string | Promise<string> } = {}) {
    await this.loadPlugins(); const decl = await declareProject(this.config, "project" in o ? o.project : UNSET, o.askProject);   // the user says whether this run is part of a project; the harness never guesses (§7.3.10)
    o = { ...o, manifestExtra: { ...(o.manifestExtra ?? {}), project: decl } }; const profile = o.profile ?? this.config.profile, agent = o.agent ?? this.defaultAgent(), pf = this.preflight(profile, o.preflightExtra);
    const manifest = await this.manifestFor(profile, instruction, agent, o.budgets, o.manifestExtra);
    const run = await this.rm.createRun({ owner, profile, instruction, agent, taskId: o.taskId, configCommit: configCommit(this.config), budgets: o.budgets, evaluatorBudgets: o.evaluatorBudgets,
      provenance: { models: Object.fromEntries(Object.entries<any>(this.models).map(([r, m]) => [r, m.model_id])), harness_version: "0.1.0", unmetered: unmetered(this.config.budgets) }, preflight: pf, egress: this.config.egress?.grants, manifest });
    const loop = new LoopController(run, this.rm, this.gateway, this.runtime, this.registry, agent, this.models, this.config, o.verifier ?? null, o.hooks ?? {}, this.configDir, this.memoryGuard);
    this.loops.set(run.runId, loop); return { run, loop };
  }
  async runToEnd(owner: string, instruction: string, o: any = {}) {
    const { run, loop } = await this.createRun(owner, instruction, o);
    if ((await run.state()) === "READY") { await this.rm.start(run); await loop.runLoop(); }
    return run;
  }
  async createEvalRun(owner: string, task: Task, o: { agentOver?: any } & any = {}) {
    const spec = task.spec, bad = validateDeclaration(spec); if (bad.length) throw new Error(bad.join("; "));   // a contradictory declaration is rejected at run start as well as by `harness task validate`
    const decl = declarationFor(spec), contract = contractFor(spec), budgets = { ...this.config.budgets, ...(spec.agent?.budgets ?? {}) };
    const manifestExtra = o.manifestExtra ?? {
      task: M.rec({ id: task.id, version: spec.version, grader_sha256: testsHash(task.testsDir), outcome_contract_sha256: contractHash(contract) }),
      test_visibility: M.rec({ declaration: decl }, "the verified runtime state is recorded when the environment is ready"),
      definitions: { evaluator_rubric: { scoring: spec.scoring, outcome: contract } } };
    const agent = this.defaultAgent({ tool_allowlist: spec.agent?.tool_allowlist ?? ["*"], ...(o.agentOver ?? {}) });
    const { run, loop } = await this.createRun(owner, task.instruction, { ...o, agent, taskId: `${task.id}@${spec.version}`, budgets, manifestExtra });
    const runner = attachEval(this, loop, task);
    if ((await run.state()) === "READY") await this.exposeTests(run, loop, task, decl);   // what the environment actually does, verified and recorded apart from the policy
    return { run, loop, runner };
  }
  private async exposeTests(run: any, loop: any, task: Task, decl: any) {
    const state = (task.spec.visibility ?? "hidden") === "public" ? mountPublicTests(task.testsDir, loop.sandbox().executor) : hiddenState(), { test_ids, ...rest } = state;
    if (test_ids.length) await run.emit("test.exposed", { test_ids, mount_mode: state.mount_mode, read_only_enforced: state.read_only_enforced });
    await run.emit("test_visibility.verified", { declaration: decl, state: rest, task_id: task.id });
  }
  async attach(runId: string, verifier: any = null, hooks: any = {}, agent?: any) {
    const run = this.rm.get(runId), created = this.store.events(runId, { types: ["run.created"] })[0].payload;
    const loop = new LoopController(run, this.rm, this.gateway, this.runtime, this.registry, agent ?? created.agent, this.models, this.config, verifier, hooks, this.configDir, this.memoryGuard);
    this.loops.set(runId, loop); return loop;
  }
  async resumeRun(runId: string, o: { verifier?: any; hooks?: any; downtimeCause?: string; task?: Task } = {}) {
    const loop = await this.attach(runId, o.verifier ?? null, o.hooks ?? {});
    if (o.task) attachEval(this, loop, o.task);
    const plan = await new RecoveryManager(this).recover(runId, o.downtimeCause ?? "harness_failure");
    return { plan, final: await loop.resume(plan) };
  }
  close() { if (!this.closed) { this.closed = true; this.approvals.clearTimers(); this.writer.close(); this.store.close(); } }
}
