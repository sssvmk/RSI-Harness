/** Key types (Definition 29) and the stated equivalence per resource class (PRD: Stated Equivalence). */
export class KeyErr extends Error { name = "KeyError"; }
export class KeyType {
  constructor(public ops: Record<string, (v: any, arg: any) => [any, (v: any) => any, any]> = {}, public equiv: (a: any, b: any) => boolean = (a, b) => JSON.stringify(a) === JSON.stringify(b), public commutative = false, public resourceClass = "opaque") {}
}
export const tableKey = () => new KeyType({
  add: (table: Record<string, any>, [eid, entry]: [string, any]) => {
    if (eid in table) throw new KeyErr(`entry '${eid}' already registered`);
    return [{ ...table, [eid]: entry }, (v: Record<string, any>) => { const o = { ...v }; delete o[eid]; return o; }, eid];
  } }, undefined, true, "table");
export const chainKey = () => new KeyType({ push: (chain: any[], item: any) => [[...chain, item], (v: any[]) => v.slice(0, -1), chain.length + 1] }, undefined, false, "chain");
export const settingKey = () => new KeyType({ set: (v: any, nw: any) => [nw, () => v, null] }, undefined, false, "opaque");
export const counterKey = () => new KeyType({ incr: (v: number, n: number) => [v + n, (x: number) => x - n, null] }, undefined, true, "counter");
export const workspaceEquiv = (a: Record<string, any>, b: Record<string, any>) => {
  const strip = (w: Record<string, any>) => JSON.stringify(Object.keys(w).sort().map((p) => [p, w[p].content, w[p].mode])); return strip(a) === strip(b);
};
export const workspaceKey = () => new KeyType({
  write: (ws: Record<string, any>, [path, content, mode]: [string, string, number]) => {
    const old = ws[path], nw = { ...ws, [path]: { content, mode, mtime: 1 + (old ? old.mtime : 0), inode: Object.keys(ws).length + 1 } };
    const undo = (v: Record<string, any>) => { const out = { ...v }; if (old === undefined) delete out[path]; else out[path] = { ...old, mtime: v[path].mtime + 1, inode: old.inode + 100 }; return out; };
    return [nw, undo, null];
  } }, workspaceEquiv, false, "workspace");
/** A service broker (paper §6.2): providers register through revertible effects, so unloading one drops it from the routing set. */
export const brokerKey = () => new KeyType({
  register: (table: Record<string, any>, [pid, entry]: [string, any]) => { if (pid in table) throw new KeyErr(`provider '${pid}' already registered`); return [{ ...table, [pid]: { ...entry } }, (v: Record<string, any>) => { const o = { ...v }; delete o[pid]; return o; }, pid]; },
  set_weight: (table: Record<string, any>, [pid, w]: [string, number]) => { if (!(pid in table)) throw new KeyErr(`provider '${pid}' is not registered`); const old = table[pid].weight; return [{ ...table, [pid]: { ...table[pid], weight: w } }, (v: Record<string, any>) => (pid in v ? { ...v, [pid]: { ...v[pid], weight: old } } : v), old]; },
}, undefined, false, "table");
/** Request dispatch over a broker table: an explicit target, round-robin, or weighted (deterministic). */
export class Broker {
  n = 0; constructor(public policy = "round_robin") {}
  pick(table: Record<string, any>, target: string | null = null): string {
    const ids = Object.keys(table).filter((k) => (table[k].weight ?? 1) > 0).sort();
    if (target !== null) { if (!(target in table)) throw new KeyErr(`provider '${target}' is not registered`); return target; }
    if (!ids.length) throw new Error("no provider is registered");
    this.n += 1; if (this.policy === "round_robin") return ids[(this.n - 1) % ids.length];
    const total = ids.reduce((a, i) => a + table[i].weight, 0); let point = (this.n - 1) % total;
    for (const i of ids) { if (point < table[i].weight) return i; point -= table[i].weight; }
    return ids[ids.length - 1];
  }
}
export const STATED_EQUIVALENCE: Record<string, string> = {
  table: "entries by id and value; insertion order is not observable", counter: "the integer value", chain: "the ordered sequence of items",
  workspace: "files by path, content and mode; inode and modification time are not observable", opaque: "structural equality" };
