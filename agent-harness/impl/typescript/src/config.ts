import { versionOf } from "./configRepo.js";
import fs from "node:fs";
import YAML from "yaml";
import { canonicalize, sha256Hex } from "./util.js";
import { loadSchema, newAjv } from "./registry.js";
const DEFAULTS: any = {
  context: { compaction: { trigger_fraction: 0.75, target_fraction: 0.4, keep_recent_turns: 6 }, clear: { keep_recent_results: 10 } },
  loop: { max_idle_turns: 3, stuck: { threshold: 4, extra_repeats: 2 }, max_verification_failures: 3 },
  gateway: { max_attempts: 4, timeout_s: 120 }, tools: { dynamic_threshold: 20, selection_error_alert: 0.05 },
  recovery: { max_excluded_downtime_s: 300, verify_last_n: 1000 }, approvals: { channels: ["cli"], expiry_s: 3600, approvers: [], allow_owner_approval: false },
  egress: { default: "deny", grants: [] }, cache: { semantic: "off" },
};
const isObj = (x: any) => x && typeof x === "object" && !Array.isArray(x);
export function merge(base: any, over: any): any { const out = JSON.parse(JSON.stringify(base)); for (const [k, v] of Object.entries(over)) out[k] = isObj(v) && isObj(out[k]) ? merge(out[k], v) : v; return out; }
export function loadConfig(src: string | object): any {
  const raw: any = typeof src === "string" ? YAML.parse(fs.readFileSync(src, "utf8")) : src;
  const ajv = newAjv(), v = ajv.compile(loadSchema("config.schema.json"));
  if (!v(raw)) throw new Error(`invalid config: ${ajv.errorsText(v.errors)}`);
  return merge(DEFAULTS, raw);
}
/** The version a run is pinned to: the git commit of `config_repo` when one is configured (suffixed `+dirty` for uncommitted changes), otherwise a content hash of the canonical config. */
export const configCommit = (cfg: any) => (cfg.config_repo ? versionOf(cfg.config_repo) : sha256Hex(canonicalize(cfg)).slice(0, 40));
