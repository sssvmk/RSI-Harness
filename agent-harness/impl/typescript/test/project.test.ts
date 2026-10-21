/** Project identification (design §7.3.10): the harness asks the user and never infers whether a run belongs to a project. Mirrors test_project.py. */
import { describe, expect, it } from "vitest";
import * as M from "../src/manifest.js";
import { ProjectError, ProjectNotDeclared, UNSET, declare, enabled, parseAnswer } from "../src/project.js";
import { eventsOf, mk, oa } from "./helpers.js";

const ON = { memory: { project: { enabled: true } } };
const rejects = async (p: Promise<any>, cls: any, re: RegExp) => { try { await p; } catch (e: any) { expect(e).toBeInstanceOf(cls); expect(e.message).toMatch(re); return; } throw new Error("did not throw"); };

describe("the answer", () => {
  it.each([["alpha", "alpha"], ["  My Project 2  ", "My Project 2"], ["a.b-c_d", "a.b-c_d"], ["none", null], ["None", null], ["no", null], ["", null], ["  ", null], ["not a project", null], ["no project", null]])("an answer is a project name or not a project: %j", (text: any, expect_: any) => { expect(parseAnswer(text)).toBe(expect_); });
  it.each(["../etc", "a/b", "-leading", "x".repeat(64), "semi;colon", "tab\there", "<script>"])("anything that is not a clean name is refused rather than guessed: %j", (bad: string) => { expect(() => parseAnswer(bad)).toThrow(/not a project name/); });
});
describe("declaring", () => {
  it("without an answer a run is refused only when project memory is on", async () => {
    expect([enabled(ON), enabled({}), enabled({ memory: {} }), enabled({ memory: { project: { enabled: false } } })]).toEqual([true, false, false, false]);
    expect([await declare({}), await declare({ memory: { project: { enabled: false } } })]).toEqual([null, null]);   // off, and nothing said: nothing to record
    await rejects(declare(ON), ProjectNotDeclared, /does not guess/);
  });
  it("a name the caller supplies and an answer the user gives are both recorded as the user's", async () => {
    expect(await declare(ON, "alpha")).toEqual({ id: "alpha", declared_by: "user", asked: false }); expect(await declare(ON, null)).toEqual({ id: null, declared_by: "user", asked: false });   // an explicit "no project" is an answer
    const seen: string[] = []; expect(await declare(ON, UNSET, (q) => { seen.push(q); return "Beta Project"; })).toEqual({ id: "Beta Project", declared_by: "user", asked: true }); expect(seen[0]).toContain("part of a project");
    expect(await declare(ON, UNSET, async () => "none")).toEqual({ id: null, declared_by: "user", asked: true });
    expect((await declare(ON, "alpha", () => { throw new Error("the user was asked although the caller already answered"); }))!.asked).toBe(false);   // no second question
    await rejects(declare(ON, UNSET, () => "a/b"), ProjectError, /not a project name/);
  });
});
describe("in a run", () => {
  it("a run with project memory on and no answer is refused before anything is created", async () => { const h = mk([oa("d")], ON); await rejects(h.createRun("u", "go"), ProjectNotDeclared, /does not guess/); expect(h.store.listRuns()).toEqual([]); });
  it("the answer is recorded in the manifest with how it was given", async () => {
    const h = mk([oa("d")], ON), field = (run: any) => eventsOf(h, run, "run.created")[0].payload.manifest.fields.project;
    const { run } = await h.createRun("u", "go", { project: "alpha" }); expect([field(run).status, field(run).value]).toEqual(["recorded", { id: "alpha", declared_by: "user", asked: false }]);
    const { run: r2 } = await h.createRun("u", "go", { askProject: () => "Gamma" }); expect(field(r2).value).toEqual({ id: "Gamma", declared_by: "user", asked: true });
    const { run: r3 } = await h.createRun("u", "go", { project: null }); expect(field(r3).value.id).toBeNull(); expect([M.validate(eventsOf(h, r3, "run.created")[0].payload.manifest, "core"), M.START.includes("project")]).toEqual([[], true]);
  });
  it("when project memory is off the field is not applicable and runs start as before", async () => {
    const h = mk([oa("d")]), { run } = await h.createRun("u", "go"), f = eventsOf(h, run, "run.created")[0].payload.manifest.fields.project; expect([f.status, f.value, f.reason.includes("not enabled")]).toEqual(["not_applicable", null, true]);
  });
});
