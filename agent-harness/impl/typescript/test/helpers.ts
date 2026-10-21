import fs from "node:fs";
import os from "node:os";
import path from "node:path";
import YAML from "yaml";
import { afterEach } from "vitest";
import { Harness } from "../src/app.js";
import { loadConfig } from "../src/config.js";
import { DictSecretProvider } from "../src/credentials.js";
import { ScriptedTransport } from "../src/gateway.js";
import { LocalProcessBackend } from "../src/sandbox.js";
import { FakeClock } from "../src/util.js";

export const tmpdir = () => fs.mkdtempSync(path.join(os.tmpdir(), "h-ts-"));
/** Build an OpenAI-shaped wire response. calls: [toolName, args][] */
export function oa(content: string | null = null, calls?: [string, any][], fr?: string, usage: [number, number] = [10, 5]): [number, any] {
  const m: any = { role: "assistant", content };
  if (calls) m.tool_calls = calls.map(([n, a], i) => ({ id: `call_${i}`, type: "function", function: { name: n.replaceAll(".", "-"), arguments: JSON.stringify(a) } }));
  return [200, { choices: [{ message: m, finish_reason: fr ?? (calls ? "tool_calls" : "stop") }], usage: { prompt_tokens: usage[0], completion_tokens: usage[1] } }];
}
export const call = (name: string, args: any = {}) => oa(null, [[name, args]]);
export const FIN = () => oa(null, [["finish", { summary: "done" }]]);
const STRICT = { evaluator: { independence: "deterministic" }, retention: { events_days: 365, blobs_days: 90 }, auth: { oidc: { issuer: "https://idp", audience: "harness" } }, approvals: { channels: ["cli"], approvers: ["boss"] } };
export function makeConfig(tmp: string, profile = "core", over: any = {}) {
  const cfg: any = { profile, storage: { event_log: { kind: "sqlite", path: path.join(tmp, "log.db") }, blobs: { kind: "fs", root: path.join(tmp, "blobs") } },
    models: [{ ref: "main", provider: "openai", model_id: "m", window: 100000, secret: "k", price: { in: 1.0, out: 2.0 } }], sandbox: { backend: "local", allow_dev_tier: true },
    budgets: { turns: 30, tokens: 1_000_000, time_s: 3600, cost: 100, tools: 500 } };
  if (profile !== "core") Object.assign(cfg, JSON.parse(JSON.stringify(STRICT)));
  for (const [k, v] of Object.entries<any>(over)) cfg[k] = v && typeof v === "object" && !Array.isArray(v) && typeof cfg[k] === "object" ? { ...cfg[k], ...v } : v;
  return loadConfig(cfg);
}
export const tierDouble = (tier: string) => class extends LocalProcessBackend { tier = tier; };
let counter = 0;
const made: Harness[] = [];
afterEach(() => { while (made.length) made.pop()!.close(); });
export interface MkOpts { profile?: string; clock?: FakeClock; ruleset?: any; configDir?: string; tier?: string; tmp?: string; [k: string]: any }
export function mk(script: any[] = [], o: MkOpts = {}) {
  const { profile = "core", clock, ruleset, configDir, tier = "auto", tmp = tmpdir(), ...over } = o;
  let backend: any;
  if (tier === "auto" && profile !== "core") backend = new (tierDouble({ standard: "T-Std", high: "T-High" }[profile as string]!))(path.join(tmp, "sbx"));
  else if (tier !== "auto" && tier !== "T-Dev") backend = new (tierDouble(tier))(path.join(tmp, "sbx"));
  else backend = new LocalProcessBackend(path.join(tmp, "sbx"));
  const transport = new ScriptedTransport(script);
  const h = new Harness(makeConfig(tmp, profile, over), { transport, secrets: new DictSecretProvider({ k: "sk-test-0000000000000000" }), backend, clock: clock ?? new FakeClock(), ruleset, configDir, idFactory: () => `ID${++counter}` });
  (h as any).transport = transport; (h as any).tmp = tmp; made.push(h);
  return h as Harness & { transport: ScriptedTransport; tmp: string };
}
export const eventsOf = (h: Harness, run: any, ...types: string[]) => h.store.events(run.runId, { types: types.length ? types : undefined });
export const sleep = (ms: number) => new Promise((r) => setTimeout(r, ms));
export async function waitFor(pred: () => boolean, ms = 4000) { const end = Date.now() + ms; while (Date.now() < end) { if (pred()) return; await sleep(10); } throw new Error("waitFor timeout"); }
export const stateOf = (h: Harness, run: any) => h.store.runRow(run.runId)!.state;

// ---- task package kit (identical grader to the Python suite) ----
const GRADE = `
import json, os
ws, scratch = os.environ["HARNESS_WORKSPACE"], os.environ["HARNESS_SCRATCH"]
try:
    base = float(open(os.path.join(ws, "out", "result.txt")).read().strip())
except Exception as e:
    raise SystemExit("SECRETSTACK: grader crashed reading result: %r" % (e,))
seed = int(os.environ.get("HARNESS_SEED", "0"))
noise = ((seed % 7) / 1000.0) if os.environ.get("HARNESS_CONFIRM") else 0.0
sub = os.environ.get("HARNESS_TESTS", "")
if os.environ.get("HARNESS_CONFIRM") and os.path.exists(os.path.join(sub, "heldout.marker")):
    noise = -0.05
json.dump({"schema_version": 1, "valid": True, "primary": round(base + noise, 6), "metrics": {"accuracy": base, "runtime_s": 1.5},
           "feedback_text": "HIDDEN-HINT: case 7 failed and the answer is 42 " + "é" * 40,
           "evaluator": {"kind": "same_model_low_independence", "model_ref": "x"}, "instances_scored": 10},
          open(os.path.join(scratch, "score.json"), "w"))
`;
export function makeTask(root: string, name = "t1", o: any = {}): string {
  const d = path.join(root, name);
  fs.mkdirSync(path.join(d, "environment"), { recursive: true }); fs.mkdirSync(path.join(d, "tests", "confirm"), { recursive: true });
  fs.writeFileSync(path.join(d, "instruction.md"), "Write /workspace/run.sh producing out/result.txt with a score.");
  fs.writeFileSync(path.join(d, "environment", "Dockerfile"), "FROM scratch\n");
  fs.writeFileSync(path.join(d, "tests", "prepare.sh"), "echo prepared > $HARNESS_SCRATCH/prepared\n");
  fs.writeFileSync(path.join(d, "tests", "grade.py"), GRADE); fs.writeFileSync(path.join(d, "tests", "confirm", "heldout.marker"), "1");
  fs.writeFileSync(path.join(d, "tests", "confirm", "grade.py"), GRADE); fs.writeFileSync(path.join(d, "tests", "confirm", "prepare.sh"), "true\n");
  const spec: any = { schema_version: 1, id: name, version: "1.0.0", instruction: "instruction.md", environment: { work_image: { build: "environment/Dockerfile" }, users: { agent: 1000, candidate: 1001, judge: 1002 } }, visibility: "hidden",
    agent: { budgets: { turns: 30, tokens: 2_000_000, time_s: 600, submissions: 5 }, tool_allowlist: ["fs.*", "shell.run", "submit", "finish", "recall"] },
    scoring: { steps: o.steps ?? [{ id: "prepare", run_as: "judge", cmd: ["bash", "/tests/prepare.sh"], timeout_s: 30 }, { id: "run", run_as: "candidate", cmd: ["bash", "/workspace/run.sh"], timeout_s: 30, cwd: "/workspace" }, { id: "grade", run_as: "judge", cmd: ["python3", "/tests/grade.py"], timeout_s: 30 }],
      report_path: "/scratch/score.json", primary: { metric: "accuracy", direction: "maximize" } },
    submission: { feedback: o.feedback ?? { level: "score", max_bytes: 2048 } }, selection: { rule: "best_primary", tie_break: "earliest" }, confirmation: { mode: "reseed", trials: 3 } };
  for (const [k, v] of Object.entries<any>(o.over ?? {})) spec[k] = v && typeof v === "object" && !Array.isArray(v) && typeof spec[k] === "object" ? { ...spec[k], ...v } : v;
  fs.writeFileSync(path.join(d, "task.yaml"), YAML.stringify(spec));
  return d;
}
export const solution = (score: number): [string, any][] => [["fs.write", { path: "/workspace/run.sh", content: `mkdir -p out && echo ${score} > out/result.txt\n` }], ["submit", { note: `s=${score}` }]];
