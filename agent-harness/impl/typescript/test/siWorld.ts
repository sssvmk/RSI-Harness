/** A simulated improvement environment: real git repos and event log, scripted proposer, deterministic content-driven observer (mirrors tests/si_world.py). */
import fs from "node:fs";
import path from "node:path";
import { BlobStore } from "../src/blobs.js";
import { EventStore, EventWriter } from "../src/eventlog.js";
import { CampaignContext, CampaignController } from "../src/selfimprove/campaign.js";
import { EvalSetRegistry } from "../src/selfimprove/evalsets.js";
import { GitRepo } from "../src/selfimprove/gitrepo.js";
import { GatePolicy } from "../src/selfimprove/policy.js";
import { PromotionService } from "../src/selfimprove/promotion.js";
import { ComponentRegistry } from "../src/selfimprove/registry.js";
import { applyMigration002 } from "../src/selfimprove/schema.js";
import { buildAnswerIndex } from "../src/selfimprove/screens.js";
import { FakeClock, sha256Hex, ulid } from "../src/util.js";

export const REGISTRY_YAML = `
schema_version: 1
components:
  - {path: "prompts/**", component: system_prompt, tier: T1, owner: platform, origin: human}
  - {path: "memory/playbook.jsonl", component: playbook, tier: T0, owner: platform, origin: learned}
  - {path: "tools/**", component: tool_description, tier: T1, owner: platform, origin: human}
  - {path: "skills/triage/**", component: skill, tier: T2, owner: ops, origin: learned, provides: [skill.triage]}
  - {path: "workflows/**", component: workflow, tier: T2, owner: ops, origin: learned, requires: [skill.triage]}
`;
export const CONFIG_FILES: Record<string, string> = { "components.yaml": REGISTRY_YAML, "prompts/system.md": "You are an agent.\nBe careful.\n", "prompts/settings.yaml": "temperature: 0.2\nbudgets:\n  turns: 10\nmodel: base\n",
  "memory/playbook.jsonl": "", "tools/search.md": "Search the web.\n", "skills/triage/SKILL.md": "Triage.\n", "workflows/flow.yaml": "steps: []\n" };
export const SCORING_IDS = Array.from({ length: 30 }, (_, i) => `s${String(i).padStart(3, "0")}`), HELDOUT_IDS = Array.from({ length: 20 }, (_, i) => `h${String(i).padStart(3, "0")}`), MUST_PASS = SCORING_IDS.slice(0, 3);
export const PARAMS = { alpha: 0.05, B: 400, holm: true, margins: { default: 0.02 }, heldout_margin: 0.02, preserved_margin: 0.02 };
export const MANIFEST = { failure_evidence: ["pat-1"], root_cause: "x", fix: "y", predicted_fixes: [{ metric: "target", direction: "up" }], at_risk_regressions: [{ metric: "cost" }] };
const u = (...parts: any[]) => parseInt(sha256Hex(parts.join("|")).slice(0, 8), 16) / 4294967296.0;
export const editHint = (text: string, marker = "Be careful.\n") => ({ path: "prompts/system.md", op: "replace", search: marker, replace: marker + text + "\n" });
export const proposal = (...edits: any[]) => ({ edits, manifest: MANIFEST });
export const HINT = (t: string) => proposal(editHint(t));
const count = (text: string, tag: string) => text.split(tag).length - 1;

/** Scores depend on the *content* of the prompt at a commit: HINT+ helps, HINT- hurts, VERBOSE raises cost, OVERFIT helps scoring and hurts held-out, FMT- hurts only the
 *  format-varied variant, BREAK- breaks must-pass instances. */
export class SimObserver {
  calls: any[][] = [];
  constructor(public repo: GitRepo) {}
  scores(commit: string, role: string, variant: string, model: string, seed: string) {
    this.calls.push([commit, role, variant, model, seed]);
    const text = (this.repo.showFile(commit, "prompts/system.md") ?? "") + "\n" + (this.repo.showFile(commit, "skills/triage/SKILL.md") ?? "") + "\n" + (this.repo.showFile(commit, "memory/playbook.jsonl") ?? ""), n = (t: string) => count(text, t);
    let eff = 0.1 * n("HINT+") - 0.1 * n("HINT-");
    eff += role === "scoring" ? 0.2 * n("OVERFIT") : -0.3 * n("OVERFIT");
    if (variant === "format_varied") eff = eff * 0.9 - 0.2 * n("FMT-");
    const ids = role === "scoring" ? SCORING_IDS : HELDOUT_IDS, target: any = {}, cost: any = {}, passes: any = {};
    for (const i of ids) {
      const base = 0.4 + 0.2 * u("base", i); let s = base + (eff ? eff * (0.5 + u("eff", i)) : 0.0);
      if (n("BREAK-") && MUST_PASS.includes(i)) s = 0.0;
      target[i] = s; cost[i] = 1.0 + 0.05 * n("VERBOSE"); passes[i] = s >= 0.5 ? 1 : 0;
    }
    return { target, guardrails: { cost }, passes, usage: { compute: 1.0, cost: 0.5 } };
  }
}
export class ScriptedProposer {
  calls: any[] = [];
  constructor(public script: Record<string, any>) {}
  propose(r: number, n: number, base: string, _ctx: any) { this.calls.push([r, n, base]); const v = this.script[`${r},${n}`] ?? this.script.default; return typeof v === "function" ? v(r, n, base) : v ?? null; }
}
export class World {
  clock: FakeClock; store!: EventStore; writer!: EventWriter; blobs!: BlobStore; config!: GitRepo; improver!: GitRepo; control!: GitRepo; registry = ComponentRegistry.fromYaml(REGISTRY_YAML);
  policy?: GatePolicy; cid: string; observer!: SimObserver; proposer: ScriptedProposer; params: any; high: boolean; hooks: any; spec: any; answers: string[]; ctx!: CampaignContext; evalsets!: EvalSetRegistry; promotion!: PromotionService;
  constructor(public tmp: string, script: Record<string, any>, o: { high?: boolean; specOver?: any; params?: any; review?: any; answers?: string[]; clock?: FakeClock; hooks?: any; cid?: string; policy?: GatePolicy } = {}) {
    fs.mkdirSync(tmp, { recursive: true }); this.clock = o.clock ?? new FakeClock(); this.store = new EventStore(path.join(tmp, "log.db")); this.writer = new EventWriter(this.store, this.clock).start(); this.blobs = new BlobStore(path.join(tmp, "blobs"));
    const mk = (name: string, files: Record<string, string>) => (fs.existsSync(path.join(tmp, name, ".git")) ? new GitRepo(path.join(tmp, name)) : GitRepo.init(path.join(tmp, name), files));
    this.config = mk("config", CONFIG_FILES); this.improver = mk("improver", { "prompts/proposer.md": "Propose edits.\n" }); this.control = mk("control", { "improvement.yaml": "gate: {alpha: 0.05}\n" });
    this.cid = o.cid ?? ulid(); this.observer = new SimObserver(this.config); this.proposer = new ScriptedProposer(script); this.params = o.params ?? PARAMS; this.high = o.high ?? false; this.hooks = o.hooks ?? {}; this.policy = o.policy; this.answers = o.answers ?? [];
    this.spec = { campaign_id: this.cid, base: { repo: "harness-config", ref: "main" }, target_models: ["main"], workload: { feedback: "set-f", scoring: "set-s", heldout: "set-h", confirm: "set-c", transfer: [], must_pass_ids: MUST_PASS, target_direction: "maximize", guardrail_directions: { cost: "minimize" } },
      tiers_allowed: ["T0", "T1"], improver: { commit: this.improver.head() }, search: { mode: "mixed", strategy: "archive", candidates_per_round: 2 }, budgets: { proposals: 40, candidate_evals: 80, compute: 2000, cost: 500.0, time_s: 86400 }, stop: { rounds_max: 3, plateau: { window: 3, epsilon: 0.0 } }, dry_run: false };
    for (const [k, v] of Object.entries<any>(o.specOver ?? {})) this.spec[k] = v && typeof v === "object" && !Array.isArray(v) && typeof this.spec[k] === "object" ? { ...this.spec[k], ...v } : v;
    this.ctx = new CampaignContext(this.store, this.writer, this.blobs, this.clock, this.cid, "alice"); this.evalsets = new EvalSetRegistry(this.store, this.writer, this.clock, 20);
    this.promotion = new PromotionService(this.ctx, this.config, this.registry, { t1_sample_rate: 0.2, high_assurance: this.high, ...(o.review ?? {}) }, o.policy);
  }
  async setup() {
    await applyMigration002(this.writer);
    for (const [sid, role, ids] of [["set-s", "scoring", SCORING_IDS], ["set-h", "heldout", HELDOUT_IDS]] as const)
      if (!this.evalsets.sets.has(sid)) await this.evalsets.register(sid, role, ids.map((i, k) => ({ id: i, text: `${role} task number ${k} about topic ${i} ${role}` })));
    return this;
  }
  controller(o: any = {}) {
    return new CampaignController(this.ctx, this.spec, { configRepo: this.config, improverRepo: this.improver, controlRepo: this.control, registry: this.registry, evalsets: this.evalsets, promotion: this.promotion, proposer: this.proposer, observer: this.observer,
      params: this.params, policy: this.policy, highAssurance: this.high, hooks: { ...this.hooks, ...(o.hooks ?? {}) }, answerIndex: buildAnswerIndex(this.answers), instanceIds: [...SCORING_IDS, ...HELDOUT_IDS], ...o, hooksOverride: undefined } as any);
  }
  close() { this.writer.close(); this.store.close(); }
  events(...t: string[]) { return this.ctx.events(...t); }
}
export const byType = (w: World, t: string) => w.events(t).map((e: any) => e.payload);
export const decisions = (w: World) => Object.fromEntries(w.events("gate.decided").map((e: any) => [e.payload.proposal_id, e.payload]));
