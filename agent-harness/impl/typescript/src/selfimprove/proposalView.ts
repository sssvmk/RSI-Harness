/** The proposer's sandbox view (§17.6): read-only, and containing *only* feedback-set artifacts (§17.4 Visibility). */
import fs from "node:fs";
import path from "node:path";
import { GitRepo } from "./gitrepo.js";
import { ComponentRegistry } from "./registry.js";
export function buildProposalView(dest: string, repo: GitRepo, baseCommit: string, registry: ComponentRegistry, tiersAllowed: string[], editsMaxLines: number, feedback: any, attempted: any[] = []): string {
  for (const sub of ["config", "patterns", "reports", "preserved", "attempted", "traces"]) fs.mkdirSync(path.join(dest, sub), { recursive: true });
  for (const rel of repo.git(["ls-tree", "-r", "--name-only", baseCommit]).split("\n")) {
    const text = repo.showFile(baseCommit, rel);
    if (text !== null) { const f = path.join(dest, "config", rel); fs.mkdirSync(path.dirname(f), { recursive: true }); fs.writeFileSync(f, text); }
  }
  fs.writeFileSync(path.join(dest, "patterns", "patterns.json"), JSON.stringify(feedback.patterns ?? [], null, 1));
  for (const [rid, text] of Object.entries<string>(feedback.reports ?? {})) fs.writeFileSync(path.join(dest, "reports", `${rid}.md`), text);
  for (const [rid, text] of Object.entries<string>(feedback.traces ?? {})) fs.writeFileSync(path.join(dest, "traces", `${rid}.jsonl`), text);
  fs.writeFileSync(path.join(dest, "preserved", "summary.json"), JSON.stringify(feedback.preserved ?? [], null, 1));   // summaries only
  fs.writeFileSync(path.join(dest, "attempted", "summary.json"), JSON.stringify(attempted.map((a) => ({ summary: a.summary, reason: a.reason })), null, 1));
  const paths = [...new Set(registry.entries.filter((e) => tiersAllowed.includes(e.tier)).map((e) => e.path))].sort();
  fs.writeFileSync(path.join(dest, "surface.md"), "# Editable surface\n\n" + paths.map((p) => `- \`${p}\``).join("\n") + `\n\nEdit cap: ${editsMaxLines} changed lines. Anything not listed is not editable.\n`);
  return dest;
}
