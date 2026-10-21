/** Evaluation harness (design §13): task packages, submit -> snapshot -> fresh judge sandbox -> report -> feedback, selection, confirmation. */
import { observation } from "./failure.js";
import fs from "node:fs";
import path from "node:path";
import YAML from "yaml";
import { BudgetController } from "./budgets.js";
import { formatTs, sha256Hex, ulid } from "./util.js";
import { loadSchema, newAjv } from "./registry.js";
import { restoreSnapshot, takeSnapshot } from "./sandbox.js";
import { bootstrapCi, seedFor } from "./stats.js";
import { ToolError } from "./runtime.js";
import { Verifier } from "./verifier.js";
import { contractFor, evaluateContract, validateContract } from "./outcome.js";
import { trajectoryMetrics } from "./telemetry.js";
import { validateDeclaration } from "./visibility.js";

export class Task { constructor(public dir: string, public spec: any) {} get id() { return this.spec.id; } get testsDir() { return path.join(this.dir, "tests"); } get instruction() { return fs.readFileSync(path.join(this.dir, this.spec.instruction), "utf8"); } }
export function validateTask(dir: string): string[] {
  const f = path.join(dir, "task.yaml"), errs: string[] = [];
  if (!fs.existsSync(f)) return [`${f} not found`];
  let spec: any;
  try { spec = YAML.parse(fs.readFileSync(f, "utf8")); const ajv = newAjv(), v = ajv.compile(loadSchema("task.schema.json")); if (!v(spec)) return [`task.yaml invalid: ${ajv.errorsText(v.errors)}`]; } catch (e: any) { return [`task.yaml invalid: ${e.message}`]; }
  if (!fs.existsSync(path.join(dir, spec.instruction))) errs.push(`instruction file ${spec.instruction} missing`);
  const build = spec.environment.work_image?.build; if (build && !fs.existsSync(path.join(dir, build))) errs.push(`work_image build file ${build} missing`);
  const users = Object.values(spec.environment.users ?? {}); if (new Set(users).size !== users.length) errs.push("environment.users must be distinct");
  const ids = spec.scoring.steps.map((s: any) => s.id); if (new Set(ids).size !== ids.length) errs.push("scoring step ids must be unique");
  for (const s of spec.scoring.steps) for (const a of s.cmd) if (a.startsWith("/tests/") && !fs.existsSync(path.join(dir, "tests", a.slice(7)))) errs.push(`step ${s.id}: ${a} not found under tests/`);
  errs.push(...validateDeclaration(spec));
  if (spec.outcome !== undefined && spec.outcome !== null) errs.push(...validateContract({ task_id: spec.id, ...spec.outcome }));
  if (spec.visibility === "public") {
    if (spec.purpose !== "development") errs.push("public tests are development aids: a public task must declare purpose: development");
    if (spec.confirmation?.mode === "heldout_set") errs.push("public tests cannot support a held-out or confirmation claim: confirmation.mode heldout_set is not allowed");
  }
  if ((spec.visibility ?? "hidden") === "hidden") {
    if (fs.existsSync(path.join(dir, "environment", "tests"))) errs.push("hidden task places tests/ inside environment/ (agent-reachable)");
    if ((spec.candidate_outputs ?? "/workspace/out").startsWith("/tests")) errs.push("candidate_outputs must not be under /tests");
  }
  if (spec.confirmation?.mode === "heldout_set" && !fs.existsSync(path.join(dir, "tests", "confirm"))) errs.push("confirmation.mode heldout_set requires tests/confirm/");
  return errs;
}
export function loadTask(dir: string): Task { const e = validateTask(dir); if (e.length) throw new Error("invalid task: " + e.join("; ")); return new Task(dir, YAML.parse(fs.readFileSync(path.join(dir, "task.yaml"), "utf8"))); }
export const truncateUtf8 = (s: string, n: number) => Buffer.from(s, "utf8").subarray(0, n).toString("utf8").replace(/\uFFFD+$/, "");
/** The feedback before any size limit: only the score and the judge-authored feedback_text ever reach the agent, never raw test output (§13.5). */
export function feedbackText(report: any, level: string): string {
  if (level === "none") return "Submission received.";
  if (!report.valid) return "Submission was invalid: it could not be scored.";
  let txt = `Score: ${num(report.primary)}`; if (level === "score_and_summary" && report.feedback_text) txt += "\n" + report.feedback_text; return txt;
}
export const shapeFeedback = (report: any, level: string, maxBytes: number): string => truncateUtf8(feedbackText(report, level), maxBytes);
const num = (x: any) => (Number.isInteger(x) ? `${x}.0` : String(x));   // Python renders floats as 0.8 / 1.0; keep feedback text identical across implementations

export class EvalRunner {
  private validate: any; spec: any;
  constructor(public h: any, public task: Task) { this.spec = task.spec; const ajv = newAjv(); this.validate = ajv.compile(loadSchema("score.report.v1.json")); }
  private async score(run: any, manifestHash: string, envExtra: any = {}, testsSub: string | null = null, events: any = null) {
    const h = this.h, judge = await h.sandboxes.create(run, "judge");
    try {
      restoreSnapshot(manifestHash, h.blobs, judge);
      fs.cpSync(testsSub ? path.join(this.task.testsDir, testsSub) : this.task.testsDir, judge.executor.roots["/tests"], { recursive: true });
      if (events) await run.emit("judge.started", { submission_id: events.submission_id, sandbox_id: judge.sandbox_id });
      let report: any = null; const errors: string[] = [];
      for (const step of this.spec.scoring.steps) {
        const r = await judge.executor.execRun(step.cmd, { cwd: step.cwd ?? "/workspace", user: step.run_as, timeoutS: step.timeout_s, env: envExtra });
        if (r.timed_out || r.exit_code !== 0) { errors.push(`step ${step.id} ${r.timed_out ? "timed out" : "exited " + r.exit_code}`); break; }
      }
      if (!errors.length) {
        try { report = JSON.parse(judge.executor.fsRead(this.spec.scoring.report_path).content); if (!this.validate(report)) throw new Error(JSON.stringify(this.validate.errors)); }
        catch (e: any) { errors.push(`report invalid: ${e.message}`); report = null; }
      }
      if (!report) report = { schema_version: 1, valid: false, primary: 0.0, metrics: {}, evaluator: { kind: "deterministic" }, errors };
      if ((this.spec.visibility ?? "hidden") === "public") report.generalization_claim = false;   // public tests are development aids and cannot support a generalization claim (PRD §3.6)
      report.evaluator = { kind: "deterministic", model_ref: null };   // set by the control plane, never by judge scripts (§13.4)
      report.errors ??= []; for (const e of errors) if (!report.errors.includes(e)) report.errors.push(e);
      return report;
    } finally { await h.sandboxes.destroy(run, judge); }
  }
  async submit(run: any, work: any, note: string | null) {
    const bc = new BudgetController(run), [rid, ex] = await bc.reserve({ submissions: 1 });   // counted before the job is launched, and atomic (§7.4.4)
    if (ex) throw new ToolError("submit.budget_exhausted", "tool", "no submissions remaining", false, "Call finish; your best submission will be selected");
    const sid = ulid();
    await run.emit("submission.created", { submission_id: sid, snapshot_id: null, note });
    await bc.commit(rid!, { submissions: 1 });
    await run.transition("SCORING", `submission ${sid}`);
    return this.process(run, work, sid);
  }
  async resumeSubmission(run: any, work: any) { const ids = Object.keys(await run.proj("submissions")); return ids.length ? this.process(run, work, ids[ids.length - 1]) : null; }
  private async process(run: any, work: any, sid: string): Promise<string> {
    const h = this.h; let s = (await run.proj("submissions"))[sid];
    if (s.status === "created") {
      h.backend.pause(work);
      const snap = takeSnapshot(work, h.blobs), snapshotId = ulid();
      await h.writer.execute("INSERT INTO snapshots(snapshot_id,run_id,taken_at_seq,manifest_hash,kind,created_at) VALUES(?,?,?,?,?,?)", [snapshotId, run.runId, h.store.lastSeq(run.runId), snap.manifest_hash, "submission", formatTs(h.clock.nowMs())]);
      await run.emit("snapshot.created", { snapshot_id: snapshotId, submission_id: sid, manifest_hash: snap.manifest_hash, chain_head: h.store.headHash(run.runId) });
      s = (await run.proj("submissions"))[sid];
    }
    if (s.status === "snapshotted" || s.status === "judging") {
      const row = h.store.get("SELECT manifest_hash FROM snapshots WHERE snapshot_id=?", s.snapshot_id);
      const report = await this.score(run, row.manifest_hash, {}, null, { submission_id: sid }), ref = h.blobs.putJson(report);
      await run.emit("judge.completed", { submission_id: sid, valid: report.valid, primary: report.primary, score_report_ref: ref, metrics: report.metrics, evaluator_kind: report.evaluator.kind });
      s = (await run.proj("submissions"))[sid];
    }
    const report = h.blobs.getJson(s.report_ref), fb = this.spec.submission?.feedback ?? { level: "score", max_bytes: 2048 };
    const raw = feedbackText({ ...report, primary: report.primary }, fb.level), cap = fb.max_bytes ?? 2048, text = truncateUtf8(raw, cap);
    if (s.status !== "feedback") {
      await run.emit("feedback.delivered", { submission_id: sid, level: fb.level, bytes: Buffer.byteLength(text) });
      if (Buffer.byteLength(raw) > cap) await run.emit("failure.observed", observation(h.rm.rules, "eval_integrity.over_granular_feedback", sid, { bytes_offered: Buffer.byteLength(raw), cap, level: fb.level }));   // the cap held; that it had to is a signal of leaked or over-granular feedback (PRD §3.7)
    }
    h.backend.resume(work);
    if ((await run.state()) === "SCORING") await run.transition("RUNNING", "feedback.delivered");
    return text;
  }
  async select(run: any) {
    const subs = await run.proj("submissions"), scored = Object.entries<any>(subs).filter(([, s]) => s.valid && s.primary !== null);
    if (!scored.length) return null;
    const maxi = this.spec.scoring.primary.direction === "maximize", tie = this.spec.selection?.tie_break ?? "earliest";
    let best = scored[0];
    for (const [sid, s] of scored.slice(1)) { const better = maxi ? s.primary > best[1].primary : s.primary < best[1].primary; if (better || (s.primary === best[1].primary && tie === "latest")) best = [sid, s]; }
    const nScored = Object.values<any>(subs).filter((s) => s.status === "scored" || s.status === "feedback").length;
    await run.emit("selection.made", { rule: this.spec.selection?.rule ?? "best_primary", submission_id: best[0], n_submissions: nScored });
    return { submission_id: best[0], primary: best[1].primary, n_submissions: nScored, snapshot_id: best[1].snapshot_id };
  }
  async confirm(run: any, sel: any) {
    const cfg = this.spec.confirmation ?? { mode: "reseed", trials: 3 }, row = this.h.store.get("SELECT manifest_hash FROM snapshots WHERE snapshot_id=?", sel.snapshot_id), primaries: number[] = []; let allValid = true;
    for (let i = 0; i < (cfg.trials ?? 3); i++) {
      const rep = cfg.mode === "reseed"
        ? await this.score(run, row.manifest_hash, { HARNESS_SEED: String(parseInt(sha256Hex(`${run.runId}:${sel.submission_id}:${i}`).slice(0, 12), 16)), HARNESS_CONFIRM: "1" })
        : await this.score(run, row.manifest_hash, { HARNESS_CONFIRM: "1" }, "confirm");
      if (!rep.valid) allValid = false; primaries.push(rep.primary);
      await run.emit("confirmation.trial", { submission_id: sel.submission_id, trial: i, valid: rep.valid, primary: rep.primary });
    }
    let result: any;
    if (!allValid) result = { valid: false, trials: primaries.length, reason: "a confirmation trial was invalid" };
    else { const ci = bootstrapCi(primaries, seedFor(run.runId, "confirm:" + sel.submission_id), this.spec.replication?.confidence ?? 0.95, 2000); result = { valid: true, trials: primaries.length, mean: ci.mean, lower: ci.lower, upper: ci.upper, unconfirmed_upper_bound: sel.primary, submissions_used: sel.n_submissions }; }
    await run.emit("confirmation.completed", { rule: cfg.mode, submission_id: sel.submission_id, n_submissions: sel.n_submissions, result });
    return result;
  }
}
/** Completion evidence for evaluation runs: the selected submission passes; confirmed when >= 2 were scored (§6.3, §13.6). */
export class EvalVerifier implements Verifier {
  constructor(public r: EvalRunner) {}
  async verify(run: any, sb: any) {
    const sel = await this.r.select(run); let value: number | null = null, conf: any = null, detail = "no valid scored submission", evidence: any = {}, report: any = {};
    if (sel) {
      value = sel.primary; const ev = this.r.h.store.events(run.runId, { types: ["judge.completed"] }).reverse().find((e: any) => e.payload.submission_id === sel.submission_id);
      report = ev ? { valid: true, primary: ev.payload.primary, metrics: ev.payload.metrics ?? {} } : {};
      if (sel.n_submissions >= 2) { conf = await this.r.confirm(run, sel); if (!conf.valid) { value = null; detail = conf.reason; evidence = { selection: sel, confirmation: conf }; } else value = conf.mean; }
    }
    // the verdict comes from the outcome contract, fixed before execution, and from evidence (§13.11); a task without a contract gets one from its pass threshold
    const traj = trajectoryMetrics(this.r.h.store.events(run.runId));   // the transcript's deterministic metrics, recorded apart from the outcome (§13.11)
    await run.emit("trajectory.analyzed", { task_id: this.r.spec.id, metrics: traj });
    const contract = contractFor(this.r.spec);
    const out = evaluateContract(contract, { value, report, fs_read: sb?.executor ? (p: string) => sb.executor.fsRead(p).content : null, facts: {} });
    await run.emit("outcome.evaluated", { task_id: this.r.spec.id, ...out });
    if (value !== null) { detail = conf ? `confirmed value ${num(value)}` : `score ${num(value)} (unconfirmed upper bound; single submission)`; evidence = { selection: sel, confirmation: conf, value }; }
    return { passed: out.verdict === "SUCCESS", detail: `${detail}; outcome ${out.verdict}`, evidence: { ...evidence, outcome: { verdict: out.verdict, contract_hash: out.contract_hash }, trajectory: traj } };
  }
}
export function attachEval(h: any, loop: any, task: Task): EvalRunner {
  const runner = new EvalRunner(h, task);
  loop.hooks.submit = (note: string | null) => runner.submit(loop.run, loop.sandbox(), note);
  loop.hooks.submit_resume = () => runner.resumeSubmission(loop.run, loop.sandbox());
  loop.verifier = new EvalVerifier(runner);
  return runner;
}
