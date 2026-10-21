#!/usr/bin/env -S npx tsx
/** Minimal CLI (design §14.4 subset): preflight, verify, inspect, task validate, demo. Mirrors the Python CLI. */
import fs from "node:fs";
import os from "node:os";
import path from "node:path";
import { Harness } from "./app.js";
import { loadConfig } from "./config.js";
import { DictSecretProvider } from "./credentials.js";
import { validateTask } from "./eval.js";
import { EventStore } from "./eventlog.js";
import { ScriptedTransport } from "./gateway.js";
import { foldAll } from "./projections.js";
import { CommandVerifier } from "./verifier.js";
import { telemetryCommand } from "./cliTelemetry.js";
import { KINDS } from "./plugins.js";
import { categories } from "./telemetry.js";

const KEY = ["run.state_changed", "run.terminated", "failure.classified", "approval.requested", "approval.decided", "recovery.uncertain", "verification.completed", "submission.created", "judge.completed", "selection.made", "confirmation.completed", "budget.exhausted", "stuck_loop.detected"];
function verify(db: string, run?: string, lastN?: number) {
  const s = new EventStore(db); let bad = 0;
  for (const r of run ? [run] : s.listRuns().map((x: any) => x.run_id)) { const v = s.verify(r, lastN); console.log(`${r}  ${v.ok ? "OK " : "BAD"}  checked=${v.checked}${v.ok ? "" : `  seq=${v.bad_seq} ${v.reason}`}`); bad += v.ok ? 0 : 1; }
  return bad ? 1 : 0;
}
function inspect(db: string, run?: string, all = false, telemetry = false) {
  const s = new EventStore(db);
  if (!run) { for (const r of s.listRuns() as any[]) console.log(`${r.run_id}  ${String(r.state).padEnd(18)} ${String(r.profile).padEnd(9)} owner=${r.owner} events=${r.last_seq} reason=${r.terminal_reason}`); return 0; }
  const evs = s.events(run); if (!evs.length) { console.error("no such run"); return 2; }
  if (telemetry) { console.log(JSON.stringify(categories(evs, run), null, 1)); return 0; }
  const rs = foldAll("run_state", evs), b = foldAll("budgets", evs).agent;
  console.log(`run ${run}: ${rs.state} (reason: ${rs.terminal_reason}), active ${rs.active_ms} ms, excluded ${rs.excluded_ms} ms`);
  console.log("budgets: " + Object.entries<any>(b).map(([k, v]) => `${k} ${v.used}/${v.limit}`).join(", "));
  for (const e of evs) if (all || KEY.includes(e.type)) {
    const brief = Object.fromEntries(Object.entries<any>(e.payload).filter(([k, v]) => typeof v !== "object" || v === null || k === "result"));
    console.log(`  ${String(e.seq).padStart(4)} ${e.ts.slice(11, 23)} ${e.type.padEnd(26)} ${JSON.stringify(brief).slice(0, 110)}`);
  }
  console.log("chain:", s.verify(run).ok ? "OK" : "BROKEN"); return 0;
}
async function demo(all: boolean) {
  const tmp = fs.mkdtempSync(path.join(os.tmpdir(), "harness-demo-ts-"));
  const resp = (calls: [string, any][]) => [200, { choices: [{ message: { role: "assistant", content: null, tool_calls: calls.map(([n, a], i) => ({ id: `c${i}`, type: "function", function: { name: n.replaceAll(".", "-"), arguments: JSON.stringify(a) } })) }, finish_reason: "tool_calls" }], usage: { prompt_tokens: 100, completion_tokens: 20 } }];
  const cfg = loadConfig({ profile: "core", storage: { event_log: { kind: "sqlite", path: path.join(tmp, "log.db") }, blobs: { kind: "fs", root: path.join(tmp, "blobs") } },
    models: [{ ref: "main", provider: "openai", model_id: "scripted", window: 100000, secret: "k" }], sandbox: { backend: "local", allow_dev_tier: true }, budgets: { turns: 10, tokens: 100000, time_s: 120 } });
  const h = new Harness(cfg, { transport: new ScriptedTransport([resp([["fs.write", { path: "/workspace/hello.py", content: "print('hello from the sandbox')\n" }]]), resp([["shell.run", { cmd: "python3 hello.py" }]]), resp([["finish", { summary: "wrote and ran hello.py" }]])]), secrets: new DictSecretProvider({ k: "demo-not-a-real-key" }) });
  const run = await h.runToEnd("demo", "write and run a hello script", { verifier: new CommandVerifier(["true"]) }); h.close();
  console.log(`database: ${path.join(tmp, "log.db")}\n`); return inspect(path.join(tmp, "log.db"), run.runId, all);
}
async function main(argv: string[]): Promise<number> {
  const [cmd, ...rest] = argv, opt = (n: string) => { const i = rest.indexOf(n); return i >= 0 ? rest[i + 1] : undefined; };
  switch (cmd) {
    case "verify": return verify(opt("--db")!, opt("--run"), opt("--last-n") ? Number(opt("--last-n")) : undefined);
    case "inspect": return inspect(opt("--db")!, opt("--run"), rest.includes("--all"), rest.includes("--telemetry"));
    case "plugins": { for (const [k, v] of Object.entries<any>(KINDS).sort(([a], [b]) => (a < b ? -1 : 1))) { console.log(`${k.padEnd(13)} interface ${v.interface_version}  needs ${v.min_profile.padEnd(8)} grants ${v.grants.join(",").padEnd(22)} ${v.summary.split(".")[0]}`); if (rest.includes("--methods")) console.log("              methods: " + Object.entries<any>(v.methods).map(([m, s]) => `${m}(${s.effect === "write" ? "write" : "read"})`).join(", ")); } return 0; }
    case "events": case "usage": case "alerts": case "metrics": case "integrity": return telemetryCommand(cmd, rest, console.log, console.error);
    case "task": { if (rest[0] !== "validate") break; const e = validateTask(rest[1]); console.log(e.length ? e.map((x) => `error: ${x}`).join("\n") : "OK"); return e.length ? 1 : 0; }
    case "preflight": { const h = new Harness(loadConfig(opt("--config")!), { transport: new ScriptedTransport([]) }); const r = h.preflight(); h.close(); for (const c of r.checks) console.log(`${c.id}  ${c.ok ? "pass" : c.severity === "error" ? "FAIL" : "warn"}  ${c.message}`); return r.ok ? 0 : 1; }
    case "demo": return demo(rest.includes("--all"));
  }
  console.error("usage: harness-ts {verify|inspect|events|usage|alerts|metrics|task validate|preflight|demo} [options]"); return 2;
}
main(process.argv.slice(2)).then((c) => process.exit(c), (e) => { console.error(e); process.exit(1); });
