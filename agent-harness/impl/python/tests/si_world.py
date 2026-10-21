"""A simulated improvement environment: real git repos and event log, scripted proposer, deterministic content-driven observer."""
import hashlib, json, os
from pathlib import Path
from harness.blobs import BlobStore
from harness.clock import FakeClock
from harness.eventlog.store import EventStore, EventWriter
from harness.ids import ulid
from harness.selfimprove.campaign import CampaignContext, CampaignController
from harness.selfimprove.evalsets import EvalSetRegistry
from harness.selfimprove.gitrepo import GitRepo
from harness.selfimprove.promotion import PromotionService
from harness.selfimprove.registry import ComponentRegistry
from harness.selfimprove.schema import apply_migration_002

REGISTRY_YAML = """
schema_version: 1
components:
  - {path: "prompts/**", component: system_prompt, tier: T1, owner: platform, origin: human}
  - {path: "memory/playbook.jsonl", component: playbook, tier: T0, owner: platform, origin: learned}
  - {path: "tools/**", component: tool_description, tier: T1, owner: platform, origin: human}
  - {path: "skills/triage/**", component: skill, tier: T2, owner: ops, origin: learned, provides: [skill.triage]}
  - {path: "workflows/**", component: workflow, tier: T2, owner: ops, origin: learned, requires: [skill.triage]}
"""
CONFIG_FILES = {"components.yaml": REGISTRY_YAML, "prompts/system.md": "You are an agent.\nBe careful.\n", "prompts/settings.yaml": "temperature: 0.2\nbudgets:\n  turns: 10\nmodel: base\n",
                "memory/playbook.jsonl": "", "tools/search.md": "Search the web.\n", "skills/triage/SKILL.md": "Triage.\n", "workflows/flow.yaml": "steps: []\n"}
SCORING_IDS = [f"s{i:03d}" for i in range(30)]
HELDOUT_IDS = [f"h{i:03d}" for i in range(20)]
MUST_PASS = SCORING_IDS[:3]
PARAMS = {"alpha": 0.05, "B": 400, "holm": True, "margins": {"default": 0.02}, "heldout_margin": 0.02, "preserved_margin": 0.02}
MANIFEST = {"failure_evidence": ["pat-1"], "root_cause": "x", "fix": "y", "predicted_fixes": [{"metric": "target", "direction": "up"}], "at_risk_regressions": [{"metric": "cost"}]}


def u(*parts) -> float:
    return int(hashlib.sha256("|".join(map(str, parts)).encode()).hexdigest()[:8], 16) / 4294967296.0


def edit_hint(text: str, marker: str = "Be careful.\n") -> dict:
    return {"path": "prompts/system.md", "op": "replace", "search": marker, "replace": marker + text + "\n"}


def proposal(*edits, manifest=None):
    return {"edits": list(edits), "manifest": manifest or MANIFEST}


class SimObserver:
    """Scores depend on the *content* of the prompt at a commit: `HINT+` lines help, `HINT-` hurt, `VERBOSE` raises cost, `OVERFIT` helps scoring and hurts held-out,
    `FMT-` hurts only the format-varied variant, `BREAK-` breaks must-pass instances."""
    def __init__(self, repo: GitRepo):
        self.repo, self.calls = repo, []

    def scores(self, commit, role, variant, model, seed):
        self.calls.append((commit, role, variant, model, seed))
        text = (self.repo.show_file(commit, "prompts/system.md") or "") + "\n" + (self.repo.show_file(commit, "skills/triage/SKILL.md") or "") + "\n" + (self.repo.show_file(commit, "memory/playbook.jsonl") or "")   # prompt, skill and playbook all steer behaviour
        n = lambda tag: text.count(tag)
        eff = 0.1 * n("HINT+") - 0.1 * n("HINT-")
        eff += (0.2 * n("OVERFIT")) if role == "scoring" else (-0.3 * n("OVERFIT"))
        if variant == "format_varied":
            eff = eff * 0.9 - 0.2 * n("FMT-")
        ids = SCORING_IDS if role == "scoring" else HELDOUT_IDS
        tgt, cost, passes = {}, {}, {}
        for i in ids:
            base = 0.4 + 0.2 * u("base", i)
            s = base + (eff * (0.5 + u("eff", i)) if eff else 0.0) + 0.0 * u(seed, i)
            if n("BREAK-") and i in MUST_PASS:
                s = 0.0
            tgt[i], cost[i] = s, 1.0 + 0.05 * n("VERBOSE")
            passes[i] = 1 if s >= 0.5 else 0
        return {"target": tgt, "guardrails": {"cost": cost}, "passes": passes, "usage": {"compute": 1.0, "cost": 0.5}}


class ScriptedProposer:
    def __init__(self, script: dict):
        self.script, self.calls = script, []

    def propose(self, r, n, base, ctx):
        self.calls.append((r, n, base))
        v = self.script.get((r, n), self.script.get("default"))
        return v(r, n, base) if callable(v) else v


HINT = lambda t: proposal(edit_hint(t))


class World:
    def __init__(self, tmp: Path, script: dict, *, high=False, spec_over=None, params=None, review=None, answers=(), clock=None, hooks=None, cid=None, policy=None):
        self.tmp = Path(tmp); self.tmp.mkdir(parents=True, exist_ok=True)
        self.clock = clock or FakeClock()
        self.store = EventStore(str(self.tmp / "log.db")); self.writer = EventWriter(self.store, self.clock).start(); self.blobs = BlobStore(self.tmp / "blobs")
        mk = lambda name, files: GitRepo(self.tmp / name) if (self.tmp / name / ".git").exists() else GitRepo.init(self.tmp / name, files)
        self.config = mk("config", CONFIG_FILES)
        self.improver = mk("improver", {"prompts/proposer.md": "Propose edits.\n"})
        self.control = mk("control", {"improvement.yaml": "gate: {alpha: 0.05}\n"})
        self.registry = ComponentRegistry.from_yaml(REGISTRY_YAML)
        self.cid = cid or ulid()
        self.observer, self.proposer = SimObserver(self.config), ScriptedProposer(script)
        self.params, self.high, self.hooks = params or PARAMS, high, hooks or {}
        self.review = {"t1_sample_rate": 0.2, "high_assurance": high, **(review or {})}
        self.spec = {"campaign_id": self.cid, "base": {"repo": "harness-config", "ref": "main"}, "target_models": ["main"],
                     "workload": {"feedback": "set-f", "scoring": "set-s", "heldout": "set-h", "confirm": "set-c", "transfer": [], "must_pass_ids": MUST_PASS,
                                  "target_direction": "maximize", "guardrail_directions": {"cost": "minimize"}},
                     "tiers_allowed": ["T0", "T1"], "improver": {"commit": self.improver.head()}, "search": {"mode": "mixed", "strategy": "archive", "candidates_per_round": 2},
                     "budgets": {"proposals": 40, "candidate_evals": 80, "compute": 2000, "cost": 500.0, "time_s": 86400}, "stop": {"rounds_max": 3, "plateau": {"window": 3, "epsilon": 0.0}}, "dry_run": False}
        for k, v in (spec_over or {}).items():
            self.spec[k] = {**self.spec[k], **v} if isinstance(v, dict) and isinstance(self.spec.get(k), dict) else v
        self.answers = answers
        self.ctx = CampaignContext(self.store, self.writer, self.blobs, self.clock, self.cid, "alice")
        self.evalsets = EvalSetRegistry(self.store, self.writer, self.clock, max_queries_per_set=20)
        self.policy = policy
        self.promotion = PromotionService(self.ctx, self.config, self.registry, self.review, policy=policy)

    async def setup(self, evalsets=True):
        await apply_migration_002(self.writer)
        if evalsets:
            for sid, role, ids in (("set-s", "scoring", SCORING_IDS), ("set-h", "heldout", HELDOUT_IDS)):
                if sid not in self.evalsets.sets:
                    await self.evalsets.register(sid, role, [{"id": i, "text": f"{role} task number {k} about topic {i} {role}"} for k, i in enumerate(ids)])
        return self

    def controller(self, **kw) -> CampaignController:
        from harness.selfimprove.screens import build_answer_index
        return CampaignController(self.ctx, self.spec, config_repo=self.config, improver_repo=self.improver, control_repo=self.control, registry=self.registry,
                                  evalsets=self.evalsets, promotion=self.promotion, proposer=self.proposer, observer=self.observer, params=self.params, high_assurance=self.high,
                                  hooks={**self.hooks, **kw.pop("hooks", {})}, policy=self.policy, answer_index=build_answer_index(list(self.answers)), instance_ids=SCORING_IDS + HELDOUT_IDS, **kw)

    def close(self):
        self.writer.close()

    def events(self, *types):
        return self.ctx.events(*types)



async def make(tmp_path, script, **kw):
    w = World(tmp_path, script, **kw)
    await w.setup()
    return w
