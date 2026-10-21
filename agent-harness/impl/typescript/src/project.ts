/** Project identification (design §7.3.10). The harness never infers whether a run belongs to a project. When project memory is enabled, a run cannot start until the user has answered, either by supplying the answer or by being
 *  asked, and the answer (a project name, or none) is recorded in the run manifest. Mirrors project.py. */
export const NAME_RE = /^[A-Za-z0-9][A-Za-z0-9 ._\-]{0,62}$/;
const NONE_WORDS = new Set(["", "none", "no", "n", "no project", "not a project", "not part of a project"]);
export const QUESTION = "Is this work part of a project? Enter the project's name, or 'none' if it is not.";
export const UNSET: unique symbol = Symbol("unset");   // the caller said nothing about the project, which is different from saying "none"
export class ProjectError extends Error {}
/** Project memory is enabled and the user has not said whether this run belongs to a project. */
export class ProjectNotDeclared extends ProjectError {}
export function parseAnswer(text: string): string | null {
  const t = (text ?? "").trim(); if (NONE_WORDS.has(t.toLowerCase())) return null;
  if (!NAME_RE.test(t)) throw new ProjectError(`'${t}' is not a project name: use letters, digits, spaces, dots, dashes and underscores, starting with a letter or digit (at most 63 characters), or answer 'none'`);
  return t;
}
export const enabled = (config: any): boolean => Boolean(config?.memory?.project?.enabled ?? false);
/** The project declaration for a run, or null when project memory is off and nothing was said. A project the caller names is the user's own answer; otherwise the user is asked; otherwise the run is refused. */
export async function declare(config: any, project: any = UNSET, ask?: (q: string) => string | Promise<string>) {
  if (project !== UNSET) return { id: project === null ? null : parseAnswer(String(project)), declared_by: "user", asked: false };
  if (ask) return { id: parseAnswer(await ask(QUESTION)), declared_by: "user", asked: true };
  if (enabled(config)) throw new ProjectNotDeclared("project memory is enabled, so the user must say whether this run is part of a project (pass project=<name> or project=null, or a function that asks); the harness does not guess");
  return null;
}
