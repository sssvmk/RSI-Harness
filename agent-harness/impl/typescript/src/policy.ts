/** Policy engine (design §11): identical semantics to the Python implementation, enforced by shared vectors. */
const MISSING = Symbol("missing");
const BAD_REGEX = /\(\?[=!<]|\\[1-9]/;

export function globMatch(pattern: string, text: string): boolean {
  let rx = "";
  for (const c of pattern) rx += c === "*" ? ".*" : c === "?" ? "." : c.replace(/[.*+?^${}()|[\]\\\/]/g, "\\$&");
  return new RegExp(`^(?:${rx})$`, "su").test(text);
}
export function parsePath(path: string): (string | number)[] {
  if (!path.startsWith("$")) throw new Error(`path must start with $: ${path}`);
  const out: (string | number)[] = [];
  for (const m of path.slice(1).matchAll(/\.([A-Za-z_][A-Za-z0-9_\-]*)|\[(\d+)\]/g)) out.push(m[1] !== undefined ? m[1] : Number(m[2]));
  const back = "$" + out.map((x) => (typeof x === "string" ? `.${x}` : `[${x}]`)).join("");
  if (back !== path) throw new Error(`unsupported path syntax: ${path}`);
  return out;
}
export function resolvePath(doc: any, path: string): any {
  let cur = doc;
  for (const step of parsePath(path)) {
    if (typeof step === "number") { if (!Array.isArray(cur) || step >= cur.length) return MISSING; cur = cur[step]; }
    else { if (cur === null || typeof cur !== "object" || Array.isArray(cur) || !(step in cur)) return MISSING; cur = cur[step]; }
  }
  return cur;
}
const isNum = (x: any) => typeof x === "number";
export function deepEq(a: any, b: any): boolean {
  if (typeof a === "boolean" || typeof b === "boolean") return a === b;
  if (isNum(a) && isNum(b)) return a === b;
  if (a === null || b === null) return a === b;
  if (Array.isArray(a) || Array.isArray(b)) return Array.isArray(a) && Array.isArray(b) && a.length === b.length && a.every((x, i) => deepEq(x, b[i]));
  if (typeof a === "object" && typeof b === "object") { const ka = Object.keys(a), kb = Object.keys(b); return ka.length === kb.length && ka.every((k) => k in b && deepEq(a[k], b[k])); }
  return typeof a === typeof b && a === b;
}
export function argMatches(args: any, cond: any): boolean {
  const { op, value: val } = cond;
  const got = resolvePath(args, cond.path);
  if (op === "exists") return (got !== MISSING) === (val === undefined || val === null ? true : Boolean(val));
  if (got === MISSING) return false;
  switch (op) {
    case "eq": return deepEq(got, val);
    case "ne": return !deepEq(got, val);
    case "in": return Array.isArray(val) && val.some((v) => deepEq(got, v));
    case "glob": return typeof got === "string" && typeof val === "string" && globMatch(val, got);
    case "regex": return typeof got === "string" && typeof val === "string" && new RegExp(val, "u").test(got);
    case "lt": case "lte": case "gt": case "gte": {
      if (!(isNum(got) && isNum(val))) return false;
      return op === "lt" ? got < val : op === "lte" ? got <= val : op === "gt" ? got > val : got >= val;
    }
  }
  return false;
}
export function validateRuleset(rs: any): string[] {
  const errs: string[] = [], seen = new Set<string>();
  for (const r of rs.rules ?? []) {
    if (seen.has(r.id)) errs.push(`duplicate rule id ${r.id}`);
    seen.add(r.id);
    for (const c of r.match?.args ?? []) {
      try { parsePath(c.path); } catch (e: any) { errs.push(`${r.id}: ${e.message}`); }
      if (c.op === "regex") {
        if (typeof c.value !== "string" || BAD_REGEX.test(c.value)) errs.push(`${r.id}: regex outside RE2 subset`);
        else { try { new RegExp(c.value, "u"); } catch (e: any) { errs.push(`${r.id}: bad regex (${e.message})`); } }
      }
    }
  }
  return errs;
}
export function ruleMatches(rule: any, call: any): boolean {
  const m = rule.match ?? {};
  if (m.tool && !m.tool.some((g: string) => globMatch(g, call.tool))) return false;
  if (m.effect_class && !m.effect_class.includes(call.effect_class ?? null)) return false;
  if (m.profile && !m.profile.includes(call.profile)) return false;
  if (m.actor_role && !m.actor_role.includes(call.actor_role)) return false;
  return (m.args ?? []).every((c: any) => argMatches(call.args ?? {}, c));
}
export function defaultDecision(effect: string | null | undefined, profile: string): string {
  if (effect === "read_only") return "allow";
  if (effect === "reversible_write") return profile === "core" ? "allow" : "require_approval";
  return "require_approval";
}
export function decide(ruleset: any, call: any) {
  const matched = (ruleset.rules ?? []).filter((r: any) => ruleMatches(r, call));
  if (matched.length) {
    for (const action of ["deny", "require_approval", "allow"]) {
      const hit = matched.filter((r: any) => r.action === action);
      if (hit.length) return { decision: action, matched_rule_ids: matched.map((r: any) => r.id), default_used: null as string | null,
        approval: action === "require_approval" ? hit[0].approval ?? null : null, winning_rule_id: hit[0].id as string | null };
    }
  }
  return { decision: defaultDecision(call.effect_class, call.profile), matched_rule_ids: [] as string[], default_used: (call.effect_class ?? "undeclared") as string | null, approval: null, winning_rule_id: null as string | null };
}
