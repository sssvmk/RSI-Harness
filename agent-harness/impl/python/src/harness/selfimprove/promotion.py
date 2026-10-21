"""Promotion, canary, rollback and monitoring (§17.10) over the three-repository model."""
from __future__ import annotations
from dataclasses import dataclass
from ..canonical import sha256_hex
from ..clock import format_ts
from ..stats import bootstrap_ci, improvements, seed_for
from .gitrepo import GitRepo
from .policy import GatePolicy
from .registry import ComponentRegistry


def unit_interval(s: str) -> float:
    """Deterministic value in [0,1) from a string (first 32 bits of SHA-256)."""
    return int(sha256_hex(s)[:8], 16) / 4294967296.0


def promotion_id(campaign_id: str, candidate_id: str) -> str:
    return "P" + sha256_hex(f"{campaign_id}:{candidate_id}")[:25]


class ConfigResolver:
    """Maps a run to its config commit and pins it for the run's life (§17.10, §3.5.8). A T2 promotion first reaches `fraction` of new runs."""

    def __init__(self, repo: GitRepo, events: list[dict]):
        self.repo = repo
        self.canary = None
        done = {e["payload"]["promotion_id"] for e in events if e["type"] in ("promotion.canary_completed", "rollback.performed")}
        for e in events:
            if e["type"] == "promotion.committed" and e["payload"].get("canary") and e["payload"]["promotion_id"] not in done:
                self.canary = e["payload"]

    def resolve(self, run_id: str) -> str:
        main = self.repo.head("main")
        if self.canary and self.canary["commit"] == main:
            return main if unit_interval(run_id) < self.canary["canary"]["fraction"] else self.canary["parent"]
        return main


class RevertPlanner:
    """§3.5.6: later changes are independent of the target when touched keys are disjoint and no dependency edge connects them."""

    def __init__(self, registry: ComponentRegistry):
        self.reg = registry
        self.comp = self._components()

    def _components(self) -> dict[str, int]:
        ents = self.reg.entries
        parent = list(range(len(ents)))

        def find(i):
            while parent[i] != i:
                parent[i] = parent[parent[i]]; i = parent[i]
            return i
        for i, a in enumerate(ents):
            for j, b in enumerate(ents):
                if i != j and set(a.raw.get("requires", [])) & set(b.raw.get("provides", [])):
                    parent[find(i)] = find(j)
        return {e.path: find(i) for i, e in enumerate(ents)}

    def _keys(self, touched: list[str]) -> tuple[set[str], set[int]]:
        comps = set()
        for p in touched:
            e = self.reg.entry_for(p)
            if e:
                comps.add(self.comp[e.path])
        return set(touched), comps

    def plan(self, target: dict, later: list[dict]) -> dict:
        tk, tc = self._keys(target["touched"])
        deps = []
        for l in later:
            lk, lc = self._keys(l["touched"])
            if tk & lk or tc & lc:
                deps.append(l["promotion_id"])
        return {"independent": not deps, "dependents": list(reversed(deps)), "target": target["promotion_id"]}


class PromotionService:
    def __init__(self, ctx, repo: GitRepo, registry: ComponentRegistry, cfg: dict | None = None, policy: GatePolicy | None = None):
        self.ctx, self.repo, self.reg = ctx, repo, registry
        c = {"high_assurance": False, **(cfg or {})}
        if policy is None:                                            # the older flat settings still work and are folded into a policy
            over: dict = {"T1": {"review": {}}, "T2": {}}
            if "t1_sample_rate" in c: over["T1"]["review"]["sample_rate"] = c["t1_sample_rate"]
            if "review_window_h" in c: over["T1"]["review"]["window_h"] = c["review_window_h"]
            if "canary_fraction" in c: over["T2"]["canary_fraction"] = c["canary_fraction"]
            policy = GatePolicy.resolve({"tiers": {t: v for t, v in over.items() if v and (t != "T1" or v["review"])}})
        self.cfg, self.policy = c, policy
        self.planner = RevertPlanner(registry)

    async def promote(self, campaign: dict, cand: dict, decision: dict, approval: dict | None = None) -> dict:
        ctx, pid = self.ctx, promotion_id(campaign["campaign_id"], cand["candidate_id"])
        done = ctx.find(f"promo:{pid}", "promotion.committed")
        if done:
            return {"status": "promoted", "promotion_id": pid, "commit": done["payload"]["commit"], "replayed": True}
        if campaign.get("dry_run"):
            await ctx.emit_once(f"promo-denied:{pid}", "promotion.denied", {"candidate_id": cand["candidate_id"], "reason": "dry_run"})
            return {"status": "denied", "reason": "dry_run"}
        if not decision.get("accepted"):
            return {"status": "denied", "reason": "gate_not_accepted"}
        tier = cand["tier"]
        if tier in ("T3", "T4"):
            return {"status": "denied", "reason": "tier_not_promotable"}
        if tier == "T2":
            if not self.cfg["high_assurance"]:
                await ctx.emit_once(f"promo-denied:{pid}", "promotion.denied", {"candidate_id": cand["candidate_id"], "reason": "t2_requires_high_assurance"})
                return {"status": "denied", "reason": "t2_requires_high_assurance"}          # PF-23
        review = self.policy.review(tier)
        if tier == "T2" or review["mode"] == "blocking":              # T2 is always blocking; an environment may also make a lower tier blocking
            why = self._approval_problem(approval, campaign)
            if why:
                await ctx.emit_once(f"review-req:{pid}", "review.requested", {"promotion_id": pid, "kind": "promotion", "candidate_id": cand["candidate_id"], "reason": why})
                return {"status": "awaiting_approval", "reason": why}
        main = self.repo.head("main")
        if main != cand["commit"]:
            if main != cand["parent_commit"]:
                if self.repo.is_ancestor(cand["parent_commit"], main):
                    await ctx.emit_once(f"stale:{pid}:{main}", "promotion.stale", {"candidate_id": cand["candidate_id"], "base": cand["parent_commit"], "main": main})
                    return {"status": "stale", "needs_regate": True, "main": main}           # a stale base is never merged
                return {"status": "denied", "reason": "base_not_ancestor"}
            self.repo.ff_merge(cand["commit"])
        touched = self.repo.changed_in(cand["commit"])
        self.repo.tag(f"promo/{pid}", cand["commit"])
        canary = {"fraction": self.policy.canary_fraction(tier)} if self.policy.canary_fraction(tier) > 0 else None
        await ctx.emit_once(f"promo:{pid}", "promotion.committed", {"promotion_id": pid, "candidate_id": cand["candidate_id"], "commit": cand["commit"], "parent": cand["parent_commit"],
                                                                     "tier": tier, "touched": touched, "canary": canary, "approvals": approval})
        await ctx.writer.execute("INSERT OR IGNORE INTO promotions(promotion_id,campaign_id,candidate_id,repo,commit_id,tier,approvals,promoted_at) VALUES(?,?,?,?,?,?,?,?)",
                                 (pid, campaign["campaign_id"], cand["candidate_id"], "harness-config", cand["commit"], tier, None if approval is None else str(approval.get("approver")),
                                  format_ts(ctx.clock.now_ms())))
        if review["mode"] == "sampled" and unit_interval("review:" + pid) < review["sample_rate"]:
            await ctx.emit_once(f"review-req:{pid}", "review.requested", {"promotion_id": pid, "kind": f"{tier.lower()}_sample", "due_h": review["window_h"]})
        return {"status": "promoted", "promotion_id": pid, "commit": cand["commit"], "canary": canary}

    def _approval_problem(self, a: dict | None, campaign: dict) -> str | None:
        if not a or a.get("kind") != "promotion":
            return "missing_approval"
        if "approver" not in a.get("roles", []):
            return "approver_role_required"
        if a.get("approver") == campaign.get("owner"):
            return "approver_is_campaign_owner"
        att = a.get("attachments", {})
        miss = [k for k in ("diff", "audit_report", "gate_decision", "transfer_results") if not att.get(k)]
        return f"missing_attachments:{','.join(miss)}" if miss else None

    async def review_decided(self, promotion_id: str, approved: bool, reviewer: str, later: list[dict]) -> dict:
        await self.ctx.emit_once(f"review-dec:{promotion_id}", "review.decided", {"promotion_id": promotion_id, "approved": approved, "reviewer": reviewer})
        return {"status": "kept"} if approved else await self.rollback(promotion_id, later, reason="review_rejected")

    async def rollback(self, pid: str, later: list[dict], reason: str, allow_cascade: bool = False) -> dict:
        ctx = self.ctx
        done = ctx.find(f"rollback:{pid}", "rollback.performed")
        if done:
            return {"status": "rolled_back", "replayed": True, "revert_commit": done["payload"]["revert_commits"][-1]}
        committed = {e["payload"]["promotion_id"]: e["payload"] for e in ctx.events("promotion.committed")}
        target = committed[pid]
        plan = self.planner.plan({"promotion_id": pid, "touched": target["touched"]},
                                 [{"promotion_id": l["promotion_id"], "touched": l["touched"]} for l in later if l["promotion_id"] != pid])
        if not plan["independent"] and not allow_cascade:
            await ctx.emit_once(f"revert-blocked:{pid}", "revert.blocked", {"promotion_id": pid, "dependents": plan["dependents"]})
            return {"status": "revert.blocked", "dependents": plan["dependents"]}
        order = plan["dependents"] + [pid]
        reverts = []
        for p in order:
            c = committed[p]
            if not self.repo.tag_exists(f"rollback/{p}"):
                reverts.append(self.repo.revert(c["commit"]))
                self.repo.tag(f"rollback/{p}", self.repo.head("main"))
            await ctx.writer.execute("UPDATE promotions SET rolled_back_at=? WHERE promotion_id=?", (format_ts(ctx.clock.now_ms()), p))
        await ctx.emit_once(f"rollback:{pid}", "rollback.performed", {"promotion_id": pid, "reason": reason, "reverted": order, "revert_commits": reverts or [self.repo.head("main")]})
        return {"status": "rolled_back", "reverted": order, "revert_commit": self.repo.head("main")}


class PromotionMonitor:
    """Compares the post-promotion cohort with the pre-promotion cohort using the §13.7 interval (pairing by position after truncating to the shorter cohort)."""

    def __init__(self, B: int = 10000, alpha: float = 0.05):
        self.B, self.alpha = B, alpha

    def check(self, run_id: str, promotion: dict, metrics: dict, auto_rollback_tiers: list[str]) -> dict:
        """metrics = {name: {pre:[], post:[], direction, margin, role: target|guardrail}}"""
        breaches = []
        for name, m in sorted(metrics.items()):
            n = min(len(m["pre"]), len(m["post"]))
            if n == 0:
                continue
            x = improvements(m["pre"][:n], m["post"][:n], m["direction"])
            ci = bootstrap_ci(x, seed_for(run_id, f"monitor:{promotion['promotion_id']}:{name}"), 1 - self.alpha, self.B)
            if m.get("role") == "target":
                bad = ci["upper"] < 0                                  # confident regression of the very metric the change was meant to improve
            else:
                bad = ci["lower"] < -m.get("margin", 0.02)             # the guardrail can no longer be shown non-inferior
            if bad:
                breaches.append({"metric": name, "lower": ci["lower"], "upper": ci["upper"], "delta": ci["mean"]})
        tier = promotion["tier"]
        action = "none" if not breaches else ("rollback" if tier in auto_rollback_tiers else "alert")
        return {"breach": bool(breaches), "breaches": breaches, "action": action}
