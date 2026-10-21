"""Tool manifests and registry (design §9.1, §9.3, §9.5)."""
from __future__ import annotations
import json
from dataclasses import dataclass
from pathlib import Path
from typing import Any, Awaitable, Callable
import jsonschema
from ..canonical import canonicalize, sha256_hex
from ..policy import glob_match

SCHEMA_DIR = Path(__file__).resolve().parents[5] / "spec" / "schemas"


def load_schema(name: str) -> dict:
    return json.loads((SCHEMA_DIR / name).read_text())


class ManifestError(Exception):
    pass


def manifest_hash(m: dict) -> str:
    return sha256_hex(canonicalize({k: v for k, v in m.items() if k != "content_hash"}))


@dataclass
class Tool:
    manifest: dict
    handler: Callable[..., Awaitable[Any]]
    reconcile: Callable[..., Awaitable[Any]] | None = None  # for non_idempotent tools: decide what happened after a crash


class ToolRegistry:
    def __init__(self):
        self._schema = load_schema("tool.manifest.v1.json")
        self.tools: dict[str, Tool] = {}
        self.warnings: list[dict] = []

    def register(self, manifest: dict, handler, reconcile=None) -> dict:
        m = dict(manifest)
        try:
            jsonschema.validate(m, self._schema)
            jsonschema.Draft202012Validator.check_schema(m["input_schema"])
        except jsonschema.SchemaError as e:
            raise ManifestError(f"{m.get('name')}: invalid input_schema: {e.message}")
        except jsonschema.ValidationError as e:
            raise ManifestError(f"{m.get('name')}: {e.message}")
        if "idempotency" not in m:  # defaults per §9.1, with warning events
            m["idempotency"] = "non_idempotent"
            self.warnings.append({"tool": m["name"], "field": "idempotency", "default": "non_idempotent"})
        if "effect_class" not in m:
            m["effect_class"] = None  # undeclared: policy requires approval
            self.warnings.append({"tool": m["name"], "field": "effect_class", "default": "requires_approval"})
        m.setdefault("result_cap_tokens", 4000)
        m.setdefault("timeout_s", 120)
        m.setdefault("parallel_safe", False)
        declared = m.get("content_hash")
        m["content_hash"] = manifest_hash(m)
        if declared and declared != m["content_hash"]:
            raise ManifestError(f"{m['name']}: content_hash mismatch")
        self.tools[m["name"]] = Tool(m, handler, reconcile)
        return m

    def get(self, name: str) -> Tool | None:
        return self.tools.get(name)

    def allowed(self, allowlist: list[str]) -> list[str]:
        return sorted(n for n in self.tools if any(glob_match(g, n) for g in allowlist))

    def exposed(self, allowlist: list[str], always_loaded: list[str], loaded_extra: list[str], threshold: int = 20) -> list[dict]:
        """Manifests for S1 (sorted by name); beyond `threshold` tools only always-loaded + searched ones are shown."""
        names = self.allowed(allowlist)
        if len(names) > threshold:
            keep = {n for n in names if any(glob_match(g, n) for g in always_loaded)} | set(loaded_extra)
            names = sorted(n for n in names if n in keep)
        return [self.tools[n].manifest for n in names]

    def search(self, query: str, allowlist: list[str], limit: int = 5) -> list[dict]:
        words = [w.lower() for w in query.split()]
        scored = []
        for n in self.allowed(allowlist):
            m = self.tools[n].manifest
            hay = (n + " " + m["description"]).lower()
            s = sum(hay.count(w) for w in words)
            if s:
                scored.append((-s, n))
        return [self.tools[n].manifest for _, n in sorted(scored)[:limit]]


class McpGateway:
    """Hash-pinned MCP registry check (§9.5). `client.list_tools()` is supplied by the real MCP SDK in production."""

    def __init__(self, registry: ToolRegistry, pinned: dict[str, dict[str, str]]):
        self.registry, self.pinned = registry, pinned  # {server: {tool: content_hash}}

    async def connect(self, run, server: str, client) -> list[str]:
        accepted = []
        for tool in await client.list_tools():
            actual = sha256_hex(canonicalize(tool))
            expected = self.pinned.get(server, {}).get(tool["name"])
            if expected != actual:
                await run.emit("mcp.definition_changed", {"server": server, "tool": tool["name"], "expected_hash": expected, "actual_hash": actual})
                continue  # blocked until re-approved through the promotion path
            accepted.append(tool["name"])
        return accepted
