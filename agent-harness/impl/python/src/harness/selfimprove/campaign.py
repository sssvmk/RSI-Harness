"""CampaignController (§17.3): mine -> propose -> validate -> evaluate -> gate -> promote, with budgets, plateau stop and recovery.

Every step is idempotent by event key: a step whose event already exists is never repeated, so a crash at any event boundary resumes without
double-counted budgets or a double promotion (§19.7 test 11). Budget use is a *fold over events*, not a separate counter.
The model-driven parts are interfaces: `proposer.propose(...)` and `observer.scores(...)`; neither is implemented here."""
from __future__ import annotations
import inspect, json
from ..canonical import canonicalize, sha256_hex
from ..clock import format_ts, parse_ts
from ..ids import ulid
from ..stats import mean
from . import gate as G
from .edits import EditApplier
from .evalsets import EvalSetError, eval_seed
from .gitrepo import GitRepo
from .policy import GatePolicy
from . import observe as OBS
from ..failure import load_rules, observation
from .promotion import PromotionService
from .registry import ComponentRegistry
from .screens import run_screens
from .search import novelty_rejected

BUDGET_DIMS = ("proposals", "candidate_evals", "compute", "cost", "time_s")


class StopCampaign(Exception):
    def __init__(self, reason: str, failure_class: str | None = None):
        super().__init__(reason)
        self.reason, self.failure_class = reason, failure_class


class RecursionDenied(Exception):
    pass


class CampaignContext:
    """Event access for one campaign (a run of kind improvement_campaign)."""

    def __init__(self, store, writer, blobs, clock, campaign_id: str, owner: str):
        self.store, self.writer, self.blobs, self.clock, self.campaign_id, self.owner = store, writer, blobs, clock, campaign_id, owner

    async def ensure_run(self, base_commit: str, depth: int, parent: str | None) -> None:
        if self.store.run_row(self.campaign_id) is None:
            await self.writer.submit({"run_id": self.campaign_id, "type": "run.created", "actor": {"kind": "system", "id": "campaign"},
                                      "payload": {"owner": self.owner, "profile": "core", "config_commit": base_commit, "kind": "improvement_campaign", "parent_run_id": parent, "depth": depth}})

    def find(self, key: str, typ: str | None = None) -> dict | None:
        q, a = "SELECT * FROM events WHERE run_id=? AND correlation_id=?", [self.campaign_id, key]
        if typ:
            q += " AND type=?"; a.append(typ)
        r = self.store.conn().execute(q + " ORDER BY seq LIMIT 1", a).fetchone()
        if not r:
            return None
        from ..eventlog.store import row_to_event
        return row_to_event(r)

    async def emit_once(self, key: str, typ: str, payload: dict) -> dict:
        ex = self.find(key, typ)
        if ex:
            return ex
        return await self.writer.submit({"run_id": self.campaign_id, "type": typ, "payload": payload, "correlation_id": key, "actor": {"kind": "system", "id": "campaign"}})

    def events(self, *types: str) -> list[dict]:
        return self.store.events(self.campaign_id, types=list(types) or None)


def derive_id(*parts) -> str:
    return sha256_hex(":".join(str(p) for p in parts))[:26].upper()


class CampaignController:
    def __init__(self, ctx: CampaignContext, spec: dict, *, config_repo: GitRepo, improver_repo: GitRepo, control_repo: GitRepo, registry: ComponentRegistry,
                 evalsets, promotion: PromotionService, proposer, observer, params: dict, depth: int = 1, max_depth: int = 2, high_assurance: bool = False,
                 hooks: dict | None = None, answer_index: list[str] | None = None, instance_ids: list[str] | None = None, parent_run_id: str | None = None,
                 max_lines: int = 200, rules: dict | None = None, policy: GatePolicy | None = None, taxonomy: dict | None = None, thresholds: dict | None = None):
        self.ctx, self.spec = ctx, spec
        self.repo, self.improver, self.control, self.reg = config_repo, improver_repo, control_repo, registry
        self.evalsets, self.promotion, self.proposer, self.observer = evalsets, promotion, proposer, observer
        self.params, self.depth, self.max_depth, self.high = params, depth, max_depth, high_assurance
        self.policy = policy or GatePolicy.uniform(params)          # per-tier gate settings; a bare `params` dict applies to every tier, as before
        self.hooks, self.answer_index, self.instance_ids, self.parent = hooks or {}, answer_index or [], instance_ids or [], parent_run_id
        self.applier = EditApplier(config_repo, registry, max_lines)
        self.rules = rules
        self.taxonomy, self.thresholds = taxonomy or load_rules(), OBS.th(thresholds)
        self.cid = ctx.campaign_id
        self.campaign = {"campaign_id": self.cid, "owner": ctx.owner, "dry_run": bool(spec.get("dry_run"))}

    # ------------------------------------------------------------------ helpers
    def _used(self) -> dict:
        ev = self.ctx.events("proposal.created", "proposal.rejected", "candidate.evaluated")
        used = {"proposals": 0, "candidate_evals": 0, "compute": 0.0, "cost": 0.0, "time_s": 0.0}
        for e in ev:
            if e["type"] in ("proposal.created", "proposal.rejected"):
                used["proposals"] += 1
            else:
                used["candidate_evals"] += 1
                used["compute"] += e["payload"]["usage"].get("compute", 0.0); used["cost"] += e["payload"]["usage"].get("cost", 0.0)
        first = self.ctx.events("campaign.created")
        if first:
            used["time_s"] = (self.ctx.clock.now_ms() - parse_ts(first[0]["ts"])) / 1000.0
        return used

    async def _budget(self, dim: str, delta: float = 1) -> None:
        lim, used = self.spec["budgets"].get(dim), self._used()[dim]
        if lim is None:
            return
        if used + delta > lim:
            raise StopCampaign(f"budget_exhausted:{dim}")
        if used + delta >= 0.8 * lim:
            await self.ctx.emit_once(f"budget-warn:{dim}", "budget.warning", {"dimension": dim, "used": used + delta, "limit": lim})

    async def _state(self, to: str) -> None:
        cur = self.ctx.events("campaign.state_changed")
        frm = cur[-1]["payload"]["to"] if cur else "CREATED"
        if frm != to:
            await self.ctx.emit_once(f"state:{len(cur)}:{to}", "campaign.state_changed", {"from": frm, "to": to})

    def _surface_hashes(self) -> dict:
        return {"improver_tree": self.improver.worktree_hash(), "control_tree": self.control.worktree_hash()}

    async def _integrity(self, key: str) -> None:
        start = self.ctx.find("integrity:start", "surface.integrity_checked")
        now = self._surface_hashes()
        await self.ctx.emit_once(key, "surface.integrity_checked", {**now, "ok": now == {k: start["payload"][k] for k in now}})
        if now != {k: start["payload"][k] for k in now}:
            raise StopCampaign("integrity_failure", "eval_integrity.tampering")

    # ------------------------------------------------------------------ lifecycle
    async def start(self) -> None:
        if self.depth > self.max_depth:
            await self.ctx.ensure_run("-", self.depth, self.parent)
            await self.ctx.emit_once("recursion-denied", "recursion.denied", {"depth": self.depth, "max_depth": self.max_depth})
            raise RecursionDenied(f"depth {self.depth} exceeds max_depth {self.max_depth}")
        base = self.repo.head("main")
        await self.ctx.ensure_run(base, self.depth, self.parent)
        await self.ctx.emit_once("created", "campaign.created", {"spec": self.spec, "depth": self.depth, "dry_run": self.campaign["dry_run"], "owner": self.ctx.owner,
                                                                  "policy": self.policy.resolved, "policy_fingerprint": self.policy.fingerprint, "policy_environment": self.policy.environment, "policy_diff": self.policy.diff()})
        if self.policy.relaxed():                                   # a deployment that runs a leaner gate than the defaults says so, in the log, where reviewers will look
            await self.ctx.emit_once("policy-relaxed", "policy.relaxed", {"environment": self.policy.environment, "changes": self.policy.relaxed(), "fingerprint": self.policy.fingerprint})
        await self._state("PREPARING")
        if "T2" in self.spec["tiers_allowed"] and not self.high:
            await self.ctx.emit_once("pf23", "campaign.stopped", {"reason": "preflight_failed:PF-23", "detail": "T2 and meta-change need a High-assurance deployment"})
            raise StopCampaign("preflight_failed:PF-23")
        await self.ctx.emit_once("integrity:start", "surface.integrity_checked", {**self._surface_hashes(), "ok": True, "base": base,
                                                                                  "improver_commit": self.spec["improver"]["commit"]})

    async def run(self) -> dict:
        try:
            await self.start()
            while True:
                reason = self._stop_reason()
                if reason:
                    break
                r = self._next_round()
                try:
                    await self._round(r)
                except StopCampaign as e:
                    reason = e.reason
                    if e.failure_class:
                        await self.ctx.emit_once("fail-class", "failure.classified", {"class": e.failure_class.split(".")[0], "code": e.failure_class})
                    break
        except StopCampaign as e:
            return await self._stop(e.reason)
        return await self._stop(reason)

    def _next_round(self) -> int:
        done = {e["payload"]["round"] for e in self.ctx.events("campaign.round_completed")}
        r = 1
        while r in done:
            r += 1
        return r

    def _stop_reason(self) -> str | None:
        done = self.ctx.events("campaign.round_completed")
        st = self.spec["stop"]
        if len(done) >= st["rounds_max"]:
            return "rounds_max"
        pl = st.get("plateau")
        if pl and len(done) >= pl["window"]:
            last = done[-pl["window"]:]
            if all(not any(p["lower"] > pl.get("epsilon", 0.0) for p in e["payload"]["promoted"]) for e in last):
                return "plateau"
        if any(e["payload"].get("reason") == "no_addressable_patterns" for e in self.ctx.events("campaign.round_completed")):
            return "no_addressable_patterns"
        return None

    async def _stop(self, reason: str) -> dict:
        await self._state("TEARDOWN")
        await self.ctx.emit_once("stopped", "campaign.stopped", {"reason": reason, "used": self._used()})
        await self._state("STOPPED")
        return self.summary()

    def summary(self) -> dict:
        ev = self.ctx
        stopped = ev.find("stopped", "campaign.stopped")
        return {"campaign_id": self.cid, "stopped": stopped["payload"]["reason"] if stopped else None, "used": self._used(),
                "proposals": len(ev.events("proposal.created")), "rejected": len(ev.events("proposal.rejected")),
                "promotions": [e["payload"] for e in ev.events("promotion.committed")], "rounds": len(ev.events("campaign.round_completed"))}

    async def _note(self, key: str, code: str, subject: str, evidence: dict) -> None:
        await self.ctx.emit_once(f"obs:{key}:{code}", "failure.observed", observation(self.taxonomy, code, subject, evidence))

    # ------------------------------------------------------------------ one round
    async def _round(self, r: int) -> None:
        ctx = self.ctx
        rs = await ctx.emit_once(f"round:{r}", "campaign.round_started", {"round": r, "base_commit": self.repo.head("main")})
        base = rs["payload"]["base_commit"]
        await self._state("PROPOSING")
        cands, rejected = [], 0
        for n in range(1, self.spec["search"]["candidates_per_round"] + 1):
            c = await self._proposal(r, n, base)
            if c is None:
                rejected += 1
            elif c.get("exhausted"):
                await ctx.emit_once(f"round-done:{r}", "campaign.round_completed", {"round": r, "promoted": [], "reason": "no_addressable_patterns", "proposals": n})
                return
            else:
                cands.append(c)
        for code, ev in OBS.from_round([c["added_text"] for c in cands], self.thresholds): await self._note(f"round{r}", code, f"round:{r}", ev)
        await self._state("EVALUATING")
        evaluated = []
        for c in cands:
            evaluated.append((c, await self._evaluate(c, base)))
        promoted = []
        if evaluated:
            await self._integrity(f"integrity:gate:{r}")
            await self._state("GATING")
            decisions = await self._gate(r, evaluated)
            await self._state("PROMOTING")
            accepted = [(c, d) for (c, _), d in zip(evaluated, decisions) if d["accepted"]]
            if accepted:
                c, d = max(accepted, key=lambda cd: cd[1]["target"]["delta"])
                out = await self._promote(r, c, d)
                if out and out.get("status") == "promoted":
                    promoted.append({"candidate_id": out["candidate_id"], "lower": out["lower"], "promotion_id": out["promotion_id"]})
        await ctx.emit_once(f"round-done:{r}", "campaign.round_completed", {"round": r, "promoted": promoted, "proposals": len(cands) + rejected, "rejected": rejected})

    # ------------------------------------------------------------------ proposals and validation (§17.6, §17.7)
    async def _proposal(self, r: int, n: int, base: str) -> dict | None:
        ctx, pid = self.ctx, derive_id(self.cid, r, n)
        ex = ctx.find(f"prop:{pid}")
        if ex:
            return None if ex["type"] == "proposal.rejected" else {**ex["payload"], "candidate_id": pid}
        await self._budget("proposals")
        raw = self.proposer.propose(r, n, base, {"campaign_id": self.cid})
        if inspect.isawaitable(raw): raw = await raw                                  # a proposer taken from a plugin is asynchronous
        if raw is None:
            return {"exhausted": True}
        reasons = self._validate_manifest(raw.get("manifest"))
        edits = raw.get("edits")
        edit_hash = sha256_hex(canonicalize(edits)) if isinstance(edits, list) else None
        if not reasons and edit_hash and self._previously_rejected(edit_hash, pid):
            reasons.append({"code": "replayed_rejected_edit"})
        commit, tier, added, lines = None, None, [], 0
        branch = f"cand/{self.cid}/{r}/{n}"
        if not reasons:
            mh = sha256_hex(canonicalize(raw["manifest"]))
            res = self.applier.apply(base, edits, self.spec["tiers_allowed"], branch, f"proposal {pid}\n\nmanifest-hash: {mh}")
            reasons += res.reasons
            if res.ok:
                commit, tier, added, lines = res.commit, res.tier, res.added, res.changed_lines
                for v in run_screens(res.added, self.answer_index, self.instance_ids, self.rules):
                    reasons.append({"code": f"screen.{v['screen']}", "path": v["path"], "detail": v["detail"]})
                text = "\n".join(a["added"] for a in added)
                idx = novelty_rejected(text, [self._edit_text(e) for e in ctx.events("proposal.created")])
                if idx is not None and not reasons:
                    reasons.append({"code": "novelty.too_similar", "detail": f"similar to archived candidate #{idx}"})
        if reasons:
            if commit:
                self.repo.git("branch", "-D", branch, check=False)
            await ctx.writer.execute("INSERT OR IGNORE INTO negative_results(id,campaign_id,pattern_id,edit_summary_ref,reason,created_at) VALUES(?,?,?,?,?,?)",
                                     (edit_hash or pid, self.cid, pid, ctx.blobs.put_json(edits if isinstance(edits, list) else []), reasons[0]["code"], format_ts(ctx.clock.now_ms())))
            await ctx.writer.execute("INSERT INTO negative_results_fts(text,id) VALUES(?,?)", (json.dumps(reasons), edit_hash or pid))
            await ctx.emit_once(f"prop:{pid}", "proposal.rejected", {"proposal_id": pid, "round": r, "n": n, "reasons": reasons, "edit_hash": edit_hash})
            for code, ev in OBS.from_rejection(reasons): await self._note(pid, code, pid, ev)
            return None
        ref = ctx.blobs.put_json(edits)
        payload = {"proposal_id": pid, "candidate_id": pid, "round": r, "n": n, "parent_commit": base, "commit": commit, "branch": branch, "tier": tier, "edit_hash": edit_hash,
                   "manifest_hash": sha256_hex(canonicalize(raw["manifest"])), "manifest": raw["manifest"], "changed_lines": lines, "edit_text_ref": ref, "added_text": "\n".join(a["added"] for a in added)}
        await ctx.writer.execute("INSERT OR IGNORE INTO candidates(candidate_id,campaign_id,round,parent_commit,commit_id,tier,status,edit_text_ref) VALUES(?,?,?,?,?,?,'valid',?)", (pid, self.cid, r, base, commit, tier, ref))
        await ctx.emit_once(f"valid:{pid}", "validation.completed", {"proposal_id": pid, "ok": True})
        await ctx.emit_once(f"prop:{pid}", "proposal.created", payload)
        return payload

    @staticmethod
    def _edit_text(ev: dict) -> str:
        return ev["payload"].get("added_text", "")

    def _previously_rejected(self, edit_hash: str, pid: str) -> bool:
        """A replay of an earlier rejected edit (by any *other* proposal; a resumed proposal never counts as its own replay)."""
        return self.ctx.store.conn().execute("SELECT 1 FROM negative_results WHERE id=? AND COALESCE(pattern_id,'') != ?", (edit_hash, pid)).fetchone() is not None

    @staticmethod
    def _validate_manifest(m) -> list[dict]:
        if not isinstance(m, dict):
            return [{"code": "manifest.missing"}]
        miss = [k for k in ("failure_evidence", "root_cause", "fix", "predicted_fixes", "at_risk_regressions") if k not in m]
        if miss:
            return [{"code": "manifest.incomplete", "detail": ",".join(miss)}]
        if not m["failure_evidence"] or not all(isinstance(p, dict) and "metric" in p and "direction" in p for p in m["predicted_fixes"]) \
                or not all(isinstance(p, dict) and "metric" in p for p in m["at_risk_regressions"]):
            return [{"code": "manifest.malformed"}]
        return []

    # ------------------------------------------------------------------ evaluation (§17.8.1)
    def _observe(self, commit: str, role: str, set_id: str, variant: str, model: str, seed: str) -> tuple[dict, dict]:
        key = sha256_hex(f"{commit}|{set_id}|{variant}|{model}|{seed}")
        row = self.ctx.store.conn().execute("SELECT observations_ref FROM observation_cache WHERE key=?", (key,)).fetchone()
        if row:
            return self.ctx.blobs.get_json(row["observations_ref"]), {}
        s = self.observer.scores(commit, role, variant, model, seed)
        usage = s.get("usage", {})
        return s, usage

    async def _cache(self, commit: str, set_id: str, variant: str, model: str, seed: str, scores: dict) -> None:
        key = sha256_hex(f"{commit}|{set_id}|{variant}|{model}|{seed}")
        await self.ctx.writer.execute("INSERT OR REPLACE INTO observation_cache(key,config_commit,set_id,model_ref,seeds_hash,observations_ref,created_at) VALUES(?,?,?,?,?,?,?)",
                                      (key, commit, set_id, model, sha256_hex(seed), self.ctx.blobs.put_json(scores), format_ts(self.ctx.clock.now_ms())))

    def _pair(self, base: dict, cand: dict, workload: dict) -> dict:
        ids = sorted(cand["target"])
        if sorted(base["target"]) != ids:
            raise ValueError("baseline and candidate observations must cover the same instances")
        tdir = workload.get("target_direction", "maximize")
        out = {"target": {"b": [base["target"][i] for i in ids], "c": [cand["target"][i] for i in ids], "direction": tdir}, "guardrails": []}
        for m in sorted(cand.get("guardrails", {})):
            out["guardrails"].append({"metric": m, "b": [base["guardrails"][m][i] for i in ids], "c": [cand["guardrails"][m][i] for i in ids],
                                      "direction": workload.get("guardrail_directions", {}).get(m, "minimize"), **({"margin": workload["margins"][m]} if m in workload.get("margins", {}) else {})})
        if "passes" in cand:
            must = [ids.index(i) for i in workload.get("must_pass_ids", []) if i in ids]
            out["preserved"] = {"b": [base["passes"][i] for i in ids], "c": [cand["passes"][i] for i in ids], "must_pass": must}
        return out

    async def _evaluate(self, c: dict, base_commit: str) -> dict:
        ctx, pid = self.ctx, c["candidate_id"]
        ex = ctx.find(f"eval:{pid}", "candidate.evaluated")
        if ex:
            return ctx.blobs.get_json(ex["payload"]["data_ref"])
        await self._budget("candidate_evals")
        await self._integrity(f"integrity:eval:{pid}")
        wl, data, usage = self.spec["workload"], {}, {"compute": 0.0, "cost": 0.0}
        for model in self.spec["target_models"]:
            variants = {}
            for variant in ("canonical", "format_varied"):
                seed = eval_seed(self.cid, c["round"], pid, wl["scoring"])
                cs, cu = self._observe(c["commit"], "scoring", wl["scoring"], variant, model, seed)
                bs, bu = self._observe(c["parent_commit"], "scoring", wl["scoring"], variant, model, seed)
                if bu:
                    await self._cache(c["parent_commit"], wl["scoring"], variant, model, seed, bs)
                for u in (cu, bu):
                    for k in usage:
                        usage[k] += u.get(k, 0.0)
                variants[variant] = self._pair(bs, cs, wl)
            data[model] = {"variants": variants}
        first = next(iter(data.values()))["variants"]["canonical"]
        summary = {"target": mean(G.improvements(first["target"]["b"], first["target"]["c"], first["target"]["direction"])),
                   "cost": usage["cost"], "latency": float(first["guardrails"][0]["c"][0]) if first["guardrails"] else 0.0}
        ref = ctx.blobs.put_json(data)
        await ctx.emit_once(f"eval:{pid}", "candidate.evaluated", {"candidate_id": pid, "data_ref": ref, "usage": usage, "summary": summary})
        await ctx.writer.execute("UPDATE candidates SET status='evaluated', observations_ref=? WHERE candidate_id=?", (ref, pid))
        return data

    # ------------------------------------------------------------------ gate (§17.8.2)
    def _archive(self, r: int) -> list[dict]:
        """Accepted candidates of *previous* rounds only, so a resumed round sees exactly what an uninterrupted one saw."""
        return [e["payload"]["summary"] for e in self.ctx.events("gate.decided") if e["payload"]["accepted"] and "summary" in e["payload"] and e["payload"].get("round", 0) < r]

    async def _gate(self, r: int, evaluated: list[tuple[dict, dict]]) -> list[dict]:
        ctx, wl = self.ctx, self.spec["workload"]
        existing = [ctx.find(f"gate:{c['candidate_id']}", "gate.decided") for c, _ in evaluated]
        if all(existing):
            return [e["payload"] for e in existing]
        archive = self._archive(r)
        summaries = {c["candidate_id"]: ctx.find(f"eval:{c['candidate_id']}", "candidate.evaluated")["payload"]["summary"] for c, _ in evaluated}
        cdata = [{"proposal_id": c["candidate_id"], "tier": c["tier"], "models": data, "summary": summaries[c["candidate_id"]], "archive": archive} for c, data in evaluated]
        alphas: list = [None] * len(cdata)
        by_tier: dict[str, list[int]] = {}
        for i, cd in enumerate(cdata): by_tier.setdefault(cd["tier"], []).append(i)
        for tier, idx in by_tier.items():                            # Holm is applied within a tier: each tier has its own alpha, so each is its own family of tests
            for i, al in zip(idx, G.round_alphas(self.cid, [cdata[i] for i in idx], self.policy.gate_params(tier))): alphas[i] = al
        out = []
        for i, (c, data) in enumerate(evaluated):
            pid = c["candidate_id"]
            ex = existing[i]
            if ex:
                out.append(ex["payload"]); continue
            dec = G.evaluate_candidate(self.cid, cdata[i], self.policy.gate_params(c["tier"]), alphas[i])
            if dec["needs_heldout"]:
                dec = await self._heldout(c, cdata[i], alphas[i], dec)
            dec["summary"], dec["round"] = summaries[pid], r
            await ctx.emit_once(f"gate:{pid}", "gate.decided", dec)
            for code, ev in OBS.from_decision(dec, self.thresholds): await self._note(pid, code, pid, ev)
            await ctx.writer.execute("UPDATE candidates SET status=? WHERE candidate_id=?", ("accepted" if dec["accepted"] else "rejected", pid))
            if not dec["accepted"]:
                await ctx.writer.execute("INSERT OR IGNORE INTO negative_results(id,campaign_id,pattern_id,edit_summary_ref,reason,created_at) VALUES(?,?,?,?,?,?)",
                                         (c["edit_hash"], self.cid, pid, c["edit_text_ref"], "gate:" + (dec["reasons"][0] if dec["reasons"] else "rejected"), format_ts(ctx.clock.now_ms())))
            out.append(dec)
        return out

    async def _heldout(self, c: dict, cd: dict, alpha: dict, dec: dict) -> dict:
        ctx, wl, pid = self.ctx, self.spec["workload"], c["candidate_id"]
        ho = ctx.find(f"heldout:{pid}", "heldout.queried")
        if ho:
            held = ctx.blobs.get_json(ho["payload"]["data_ref"])
        else:
            try:
                n = await self.evalsets.record_query(wl["heldout"], self.cid, pid)
            except EvalSetError as e:                       # only the expected refusal; any other error is a bug and must surface
                dec["reasons"].append(f"heldout:{e.code}"); dec["needs_heldout"] = False; dec["accepted"] = False
                return dec
            sb = OBS.selection_bias(n, self.evalsets.max_q, self.thresholds)
            if sb: await self._note(f"set:{wl['heldout']}", sb[0], wl["heldout"], sb[1])
            held = {}
            for model in self.spec["target_models"]:
                seed = eval_seed(self.cid, c["round"], pid, wl["heldout"])
                cs = self.observer.scores(c["commit"], "heldout", "canonical", model, seed)
                bs = self.observer.scores(c["parent_commit"], "heldout", "canonical", model, seed)
                held[model] = self._pair(bs, cs, wl)
            ref = ctx.blobs.put_json(held)
            await ctx.emit_once(f"heldout:{pid}", "heldout.queried", {"candidate_id": pid, "set_id": wl["heldout"], "data_ref": ref, "queries": n})
        cd2 = json.loads(json.dumps(cd))
        for model, d in held.items():
            cd2["models"][model]["heldout"] = {"target": d["target"], "preserved": d.get("preserved")}
        return G.evaluate_candidate(self.cid, cd2, self.policy.gate_params(c["tier"]), alpha)

    # ------------------------------------------------------------------ promotion (§17.10)
    async def _promote(self, r: int, c: dict, d: dict) -> dict | None:
        cand = {"candidate_id": c["candidate_id"], "commit": c["commit"], "parent_commit": c["parent_commit"], "tier": c["tier"]}
        if "before_promote" in self.hooks:
            self.hooks["before_promote"](c)                       # test seam: lets a scenario advance `main` during evaluation
        attempt = 0
        while True:
            res = await self.promotion.promote(self.campaign, cand, d, self.hooks.get("approval"))
            if res["status"] == "promoted":
                return {**res, "candidate_id": cand["candidate_id"], "lower": d["target"]["lower"]}
            if res["status"] != "stale" or attempt >= 3:
                return {**res, "candidate_id": cand["candidate_id"]}
            attempt += 1                                          # stale base: rebase the edits onto the new main and run the gate again
            cand, d = await self._regate(r, c, attempt)
            if cand is None:
                return {"status": "denied", "reason": "rebase_failed"}

    async def _regate(self, r: int, c: dict, k: int):
        ctx, new_base = self.ctx, self.repo.head("main")
        pid = derive_id(self.cid, r, f"{c['n']}rb{k}")
        edits = ctx.blobs.get_json(c["edit_text_ref"])
        res = self.applier.apply(new_base, edits, self.spec["tiers_allowed"], f"cand/{self.cid}/{r}/{c['n']}-rb{k}", f"proposal {pid}\n\nmanifest-hash: {c['manifest_hash']}")
        await ctx.emit_once(f"regate:{pid}", "gate.regated", {"candidate_id": c["candidate_id"], "new_candidate_id": pid, "new_base": new_base, "ok": res.ok, "reasons": res.reasons})
        if not res.ok:
            return None, None
        nc = {**c, "candidate_id": pid, "proposal_id": pid, "parent_commit": new_base, "commit": res.commit, "tier": res.tier}
        data = await self._evaluate(nc, new_base)
        decisions = await self._gate(r, [(nc, data)])
        return {"candidate_id": pid, "commit": res.commit, "parent_commit": new_base, "tier": res.tier}, decisions[0]
