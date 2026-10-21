"""Static anti-gaming screens (§17.7): declarative rules in spec/screens.json with shared vectors."""
from __future__ import annotations
import hashlib, json, re
from pathlib import Path
import yaml

RULES_PATH = Path(__file__).resolve().parents[5] / "spec" / "screens.json"


def load_rules(path: Path | None = None) -> dict:
    return json.loads((path or RULES_PATH).read_text())


def _rx(p: dict) -> re.Pattern:
    return re.compile(p["re"], re.I if "i" in p.get("flags", "") else 0)


def tokens(text: str) -> list[str]:
    return re.findall(r"[a-z0-9]+", text.lower())


def ngram_hashes(text: str, n: int) -> set[str]:
    t = tokens(text)
    return {hashlib.sha256(" ".join(t[i:i + n]).encode()).hexdigest() for i in range(len(t) - n + 1)}


def build_answer_index(answers: list[str], n: int = 5) -> list[str]:
    """Control-plane side: only hashes ever leave it. (Answers shorter than n tokens cannot be indexed.)"""
    out: set[str] = set()
    for a in answers:
        out |= ngram_hashes(a, n)
    return sorted(out)


def _front_matter_or_doc(path: str, text: str | None):
    if text is None:
        return {}
    try:
        if path.endswith((".yaml", ".yml", ".json")):
            return yaml.safe_load(text) or {}
        if path.endswith(".md") and text.startswith("---\n"):
            end = text.find("\n---", 4)
            return (yaml.safe_load(text[4:end]) or {}) if end > 0 else {}
    except yaml.YAMLError:
        return None
    return {}


def _t4_entries(node, keys: set[str], prefix="") -> dict:
    out: dict = {}
    if isinstance(node, dict):
        for k, v in node.items():
            kp = f"{prefix}.{k}" if prefix else str(k)
            if str(k).lower() in keys:
                out[kp] = json.dumps(v, sort_keys=True, default=str)
            out.update(_t4_entries(v, keys, kp))
    elif isinstance(node, list):
        for i, v in enumerate(node):
            out.update(_t4_entries(v, keys, f"{prefix}[{i}]"))
    return out


def run_screens(edits: list[dict], answer_index: list[str], instance_ids: list[str], rules: dict | None = None) -> list[dict]:
    """edits: [{path, added, before, after}]. Returns violations ordered by screen (a..f); empty means clean."""
    R = rules or load_rules()
    idx, ids, v = set(answer_index), [i for i in instance_ids if i], []
    keys = {k.lower() for k in R["t4_keys"]}
    for e in edits:
        path, added = e["path"], e.get("added") or ""
        if idx and ngram_hashes(added, R["ngram_n"]) & idx:
            v.append({"screen": "answer_echo", "path": path, "detail": "edit contains a span matching a known answer"})
    for e in edits:
        lines = [l.strip() for l in (e.get("added") or "").split("\n") if l.strip()]
        w, seen = R["duplicate_block"]["window_lines"], {}
        for i in range(len(lines) - w + 1):
            blk = "\n".join(lines[i:i + w])
            if len(blk) >= R["duplicate_block"]["min_chars"]:
                seen.setdefault(blk, []).append(i)
        if any(len(ix) >= 2 for ix in seen.values()):
            v.append({"screen": "duplicate_blocks", "path": e["path"], "detail": "a block of lines is repeated"})
    for e in edits:
        if any(_rx(p).search(e.get("added") or "") for p in R["output_interface"]):
            v.append({"screen": "output_interface", "path": e["path"], "detail": "edit writes to a score or report path"})
    for e in edits:
        hit = next((p["re"] for p in R["protected_refs"] if _rx(p).search(e.get("added") or "")), None)
        if hit:
            v.append({"screen": "protected_refs", "path": e["path"], "detail": f"references a protected location ({hit})"})
    for e in edits:
        t = e.get("added") or ""
        if any(i in t for i in ids) or any(_rx(p).search(t) for p in R["hardcoded_seed"]):
            v.append({"screen": "hardcoded_ids", "path": e["path"], "detail": "hard-coded instance id or seed"})
    for e in edits:
        b, a = _front_matter_or_doc(e["path"], e.get("before")), _front_matter_or_doc(e["path"], e.get("after"))
        if b is None or a is None:
            continue
        eb, ea = _t4_entries(b, keys), _t4_entries(a, keys)
        changed = sorted(k for k in set(eb) | set(ea) if eb.get(k) != ea.get(k))
        if changed:
            v.append({"screen": "t4_keys", "path": e["path"], "detail": "changes T4-owned keys: " + ", ".join(changed)})
    return v
