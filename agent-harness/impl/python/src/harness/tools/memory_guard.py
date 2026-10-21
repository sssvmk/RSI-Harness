"""MemoryGuard (PRD §4.3 Memory Write-Path Integrity, design §11.6): provenance tagging, quarantine of untrusted writes, refusal of behavior-changing paths.
The write path is configurable by the administrator (`memory.write_path` in the harness configuration): which sources are low-trust, whether their writes must be confirmed,
and an optional contextual-integrity screen of regular expressions that must not appear in content to be remembered."""
from __future__ import annotations
import re
from ..canonical import sha256_hex

BEHAVIOR_PREFIXES = ("prompts/", "tools/", "skills/", "memory/", "subagents/", "middleware/", "context/")
DEFAULT_WRITE_POLICY = {"low_trust_sources": ["email", "web", "advertisement", "third_party", "subagent"], "confirmation": "required", "screen_patterns": []}
_KEYS = set(DEFAULT_WRITE_POLICY)


def resolve_write_policy(cfg: dict | None) -> dict:
    over = cfg or {}
    if set(over) - _KEYS: raise ValueError(f"memory.write_path: unknown setting(s) {sorted(set(over) - _KEYS)}")
    p = {**DEFAULT_WRITE_POLICY, **over}
    if not isinstance(p["low_trust_sources"], list) or not all(isinstance(x, str) for x in p["low_trust_sources"]): raise ValueError("memory.write_path.low_trust_sources must be a list of strings")
    if p["confirmation"] not in ("required", "none"): raise ValueError("memory.write_path.confirmation must be 'required' or 'none'")
    if not isinstance(p["screen_patterns"], list): raise ValueError("memory.write_path.screen_patterns must be a list")
    norm = []
    for pat in p["screen_patterns"]:                                          # a string (case-sensitive) or {re, flags} with flags "i" for case-insensitive: portable across implementations
        d = {"re": pat, "flags": ""} if isinstance(pat, str) else dict(pat) if isinstance(pat, dict) and set(pat) <= {"re", "flags"} and isinstance(pat.get("re"), str) else None
        if d is None or d.get("flags", "") not in ("", "i"): raise ValueError(f"memory.write_path.screen_patterns: {pat!r} must be a string or {{re, flags}} with flags '' or 'i'")
        d.setdefault("flags", "")
        try: re.compile(d["re"])
        except re.error as e: raise ValueError(f"memory.write_path.screen_patterns: {d['re']!r} is not a valid expression ({e})")
        norm.append(d)
    p["screen_patterns"] = norm
    return p


def decide_write(policy: dict, path: str, provenance: dict, content: str) -> dict:
    """The decision alone, without side effects: {status: write|refused|quarantined, reason}. Shared by both implementations and checked by vector."""
    if path.lstrip("/").startswith(BEHAVIOR_PREFIXES):
        return {"status": "refused", "reason": "behavior-changing path must go through the self-improvement gate"}
    src = provenance.get("source")
    if policy["confirmation"] == "required" and src in policy["low_trust_sources"] and not provenance.get("confirmed_by"):
        return {"status": "quarantined", "reason": f"low-trust source {src!r} needs confirmation"}
    hit = next((pat for pat in policy["screen_patterns"] if re.search(pat["re"], content, re.I if "i" in pat["flags"] else 0)), None)
    if hit is not None:
        return {"status": "quarantined", "reason": "contextual-integrity screen", "pattern": hit["re"]}
    if provenance.get("trust") == "untrusted":
        return {"status": "quarantined", "reason": "untrusted provenance"}
    return {"status": "write", "reason": None}


class MemoryGuard:
    def __init__(self, svc, policy: dict | None = None):
        self.svc, self.policy_error = svc, None
        if policy is not None:
            self.policy = resolve_write_policy(policy)                         # an explicit policy that is invalid is a programming error
        else:
            try:
                self.policy = resolve_write_policy(((getattr(svc, "config", None) or {}).get("memory") or {}).get("write_path"))
            except ValueError as e:                                             # a bad configuration is reported by preflight (PF-U02) and meanwhile the safe default applies
                self.policy, self.policy_error = resolve_write_policy(None), str(e)

    async def write(self, run, path: str, content: str, provenance: dict, sandbox_writer=None) -> dict:
        h = self.svc.blobs.put(content.encode("utf-8"))
        d = decide_write(self.policy, path, provenance, content)
        if d["status"] == "refused":
            await run.emit("memory.quarantined", {"path": path, "provenance": provenance, "trust": provenance.get("trust"), "reason": d["reason"]})
            return {"status": "refused", "reason": "requires self-improvement gate (§17)"}
        if d["status"] == "quarantined":
            await run.emit("memory.quarantined", {"path": path, "provenance": provenance, "trust": provenance.get("trust"), "reason": d["reason"], "content_hash": h,
                                                  **({"pattern": d["pattern"]} if "pattern" in d else {})})
            return {"status": "quarantined", "content_hash": h, "reason": d["reason"]}
        if sandbox_writer:
            await sandbox_writer()
        await run.emit("memory.written", {"path": path, "provenance": provenance, "trust": provenance.get("trust"), "content_hash": h})
        return {"status": "written", "content_hash": h}
