"""The plugin interface (design §18.11). The harness core ships no capability that needs a model, a corpus, a browser, a container host or special hardware. Each such capability is a plugin that implements a contract
the harness defines (`spec/plugins/kinds.json`), is verified when it is loaded, and is called only through a handle that validates, times, budgets, audits and fails closed. A plugin is also a component of the component
runtime, so features that need one activate when it is loaded and deactivate when it is removed."""
from __future__ import annotations
import asyncio, importlib, inspect, json, time
from dataclasses import dataclass
from pathlib import Path
from typing import Any
import jsonschema
from .canonical import sha256_hex, canonicalize
from .components.core import Component, Provide, World
from .plugin_api import ERROR_CODES, INTERFACES, Plugin, PluginRejection, RETRYABLE, to_plain
from .tools.registry import load_schema

SPEC = Path(__file__).resolve().parents[4] / "spec" / "plugins" / "kinds.json"
KINDS: dict = json.loads(SPEC.read_text())["kinds"]
RANK = {"core": 0, "standard": 1, "high": 2}
TRUST = {"untrusted_output": "untrusted", "trusted_output": "tool_trusted"}


class PluginError(Exception):
    code = "plugin.error"


class ManifestInvalid(PluginError): code = "plugin.manifest_invalid"
class PolicyDenied(PluginError): code = "plugin.policy_denied"
class ConformanceFailed(PluginError): code = "plugin.conformance_failed"
class PluginUnavailable(PluginError): code = "plugin.unavailable"
class ContractViolation(PluginError): code = "plugin.contract_violation"
class PluginTimeout(PluginError): code = "plugin.timeout"
class PluginFailed(PluginError): code = "plugin.failed"
class PluginBudgetExhausted(PluginError): code = "plugin.budget_exhausted"


class PluginRejected(PluginError):
    """The plugin refused a call with one of the failures its contract declares for the method. `reason` is the contract's error code and `retryable` says whether the same call may succeed later (unavailable, rate_limited)."""
    code = "plugin.rejected"
    def __init__(self, reason: str, message: str = ""):
        super().__init__(f"{reason}: {message}" if message and message != reason else reason); self.reason, self.retryable = reason, reason in RETRYABLE


def _errors(schema: dict, value: Any) -> list[str]:
    return [f"{'/'.join(map(str, e.path)) or '(root)'}: {e.message}" for e in sorted(jsonschema.Draft202012Validator(schema).iter_errors(value), key=lambda e: list(e.path))]


def validate_manifest(m: dict, profile: str = "core", trusted_in_process: set | frozenset = frozenset()) -> list[str]:
    """Everything that can be decided from the manifest alone: its shape, a known kind, a compatible interface, grants within what the kind allows, and the profile's trust policy."""
    errs = _errors(load_schema("plugin.manifest.v1.json"), m)
    if errs: return errs
    k = KINDS.get(m["kind"])
    if k is None: return [f"unknown plugin kind {m['kind']!r} (known: {', '.join(sorted(KINDS))})"]
    if m["interface_version"].split(".")[0] != k["interface_version"].split(".")[0]: errs.append(f"interface {m['interface_version']} is not compatible with {m['kind']} interface {k['interface_version']}")
    extra = set(m.get("grants", [])) - set(k["grants"])
    if extra: errs.append(f"grants {sorted(extra)} are not allowed for a {m['kind']} plugin (allowed: {k['grants']})")
    ev = m.get("evaluator")
    if m["kind"] == "evaluator":                                   # an evaluator declares its type, and a model-based one declares the model and credential it uses
        if ev is None: errs.append("an evaluator plugin must declare evaluator.type (code, llm_judge, or typesafe_ai)")
        elif ev["type"] == "code" and (ev.get("model_ref") or ev.get("credential_ref")): errs.append("a code evaluator uses no model, so it must not declare model_ref or credential_ref")
        elif ev["type"] != "code" and not (ev.get("model_ref") and ev.get("credential_ref")): errs.append(f"a {ev['type']} evaluator must declare the model_ref and credential_ref it uses, so that its independence can be checked")
    elif ev is not None: errs.append("only an evaluator plugin may declare an evaluator block")
    e = m["entry"]
    if m["trust"] == "in_process" and not (e.get("class") or (e.get("module") and e.get("factory"))): errs.append("an in_process plugin needs entry.module and entry.factory, or entry.class (module:ClassName)")
    if m["trust"] == "subprocess" and not e.get("command"): errs.append("a subprocess plugin needs entry.command")
    return errs


def policy_errors(m: dict, profile: str, trusted_in_process: set | frozenset) -> list[str]:
    k = KINDS[m["kind"]]; out = []
    if RANK[profile] < RANK[k["min_profile"]]: out.append(f"a {m['kind']} plugin needs a {k['min_profile']} deployment or stricter; this one is {profile}")
    if m["trust"] == "in_process" and RANK[profile] >= RANK["standard"] and m["id"] not in trusted_in_process: out.append(f"{m['id']}: standard and high deployments run plugins out of process unless the operator lists them as trusted")
    return out


def independence_level(manifest: dict, agent: dict) -> dict:
    """The independence of an evaluator from the agent under test, derived and never declared (design §13.4). A `code` evaluator is deterministic. A model-based one is independent only if its model, its context and its
    credential all differ from the agent's; the dimensions it shares are recorded. agent: {model_ref, credential_ref, context}."""
    ev = manifest["evaluator"]
    if ev["type"] == "code": return {"level": "deterministic", "type": "code", "shared": []}
    shared = [d for d, same in (("model", ev.get("model_ref") == agent.get("model_ref")), ("credential", ev.get("credential_ref") == agent.get("credential_ref")), ("context", ev.get("context", "shared") == "shared")) if same]
    return {"level": "independent_model" if not shared else "same_model_low_independence", "type": ev["type"], "shared": shared}


def may_gate(level: str, profile: str, material_claim: bool = False, backed: bool = False) -> tuple[bool, str]:
    """Whether an evaluation at this independence level may gate a promotion, a release or a claim. Core labels and allows; Standard needs a deterministic or independent evaluator; High also needs a claim that rests
    on an independent model to be backed by a deterministic check or a second independent evaluator."""
    if profile == "core": return True, "core labels the level and does not restrict"
    if level == "same_model_low_independence": return False, "an evaluator that shares the agent's model, context or credential is advisory in standard and high deployments"
    if profile == "high" and material_claim and level == "independent_model" and not backed: return False, "a material claim resting on an independent model needs a deterministic check or a second independent evaluator behind it"
    return True, "independent enough to gate"


def exfiltration_claim(observed: dict | None) -> dict:
    """What the harness may claim about exfiltration monitoring (design §10.6). The core monitors network activity and claims partial conformance. It claims payload inspection only when the isolation plugin's verified settings
    contain an `egress_inspection` record that names a verified inspection point, says the policy is applied and the outcomes are recorded for audit, and documents its coverage gaps and bypass conditions. Supporting TLS
    interception is not enough, and the harness never claims complete detection."""
    eg = (observed or {}).get("egress_inspection")
    if not isinstance(eg, dict): return {"claim": "partial", "reasons": ["the isolation plugin reported no payload inspection point"], "coverage_gaps": [], "bypass_conditions": []}
    why = []
    if not (isinstance(eg.get("inspection_point"), str) and eg["inspection_point"].strip()): why.append("no inspection point is named")
    if eg.get("policy_applied") is not True: why.append("the exfiltration policy is not applied")
    if eg.get("audit_records") is not True: why.append("inspection outcomes are not recorded for audit")
    if not isinstance(eg.get("coverage_gaps"), list): why.append("coverage gaps are not documented")
    if not isinstance(eg.get("bypass_conditions"), list): why.append("bypass conditions are not documented")
    if eg.get("verified") is not True: why.append("the inspection point has not been verified to be effective")
    return {"claim": "partial" if why else "payload_inspection", "reasons": why, "coverage_gaps": eg.get("coverage_gaps") if isinstance(eg.get("coverage_gaps"), list) else [], "bypass_conditions": eg.get("bypass_conditions") if isinstance(eg.get("bypass_conditions"), list) else []}


def manifest_hash(m: dict) -> str:
    return sha256_hex(canonicalize(m))


def file_sha256(path: str | Path) -> str:
    return sha256_hex(Path(path).read_bytes())


@dataclass
class PluginResult:
    value: Any
    trust: str            # `untrusted` for what a model or an index produced, `tool_trusted` for what the operator's own infrastructure reported
    plugin_id: str
    method: str


class InProcessPlugin:
    """Adapts an object whose methods are named like the contract's methods (sync or async) to the one `rpc` shape the host uses."""
    def __init__(self, obj): self.obj = obj
    async def rpc(self, method: str, params: dict, timeout_s: float = 30):
        fn = getattr(self.obj, method, None)
        if fn is None: raise PluginFailed(f"the plugin has no method {method}")
        r = fn(**params)
        return to_plain(await r if inspect.isawaitable(r) else r)             # a typed result class becomes the plain JSON the contract validates
    async def close(self): pass


class SubprocessPlugin:
    """A plugin in its own process, speaking newline-delimited JSON over stdio: {"id", "method", "params"} out, {"id", "result"} or {"id", "error": {"code", "message"}} back. A call that times out kills the
    process (fail closed), and the next call starts a fresh one."""
    def __init__(self, command: list[str], env: dict | None = None, cwd: str | None = None):
        self.command, self.env, self.cwd, self.proc, self._n = command, env, cwd, None, 0

    async def _start(self):
        if self.proc is None or self.proc.returncode is not None:
            self.proc = await asyncio.create_subprocess_exec(*self.command, stdin=asyncio.subprocess.PIPE, stdout=asyncio.subprocess.PIPE, stderr=asyncio.subprocess.DEVNULL, env=self.env, cwd=self.cwd)

    async def rpc(self, method: str, params: dict, timeout_s: float = 30):
        await self._start(); self._n += 1; rid = self._n
        async def go():
            self.proc.stdin.write((json.dumps({"id": rid, "method": method, "params": params}) + "\n").encode()); await self.proc.stdin.drain()
            line = await self.proc.stdout.readline()
            if not line: raise PluginFailed("the plugin process closed its output")
            return json.loads(line)
        try: resp = await asyncio.wait_for(go(), timeout_s)
        except asyncio.TimeoutError:
            await self.close(); raise
        if resp.get("id") != rid: raise PluginFailed("the plugin answered a different request")
        if "error" in resp:
            if resp["error"].get("code") in ERROR_CODES: raise PluginRejection(resp["error"]["code"], resp["error"].get("message", ""))             # a contract error crosses the process boundary as its code
            raise PluginFailed(f"{resp['error'].get('code', 'error')}: {resp['error'].get('message', '')}")
        return resp.get("result")

    async def close(self):
        if self.proc and self.proc.returncode is None:
            try: self.proc.kill()
            except ProcessLookupError: pass
            await self.proc.wait()
        self.proc = None


class PluginHandle:
    """The only way the harness calls a plugin. Input and output are validated against the kind's contract, every call has a timeout, and repeated failures quarantine the plugin so that it stops being called."""
    def __init__(self, host: "PluginHost", manifest: dict, impl, fiber: str, code_sha256: str | None):
        self.host, self.manifest, self.impl, self.fiber, self.code_sha256 = host, manifest, impl, fiber, code_sha256
        self.id, self.kind, self.state, self.failures = manifest["id"], manifest["kind"], "active", 0

    async def call(self, method: str, args: dict | None = None, run=None, budget: bool = True) -> PluginResult:
        spec = KINDS[self.kind]["methods"].get(method)
        if spec is None: raise ContractViolation(f"{self.kind} has no method {method!r}")
        args = args or {}
        bad = _errors(spec["input"], args)
        if bad: raise ContractViolation(f"{self.id}.{method} input: {bad[0]}")
        if self.state != "active" or self.host.world.fibers.get(self.fiber) is None or self.host.world.fibers[self.fiber].state != "ACTIVE": raise PluginUnavailable(f"plugin {self.id} is {self.state}")
        rid = None
        if run is not None and budget:
            from .budgets import BudgetController
            bc = BudgetController(run); rid, ex = await bc.reserve({"tools": 1})
            if ex: raise PluginBudgetExhausted(f"the {ex} budget cannot cover a plugin call")
        t0 = time.monotonic(); code = None
        try:
            try: out = await asyncio.wait_for(self.impl.rpc(method, args, spec["timeout_s"]), spec["timeout_s"])
            except asyncio.TimeoutError: code = "timeout"; raise PluginTimeout(f"{self.id}.{method} exceeded {spec['timeout_s']}s") from None
            except PluginRejection as r:                                # a refusal the contract may declare; anything undeclared is a contract violation
                if r.code not in {e["code"] for e in spec.get("errors", [])}: code = "contract_violation"; raise ContractViolation(f"{self.id}.{method} signalled {r.code!r}, which the contract does not declare for this method") from None
                code = f"rejected.{r.code}"; raise PluginRejected(r.code, r.message) from None
            except PluginError as e: code = e.code; raise
            except Exception as e: code = "failed"; raise PluginFailed(f"{self.id}.{method}: {type(e).__name__}") from e
            bad = _errors(spec["output"], out)
            if bad: code = "contract_violation"; raise ContractViolation(f"{self.id}.{method} output: {bad[0]}")
            self.failures = 0
            if run is not None:
                await run.emit("plugin.called", {"plugin_id": self.id, "kind": self.kind, "method": method, "ok": True, "duration_ms": round((time.monotonic() - t0) * 1000), "trust": TRUST[KINDS[self.kind]["trust"]]})
                if rid: await bc.commit(rid, {"tools": 1})
            return PluginResult(out, TRUST[KINDS[self.kind]["trust"]], self.id, method)
        except PluginRejected as e:                                                # a correct refusal is not a plugin fault, except that a backend that is down is
            counts = e.reason == "unavailable"; self.failures += 1 if counts else 0
            if run is not None:
                await run.emit("plugin.called", {"plugin_id": self.id, "kind": self.kind, "method": method, "ok": False, "error": e.reason, "retryable": e.retryable, "duration_ms": round((time.monotonic() - t0) * 1000), "trust": TRUST[KINDS[self.kind]["trust"]]})
                if rid: await bc.commit(rid, {"tools": 1})
                if counts and self.failures >= self.host.max_failures and self.state == "active": await run.emit("plugin.quarantined", {"plugin_id": self.id, "kind": self.kind, "after_failures": self.failures})
            if counts and self.failures >= self.host.max_failures: self.state = "quarantined"
            raise
        except PluginError:
            self.failures += 1
            if run is not None:
                await run.emit("plugin.failed", {"plugin_id": self.id, "kind": self.kind, "method": method, "error": code, "consecutive_failures": self.failures})
                if rid: await bc.commit(rid, {"tools": 1})                                   # a failed call still cost a call
                if self.failures >= self.host.max_failures and self.state == "active": await run.emit("plugin.quarantined", {"plugin_id": self.id, "kind": self.kind, "after_failures": self.failures})
            if self.failures >= self.host.max_failures: self.state = "quarantined"
            raise


class PluginHost:
    def __init__(self, profile: str = "core", trusted_in_process: set | None = None, max_failures: int = 3, strict: bool = False, verify_errors: bool = False):
        """strict: an in-process plugin must be an instance of its kind's interface class (`harness.plugin_api`), like a Java class that implements an interface. Otherwise any object with the right methods is accepted."""
        if profile not in RANK: raise ValueError(f"unknown profile {profile!r}")
        self.profile, self.trusted, self.max_failures, self.strict, self.verify_errors = profile, frozenset(trusted_in_process or ()), max_failures, strict, verify_errors
        self.world, self.handles, self.history = World(), {}, []

    # ----------------------------------------------------------------------------------------------- loading
    async def load(self, manifest: dict, impl=None, code_path: str | Path | None = None) -> PluginHandle:
        """Verify a plugin and make it available. Nothing is partly loaded: if any check fails the plugin is not registered."""
        errs = validate_manifest(manifest, self.profile, self.trusted)
        if errs: self._note("plugin.rejected", manifest, errs); raise ManifestInvalid("; ".join(errs))
        errs = policy_errors(manifest, self.profile, self.trusted)
        if errs: self._note("plugin.rejected", manifest, errs); raise PolicyDenied("; ".join(errs))
        if manifest["id"] in self.handles: raise ManifestInvalid(f"plugin {manifest['id']} is already loaded")
        if any(h.kind == manifest["kind"] and h.state != "unloaded" for h in self.handles.values()): raise ManifestInvalid(f"a {manifest['kind']} plugin is already loaded; unload it first")
        code = file_sha256(code_path) if code_path else manifest["entry"].get("code_sha256")
        if code_path and manifest["entry"].get("code_sha256") and manifest["entry"]["code_sha256"] != code: self._note("plugin.rejected", manifest, ["code hash mismatch"]); raise ManifestInvalid("the plugin's code does not match the hash in its manifest")
        if impl is None: impl = self._build(manifest)
        elif not hasattr(impl, "rpc"): impl = InProcessPlugin(self._implements(manifest, impl))
        problems = await self._conformance(manifest, impl)
        if problems:
            await impl.close(); self._note("plugin.rejected", manifest, problems); raise ConformanceFailed("; ".join(problems))
        comp = Component(f"plugin.{manifest['id']}", lambda c, k=f"plugin.{manifest['kind']}": [Provide(k, manifest["id"])], provides={f"plugin.{manifest['kind']}": manifest["version"]}, version=manifest["version"])
        fiber = self.world.insert(comp); self.world.settle()
        h = PluginHandle(self, manifest, impl, fiber, code); self.handles[manifest["id"]] = h; self._note("plugin.loaded", manifest, []); return h

    def _build(self, m: dict):
        e = m["entry"]
        if m["trust"] == "subprocess": return SubprocessPlugin(e["command"])
        if e.get("class"):                                          # an implementation class named in the deployment configuration, like a Java class named in a config file
            modname, _, clsname = e["class"].partition(":"); want = INTERFACES[m["kind"]]
            try: cls = getattr(importlib.import_module(modname), clsname)
            except (ImportError, AttributeError) as x: self._note("plugin.rejected", m, [f"cannot load {e['class']}"]); raise ManifestInvalid(f"cannot load {e['class']}: {x}") from x
            if not (isinstance(cls, type) and issubclass(cls, want)): self._note("plugin.rejected", m, ["does not implement the interface"]); raise ManifestInvalid(f"{e['class']} must be a class that implements {want.__name__} (the {m['kind']} interface)")
            try: obj = cls(**e.get("config", {}))
            except TypeError as x: self._note("plugin.rejected", m, [str(x)]); raise ManifestInvalid(f"{e['class']} cannot be created with its configuration: {x}") from x
            return InProcessPlugin(obj)
        obj = getattr(importlib.import_module(e["module"]), e["factory"])(m)
        return InProcessPlugin(self._implements(m, obj))

    def _implements(self, m: dict, obj):
        """An object that is an instance of another kind's interface is refused, and in strict mode an object that is not an instance of this kind's interface is refused."""
        want = INTERFACES[m["kind"]]
        for kind, iface in INTERFACES.items():
            if kind != m["kind"] and isinstance(obj, iface) and not isinstance(obj, want): self._note("plugin.rejected", m, [f"implements {kind}"]); raise ManifestInvalid(f"{m['id']} implements the {kind} interface, and its manifest says {m['kind']}")
        if self.strict and not isinstance(obj, want): self._note("plugin.rejected", m, ["does not implement the interface"]); raise ManifestInvalid(f"{m['id']} must be an instance of {want.__module__}.{want.__name__} (a class that implements the {m['kind']} interface)")
        return obj

    async def _conformance(self, m: dict, impl) -> list[str]:
        """Call every method of the contract with its sample input and check what comes back. `describe` must say what the manifest says, and `health` must be ok."""
        out, made = [], {}                                                   # ids the plugin created, which the calls that follow must use
        for name, spec in KINDS[m["kind"]]["methods"].items():
            args = {**spec["sample"], **{k: v for k, v in made.items() if k in spec["input"]["properties"]}}
            try: r = await asyncio.wait_for(impl.rpc(name, args, spec["timeout_s"]), spec["timeout_s"])
            except asyncio.TimeoutError: out.append(f"{name}: timed out"); continue
            except Exception as e: out.append(f"{name}: {type(e).__name__}: {e}"); continue
            bad = _errors(spec["output"], r)
            if bad: out.append(f"{name}: output {bad[0]}"); continue
            for creator, key in (("open", "session"), ("fork", "fork_id"), ("provision", "sandbox_id")):
                if name == creator and isinstance(r, dict) and key in r: made[key] = r[key]
            if name == "describe" and (r["id"], r["kind"], r["version"]) != (m["id"], m["kind"], m["version"]): out.append(f"describe says {r['id']}/{r['kind']}/{r['version']}, the manifest says {m['id']}/{m['kind']}/{m['version']}")
            if name == "health" and not r["ok"]: out.append("health is not ok")
        if self.verify_errors: out += await self._probes(m, impl)
        return out

    async def _probes(self, m: dict, impl) -> list[str]:
        """Run each declared error's probe, a safe input that must raise exactly that error (design §18.11). A plugin that does not signal what its contract says it signals is not loaded."""
        out = []
        for name, spec in KINDS[m["kind"]]["methods"].items():
            for e in spec.get("errors", []):
                if e.get("probe") is None: continue
                try: await asyncio.wait_for(impl.rpc(name, e["probe"], spec["timeout_s"]), spec["timeout_s"]); out.append(f"{name}: the {e['code']} probe did not raise")
                except PluginRejection as r:
                    if r.code != e["code"]: out.append(f"{name}: the {e['code']} probe raised {r.code}")
                except asyncio.TimeoutError: out.append(f"{name}: the {e['code']} probe timed out")
                except Exception as x: out.append(f"{name}: the {e['code']} probe raised {type(x).__name__}, not a contract error")
        return out

    async def unload(self, plugin_id: str) -> None:
        """Remove a plugin. What depended on it deactivates through the component runtime, and a subprocess is killed."""
        h = self.handles.get(plugin_id)
        if h is None: raise PluginUnavailable(f"plugin {plugin_id} is not loaded")
        h.state = "unloaded"; self.world.retire(h.fiber); self.world.settle(); self.world.sweep(); await h.impl.close(); del self.handles[plugin_id]; self._note("plugin.unloaded", h.manifest, [])

    async def replace(self, manifest: dict, impl=None, code_path=None) -> PluginHandle:
        old = next((h for h in self.handles.values() if h.kind == manifest["kind"]), None)
        if old: await self.unload(old.id)
        return await self.load(manifest, impl, code_path)

    async def close(self) -> None:
        for pid in list(self.handles): await self.unload(pid)

    # ----------------------------------------------------------------------------------------------- use
    def has(self, kind: str) -> bool:
        return any(h.kind == kind and h.state == "active" for h in self.handles.values())

    def get(self, kind: str) -> PluginHandle:
        """The active plugin of a kind. A feature that needs a plugin that is not there does not run (fail closed)."""
        h = next((h for h in self.handles.values() if h.kind == kind and h.state == "active"), None)
        if h is None: raise PluginUnavailable(f"no active {kind} plugin is loaded")
        return h

    def definition(self, plugin_id: str) -> dict:
        h = self.handles.get(plugin_id)
        if h is None: raise PluginUnavailable(f"plugin {plugin_id} is not loaded")
        return {"manifest": h.manifest, "code_sha256": h.code_sha256}

    def definitions(self) -> dict[str, dict]:
        """What a run pins: the manifest and code hash of every loaded plugin, so a plugin swapped during a run is a definition mismatch."""
        return {f"plugin.{pid}": self.definition(pid) for pid in sorted(self.handles)}

    def requirements_met(self, kinds: list[str]) -> list[str]:
        return [f"no active {k} plugin is loaded" for k in kinds if not self.has(k)]

    def _note(self, kind: str, m: dict, problems: list[str]) -> None:
        self.history.append({"event": kind, "plugin_id": m.get("id"), "kind": m.get("kind"), "version": m.get("version"), "problems": problems})
