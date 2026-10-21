import { fold, report } from "./integrity.js";
/** `harness events | usage | alerts | metrics` (design §14.4). Mirrors the Python CLI: pure reads of the run database, content gated by role, everything redacted, and a reviewer's reads logged. */
import crypto from "node:crypto";
import os from "node:os";
import { EventStore } from "./eventlog.js";
import { activeAlerts } from "./selection.js";
import * as T from "./telemetry.js";

export function telemetryCommand(cmd: string, rest: string[], out: (s: string) => void, err: (s: string) => void = () => {}): number {
  const opt = (n: string) => { const i = rest.indexOf(n); return i >= 0 ? rest[i + 1] : undefined; }, has = (n: string) => rest.includes(n);
  const db = opt("--db")!, run = opt("--run"), json = has("--json"), s = new EventStore(db);
  if (cmd === "integrity") {   // the status of each evaluation-integrity class, derived from its detectors' recorded status; a detector nobody has run is NOT_RUN and keeps its class open
    const runs = run ? [run] : s.listRuns().map((r: any) => r.run_id as string), evs = runs.flatMap((r) => s.events(r).filter((e: any) => e.type === "integrity.detector_status")).sort((a: any, b: any) => (a.ts < b.ts ? -1 : a.ts > b.ts ? 1 : a.run_id < b.run_id ? -1 : a.run_id > b.run_id ? 1 : a.seq - b.seq));
    const age = opt("--max-age-s"), rep = report(fold(evs), new Date().toISOString().replace(/\.\d+Z$/, "Z"), age === undefined ? null : Number(age));
    if (json) { out(JSON.stringify(rep)); return 0; }
    for (const [cls, c] of Object.entries<any>(rep.classes)) { out(`${cls.padEnd(24)} ${c.status}`); for (const d of c.detectors) out(`   ${d.id.padEnd(30)} v${d.version} ${d.severity.padEnd(8)} ${d.status.padEnd(27)} counts as ${d.effective}`); for (const w of c.waivers) out(`   waiver on ${w.detector} approved by ${w.approved_by} until ${w.expires_at}: ${w.rationale}`); }
    for (const r of rep.rejected) out(`rejected event ${r.index}: ${r.reason}`); return 0;
  }
  if (cmd === "alerts") {
    const runs = run ? [run] : s.listRuns().map((r: any) => r.run_id as string); let n = 0;
    for (const rid of runs) {
      const evs = s.events(rid), live = new Set(activeAlerts(evs).map((x: any) => `${x.rule}\u0000${String(x.context?.tool ?? "")}`));
      for (const e of evs) if (e.type === "alert.raised" || e.type === "alert.resolved") {
        const p = e.payload; n++; const state = e.type === "alert.raised" && live.has(`${p.rule}\u0000${String(p.context?.tool ?? "")}`) ? "active" : e.type === "alert.resolved" ? "resolved" : "superseded";
        out(typeof p.value === "number" ? `${rid} ${String(e.seq).padStart(4)} ${e.type.padEnd(15)} ${p.rule.padEnd(26)} ${String(p.severity ?? "").padEnd(9)} value=${Number(p.value.toPrecision(3))} ${state}` : `${rid} ${String(e.seq).padStart(4)} ${e.type}`);
      }
    }
    if (!n) out("no alerts recorded"); return 0;
  }
  const evs = s.events(run!); if (!evs.length) { err("no such run"); return 2; }
  if (cmd === "events") {
    const role = opt("--role") ?? "operator", rows = T.eventRows(evs, run!, role, opt("--type") ?? null, has("--errors"));
    if (role !== "operator") s.db.prepare("INSERT INTO content_access(id,run_id,who,role,what,at) VALUES(?,?,?,?,?,?)").run(crypto.randomUUID().replaceAll("-", ""), run!, opt("--who") ?? os.userInfo().username, role, `events type=${opt("--type") ?? "*"} errors=${has("--errors")}`, new Date().toISOString().replace(/\.\d+Z$/, "Z"));
    for (const r of rows) out(json ? JSON.stringify(r) : `${r.run_id} ${String(r.seq).padStart(4)} ${r.ts.slice(11, 23)} ${r.type.padEnd(26)} ${JSON.stringify(r.payload).slice(0, 140)}`); return 0;
  }
  if (cmd === "usage") { const u = T.usage(evs); out(json ? JSON.stringify({ run_id: run, ...u }) : `run ${run}: ${u.tokens.input_tokens} in, ${u.tokens.output_tokens} out, ${u.tokens.total} total; cost: ${u.cost ? `${u.cost.estimated_cost} ${u.cost.currency}` : "unavailable (no price configured)"}`); return 0; }
  if (cmd === "metrics") { out(JSON.stringify({ run_id: run, ...T.runMetrics(evs) }, null, json ? undefined : 1)); return 0; }
  return 2;
}
