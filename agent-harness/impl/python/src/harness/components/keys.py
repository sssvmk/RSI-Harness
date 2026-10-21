"""Key types (Definition 29: a value type plus the operations a holder may perform) and the stated equivalences per resource class (PRD: Stated Equivalence)."""
from __future__ import annotations
from typing import Any, Callable


class KeyType:
    """ops: name -> fn(value, arg) -> (new_value, inverse, outcome); `inverse(value) -> value` reverts the op at the state it produced (the witness g(d) = g).
    equiv: the observational equivalence the key's operations respect (Lemma 32). commutative: the Definition 46 witness, supplied by the key's provider."""
    def __init__(self, ops: dict[str, Callable] | None = None, equiv: Callable[[Any, Any], bool] | None = None, commutative: bool = False, resource_class: str = "opaque"):
        self.ops, self.equiv, self.commutative, self.resource_class = ops or {}, equiv or (lambda a, b: a == b), commutative, resource_class


def _table_add(table: dict, arg):
    eid, entry = arg
    if eid in table:
        raise KeyError(f"entry {eid!r} already registered")
    new = {**table, eid: entry}
    return new, (lambda v, _e=eid: {k: x for k, x in v.items() if k != _e}), eid


def table_key() -> KeyType:
    """A table of entries: each registration takes an entry of its own and its inverse removes that entry, so registrations commute (paper §3.4.2)."""
    return KeyType({"add": _table_add}, commutative=True, resource_class="table")


def chain_key() -> KeyType:
    """An ordered chain (e.g. middleware): NOT commutative, the order has to be imposed from outside (paper §3.4.2)."""
    def push(chain: tuple, item):
        new = (*chain, item)
        return new, (lambda v: v[:-1]), len(new)
    return KeyType({"push": push}, commutative=False, resource_class="chain")


def setting_key() -> KeyType:
    """A single setting: `set` returns an inverse that restores the previous value. NOT commutative, and reverting out of LIFO order leaves the wrong value."""
    def set_(v, new):
        return new, (lambda _x, _old=v: _old), None
    return KeyType({"set": set_}, commutative=False, resource_class="opaque")


def counter_key() -> KeyType:
    """A counter whose operations publish no outcome, so increments commute."""
    def incr(v: int, n):
        return v + n, (lambda x, _n=n: x - _n), None
    return KeyType({"incr": incr}, commutative=True, resource_class="counter")


def workspace_equiv(a: dict, b: dict) -> bool:
    """Workspace class: files match by path, content and mode. Inode and modification time are NOT observable (stated equivalence)."""
    strip = lambda w: {p: (f["content"], f["mode"]) for p, f in w.items()}
    return strip(a) == strip(b)


def workspace_key() -> KeyType:
    def write(ws: dict, arg):
        path, content, mode = arg
        old = ws.get(path)
        new = {**ws, path: {"content": content, "mode": mode, "mtime": 1 + (old["mtime"] if old else 0), "inode": len(ws) + 1}}
        def undo(v, _p=path, _old=old):
            out = dict(v)
            if _old is None:
                out.pop(_p, None)
            else:
                out[_p] = {**_old, "mtime": v[_p]["mtime"] + 1, "inode": _old["inode"] + 100}   # the revert does not restore mtime or inode, only content and mode
            return out
        return new, undo, None
    return KeyType({"write": write}, equiv=workspace_equiv, commutative=False, resource_class="workspace")


def broker_key() -> KeyType:
    """A service broker (paper §6.2): providers register through revertible effects, so unloading one drops it from the routing set. Registrations commute;
    `set_weight` (rolling updates) does not, so shifting traffic is done by the one orchestrator that owns the policy."""
    def register(table: dict, arg):
        pid, entry = arg
        if pid in table: raise KeyError(f"provider {pid!r} already registered")
        return {**table, pid: dict(entry)}, (lambda v, _p=pid: {k: x for k, x in v.items() if k != _p}), pid
    def set_weight(table: dict, arg):
        pid, w = arg
        if pid not in table: raise KeyError(f"provider {pid!r} is not registered")
        old = table[pid]["weight"]
        return {**table, pid: {**table[pid], "weight": w}}, (lambda v, _p=pid, _o=old: {**v, _p: {**v[_p], "weight": _o}} if _p in v else v), old
    return KeyType({"register": register, "set_weight": set_weight}, commutative=False, resource_class="table")


class Broker:
    """Request dispatch over a broker table: an explicit target, round-robin, or weighted (deterministic: the n-th request goes to the provider owning point n of the cumulative weights)."""
    def __init__(self, policy: str = "round_robin"):
        self.policy, self.n = policy, 0

    def pick(self, table: dict, target: str | None = None) -> str:
        ids = sorted(k for k, v in table.items() if v.get("weight", 1) > 0)
        if target is not None:
            if target not in table: raise KeyError(f"provider {target!r} is not registered")
            return target
        if not ids: raise LookupError("no provider is registered")
        self.n += 1
        if self.policy == "round_robin": return ids[(self.n - 1) % len(ids)]
        total = sum(table[i]["weight"] for i in ids); point = (self.n - 1) % total
        for i in ids:
            if point < table[i]["weight"]: return i
            point -= table[i]["weight"]
        return ids[-1]


# The harness states the equivalence per resource class and tests against it (tests/test_components.py).
STATED_EQUIVALENCE = {
    "table": "entries by id and value; insertion order is not observable",
    "counter": "the integer value",
    "chain": "the ordered sequence of items",
    "workspace": "files by path, content and mode; inode and modification time are not observable",
    "opaque": "structural equality",
}
