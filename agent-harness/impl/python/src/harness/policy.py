"""Policy engine (design §11): closed rule schema, identical semantics in every implementation."""
from __future__ import annotations
import re
from typing import Any

_MISSING = object()
_BAD_REGEX = re.compile(r"\(\?[=!<]|\\[1-9]")  # lookaround / backrefs are outside the RE2 subset


def glob_match(pattern: str, text: str) -> bool:
    """`*` and `?` only; case-sensitive; `*` also matches `/`."""
    rx = "".join(".*" if c == "*" else "." if c == "?" else re.escape(c) for c in pattern)
    return re.fullmatch(rx, text, re.DOTALL) is not None


def parse_path(path: str) -> list[str | int]:
    if not path.startswith("$"):
        raise ValueError(f"path must start with $: {path}")
    out: list[str | int] = []
    for m in re.finditer(r"\.([A-Za-z_][A-Za-z0-9_\-]*)|\[(\d+)\]", path[1:]):
        out.append(m.group(1) if m.group(1) is not None else int(m.group(2)))
    if "$" + "".join(f".{x}" if isinstance(x, str) else f"[{x}]" for x in out) != path:
        raise ValueError(f"unsupported path syntax: {path}")
    return out


def resolve(doc: Any, path: str) -> Any:
    cur = doc
    for step in parse_path(path):
        if isinstance(step, int):
            if not isinstance(cur, list) or step >= len(cur):
                return _MISSING
            cur = cur[step]
        else:
            if not isinstance(cur, dict) or step not in cur:
                return _MISSING
            cur = cur[step]
    return cur


def _is_num(x) -> bool:
    return isinstance(x, (int, float)) and not isinstance(x, bool)


def deep_eq(a, b) -> bool:
    if isinstance(a, bool) or isinstance(b, bool):
        return isinstance(a, bool) and isinstance(b, bool) and a == b
    if _is_num(a) and _is_num(b):
        return a == b
    if isinstance(a, dict) and isinstance(b, dict):
        return a.keys() == b.keys() and all(deep_eq(a[k], b[k]) for k in a)
    if isinstance(a, list) and isinstance(b, list):
        return len(a) == len(b) and all(deep_eq(x, y) for x, y in zip(a, b))
    return type(a) is type(b) and a == b


def arg_matches(args: Any, cond: dict) -> bool:
    op, val = cond["op"], cond.get("value")
    got = resolve(args, cond["path"])
    if op == "exists":
        return (got is not _MISSING) == (True if val is None else bool(val))
    if got is _MISSING:
        return False
    if op == "eq":
        return deep_eq(got, val)
    if op == "ne":
        return not deep_eq(got, val)
    if op == "in":
        return isinstance(val, list) and any(deep_eq(got, v) for v in val)
    if op == "glob":
        return isinstance(got, str) and isinstance(val, str) and glob_match(val, got)
    if op == "regex":
        return isinstance(got, str) and isinstance(val, str) and re.search(val, got) is not None
    if op in ("lt", "lte", "gt", "gte"):
        if not (_is_num(got) and _is_num(val)):
            return False
        return {"lt": got < val, "lte": got <= val, "gt": got > val, "gte": got >= val}[op]
    return False


def validate_ruleset(rs: dict) -> list[str]:
    """Semantic checks beyond JSON Schema: paths parse, regexes are in the safe subset, ids unique."""
    errs, seen = [], set()
    for r in rs.get("rules", []):
        if r["id"] in seen:
            errs.append(f"duplicate rule id {r['id']}")
        seen.add(r["id"])
        for c in (r.get("match") or {}).get("args", []):
            try:
                parse_path(c["path"])
            except ValueError as e:
                errs.append(f"{r['id']}: {e}")
            if c["op"] == "regex":
                v = c.get("value")
                if not isinstance(v, str) or _BAD_REGEX.search(v):
                    errs.append(f"{r['id']}: regex outside RE2 subset")
                else:
                    try:
                        re.compile(v)
                    except re.error as e:
                        errs.append(f"{r['id']}: bad regex ({e})")
    return errs


def rule_matches(rule: dict, call: dict) -> bool:
    m = rule.get("match") or {}
    if "tool" in m and not any(glob_match(g, call["tool"]) for g in m["tool"]):
        return False
    if "effect_class" in m and call.get("effect_class") not in m["effect_class"]:
        return False
    if "profile" in m and call.get("profile") not in m["profile"]:
        return False
    if "actor_role" in m and call.get("actor_role") not in m["actor_role"]:
        return False
    return all(arg_matches(call.get("args", {}), c) for c in m.get("args", []))


def default_decision(effect_class: str | None, profile: str) -> str:
    if effect_class == "read_only":
        return "allow"
    if effect_class == "reversible_write":
        return "allow" if profile == "core" else "require_approval"
    return "require_approval"  # irreversible, external_facing, undeclared


def decide(ruleset: dict, call: dict) -> dict:
    """call = {tool, effect_class, profile, actor_role, args}."""
    matched = [r for r in ruleset.get("rules", []) if rule_matches(r, call)]
    if matched:
        for action in ("deny", "require_approval", "allow"):
            hit = [r for r in matched if r["action"] == action]
            if hit:
                return {"decision": action, "matched_rule_ids": [r["id"] for r in matched], "default_used": None,
                        "approval": hit[0].get("approval") if action == "require_approval" else None, "winning_rule_id": hit[0]["id"]}
    return {"decision": default_decision(call.get("effect_class"), call["profile"]), "matched_rule_ids": [],
            "default_used": call.get("effect_class") or "undeclared", "approval": None, "winning_rule_id": None}
