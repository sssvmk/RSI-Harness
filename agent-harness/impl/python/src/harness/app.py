"""Harness: wires services into a runnable daemon-lite (no HTTP layer)."""
from __future__ import annotations
from .canonical import sha256_hex
import asyncio, tempfile
from pathlib import Path
from .approvals import ApprovalService
from .blobs import BlobStore
from .clock import Clock
from .config import config_commit, load_config
from .credentials import DictSecretProvider, Redactor
from .eventlog.store import EventStore, EventWriter
from .gateway.gateway import HttpTransport, ModelGateway
from .loop.controller import LoopController
from .plugin_consumers import register_retrieve_tool
from .plugins import PluginHost
from .project import UNSET, declare as declare_project
from .preflight import run_preflight
from .run import RunManager, Services
from .sandbox.backend import LocalProcessBackend
from .sandbox.manager import SandboxManager
from .tools.builtin import register_builtins
from .tools.memory_guard import MemoryGuard
from .tools.registry import ToolRegistry
from .tools.runtime import ToolRuntime

DEFAULT_SYSTEM = "You are an autonomous software agent. Work in /workspace using the provided tools. Finish with the finish tool when the task is complete."
CORE_RULES = {"schema_version": 1, "rules": [
    {"id": "R-core-irreversible-in-containment", "description": "Core profile: no external effects, so irreversible in-sandbox actions are allowed (§9.6, §16.2)",
     "match": {"profile": ["core"], "effect_class": ["irreversible"]}, "action": "allow"}]}
EMPTY_RULES = {"schema_version": 1, "rules": []}


def default_ruleset(profile: str) -> dict:
    return CORE_RULES if profile == "core" else EMPTY_RULES


class Harness:
    def __init__(self, config: dict, transport=None, secrets=None, backend=None, clock: Clock | None = None, ruleset: dict | None = None,
                 config_dir: str | None = None, rng=None, id_factory=None):
        self.config = config
        self.clock = clock or Clock()
        Path(config["storage"]["event_log"]["path"]).parent.mkdir(parents=True, exist_ok=True)
        self.store = EventStore(config["storage"]["event_log"]["path"])
        self.writer = EventWriter(self.store, self.clock).start()
        self.blobs = BlobStore(config["storage"]["blobs"]["root"])
        self.svc = Services(self.store, self.writer, self.blobs, self.clock, config)
        self.secrets = secrets or DictSecretProvider({})
        self.redactor = Redactor()
        self.backend = backend or LocalProcessBackend(limits=(config.get("sandbox") or {}).get("limits"))
        self.sandboxes = SandboxManager(self.backend, config.get("sandbox", {}).get("allow_dev_tier", False))
        self.rm = RunManager(self.svc, self.sandboxes)
        self.registry = ToolRegistry()
        register_builtins(self.registry)
        self.ruleset = ruleset or default_ruleset(config["profile"])
        self.approvals = ApprovalService(self.svc, self.redactor)
        self.models = {m["ref"]: m for m in config["models"]}
        pc = config.get("plugins") or {}
        self.plugins = PluginHost(config["profile"], set(pc.get("trusted_in_process", [])), pc.get("max_failures", 3), pc.get("strict", config["profile"] != "core"), bool(pc.get("verify_errors", False))); self.svc.plugins = self.plugins; self._plugins_loaded = False
        gw = config.get("gateway", {})
        self.gateway = ModelGateway(self.models, transport or HttpTransport(), self.clock, self.secrets, max_attempts=gw.get("max_attempts", 4),
                                    timeout_s=gw.get("timeout_s", 120), rng=rng, id_factory=id_factory)
        self.runtime = ToolRuntime(self.registry, self.ruleset, self.approvals, self.svc, self.redactor, config.get("tokens", {}).get("divisor", 3.0),
                                   config["tools"]["selection_error_alert"])
        self.memory_guard = MemoryGuard(self.svc)
        self.config_dir = config_dir
        self.loops: dict[str, LoopController] = {}

    async def load_plugins(self) -> None:
        """Load the plugins listed in `plugins.load`. A plugin that fails verification stops startup instead of being skipped."""
        if self._plugins_loaded: return
        self._plugins_loaded = True
        for m in (self.config.get("plugins") or {}).get("load", []): await self.plugins.load(m)
        if self.plugins.has("retrieval") and "retrieve" not in self.registry.tools: register_retrieve_tool(self.registry, self.plugins)      # the tool exists only while a retrieval plugin does

    def preflight(self, profile: str | None = None, **kw) -> dict:
        kw.setdefault("plugin_problems", self.plugins.requirements_met((self.config.get("plugins") or {}).get("required", [])))
        return run_preflight(profile or self.config["profile"], self.config, store=self.store, writer=self.writer, backend=self.backend,
                             registry=self.registry, ruleset=self.ruleset, **kw)

    def default_agent(self, **over) -> dict:
        a = {"agent_id": "agent", "model_ref": self.config["models"][0]["ref"], "system_prompt": DEFAULT_SYSTEM, "tool_allowlist": ["*"],
             "skills": [], "max_output_tokens": 4096}
        a.update(over)
        return a

    def _skill_text(self, name: str) -> str:
        p = Path(self.config_dir) / "skills" / name / "SKILL.md" if self.config_dir else None
        return p.read_text() if p and p.is_file() else name

    async def _manifest(self, profile: str, instruction: str, agent: dict, task_id: str | None, budgets: dict | None, extra: dict | None) -> dict:
        """Publish the definitions this run will use as immutable versions and build its provenance manifest (design §13.9). A required field that cannot be recorded refuses the run."""
        from . import manifest as M
        from .budgets import normalize_budgets, unmetered
        from .definitions import DefinitionStore
        ds, extra, pins = DefinitionStore(self.svc), extra or {}, []
        pins.append(await ds.publish("prompt.system", agent["system_prompt"]))
        for name in agent.get("skills", []): pins.append(await ds.publish(f"skill.{name}", self._skill_text(name)))
        for name in self.registry.allowed(agent["tool_allowlist"]): pins.append(await ds.publish(f"tool.{name}", self.registry.tools[name].manifest))
        cfg_budgets = budgets if budgets is not None else self.config.get("budgets", {})
        pins.append(await ds.publish("budget_policy", normalize_budgets(cfg_budgets)))
        pins.append(await ds.publish("workflow", {"agent": {k: v for k, v in agent.items() if k != "system_prompt"}, "loop": self.config.get("loop", {}), "context": self.config.get("context", {})}))
        tier = {"name": self.backend.tier, "backend": self.backend.name, "profile": profile, "limits": getattr(self.backend, "limits", None)}
        pins.append(await ds.publish("tier_policy", tier))
        for pid, content in (extra.get("definitions") or {}).items(): pins.append(await ds.publish(pid, content))
        for pid, content in self.plugins.definitions().items(): pins.append(await ds.publish(pid, content))     # the plugins a run uses are pinned like any other definition
        m = self.models[agent["model_ref"]]; pins = sorted(pins, key=lambda p: p["id"]); local = self.backend.name == "local"
        fields = {
            "models": M.rec([{"role": "agent", "provider": m["provider"], "model_id": m["model_id"], "version": m.get("version", m["model_id"]), "snapshot": bool(m.get("snapshot", False))}]),
            "harness_version": M.rec("0.1.0"), "config_commit": M.rec(config_commit(self.config)), "profile": M.rec(profile),
            "limits": M.rec({"agent": {"window": m["window"], "max_output_tokens": agent.get("max_output_tokens", 4096)}}),
            "definitions": M.rec(pins),
            "tier_policy": M.rec({"name": tier["name"], "version": next(p["version"] for p in pins if p["id"] == "tier_policy"), "sha256": next(p["sha256"] for p in pins if p["id"] == "tier_policy")}),
            "image_digests": M.na("the local backend runs processes, not images") if local else M.unavailable("this backend does not report image digests"),
            "build_hashes": M.na("no native executor or egress binaries are used by this backend") if not getattr(self.backend, "has_native_binaries", False) else M.unavailable("not recorded"),
            "task": extra.get("task") or M.na("not an evaluation task"),
            "inputs": M.rec({"instruction_sha256": sha256_hex(instruction.encode("utf-8")), "dataset_versions": [], "retrieval_index_version": None}, "no datasets or retrieval index are used"),
            "budgets": M.rec({"limits": normalize_budgets(cfg_budgets), "unmetered": unmetered(cfg_budgets)}),
            "test_visibility": extra.get("test_visibility") or M.na("not an evaluation task"),
            "parent_run_id": M.na("a top-level run"),
            "project": M.rec(extra["project"]) if extra.get("project") else M.na("project memory is not enabled and no project was declared"), **M.pending_final()}
        man = {"manifest_schema_version": M.SCHEMA_VERSION, "run_id": "", "fields": fields}
        errs = M.validate({**man, "run_id": "pending"}, profile)
        if errs: raise M.ManifestError("the run manifest is incomplete: " + "; ".join(errs))
        return man

    async def create_run(self, owner: str, instruction: str, agent: dict | None = None, verifier=None, task_id: str | None = None,
                         budgets: dict | None = None, evaluator_budgets: dict | None = None, hooks: dict | None = None, profile: str | None = None,
                         preflight_kw: dict | None = None, manifest_extra: dict | None = None, project=UNSET, ask_project=None):
        await self.load_plugins()
        decl = await declare_project(self.config, project, ask_project)                      # the user says whether this run is part of a project; the harness never guesses (§7.3.10)
        manifest_extra = {**(manifest_extra or {}), "project": decl}
        profile = profile or self.config["profile"]
        agent = agent or self.default_agent()
        pf = self.preflight(profile, **(preflight_kw or {}))
        manifest = await self._manifest(profile, instruction, agent, task_id, budgets, manifest_extra)
        run = await self.rm.create_run(owner, profile, instruction, agent, task_id, config_commit(self.config), budgets, evaluator_budgets,
                                       provenance={"models": {r: m["model_id"] for r, m in self.models.items()}, "harness_version": "0.1.0", "unmetered": __import__("harness.budgets", fromlist=["unmetered"]).unmetered(self.config.get("budgets"))},
                                       preflight=pf, egress=self.config.get("egress", {}).get("grants"), manifest=manifest)
        loop = LoopController(run, self.rm, self.gateway, self.runtime, self.registry, agent, self.models, self.config, verifier, hooks,
                              self.config_dir, self.memory_guard)
        self.loops[run.run_id] = loop
        return run, loop

    async def run_to_end(self, owner: str, instruction: str, **kw):
        run, loop = await self.create_run(owner, instruction, **kw)
        if await run.state() == "READY":
            await self.rm.start(run)
            await loop.run_loop()
        return run

    async def create_eval_run(self, owner: str, task, agent_over: dict | None = None, **kw):
        """Evaluation run from a task package: budgets, allowlist and instruction come from the task descriptor (§13.1)."""
        from .eval.runner import attach_eval
        spec = task.spec
        from . import manifest as M
        from .eval.outcome import contract_for, contract_hash
        from .eval.visibility import declaration_for, tests_hash, validate_declaration
        bad = validate_declaration(spec)
        if bad: raise ValueError("; ".join(bad))                                          # a contradictory declaration is rejected at run start as well as by `harness task validate`
        decl, contract = declaration_for(spec), contract_for(spec)
        extra = {"task": M.rec({"id": task.id, "version": spec["version"], "grader_sha256": tests_hash(task.tests_dir), "outcome_contract_sha256": contract_hash(contract)}),
                 "test_visibility": M.rec({"declaration": decl}, "the verified runtime state is recorded when the environment is ready"),
                 "definitions": {"evaluator_rubric": {"scoring": spec["scoring"], "outcome": contract}}}
        kw.setdefault("manifest_extra", extra)
        b = dict(self.config.get("budgets", {}))
        b.update(spec.get("agent", {}).get("budgets", {}))
        agent = self.default_agent(tool_allowlist=spec.get("agent", {}).get("tool_allowlist", ["*"]), **(agent_over or {}))
        run, loop = await self.create_run(owner, task.instruction, agent=agent, task_id=f"{task.id}@{spec['version']}", budgets=b, **kw)
        runner = attach_eval(self, loop, task)
        if await run.state() == "READY": await self._expose_tests(run, loop, task, decl)       # what the environment actually does, verified and recorded apart from the policy
        return run, loop, runner

    async def _expose_tests(self, run, loop, task, decl: dict) -> None:
        from .eval.visibility import hidden_state, mount_public_tests
        public = task.spec.get("visibility", "hidden") == "public"
        state = mount_public_tests(task.tests_dir, loop.sandbox().executor) if public else hidden_state()
        if state["test_ids"]: await run.emit("test.exposed", {"test_ids": state["test_ids"], "mount_mode": state["mount_mode"], "read_only_enforced": state["read_only_enforced"]})
        await run.emit("test_visibility.verified", {"declaration": decl, "state": {k: v for k, v in state.items() if k != "test_ids"}, "task_id": task.id})

    async def attach(self, run_id: str, verifier=None, hooks: dict | None = None, agent: dict | None = None) -> LoopController:
        """Rebuild the in-memory controller for an existing run (after a restart)."""
        run = self.rm.get(run_id)
        created = self.store.events(run_id, types=["run.created"])[0]["payload"]
        loop = LoopController(run, self.rm, self.gateway, self.runtime, self.registry, agent or created["agent"], self.models, self.config,
                              verifier, hooks, self.config_dir, self.memory_guard)
        self.loops[run_id] = loop
        return loop

    async def resume_run(self, run_id: str, verifier=None, hooks: dict | None = None, downtime_cause: str = "harness_failure", task=None):
        from .recovery import RecoveryManager
        loop = await self.attach(run_id, verifier, hooks)
        if task is not None:
            from .eval.runner import attach_eval
            attach_eval(self, loop, task)
        plan = await RecoveryManager(self).recover(run_id, downtime_cause)
        final = await loop.resume(plan)
        return plan, final

    def close(self) -> None:
        self.writer.close()
