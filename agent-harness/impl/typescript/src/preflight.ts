/** Preflight validator (§16.3): PF-01..PF-16. A failed `error` check blocks the run for its profile. */
import { safeguardProblems, unmetered } from "./budgets.js";
import { resolveWritePolicy } from "./memoryPolicy.js";
import { resolve as resolveRepo } from "./configRepo.js";
import fs from "node:fs";
import path from "node:path";
import { createHash } from "node:crypto";
import { REQUIRED } from "./budgets.js";
import { decide, validateRuleset } from "./policy.js";
import { PROFILE_TIER, TIER_RANK } from "./sandbox.js";
import { loadSchema, newAjv } from "./registry.js";
export function runPreflight(profile: string | null, config: any, o: any = {}) {
  const checks: any[] = [], valid = profile !== null && profile in PROFILE_TIER, P = valid ? profile! : "core", strict = P === "standard" || P === "high";
  const add = (id: string, ok: boolean, message: string, strictOnly = false) => checks.push({ id, ok: !!ok, severity: strict || !strictOnly ? "error" : "warn", message });
  add("PF-01", valid, valid ? `profile '${profile}' recognized` : `unknown profile '${profile}'`);
  if (o.backend) {
    const need = PROFILE_TIER[P], have = o.backend.tier, devOk = have === "T-Dev" && P === "core" && !!config.sandbox?.allow_dev_tier;
    let ok = devOk || TIER_RANK[have] >= TIER_RANK[need];
    if (config.sandbox?.runtime === "gvisor" && process.platform !== "linux") ok = false;
    add("PF-02", ok, `sandbox tier ${have} vs required ${need}` + (devOk ? " (INSECURE dev tier explicitly allowed)" : ""));
  }
  const eg = config.egress ?? {};
  add("PF-03", (eg.default ?? "deny") === "deny" && (eg.grants ?? []).every((g: any) => typeof g.host_glob === "string" && Array.isArray(g.ports) && g.ports.every((p: any) => Number.isInteger(p))), "egress default deny; grants well-formed");
  const b = config.budgets ?? {}, missing = REQUIRED[P].filter((d) => (d === "time_ms" ? !("time_s" in b) : !(d in b)));
  add("PF-04", !missing.length, `required budgets for ${P}: ` + (missing.length ? `missing ${JSON.stringify(missing)}` : "present"));
  const leaks: number[] = []; (o.sandboxEnvs ?? []).forEach((env: any, i: number) => { for (const v of Object.values(env)) for (const s of o.knownSecrets ?? []) if (s && String(v).includes(s)) leaks.push(i); });
  add("PF-05", !leaks.length, leaks.length ? `secret value found in sandbox env #${leaks[0]}` : "no known secret values in sandbox environments");
  const want = config.sandbox ?? {};
  if (o.executorPath && want.executor_sha256) add("PF-06", createHash("sha256").update(fs.readFileSync(o.executorPath)).digest("hex") === want.executor_sha256, "executor hash matches config");
  else if (o.backend && (o.backend.hasNativeBinaries ?? true)) add("PF-06", !!(want.executor_sha256 && want.egress_sha256), "executor/egress binary hashes pinned in config");
  else add("PF-06", true, "in-process executor: no binary to pin");
  if (o.ruleset) {
    try {
      const ajv = newAjv(), v = ajv.compile(loadSchema("policy.rules.v1.json"));
      if (!v(o.ruleset)) add("PF-07", false, `rule set invalid: ${ajv.errors?.[0]?.message}`);
      else { const errs = validateRuleset(o.ruleset); for (const ec of ["read_only", "reversible_write", "irreversible", "external_facing", null]) decide({ rules: [] }, { tool: "x", effect_class: ec, profile: P, args: {} }); add("PF-07", !errs.length, errs.length ? errs.join("; ") : "policy rule set valid; all effect classes have defaults"); }
    } catch (e: any) { add("PF-07", false, `rule set invalid: ${e.message}`); }
  }
  if (o.registry) add("PF-08", !o.registry.warnings.length, o.registry.warnings.length ? `${o.registry.warnings.length} tool manifests rely on defaults` : "all tools declare idempotency and effect_class", true);
  const servers = config.mcp?.servers ?? [];
  add("PF-09", servers.every((s: any) => s.tools && Object.values<string>(s.tools).every((h) => h.length === 64)), "MCP registry entries have pinned hashes");
  const ap = config.approvals ?? {};
  add("PF-10", !!ap.channels?.length && !!ap.approvers?.length, "approval channel configured and approvers exist", true);
  if (o.store) { let ok = false; try { ok = o.store.pragma("journal_mode") === "wal" && o.store.pragma("synchronous") === 2; } catch { ok = false; } add("PF-11", ok, "event log is WAL + synchronous=FULL and writable"); }
  const ind = config.evaluator?.independence, okInd = strict ? ["deterministic", "independent_model"].includes(ind) : ["deterministic", "independent_model", "same_model_low_independence"].includes(ind);
  add("PF-12", okInd, `evaluator independence '${ind}'`, true);
  const exposed = (o.hiddenTestsDirs ?? []).flatMap((d: string) => (o.workRoots ?? []).filter((r: string) => path.resolve(d).startsWith(path.resolve(r))));
  add("PF-13", !exposed.length, exposed.length ? `hidden tests reachable via ${exposed[0]}` : "hidden tests are not mounted in any work sandbox");
  add("PF-14", strict ? ["off", "scoped"].includes(config.cache?.semantic ?? "off") : true, "semantic cache disabled or scoped");
  add("PF-15", "events_days" in (config.retention ?? {}) && "blobs_days" in (config.retention ?? {}), "retention policy configured", true);
  add("PF-16", !!(config.auth?.oidc?.issuer && config.auth?.oidc?.audience), "OIDC issuer and audience configured", true);
  // ---- additions from the owner's decisions (README §11): user memory, memory write policy, configuration in git
  const mem = config.memory ?? {}, um = mem.user_memory?.enabled;
  const bcfg = config.budgets ?? {}, probs = unmetered(bcfg).length ? safeguardProblems(bcfg, bcfg.workers ?? 4, bcfg.queue ?? 100) : [];
  const pp: string[] = o.plugin_problems ?? [];
  add("PF-P01", pp.length === 0, pp.length === 0 ? "every required plugin kind is loaded and active" : "a required plugin is missing: " + pp.join("; "));
  add("PF-B01", probs.length === 0, probs.length === 0 ? "compute is unmetered; independent safeguards are in place (turn and time limits, bounded workers and queue)" : "compute is unmetered and its safeguards are incomplete: " + probs.join("; "));
  add("PF-U01", !um, !um ? "user memory is off (an agent deployment decision)" : "user memory is enabled, but the governed user-memory store (PRD §3.1) is not implemented: turn it off or build it");
  try { resolveWritePolicy(mem.write_path); add("PF-U02", true, "memory write-path policy valid"); } catch (e: any) { add("PF-U02", false, String(e.message)); }
  if (config.config_repo) {
    try { const r = resolveRepo(config.config_repo); add("PF-G01", !(r.dirty && strict), `configuration pinned to git commit ${r.commit.slice(0, 12)}` + (r.dirty ? " with UNCOMMITTED changes (marked +dirty; commit before a Standard or High run)" : "")); }
    catch (e: any) { add("PF-G01", false, `config_repo: ${e.message}`); }
  } else checks.push({ id: "PF-G01", ok: false, severity: "warn", message: "configuration is not versioned in git (set config_repo); runs are pinned to a content hash only" });
  const failed = checks.filter((c) => !c.ok && c.severity === "error");
  return { ok: !failed.length, profile, checks, failed };
}
