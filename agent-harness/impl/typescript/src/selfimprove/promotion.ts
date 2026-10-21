/** Promotion, canary, rollback and monitoring (§17.10) over the three-repository model. */
import { formatTs, sha256Hex } from "../util.js";
import { bootstrapCi, improvements, seedFor } from "../stats.js";
import { GitRepo } from "./gitrepo.js";
import { GatePolicy } from "./policy.js";
import { ComponentRegistry } from "./registry.js";
/** Deterministic value in [0,1) from a string (first 32 bits of SHA-256). */
export const unitInterval = (s: string) => parseInt(sha256Hex(s).slice(0, 8), 16) / 4294967296.0;
export const promotionId = (campaignId: string, candidateId: string) => "P" + sha256Hex(`${campaignId}:${candidateId}`).slice(0, 25);

/** Maps a run to its config commit and pins it for the run's life (§17.10, §3.5.8). A T2 promotion first reaches `fraction` of new runs. */
export class ConfigResolver {
  canary: any = null;
  constructor(public repo: GitRepo, events: any[]) {
    const done = new Set(events.filter((e) => e.type === "promotion.canary_completed" || e.type === "rollback.performed").map((e) => e.payload.promotion_id));
    for (const e of events) if (e.type === "promotion.committed" && e.payload.canary && !done.has(e.payload.promotion_id)) this.canary = e.payload;
  }
  resolve(runId: string): string {
    const main = this.repo.head("main");
    if (this.canary && this.canary.commit === main) return unitInterval(runId) < this.canary.canary.fraction ? main : this.canary.parent;
    return main;
  }
}
/** §3.5.6: later changes are independent of the target when touched keys are disjoint and no dependency edge connects them. */
export class RevertPlanner {
  comp: Map<string, number>;
  constructor(public reg: ComponentRegistry) { this.comp = this.components(); }
  private components(): Map<string, number> {
    const ents = this.reg.entries, parent = ents.map((_, i) => i);
    const find = (i: number): number => { while (parent[i] !== i) { parent[i] = parent[parent[i]]; i = parent[i]; } return i; };
    ents.forEach((a, i) => ents.forEach((b, j) => { if (i !== j && (a.raw.requires ?? []).some((r: string) => (b.raw.provides ?? []).includes(r))) parent[find(i)] = find(j); }));
    return new Map(ents.map((e, i) => [e.path, find(i)] as [string, number]));
  }
  private keys(touched: string[]): [Set<string>, Set<number>] {
    const comps = new Set<number>(); for (const p of touched) { const e = this.reg.entryFor(p); if (e) comps.add(this.comp.get(e.path)!); }
    return [new Set(touched), comps];
  }
  plan(target: any, later: any[]) {
    const [tk, tc] = this.keys(target.touched), deps: string[] = [];
    for (const l of later) { const [lk, lc] = this.keys(l.touched); if ([...tk].some((k) => lk.has(k)) || [...tc].some((c) => lc.has(c))) deps.push(l.promotion_id); }
    return { independent: deps.length === 0, dependents: [...deps].reverse(), target: target.promotion_id };
  }
}
export class PromotionService {
  cfg: any; planner: RevertPlanner; policy: GatePolicy;
  constructor(public ctx: any, public repo: GitRepo, public reg: ComponentRegistry, cfg: any = {}, policy?: GatePolicy) {
    this.cfg = { high_assurance: false, ...cfg };
    if (!policy) {                                                      // the older flat settings still work and are folded into a policy
      const over: any = { T1: { review: {} }, T2: {} };
      if ("t1_sample_rate" in this.cfg) over.T1.review.sample_rate = this.cfg.t1_sample_rate; if ("review_window_h" in this.cfg) over.T1.review.window_h = this.cfg.review_window_h; if ("canary_fraction" in this.cfg) over.T2.canary_fraction = this.cfg.canary_fraction;
      const tiers: any = {}; if (Object.keys(over.T1.review).length) tiers.T1 = over.T1; if (Object.keys(over.T2).length) tiers.T2 = over.T2;
      policy = GatePolicy.resolve({ tiers });
    }
    this.policy = policy; this.planner = new RevertPlanner(reg);
  }
  async promote(campaign: any, cand: any, decision: any, approval: any = null): Promise<any> {
    const ctx = this.ctx, pid = promotionId(campaign.campaign_id, cand.candidate_id), done = ctx.find(`promo:${pid}`, "promotion.committed");
    if (done) return { status: "promoted", promotion_id: pid, commit: done.payload.commit, replayed: true };
    if (campaign.dry_run) { await ctx.emitOnce(`promo-denied:${pid}`, "promotion.denied", { candidate_id: cand.candidate_id, reason: "dry_run" }); return { status: "denied", reason: "dry_run" }; }
    if (!decision.accepted) return { status: "denied", reason: "gate_not_accepted" };
    const tier = cand.tier;
    if (tier === "T3" || tier === "T4") return { status: "denied", reason: "tier_not_promotable" };
    if (tier === "T2") {
      if (!this.cfg.high_assurance) { await ctx.emitOnce(`promo-denied:${pid}`, "promotion.denied", { candidate_id: cand.candidate_id, reason: "t2_requires_high_assurance" }); return { status: "denied", reason: "t2_requires_high_assurance" }; }   // PF-23
    }
    const review = this.policy.review(tier);
    if (tier === "T2" || review.mode === "blocking") {                // T2 is always blocking; an environment may also make a lower tier blocking
      const why = this.approvalProblem(approval, campaign);
      if (why) { await ctx.emitOnce(`review-req:${pid}`, "review.requested", { promotion_id: pid, kind: "promotion", candidate_id: cand.candidate_id, reason: why }); return { status: "awaiting_approval", reason: why }; }
    }
    const main = this.repo.head("main");
    if (main !== cand.commit) {
      if (main !== cand.parent_commit) {
        if (this.repo.isAncestor(cand.parent_commit, main)) { await ctx.emitOnce(`stale:${pid}:${main}`, "promotion.stale", { candidate_id: cand.candidate_id, base: cand.parent_commit, main }); return { status: "stale", needs_regate: true, main }; }   // a stale base is never merged
        return { status: "denied", reason: "base_not_ancestor" };
      }
      this.repo.ffMerge(cand.commit);
    }
    const touched = this.repo.changedIn(cand.commit); this.repo.tag(`promo/${pid}`, cand.commit);
    const canary = this.policy.canaryFraction(tier) > 0 ? { fraction: this.policy.canaryFraction(tier) } : null;
    await ctx.emitOnce(`promo:${pid}`, "promotion.committed", { promotion_id: pid, candidate_id: cand.candidate_id, commit: cand.commit, parent: cand.parent_commit, tier, touched, canary, approvals: approval });
    await ctx.writer.execute("INSERT OR IGNORE INTO promotions(promotion_id,campaign_id,candidate_id,repo,commit_id,tier,approvals,promoted_at) VALUES(?,?,?,?,?,?,?,?)",
      [pid, campaign.campaign_id, cand.candidate_id, "harness-config", cand.commit, tier, approval == null ? null : String(approval.approver), formatTs(ctx.clock.nowMs())]);
    if (review.mode === "sampled" && unitInterval("review:" + pid) < review.sample_rate) await ctx.emitOnce(`review-req:${pid}`, "review.requested", { promotion_id: pid, kind: `${tier.toLowerCase()}_sample`, due_h: review.window_h });
    return { status: "promoted", promotion_id: pid, commit: cand.commit, canary };
  }
  private approvalProblem(a: any, campaign: any): string | null {
    if (!a || a.kind !== "promotion") return "missing_approval";
    if (!(a.roles ?? []).includes("approver")) return "approver_role_required";
    if (a.approver === campaign.owner) return "approver_is_campaign_owner";
    const att = a.attachments ?? {}, miss = ["diff", "audit_report", "gate_decision", "transfer_results"].filter((k) => !att[k]);
    return miss.length ? `missing_attachments:${miss.join(",")}` : null;
  }
  async reviewDecided(promotionId_: string, approved: boolean, reviewer: string, later: any[]) {
    await this.ctx.emitOnce(`review-dec:${promotionId_}`, "review.decided", { promotion_id: promotionId_, approved, reviewer });
    return approved ? { status: "kept" } : this.rollback(promotionId_, later, "review_rejected");
  }
  async rollback(pid: string, later: any[], reason: string, allowCascade = false): Promise<any> {
    const ctx = this.ctx, done = ctx.find(`rollback:${pid}`, "rollback.performed");
    if (done) return { status: "rolled_back", replayed: true, revert_commit: done.payload.revert_commits.at(-1) };
    const committed = new Map<string, any>(ctx.events("promotion.committed").map((e: any) => [e.payload.promotion_id, e.payload])), target = committed.get(pid);
    const plan = this.planner.plan({ promotion_id: pid, touched: target.touched }, later.filter((l) => l.promotion_id !== pid).map((l) => ({ promotion_id: l.promotion_id, touched: l.touched })));
    if (!plan.independent && !allowCascade) { await ctx.emitOnce(`revert-blocked:${pid}`, "revert.blocked", { promotion_id: pid, dependents: plan.dependents }); return { status: "revert.blocked", dependents: plan.dependents }; }
    const order = [...plan.dependents, pid], reverts: string[] = [];
    for (const p of order) {
      const c = committed.get(p);
      if (!this.repo.tagExists(`rollback/${p}`)) { reverts.push(this.repo.revert(c.commit)); this.repo.tag(`rollback/${p}`, this.repo.head("main")); }
      await ctx.writer.execute("UPDATE promotions SET rolled_back_at=? WHERE promotion_id=?", [formatTs(ctx.clock.nowMs()), p]);
    }
    await ctx.emitOnce(`rollback:${pid}`, "rollback.performed", { promotion_id: pid, reason, reverted: order, revert_commits: reverts.length ? reverts : [this.repo.head("main")] });
    return { status: "rolled_back", reverted: order, revert_commit: this.repo.head("main") };
  }
}
/** Compares the post-promotion cohort with the pre-promotion cohort using the §13.7 interval (paired by position after truncating to the shorter cohort). */
export class PromotionMonitor {
  constructor(public B = 10000, public alpha = 0.05) {}
  check(runId: string, promotion: any, metrics: Record<string, any>, autoRollbackTiers: string[]) {
    const breaches: any[] = [];
    for (const name of Object.keys(metrics).sort()) {
      const m = metrics[name], n = Math.min(m.pre.length, m.post.length); if (!n) continue;
      const x = improvements(m.pre.slice(0, n), m.post.slice(0, n), m.direction), ci = bootstrapCi(x, seedFor(runId, `monitor:${promotion.promotion_id}:${name}`), 1 - this.alpha, this.B);
      const bad = m.role === "target" ? ci.upper < 0 : ci.lower < -(m.margin ?? 0.02);   // confident target regression / guardrail no longer provably non-inferior
      if (bad) breaches.push({ metric: name, lower: ci.lower, upper: ci.upper, delta: ci.mean });
    }
    return { breach: breaches.length > 0, breaches, action: !breaches.length ? "none" : autoRollbackTiers.includes(promotion.tier) ? "rollback" : "alert" };
  }
}
