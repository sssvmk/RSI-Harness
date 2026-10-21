"""Component registry (Appendix A.7) and PathPolicy (§17.2.3 layer 2)."""
from __future__ import annotations
import posixpath, re
from dataclasses import dataclass
import yaml

TIERS = ["T0", "T1", "T2", "T3", "T4"]
COMPONENTS = {"system_prompt", "tool_description", "tool_implementation", "middleware", "skill", "subagent_config", "long_term_memory", "program", "playbook",
              "workflow", "context", "retrieval_config"}
PROTECTED = {"components.yaml"}          # the registry itself is never editable by the loop: it would let the loop register its own paths


def tier_rank(t: str) -> int:
    return TIERS.index(t)


def glob_to_regex(pattern: str) -> re.Pattern:
    """`**` crosses directories, `*` and `?` do not; everything else is literal."""
    out, i = "", 0
    while i < len(pattern):
        c = pattern[i]
        if pattern.startswith("**/", i):
            out += "(?:.*/)?"; i += 3; continue
        if pattern.startswith("**", i):
            out += ".*"; i += 2; continue
        out += "[^/]*" if c == "*" else "[^/]" if c == "?" else re.escape(c)
        i += 1
    return re.compile("^" + out + "$", re.DOTALL)


def normalize_path(path: str) -> tuple[str | None, str | None]:
    """Returns (normalized, None) or (None, reason). Relative, forward-slash, no `..`, no NUL."""
    if not isinstance(path, str) or not path or "\0" in path or "\\" in path:
        return None, "path.invalid"
    if path.startswith("/"):
        return None, "path.absolute"
    parts = path.split("/")
    if ".." in parts:
        return None, "path.traversal"
    norm = posixpath.normpath(path)
    if norm in (".", "") or norm.endswith("/") or path.endswith("/"):
        return None, "path.invalid"
    return norm, None


@dataclass
class Entry:
    path: str
    component: str
    tier: str
    owner: str
    origin: str
    raw: dict
    rx: re.Pattern


class RegistryError(ValueError):
    pass


class ComponentRegistry:
    def __init__(self, entries: list[Entry], schema_version: int = 1):
        self.entries, self.schema_version = entries, schema_version

    @classmethod
    def from_dict(cls, doc: dict) -> "ComponentRegistry":
        if doc.get("schema_version") != 1:
            raise RegistryError("schema_version must be 1")
        entries = []
        for e in doc.get("components", []):
            for k in ("path", "component", "tier", "owner", "origin"):
                if k not in e:
                    raise RegistryError(f"entry missing required field {k}: {e}")
            if e["tier"] not in ("T0", "T1", "T2"):
                raise RegistryError(f"registry tiers are T0-T2 only (got {e['tier']} for {e['path']})")
            if e["origin"] not in ("human", "learned"):
                raise RegistryError(f"origin must be human|learned ({e['path']})")
            if e["component"] not in COMPONENTS:
                raise RegistryError(f"unknown component {e['component']!r} ({e['path']})")
            entries.append(Entry(e["path"], e["component"], e["tier"], e["owner"], e["origin"], e, glob_to_regex(e["path"])))
        return cls(entries, doc["schema_version"])

    @classmethod
    def from_yaml(cls, text: str) -> "ComponentRegistry":
        return cls.from_dict(yaml.safe_load(text))

    def entry_for(self, path: str) -> Entry | None:
        """Longest matching entry (by pattern length); ties go to the earlier entry. Unregistered => None."""
        norm, err = normalize_path(path)
        if err or norm in PROTECTED:
            return None
        best = None
        for e in self.entries:
            if e.rx.match(norm) and (best is None or len(e.path) > len(best.path)):
                best = e
        return best

    def tier_of(self, path: str) -> str | None:
        e = self.entry_for(path)
        return e.tier if e else None

    def check(self, path: str) -> tuple[str | None, str | None]:
        """(tier, None) if editable in principle, else (None, reason)."""
        norm, err = normalize_path(path)
        if err:
            return None, err
        if norm in PROTECTED:
            return None, "path.protected"
        e = self.entry_for(path)
        return (e.tier, None) if e else (None, "path.unregistered")

    def component_of(self, path: str) -> str | None:
        e = self.entry_for(path)
        return e.component if e else None
