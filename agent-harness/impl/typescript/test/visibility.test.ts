/** Declared test visibility (design §13.1) and the evaluation-run manifest. Mirrors test_visibility.py. */
import fs from "node:fs";
import path from "node:path";
import YAML from "yaml";
import { describe, expect, it } from "vitest";
import * as M from "../src/manifest.js";
import { Task, validateTask } from "../src/eval.js";
import { declarationFor, publicFiles, testsHash, validateDeclaration } from "../src/visibility.js";
import { FIN, call, eventsOf, makeTask, mk, oa, solution, tmpdir } from "./helpers.js";

const VEC = JSON.parse(fs.readFileSync(path.resolve(import.meta.dirname, "../../../spec/vectors/visibility-vectors.json"), "utf8"));
const SCRIPT = () => [call(...solution(0.8)[0]), call(...solution(0.8)[1]), FIN(), FIN()], PUBLIC = { visibility: "public", purpose: "development" };
const taskOf = (d: string) => new Task(d, YAML.parse(fs.readFileSync(path.join(d, "task.yaml"), "utf8")));
async function fresh(over: any = {}, script: any[] = [oa("d")]) { const h = mk(script), d = makeTask(h.tmp, "t1", { over }), { run, loop } = await h.createEvalRun("u", taskOf(d)); return { h, run, loop }; }

describe("declarations", () => {
  it("a task without a full declaration gets a conservative one, and never a generalization claim", () => {
    expect(declarationFor({ visibility: "hidden" })).toEqual({ purpose: "evaluation", held_out_use: false, generalization_claim: false, public_tests: { mount_mode: "read_only" } });
    expect(declarationFor({ visibility: "hidden", confirmation: { mode: "heldout_set" } }).held_out_use).toBe(true);
    expect(declarationFor({ visibility: "public" })).toEqual({ purpose: "development_only", held_out_use: false, generalization_claim: false, public_tests: { mount_mode: "read_only" } });
    const given = { visibility: "hidden", test_visibility: { purpose: "evaluation", held_out_use: true, generalization_claim: true } }; expect(declarationFor(given).generalization_claim).toBe(true); expect(validateDeclaration(given)).toEqual([]);
  });
  it("missing, invalid, and contradictory declarations are rejected", () => {
    const t = tmpdir(), tv = (o: any) => ({ test_visibility: o });
    const bad: Record<string, any> = {
      "public + a generalization claim": { ...PUBLIC, ...tv({ purpose: "development_only", held_out_use: false, generalization_claim: true }) }, "development_only + held-out use": tv({ purpose: "development_only", held_out_use: true, generalization_claim: false }),
      "public + held-out use": { ...PUBLIC, ...tv({ purpose: "development_only", held_out_use: true, generalization_claim: false }) }, "public declared for evaluation": { ...PUBLIC, ...tv({ purpose: "evaluation", held_out_use: false, generalization_claim: false }) } };
    for (const [name, over] of Object.entries(bad)) expect(validateTask(makeTask(t, name.replace(/\W/g, "_"), { over })).some((e) => e.includes("test_visibility")), name).toBe(true);
    for (const [name, o] of Object.entries<any>({ missing: { purpose: "evaluation", held_out_use: false }, unknown: { purpose: "fun", held_out_use: false, generalization_claim: false }, writable: { purpose: "evaluation", held_out_use: false, generalization_claim: false, public_tests: { mount_mode: "read_write" } } }))
      expect(validateTask(makeTask(t, name, { over: tv(o) })).length, name).toBeGreaterThan(0);
    expect(validateTask(makeTask(t, "fine", { over: tv({ purpose: "evaluation", held_out_use: false, generalization_claim: false }) }))).toEqual([]);
  });
  it("a contradictory declaration also refuses the run", async () => {
    const h = mk([oa("d")]), d = makeTask(h.tmp, "t", { over: { test_visibility: { purpose: "development_only", held_out_use: true, generalization_claim: false } } });
    await expect(h.createEvalRun("u", taskOf(d))).rejects.toThrow(/held-out/);
  });
  it("declarations and the grader hash match the shared vectors", () => {
    for (const c of VEC.declarations) expect([declarationFor(c.spec), validateDeclaration(c.spec)], c.name).toEqual([c.declaration, c.errors]);
    const d = tmpdir(); for (const [k, v] of Object.entries<string>(VEC.tests_dir.files)) { fs.mkdirSync(path.dirname(path.join(d, k)), { recursive: true }); fs.writeFileSync(path.join(d, k), v); }
    expect(testsHash(d)).toBe(VEC.tests_dir.tests_hash); expect(publicFiles(d)).toEqual(VEC.tests_dir.public_files);   // byte-identical to Python's
  });
  it("the grader hash follows the tests", () => {
    const d = makeTask(tmpdir(), "g"), a = testsHash(path.join(d, "tests")); expect(testsHash(path.join(d, "tests"))).toBe(a); fs.writeFileSync(path.join(d, "tests", "grade.py"), "print('changed')"); const b = testsHash(path.join(d, "tests")); expect(b).not.toBe(a);
    fs.writeFileSync(path.join(d, "tests", "new.py"), "x"); expect(testsHash(path.join(d, "tests"))).not.toBe(b);
  });
});
describe("verified runtime state", () => {
  it("a hidden task's tests are verified not to be in the agent workspace", async () => {
    const { h, run, loop } = await fresh(), v = eventsOf(h, run, "test_visibility.verified")[0].payload;
    expect(v.state).toEqual({ available: false, mounted_in_agent_workspace: false, mount_mode: "read_only", read_only_enforced: false }); expect(eventsOf(h, run, "test.exposed")).toHaveLength(0); expect(fs.existsSync(loop.sandbox().executor.resolve("/workspace") + "/tests")).toBe(false);
  });
  it("a public task's tests are mounted read-only and the exposure is recorded without claiming enforcement", async () => {
    const { h, run, loop } = await fresh(PUBLIC), ex = eventsOf(h, run, "test.exposed")[0].payload, v = eventsOf(h, run, "test_visibility.verified")[0].payload;
    expect([ex.test_ids.includes("grade.py"), ex.test_ids.some((i: string) => i.startsWith("confirm/")), ex.mount_mode]).toEqual([true, false, "read_only"]);   // the held-out confirmation set is never shown
    expect([v.state.mounted_in_agent_workspace, v.state.available, v.state.read_only_enforced]).toEqual([true, true, false]);   // a file permission on the dev tier, and it says so
    const e = loop.sandbox().executor, real = e.resolve("/workspace/tests/grade.py"); expect([fs.statSync(real).isFile(), (fs.statSync(real).mode & 0o222) === 0, v.declaration.generalization_claim, v.declaration.purpose]).toEqual([true, true, false, "development_only"]); expect(fs.existsSync(e.resolve("/workspace") + "/tests/confirm")).toBe(false);
  });
});
describe("the manifest of an evaluation run", () => {
  it("records the task, grader, rubric and verified visibility, and is recomputable from the log", async () => {
    const { h, run, loop } = await fresh(PUBLIC, SCRIPT()); await h.rm.start(run); await loop.runLoop();
    const start = eventsOf(h, run, "run.created")[0].payload.manifest, f = start.fields, task = f.task.value;
    expect([task.id, task.version, task.grader_sha256.length, M.validate(start, "core")]).toEqual(["t1", "1.0.0", 64, []]);
    expect(run.pins.resolve("evaluator_rubric").scoring.primary.metric).toBe("accuracy"); expect(f.definitions.value.find((d: any) => d.id === "evaluator_rubric").version).toBe(1);
    expect(task.outcome_contract_sha256).toBe(eventsOf(h, run, "outcome.evaluated")[0].payload.contract_hash);   // the contract in the manifest is the one that judged the run
    const fin = eventsOf(h, run, "run.finished")[0].payload; expect([fin.fields.test_visibility.value.state.mounted_in_agent_workspace, fin.fields.outcome.value.verdict]).toEqual([true, "SUCCESS"]); expect(fin.manifest_hash).toBe(M.manifestHash(M.merged(start, fin.fields)));
  });
});
