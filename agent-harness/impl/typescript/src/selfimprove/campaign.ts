/** CampaignController (§17.3): mine -> propose -> validate -> evaluate -> gate -> promote, with budgets, plateau stop and recovery.
 *  Every step is idempotent by event key, so a crash at any event boundary resumes without double-counted budgets or a double promotion (§19.7 test 11).
 *  Budget use is a fold over events. The model-driven parts are interfaces (`proposer.propose`, `observer.scores`); neither is implemented here. */
import { canonicalize, formatTs, parseTs, sha256Hex } from "../util.js";
import { rowToEvent } from "../eventlog.js";
import { mean } from "../stats.js";
import * as G from "./gate.js";
import { EditApplier } from "./edits.js";
import { EvalSetError, evalSeed } from "./evalsets.js";
import { GitRepo } from "./gitrepo.js";
import { GatePolicy } from "./policy.js";
import * as OBS from "./observe.js";
import { loadRules, observation } from "../failure.js";
import { PromotionService } from "./promotion.js";
import { ComponentRegistry } from "./registry.js";
import { runScreens } from "./screens.js";
import { noveltyRejected } from "./search.js";

export class StopCampaign extends Error { constructor(public reason: string, public failureClass: string | null = null) { super(reason); } }
export class RecursionDenied extends Error {}
const clone = <T>(x: T): T => JSON.parse(JSON.stringify(x));
export const deriveId = (...parts: any[]) => sha256Hex(parts.map(String).join(":")).slice(0, 26).toUpperCase();

/** Event access for one campaign (a run of kind improvement_campaign). */
export class CampaignContext {
  constructor(public store: any, public writer: any, public blobs: any, public clock: any, public campaignId: string, public owner: string) {}
  async ensureRun(base: string, depth: number, parent: string | null) {
    if (!this.store.runRow(this.campaignId)) await this.writer.submit({ run_id: this.campaignId, type: "run.created", actor: { kind: "system", id: "campaign" }, payload: { owner: this.owner, profile: "core", config_commit: base, kind: "improvement_campaign", parent_run_id: parent, depth } });
  }
  find(key: string, typ?: string): any | null {
    const r = typ ? this.store.get("SELECT * FROM events WHERE run_id=? AND correlation_id=? AND type=? ORDER BY seq LIMIT 1", this.campaignId, key, typ) : this.store.get("SELECT * FROM events WHERE run_id=? AND correlation_id=? ORDER BY seq LIMIT 1", this.campaignId, key);
    return r ? rowToEvent(r) : null;
  }
  async emitOnce(key: string, typ: string, payload: any) {
    return this.find(key, typ) ?? this.writer.submit({ run_id: this.campaignId, type: typ, payload, correlation_id: key, actor: { kind: "system", id: "campaign" } });
  }
  events(...types: string[]) { return this.store.events(this.campaignId, { types: types.length ? types : undefined }); }
}
export interface CampaignDeps { configRepo: GitRepo; improverRepo: GitRepo; controlRepo: GitRepo; registry: ComponentRegistry; evalsets: any; promotion: PromotionService; proposer: any; observer: any; params: any;
  depth?: number; maxDepth?: number; highAssurance?: boolean; hooks?: Record<string, any>; answerIndex?: string[]; instanceIds?: string[]; parentRunId?: string | null; maxLines?: number; rules?: any; policy?: GatePolicy; taxonomy?: any; thresholds?: any }

export class CampaignController {
  repo: GitRepo; improver: GitRepo; control: GitRepo; reg: ComponentRegistry; evalsets: any; promotion: PromotionService; proposer: any; observer: any; params: any;
  taxonomy: any; thresholds: any; policy: GatePolicy; depth: number; maxDepth: number; high: boolean; hooks: Record<string, any>; answerIndex: string[]; instanceIds: string[]; parent: string | null; applier: EditApplier; rules: any; cid: string; campaign: any;
  constructor(public ctx: CampaignContext, public spec: any, d: CampaignDeps) {
    this.repo = d.configRepo; this.improver = d.improverRepo; this.control = d.controlRepo; this.reg = d.registry; this.evalsets = d.evalsets; this.promotion = d.promotion; this.proposer = d.proposer; this.observer = d.observer;
    this.params = d.params; this.policy = d.policy ?? GatePolicy.uniform(d.params); this.taxonomy = d.taxonomy ?? loadRules(); this.thresholds = OBS.th(d.thresholds); this.depth = d.depth ?? 1; this.maxDepth = d.maxDepth ?? 2; this.high = d.highAssurance ?? false; this.hooks = d.hooks ?? {}; this.answerIndex = d.answerIndex ?? []; this.instanceIds = d.instanceIds ?? [];
    this.parent = d.parentRunId ?? null; this.applier = new EditApplier(d.configRepo, d.registry, d.maxLines ?? 200); this.rules = d.rules; this.cid = ctx.campaignId;
    this.campaign = { campaign_id: this.cid, owner: ctx.owner, dry_run: !!spec.dry_run };
  }
  // ---------------------------------------------------------------- helpers
  used() {
    const used = { proposals: 0, candidate_evals: 0, compute: 0.0, cost: 0.0, time_s: 0.0 };
    for (const e of this.ctx.events("proposal.created", "proposal.rejected", "candidate.evaluated")) {
      if (e.type !== "candidate.evaluated") used.proposals++;
      else { used.candidate_evals++; used.compute += e.payload.usage.compute ?? 0.0; used.cost += e.payload.usage.cost ?? 0.0; }
    }
    const first = this.ctx.events("campaign.created");
    if (first.length) used.time_s = (this.ctx.clock.nowMs() - parseTs(first[0].ts)) / 1000.0;
    return used as Record<string, number>;
  }
  private async budget(dim: string, delta = 1) {
    const lim = this.spec.budgets[dim], used = this.used()[dim];
    if (lim === undefined || lim === null) return;
    if (used + delta > lim) throw new StopCampaign(`budget_exhausted:${dim}`);
    if (used + delta >= 0.8 * lim) await this.ctx.emitOnce(`budget-warn:${dim}`, "budget.warning", { dimension: dim, used: used + delta, limit: lim });
  }
  private async state(to: string) {
    const cur = this.ctx.events("campaign.state_changed"), frm = cur.length ? cur[cur.length - 1].payload.to : "CREATED";
    if (frm !== to) await this.ctx.emitOnce(`state:${cur.length}:${to}`, "campaign.state_changed", { from: frm, to });
  }
  private surfaceHashes() { return { improver_tree: this.improver.worktreeHash(), control_tree: this.control.worktreeHash() }; }
  private async integrity(key: string) {
    const start = this.ctx.find("integrity:start", "surface.integrity_checked"), now = this.surfaceHashes();
    const same = now.improver_tree === start.payload.improver_tree && now.control_tree === start.payload.control_tree;
    await this.ctx.emitOnce(key, "surface.integrity_checked", { ...now, ok: same });
    if (!same) throw new StopCampaign("integrity_failure", "eval_integrity.tampering");
  }
  // ---------------------------------------------------------------- lifecycle
  async start() {
    if (this.depth > this.maxDepth) {
      await this.ctx.ensureRun("-", this.depth, this.parent); await this.ctx.emitOnce("recursion-denied", "recursion.denied", { depth: this.depth, max_depth: this.maxDepth });
      throw new RecursionDenied(`depth ${this.depth} exceeds max_depth ${this.maxDepth}`);
    }
    const base = this.repo.head("main");
    await this.ctx.ensureRun(base, this.depth, this.parent);
    await this.ctx.emitOnce("created", "campaign.created", { spec: this.spec, depth: this.depth, dry_run: this.campaign.dry_run, owner: this.ctx.owner,
      policy: this.policy.resolved, policy_fingerprint: this.policy.fingerprint, policy_environment: this.policy.environment, policy_diff: this.policy.diff() });
    if (this.policy.relaxed().length) await this.ctx.emitOnce("policy-relaxed", "policy.relaxed", { environment: this.policy.environment, changes: this.policy.relaxed(), fingerprint: this.policy.fingerprint });   // a leaner gate than the defaults says so, in the log
    await this.state("PREPARING");
    if (this.spec.tiers_allowed.includes("T2") && !this.high) {
      await this.ctx.emitOnce("pf23", "campaign.stopped", { reason: "preflight_failed:PF-23", detail: "T2 and meta-change need a High-assurance deployment" });
      throw new StopCampaign("preflight_failed:PF-23");
    }
    await this.ctx.emitOnce("integrity:start", "surface.integrity_checked", { ...this.surfaceHashes(), ok: true, base, improver_commit: this.spec.improver.commit });
  }
  async run(): Promise<any> {
    let reason: string | null = null;
    try {
      await this.start();
      for (;;) {
        reason = this.stopReason(); if (reason) break;
        try { await this.round(this.nextRound()); }
        catch (e: any) {
          if (!(e instanceof StopCampaign)) throw e;
          reason = e.reason; if (e.failureClass) await this.ctx.emitOnce("fail-class", "failure.classified", { class: e.failureClass.split(".")[0], code: e.failureClass }); break;
        }
      }
    } catch (e: any) { if (e instanceof StopCampaign) return this.stop(e.reason); throw e; }
    return this.stop(reason!);
  }
  private nextRound() { const done = new Set(this.ctx.events("campaign.round_completed").map((e: any) => e.payload.round)); let r = 1; while (done.has(r)) r++; return r; }
  private stopReason(): string | null {
    const done = this.ctx.events("campaign.round_completed"), st = this.spec.stop;
    if (done.length >= st.rounds_max) return "rounds_max";
    const pl = st.plateau;
    if (pl && done.length >= pl.window) { const last = done.slice(-pl.window); if (last.every((e: any) => !e.payload.promoted.some((p: any) => p.lower > (pl.epsilon ?? 0.0)))) return "plateau"; }
    if (done.some((e: any) => e.payload.reason === "no_addressable_patterns")) return "no_addressable_patterns";
    return null;
  }
  private async stop(reason: string) {
    await this.state("TEARDOWN"); await this.ctx.emitOnce("stopped", "campaign.stopped", { reason, used: this.used() }); await this.state("STOPPED"); return this.summary();
  }
  summary() {
    const c = this.ctx, stopped = c.find("stopped", "campaign.stopped");
    return { campaign_id: this.cid, stopped: stopped ? stopped.payload.reason : null, used: this.used(), proposals: c.events("proposal.created").length, rejected: c.events("proposal.rejected").length,
      promotions: c.events("promotion.committed").map((e: any) => e.payload), rounds: c.events("campaign.round_completed").length };
  }
  private async note(key: string, code: string, subject: string, evidence: any) { await this.ctx.emitOnce(`obs:${key}:${code}`, "failure.observed", observation(this.taxonomy, code, subject, evidence)); }
  // ---------------------------------------------------------------- one round
  private async round(r: number) {
    const ctx = this.ctx, rs = await ctx.emitOnce(`round:${r}`, "campaign.round_started", { round: r, base_commit: this.repo.head("main") }), base = rs.payload.base_commit;
    await this.state("PROPOSING");
    const cands: any[] = []; let rejected = 0;
    for (let n = 1; n <= this.spec.search.candidates_per_round; n++) {
      const c = await this.proposal(r, n, base);
      if (c === null) rejected++;
      else if (c.exhausted) { await ctx.emitOnce(`round-done:${r}`, "campaign.round_completed", { round: r, promoted: [], reason: "no_addressable_patterns", proposals: n }); return; }
      else cands.push(c);
    }
    for (const [code, ev] of OBS.fromRound(cands.map((c) => c.added_text), this.thresholds)) await this.note(`round${r}`, code, `round:${r}`, ev);
    await this.state("EVALUATING");
    const evaluated: [any, any][] = []; for (const c of cands) evaluated.push([c, await this.evaluate(c, base)]);
    const promoted: any[] = [];
    if (evaluated.length) {
      await this.integrity(`integrity:gate:${r}`); await this.state("GATING");
      const decisions = await this.gate(r, evaluated); await this.state("PROMOTING");
      const accepted = evaluated.map(([c], i) => [c, decisions[i]] as [any, any]).filter(([, d]) => d.accepted);
      if (accepted.length) {
        let [c, d] = accepted[0]; for (const cd of accepted.slice(1)) if (cd[1].target.delta > d.target.delta) [c, d] = cd;   // best target delta; ties keep the earlier candidate
        const out = await this.promote(r, c, d);
        if (out && out.status === "promoted") promoted.push({ candidate_id: out.candidate_id, lower: out.lower, promotion_id: out.promotion_id });
      }
    }
    await ctx.emitOnce(`round-done:${r}`, "campaign.round_completed", { round: r, promoted, proposals: cands.length + rejected, rejected });
  }
  // ---------------------------------------------------------------- proposals and validation (§17.6, §17.7)
  private async proposal(r: number, n: number, base: string): Promise<any | null> {
    const ctx = this.ctx, pid = deriveId(this.cid, r, n), ex = ctx.find(`prop:${pid}`);
    if (ex) return ex.type === "proposal.rejected" ? null : { ...ex.payload, candidate_id: pid };
    await this.budget("proposals");
    const raw = this.proposer.propose(r, n, base, { campaign_id: this.cid });
    if (raw === null || raw === undefined) return { exhausted: true };
    const reasons: any[] = this.validateManifest(raw.manifest), edits = raw.edits, editHash = Array.isArray(edits) ? sha256Hex(canonicalize(edits)) : null;
    if (!reasons.length && editHash && this.previouslyRejected(editHash, pid)) reasons.push({ code: "replayed_rejected_edit" });
    let commit: string | null = null, tier: string | null = null, added: any[] = [], lines = 0;
    const branch = `cand/${this.cid}/${r}/${n}`;
    if (!reasons.length) {
      const mh = sha256Hex(canonicalize(raw.manifest)), res = this.applier.apply(base, edits, this.spec.tiers_allowed, branch, `proposal ${pid}\n\nmanifest-hash: ${mh}`);
      reasons.push(...res.reasons);
      if (res.ok) {
        commit = res.commit; tier = res.tier; added = res.added; lines = res.changed_lines;
        for (const v of runScreens(res.added, this.answerIndex, this.instanceIds, this.rules)) reasons.push({ code: `screen.${v.screen}`, path: v.path, detail: v.detail });
        const text = added.map((a) => a.added).join("\n"), idx = noveltyRejected(text, ctx.events("proposal.created").map((e: any) => e.payload.added_text ?? ""));
        if (idx !== null && !reasons.length) reasons.push({ code: "novelty.too_similar", detail: `similar to archived candidate #${idx}` });
      }
    }
    if (reasons.length) {
      if (commit) this.repo.git(["branch", "-D", branch], { check: false });
      await ctx.writer.execute("INSERT OR IGNORE INTO negative_results(id,campaign_id,pattern_id,edit_summary_ref,reason,created_at) VALUES(?,?,?,?,?,?)", [editHash ?? pid, this.cid, pid, ctx.blobs.putJson(Array.isArray(edits) ? edits : []), reasons[0].code, formatTs(ctx.clock.nowMs())]);
      await ctx.writer.execute("INSERT INTO negative_results_fts(text,id) VALUES(?,?)", [JSON.stringify(reasons), editHash ?? pid]);
      await ctx.emitOnce(`prop:${pid}`, "proposal.rejected", { proposal_id: pid, round: r, n, reasons, edit_hash: editHash });
      for (const [code, ev] of OBS.fromRejection(reasons)) await this.note(pid, code, pid, ev);
      return null;
    }
    const ref = ctx.blobs.putJson(edits);
    const payload = { proposal_id: pid, candidate_id: pid, round: r, n, parent_commit: base, commit, branch, tier, edit_hash: editHash, manifest_hash: sha256Hex(canonicalize(raw.manifest)), manifest: raw.manifest, changed_lines: lines, edit_text_ref: ref, added_text: added.map((a) => a.added).join("\n") };
    await ctx.writer.execute("INSERT OR IGNORE INTO candidates(candidate_id,campaign_id,round,parent_commit,commit_id,tier,status,edit_text_ref) VALUES(?,?,?,?,?,?,'valid',?)", [pid, this.cid, r, base, commit, tier, ref]);
    await ctx.emitOnce(`valid:${pid}`, "validation.completed", { proposal_id: pid, ok: true });
    await ctx.emitOnce(`prop:${pid}`, "proposal.created", payload);
    return payload;
  }
  /** A replay of an earlier rejected edit (by any *other* proposal; a resumed proposal never counts as its own replay). */
  private previouslyRejected(editHash: string, pid: string) { return !!this.ctx.store.get("SELECT 1 x FROM negative_results WHERE id=? AND COALESCE(pattern_id,'') != ?", editHash, pid); }
  private validateManifest(m: any): any[] {
    if (m === null || typeof m !== "object" || Array.isArray(m)) return [{ code: "manifest.missing" }];
    const miss = ["failure_evidence", "root_cause", "fix", "predicted_fixes", "at_risk_regressions"].filter((k) => !(k in m));
    if (miss.length) return [{ code: "manifest.incomplete", detail: miss.join(",") }];
    const ok = m.failure_evidence?.length && m.predicted_fixes.every((p: any) => p && typeof p === "object" && "metric" in p && "direction" in p) && m.at_risk_regressions.every((p: any) => p && typeof p === "object" && "metric" in p);
    return ok ? [] : [{ code: "manifest.malformed" }];
  }
  // ---------------------------------------------------------------- evaluation (§17.8.1)
  private observe(commit: string, role: string, setId: string, variant: string, model: string, seed: string): [any, any] {
    const key = sha256Hex(`${commit}|${setId}|${variant}|${model}|${seed}`), row = this.ctx.store.get("SELECT observations_ref FROM observation_cache WHERE key=?", key);
    if (row) return [this.ctx.blobs.getJson(row.observations_ref), {}];
    const s = this.observer.scores(commit, role, variant, model, seed); return [s, s.usage ?? {}];
  }
  private async cache(commit: string, setId: string, variant: string, model: string, seed: string, scores: any) {
    const key = sha256Hex(`${commit}|${setId}|${variant}|${model}|${seed}`);
    await this.ctx.writer.execute("INSERT OR REPLACE INTO observation_cache(key,config_commit,set_id,model_ref,seeds_hash,observations_ref,created_at) VALUES(?,?,?,?,?,?,?)", [key, commit, setId, model, sha256Hex(seed), this.ctx.blobs.putJson(scores), formatTs(this.ctx.clock.nowMs())]);
  }
  private pair(base: any, cand: any, wl: any): any {
    const ids = Object.keys(cand.target).sort(), bids = Object.keys(base.target).sort();
    if (JSON.stringify(ids) !== JSON.stringify(bids)) throw new Error("baseline and candidate observations must cover the same instances");
    const out: any = { target: { b: ids.map((i) => base.target[i]), c: ids.map((i) => cand.target[i]), direction: wl.target_direction ?? "maximize" }, guardrails: [] };
    for (const m of Object.keys(cand.guardrails ?? {}).sort())
      out.guardrails.push({ metric: m, b: ids.map((i) => base.guardrails[m][i]), c: ids.map((i) => cand.guardrails[m][i]), direction: wl.guardrail_directions?.[m] ?? "minimize", ...(wl.margins && m in wl.margins ? { margin: wl.margins[m] } : {}) });
    if (cand.passes) out.preserved = { b: ids.map((i) => base.passes[i]), c: ids.map((i) => cand.passes[i]), must_pass: (wl.must_pass_ids ?? []).filter((i: string) => ids.includes(i)).map((i: string) => ids.indexOf(i)) };
    return out;
  }
  private async evaluate(c: any, _base: string): Promise<any> {
    const ctx = this.ctx, pid = c.candidate_id, ex = ctx.find(`eval:${pid}`, "candidate.evaluated");
    if (ex) return ctx.blobs.getJson(ex.payload.data_ref);
    await this.budget("candidate_evals"); await this.integrity(`integrity:eval:${pid}`);
    const wl = this.spec.workload, data: any = {}, usage: any = { compute: 0.0, cost: 0.0 };
    for (const model of this.spec.target_models) {
      const variants: any = {};
      for (const variant of ["canonical", "format_varied"]) {
        const seed = evalSeed(this.cid, c.round, pid, wl.scoring), [cs, cu] = this.observe(c.commit, "scoring", wl.scoring, variant, model, seed), [bs, bu] = this.observe(c.parent_commit, "scoring", wl.scoring, variant, model, seed);
        if (Object.keys(bu).length) await this.cache(c.parent_commit, wl.scoring, variant, model, seed, bs);
        for (const u of [cu, bu]) for (const k of Object.keys(usage)) usage[k] += u[k] ?? 0.0;
        variants[variant] = this.pair(bs, cs, wl);
      }
      data[model] = { variants };
    }
    const first = Object.values<any>(data)[0].variants.canonical;
    const summary = { target: mean(G.improvementsOf(first.target)), cost: usage.cost, latency: first.guardrails.length ? Number(first.guardrails[0].c[0]) : 0.0 };
    const ref = ctx.blobs.putJson(data);
    await ctx.emitOnce(`eval:${pid}`, "candidate.evaluated", { candidate_id: pid, data_ref: ref, usage, summary });
    await ctx.writer.execute("UPDATE candidates SET status='evaluated', observations_ref=? WHERE candidate_id=?", [ref, pid]);
    return data;
  }
  // ---------------------------------------------------------------- gate (§17.8.2)
  /** Accepted candidates of *previous* rounds only, so a resumed round sees exactly what an uninterrupted one saw. */
  private archive(r: number) { return this.ctx.events("gate.decided").filter((e: any) => e.payload.accepted && e.payload.summary && (e.payload.round ?? 0) < r).map((e: any) => e.payload.summary); }
  private async gate(r: number, evaluated: [any, any][]): Promise<any[]> {
    const ctx = this.ctx, existing = evaluated.map(([c]) => ctx.find(`gate:${c.candidate_id}`, "gate.decided"));
    if (existing.every(Boolean)) return existing.map((e) => e.payload);
    const archive = this.archive(r), summaries: Record<string, any> = {};
    for (const [c] of evaluated) summaries[c.candidate_id] = ctx.find(`eval:${c.candidate_id}`, "candidate.evaluated").payload.summary;
    const cdata = evaluated.map(([c, data]) => ({ proposal_id: c.candidate_id, tier: c.tier, models: data, summary: summaries[c.candidate_id], archive })), out: any[] = [], alphas: any[] = new Array(cdata.length).fill(null), byTier = new Map<string, number[]>();
    cdata.forEach((cd, i) => { if (!byTier.has(cd.tier)) byTier.set(cd.tier, []); byTier.get(cd.tier)!.push(i); });
    for (const [tier, idx] of byTier) { const al = G.roundAlphas(this.cid, idx.map((i) => cdata[i]), this.policy.gateParams(tier)); idx.forEach((i, k) => { alphas[i] = al[k]; }); }   // Holm within a tier: each tier has its own alpha, so each is its own family
    for (let i = 0; i < evaluated.length; i++) {
      const [c] = evaluated[i], pid = c.candidate_id;
      if (existing[i]) { out.push(existing[i].payload); continue; }
      let dec = G.evaluateCandidate(this.cid, cdata[i], this.policy.gateParams(c.tier), alphas[i]);
      if (dec.needs_heldout) dec = await this.heldout(c, cdata[i], alphas[i], dec);
      dec.summary = summaries[pid]; dec.round = r;
      await ctx.emitOnce(`gate:${pid}`, "gate.decided", dec);
      for (const [code, ev] of OBS.fromDecision(dec, this.thresholds)) await this.note(pid, code, pid, ev);
      await ctx.writer.execute("UPDATE candidates SET status=? WHERE candidate_id=?", [dec.accepted ? "accepted" : "rejected", pid]);
      if (!dec.accepted) await ctx.writer.execute("INSERT OR IGNORE INTO negative_results(id,campaign_id,pattern_id,edit_summary_ref,reason,created_at) VALUES(?,?,?,?,?,?)", [c.edit_hash, this.cid, pid, c.edit_text_ref, "gate:" + (dec.reasons.length ? dec.reasons[0] : "rejected"), formatTs(ctx.clock.nowMs())]);
      out.push(dec);
    }
    return out;
  }
  private async heldout(c: any, cd: any, alpha: any, dec: any): Promise<any> {
    const ctx = this.ctx, wl = this.spec.workload, pid = c.candidate_id, ho = ctx.find(`heldout:${pid}`, "heldout.queried"); let held: any;
    if (ho) held = ctx.blobs.getJson(ho.payload.data_ref);
    else {
      let n: number;
      try { n = await this.evalsets.recordQuery(wl.heldout, this.cid, pid); }
      catch (e: any) { if (!(e instanceof EvalSetError)) throw e; dec.reasons.push(`heldout:${e.code}`); dec.needs_heldout = false; dec.accepted = false; return dec; }   // only the expected refusal; anything else is a bug
      const sb = OBS.selectionBias(n, this.evalsets.maxQueries, this.thresholds); if (sb) await this.note(`set:${wl.heldout}`, sb[0], wl.heldout, sb[1]);
      held = {};
      for (const model of this.spec.target_models) {
        const seed = evalSeed(this.cid, c.round, pid, wl.heldout), cs = this.observer.scores(c.commit, "heldout", "canonical", model, seed), bs = this.observer.scores(c.parent_commit, "heldout", "canonical", model, seed);
        held[model] = this.pair(bs, cs, wl);
      }
      await ctx.emitOnce(`heldout:${pid}`, "heldout.queried", { candidate_id: pid, set_id: wl.heldout, data_ref: ctx.blobs.putJson(held), queries: n });
    }
    const cd2 = clone(cd);
    for (const [model, d] of Object.entries<any>(held)) cd2.models[model].heldout = { target: d.target, preserved: d.preserved ?? null };
    return G.evaluateCandidate(this.cid, cd2, this.policy.gateParams(c.tier), alpha);
  }
  // ---------------------------------------------------------------- promotion (§17.10)
  private async promote(r: number, c: any, d: any): Promise<any> {
    let cand: any = { candidate_id: c.candidate_id, commit: c.commit, parent_commit: c.parent_commit, tier: c.tier };
    if (this.hooks.before_promote) this.hooks.before_promote(c);   // test seam: lets a scenario advance `main` during evaluation
    for (let attempt = 0; ; ) {
      const res = await this.promotion.promote(this.campaign, cand, d, this.hooks.approval ?? null);
      if (res.status === "promoted") return { ...res, candidate_id: cand.candidate_id, lower: d.target.lower };
      if (res.status !== "stale" || attempt >= 3) return { ...res, candidate_id: cand.candidate_id };
      attempt++;   // stale base: rebase the edits onto the new main and run the gate again
      const re = await this.regate(r, c, attempt);
      if (re === null) return { status: "denied", reason: "rebase_failed" };
      [cand, d] = re;
    }
  }
  private async regate(r: number, c: any, k: number): Promise<[any, any] | null> {
    const ctx = this.ctx, newBase = this.repo.head("main"), pid = deriveId(this.cid, r, `${c.n}rb${k}`), edits = ctx.blobs.getJson(c.edit_text_ref);
    const res = this.applier.apply(newBase, edits, this.spec.tiers_allowed, `cand/${this.cid}/${r}/${c.n}-rb${k}`, `proposal ${pid}\n\nmanifest-hash: ${c.manifest_hash}`);
    await ctx.emitOnce(`regate:${pid}`, "gate.regated", { candidate_id: c.candidate_id, new_candidate_id: pid, new_base: newBase, ok: res.ok, reasons: res.reasons });
    if (!res.ok) return null;
    const nc = { ...c, candidate_id: pid, proposal_id: pid, parent_commit: newBase, commit: res.commit, tier: res.tier }, data = await this.evaluate(nc, newBase), decisions = await this.gate(r, [[nc, data]]);
    return [{ candidate_id: pid, commit: res.commit, parent_commit: newBase, tier: res.tier }, decisions[0]];
  }
}
