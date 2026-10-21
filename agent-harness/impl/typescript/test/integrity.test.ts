/** Evaluation-integrity closure (design §15.6): detector-level accountability, with the class status derived from its detectors. Mirrors test_integrity.py and replays the shared vectors. */
import fs from "node:fs";
import path from "node:path";
import { describe, expect, it } from "vitest";
import * as I from "../src/integrity.js";
import { telemetryCommand } from "../src/cliTelemetry.js";
import { mk, oa } from "./helpers.js";

const V = JSON.parse(fs.readFileSync(path.resolve(import.meta.dirname, "../../../spec/vectors/integrity-vectors.json"), "utf8")), NOW = "2026-10-21T12:00:00Z";
const ev = (d: string, status: string, by = "ann", ts = "2026-10-21T08:00:00Z", kw: any = {}) => ({ type: "integrity.detector_status", ts, payload: { detector_id: d, status, by, ...kw } });
const PASS = (d: string, by = "ann", v = "1", ts = "2026-10-21T08:00:00Z") => ev(d, "PASS", by, ts, { evidence_ref: `ev:${d}`, evidence_version: v, run_id: "r1" });

describe("closure", () => {
  it("every scenario derives the same class status, rejections and history as the shared vectors", () => {
    for (const c of V.scenarios) {
      const s = I.fold(c.events, V.spec), r = I.report(s, c.now, c.max_age_s), flat = Object.values<any>(r.classes);
      const got = { classes: Object.fromEntries(Object.entries<any>(r.classes).map(([k, v]) => [k, v.status])), rejected: s.rejected.map((x: any) => x.index), effective: Object.fromEntries(flat.flatMap((v) => v.detectors.map((d: any) => [d.id, d.effective]))),
        history: Object.fromEntries(Object.entries<any>(s.detectors).map(([k, d]) => [k, d.history.length])), blocking: Object.fromEntries(Object.entries<any>(r.classes).map(([k, v]) => [k, v.blocking])), waivers: Object.fromEntries(Object.entries<any>(r.classes).map(([k, v]) => [k, v.waivers.map((w: any) => w.detector)])) };
      expect(got, c.name).toEqual(c.expect);
    }
  });
  it("the harness's own classes all start open because nothing has demonstrated a pass", () => {
    const spec = I.loadSpec(), r = I.report(I.fold([]), NOW);
    expect([new Set(Object.keys(r.classes))]).toEqual([new Set(["tampering", "state_carryover", "over_granular_feedback", "selection_bias", "clock_reset", "stale_grants"])]); expect(Object.keys(spec.classes).sort()).toEqual(Object.keys(r.classes).sort());
    expect([...new Set(Object.values<any>(r.classes).map((c) => c.status))]).toEqual(["OPEN"]); expect(Object.values<any>(r.classes).every((c) => c.detectors.every((d: any) => d.status === "NOT_RUN"))).toBe(true); expect(r.classes.tampering.blocking).toContain("protected_surface_hash is NOT_RUN");
    for (const c of Object.values<any>(spec.classes)) { expect(c.required.length).toBeGreaterThan(0); for (const d of c.required) for (const k of ["id", "version", "severity", "automatic", "description"]) expect(d).toHaveProperty(k); }
  });
  it("a class closes only when every required detector passes with current evidence", () => {
    const spec = V.spec, cs = (evs: any[]) => I.classStatus(I.fold(evs, spec), "c1", NOW);
    expect([cs([PASS("d1")]), cs([PASS("d1"), PASS("d2")])]).toEqual(["OPEN", "CLOSED"]);   // d2 has not run
    expect(cs([PASS("d1"), PASS("d2"), ev("d1", "FAIL", "ann", "2026-10-21T09:00:00Z")])).toBe("OPEN"); expect(cs([PASS("d1"), PASS("d2"), ev("d1", "FAIL", "ann", "2026-10-21T09:00:00Z"), ev("d1", "OPEN", "ann", "2026-10-21T09:30:00Z")])).toBe("OPEN");
    expect(cs([PASS("d1"), PASS("d2"), ev("d2", "FAIL", "ann", "2026-10-21T09:00:00Z"), ev("d2", "FIXED_PENDING_VERIFICATION", "ann", "2026-10-21T09:10:00Z", { note: "patched" })])).toBe("OPEN");   // awaiting verification
    for (const status of ["NOT_RUN", "WAIVED", "NOT_APPLICABLE"]) expect(I.effective({ status, evidence: null, waiver: null, note: null, version: "1" }, NOW), status).not.toBe("pass");   // never equivalent to PASS
  });
  it("strict mode throws on a refused change and the default records it without changing anything", () => {
    expect(() => I.fold([ev("d1", "PASS")], V.spec, true)).toThrow(/needs an evidence reference/);
    const s = I.fold([ev("d1", "PASS")], V.spec); expect([s.rejected[0].detector_id, s.detectors.d1.status, s.detectors.d1.history]).toEqual(["d1", "NOT_RUN", []]);
    for (const [events, needle] of [[[ev("d2", "FAIL"), ev("d2", "FIXED_PENDING_VERIFICATION", "ann", undefined, { note: "x" }), PASS("d2", "ann")], "someone other than the person who made it"], [[PASS("d3", "ann", "1")], "version"], [[ev("d2", "NOT_APPLICABLE")], "needs the reason"],
      [[ev("d2", "FAIL", "ann", undefined, { owner: "ann" }), ev("d2", "WAIVED", "ann", undefined, { waiver: { approved_by: "ann", rationale: "x", expires_at: "2027-01-01T00:00:00Z" } })], "other than the owner"], [[ev("nope", "FAIL")], "unknown detector"]] as [any[], string][])
      expect(() => I.fold(events, V.spec, true), needle).toThrow(new RegExp(needle));
  });
  it("the history of every change is kept and a closed class still shows its findings", () => {
    const s = I.fold([ev("d1", "FAIL", "ann", "2026-10-21T08:00:00Z", { finding_id: "F-1", note: "leaked" }), ev("d1", "FIXED_PENDING_VERIFICATION", "ann", "2026-10-21T08:30:00Z", { note: "patched" }), PASS("d1", "bob", "1", "2026-10-21T09:00:00Z"), PASS("d2")], V.spec), h = s.detectors.d1.history;
    expect([h.map((x: any) => x.to), h[0].finding_id, I.classStatus(s, "c1", NOW), h.map((x: any) => x.by)]).toEqual([["FAIL", "FIXED_PENDING_VERIFICATION", "PASS"], "F-1", "CLOSED", ["ann", "ann", "bob"]]);
  });
});
describe("the command", () => {
  it("reports each class from the events in the log", async () => {
    const h = mk([oa("d")]), { run } = await h.createRun("u", "go"), db = h.store.path, cli = (...argv: string[]) => { const out: string[] = [], code = telemetryCommand("integrity", argv, (s) => out.push(s)); return { code, text: out.join("\n") }; };
    let r = cli("--db", db); expect([r.code, (r.text.match(/OPEN/g) ?? []).length >= 6, r.text.includes("NOT_RUN"), r.text.includes("CLOSED")]).toEqual([0, true, true, false]);
    await run.emit("integrity.detector_status", { detector_id: "feedback_byte_cap", status: "PASS", by: "ci", evidence_ref: "ev:1", evidence_version: "1", run_id: run.runId });
    await run.emit("integrity.detector_status", { detector_id: "protected_surface_hash", status: "PASS", by: "ci" });   // refused: no evidence
    const rep = JSON.parse(cli("--db", db, "--json").text); expect([rep.classes.over_granular_feedback.status, rep.classes.tampering.status, rep.rejected[0].detector_id]).toEqual(["CLOSED", "OPEN", "protected_surface_hash"]);
    expect([cli("--db", db).text.includes("rejected event 1"), cli("--db", db, "--max-age-s", "0").code]).toEqual([true, 0]);
  });
});
