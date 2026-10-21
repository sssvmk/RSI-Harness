/** Declared test visibility (design §13.1, PRD §3.6). Mirrors eval/visibility.py: policy (purpose, held-out use, generalization claim) is kept apart from verified runtime state, and public-test results
 *  never authorize a generalization claim. */
import crypto from "node:crypto";
import fs from "node:fs";
import path from "node:path";

export function declarationFor(spec: any) {
  if (spec.test_visibility != null) { const d = spec.test_visibility; return { purpose: d.purpose, held_out_use: d.held_out_use, generalization_claim: d.generalization_claim, public_tests: { mount_mode: "read_only" } }; }
  const pub = (spec.visibility ?? "hidden") === "public";
  return { purpose: pub ? "development_only" : "evaluation", held_out_use: !pub && spec.confirmation?.mode === "heldout_set", generalization_claim: false, public_tests: { mount_mode: "read_only" } };
}
/** Contradictory declarations are rejected by `harness task validate` and again at run start. */
export function validateDeclaration(spec: any): string[] {
  const d = declarationFor(spec), pub = (spec.visibility ?? "hidden") === "public", errs: string[] = [];
  if (d.purpose === "development_only" && d.held_out_use) errs.push("test_visibility: development_only tests cannot be used as held-out data (held_out_use must be false)");
  if (pub && d.held_out_use) errs.push("test_visibility: public tests cannot be used as held-out data");
  if (pub && d.generalization_claim) errs.push("test_visibility: public-test results cannot authorize a generalization claim (generalization_claim must be false)");
  if (pub && d.purpose !== "development_only") errs.push("test_visibility: public tests are development aids and must declare purpose development_only");
  if (!pub && d.purpose === "development_only" && spec.test_visibility != null && d.generalization_claim) errs.push("test_visibility: development_only tests cannot support a generalization claim");
  return errs;
}
const walk = (dir: string, rel: string[] = []): string[][] => fs.readdirSync(path.join(dir, ...rel), { withFileTypes: true }).flatMap((e) => e.isDirectory() ? walk(dir, [...rel, e.name]) : [[...rel, e.name]]);
const byComponents = (a: string[], b: string[]) => { for (let i = 0; i < Math.min(a.length, b.length); i++) if (a[i] !== b[i]) return a[i] < b[i] ? -1 : 1; return a.length - b.length; };
/** The grader's identity: every file under tests/, by relative path and content. */
export function testsHash(testsDir: string): string {
  const h = crypto.createHash("sha256");
  for (const parts of walk(testsDir).sort(byComponents)) h.update(Buffer.concat([Buffer.from(parts.join("/")), Buffer.from([0]), crypto.createHash("sha256").update(fs.readFileSync(path.join(testsDir, ...parts))).digest()]));
  return h.digest("hex");
}
/** What a public task shows the agent: its tests, but never the held-out confirmation set. */
export const publicFiles = (testsDir: string): string[] => walk(testsDir).filter((p) => p[0] !== "confirm").sort(byComponents).map((p) => p.join("/"));
/** Mount a public task's tests into the agent workspace, read-only, and verify what happened (the verified state, not the intent). On the development tier read-only is a file permission that the agent's
 *  own identity can change, so `read_only_enforced` is false there and that is said plainly. */
export function mountPublicTests(testsDir: string, executor: any) {
  const ids = publicFiles(testsDir);
  for (const rel of ids) { const v = `/workspace/tests/${rel}`; executor.fsWrite(v, fs.readFileSync(path.join(testsDir, rel), "utf8")); fs.chmodSync(executor.resolve(v), 0o444); }
  const mounted = ids.length > 0 && ids.every((rel) => { const p = executor.resolve(`/workspace/tests/${rel}`); return fs.statSync(p).isFile() && (fs.statSync(p).mode & 0o222) === 0; });   // the permission bits, which hold even for a privileged process
  return { available: ids.length > 0, mounted_in_agent_workspace: mounted, mount_mode: "read_only", read_only_enforced: false, test_ids: ids };
}
export const hiddenState = () => ({ available: false, mounted_in_agent_workspace: false, mount_mode: "read_only", read_only_enforced: false, test_ids: [] as string[] });
