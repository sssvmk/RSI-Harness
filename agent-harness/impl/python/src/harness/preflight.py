"""Preflight validator (§16.3): PF-01..PF-16. A failed `error` check blocks the run for its profile."""
from __future__ import annotations
import hashlib, sys
from pathlib import Path
import jsonschema
from .budgets import REQUIRED
from .policy import decide, validate_ruleset
from .sandbox.backend import PROFILE_TIER, TIER_RANK
from .tools.registry import load_schema


def run_preflight(profile: str | None, config: dict, *, store=None, writer=None, backend=None, registry=None, ruleset=None,
                  known_secrets: list[str] | None = None, sandbox_envs: list[dict] | None = None, executor_path: str | None = None,
                  work_roots: list[str] | None = None, hidden_tests_dirs: list[str] | None = None, plugin_problems: list[str] | None = None) -> dict:
    checks: list[dict] = []
    P = profile if profile in PROFILE_TIER else "core"
    strict = P in ("standard", "high")

    def add(id_, ok, msg, strict_only=False):
        checks.append({"id": id_, "ok": bool(ok), "severity": "error" if (strict or not strict_only) else "warn", "message": msg})

    add("PF-01", profile in PROFILE_TIER, f"profile {profile!r} recognized" if profile in PROFILE_TIER else f"unknown profile {profile!r}")
    if backend is not None:
        need, have = PROFILE_TIER[P], backend.tier
        dev_ok = have == "T-Dev" and P == "core" and config.get("sandbox", {}).get("allow_dev_tier", False)
        ok = dev_ok or TIER_RANK[have] >= TIER_RANK[need]
        if config.get("sandbox", {}).get("runtime") == "gvisor" and sys.platform != "linux":
            ok = False
        add("PF-02", ok, f"sandbox tier {have} vs required {need}" + (" (INSECURE dev tier explicitly allowed)" if dev_ok else ""))
    eg = config.get("egress", {})
    grants_ok = all(isinstance(g.get("host_glob"), str) and isinstance(g.get("ports"), list) and all(isinstance(p, int) for p in g["ports"]) for g in eg.get("grants", []))
    add("PF-03", eg.get("default", "deny") == "deny" and grants_ok, "egress default deny; grants well-formed")
    missing = [d for d in REQUIRED[P] if (d == "time_ms" and "time_s" not in config.get("budgets", {})) or (d != "time_ms" and d not in config.get("budgets", {}))]
    add("PF-04", not missing, f"required budgets for {P}: " + (f"missing {missing}" if missing else "present"))
    leaks = [i for i, env in enumerate(sandbox_envs or []) for v in env.values() for s in (known_secrets or []) if s and s in str(v)]
    add("PF-05", not leaks, "no known secret values in sandbox environments" if not leaks else f"secret value found in sandbox env #{leaks[0]}")
    want = config.get("sandbox", {})
    if executor_path and want.get("executor_sha256"):
        add("PF-06", hashlib.sha256(Path(executor_path).read_bytes()).hexdigest() == want["executor_sha256"], "executor hash matches config")
    elif backend is not None and getattr(backend, "has_native_binaries", True):
        add("PF-06", bool(want.get("executor_sha256") and want.get("egress_sha256")), "executor/egress binary hashes pinned in config")
    else:
        add("PF-06", True, "in-process executor: no binary to pin")
    if ruleset is not None:
        try:
            jsonschema.validate(ruleset, load_schema("policy.rules.v1.json"))
            errs = validate_ruleset(ruleset)
            for ec in ("read_only", "reversible_write", "irreversible", "external_facing", None):
                decide({"rules": []}, {"tool": "x", "effect_class": ec, "profile": P, "args": {}})
            add("PF-07", not errs, "policy rule set valid; all effect classes have defaults" if not errs else "; ".join(errs))
        except jsonschema.ValidationError as e:
            add("PF-07", False, f"rule set invalid: {e.message}")
    if registry is not None:
        add("PF-08", not registry.warnings, "all tools declare idempotency and effect_class" if not registry.warnings else f"{len(registry.warnings)} tool manifests rely on defaults", strict_only=True)
    servers = (config.get("mcp") or {}).get("servers", [])
    add("PF-09", all(s.get("tools") and all(len(h) == 64 for h in s["tools"].values()) for s in servers), "MCP registry entries have pinned hashes")
    ap = config.get("approvals", {})
    add("PF-10", bool(ap.get("channels")) and bool(ap.get("approvers")), "approval channel configured and approvers exist", strict_only=True)
    if store is not None:
        try:
            ok = store.pragma("journal_mode") == "wal" and store.pragma("synchronous") == 2
            if writer is not None:
                writer.execute_sync("SELECT 1")
        except Exception:
            ok = False
        add("PF-11", ok, "event log is WAL + synchronous=FULL and writable")
    ind = (config.get("evaluator") or {}).get("independence")
    add("PF-12", ind in (("deterministic", "independent_model") if strict else ("deterministic", "independent_model", "same_model_low_independence")),
        f"evaluator independence {ind!r}", strict_only=True)
    from pathlib import PurePath
    exposed = [d for d in (hidden_tests_dirs or []) for r in (work_roots or []) if str(PurePath(d)).startswith(str(PurePath(r)))]
    add("PF-13", not exposed, "hidden tests are not mounted in any work sandbox" if not exposed else f"hidden tests reachable via {exposed[0]}")
    add("PF-14", (config.get("cache") or {}).get("semantic", "off") in ("off", "scoped") if strict else True, "semantic cache disabled or scoped")
    r = config.get("retention") or {}
    add("PF-15", "events_days" in r and "blobs_days" in r, "retention policy configured", strict_only=True)
    oidc = (config.get("auth") or {}).get("oidc") or {}
    add("PF-16", bool(oidc.get("issuer") and oidc.get("audience")), "OIDC issuer and audience configured", strict_only=True)
    from .budgets import safeguard_problems, unmetered as _unmetered
    um = _unmetered(config.get("budgets"))
    bc_ = config.get("budgets") or {}
    probs = safeguard_problems(bc_, bc_.get("workers", 4), bc_.get("queue", 100)) if um else []
    pp = plugin_problems or []
    add("PF-P01", not pp, "every required plugin kind is loaded and active" if not pp else "a required plugin is missing: " + "; ".join(pp))
    add("PF-B01", not probs, ("compute is unmetered; independent safeguards are in place (turn and time limits, bounded workers and queue)" if not probs else "compute is unmetered and its safeguards are incomplete: " + "; ".join(probs)))
    # ---- additions from the owner's decisions (README §11): user memory, memory write policy, configuration in git
    mem = config.get("memory") or {}
    um = (mem.get("user_memory") or {}).get("enabled")
    add("PF-U01", not um, "user memory is off (an agent deployment decision)" if not um else "user memory is enabled, but the governed user-memory store (PRD §3.1) is not implemented: turn it off or build it")
    try:
        from .tools.memory_guard import resolve_write_policy
        resolve_write_policy(mem.get("write_path")); add("PF-U02", True, "memory write-path policy valid")
    except ValueError as e:
        add("PF-U02", False, str(e))
    repo = config.get("config_repo")
    if repo:
        from .config_repo import ConfigRepoError, resolve as _resolve
        try:
            r = _resolve(repo)
            add("PF-G01", not (r["dirty"] and strict), f"configuration pinned to git commit {r['commit'][:12]}" + (" with UNCOMMITTED changes (marked +dirty; commit before a Standard or High run)" if r["dirty"] else ""))
        except ConfigRepoError as e:
            add("PF-G01", False, f"config_repo: {e}")
    else:
        checks.append({"id": "PF-G01", "ok": False, "severity": "warn", "message": "configuration is not versioned in git (set config_repo); runs are pinned to a content hash only"})
    failed = [c for c in checks if not c["ok"] and c["severity"] == "error"]
    return {"ok": not failed, "profile": profile, "checks": checks, "failed": failed}
