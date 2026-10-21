"""Definition pinning (design §13.9, PRD §3.5). The control plane publishes definitions as immutable, content-hashed versions; a run's manifest lists exactly the versions it uses, and components
resolve a definition only through the manifest. Before use the hash is verified, and a missing definition or a mismatch fails closed: nothing is substituted."""
from __future__ import annotations
import json, re
from .canonical import canonicalize, sha256_hex
from .clock import format_ts

ID_RE = re.compile(r"^[a-z][a-z0-9_.\-]*$")
FLOATING = {"latest", "stable", "head", "main", "master", "current", "default", "newest", "newer", "prod", "production"}   # aliases that move; a pin must name an exact version


class DefinitionError(ValueError):
    pass


class DefinitionMismatch(Exception):
    """A pinned definition is missing, or its content does not match the recorded hash."""
    def __init__(self, def_id: str, expected: str | None, observed: str | None, reason: str):
        super().__init__(f"{def_id}: {reason}"); self.def_id, self.expected, self.observed, self.reason = def_id, expected, observed, reason


def encode(content) -> tuple[bytes, str]:
    return (content.encode("utf-8"), "text") if isinstance(content, str) else (canonicalize(content), "json")


def lint_id(def_id: str) -> None:
    if not ID_RE.match(def_id or ""): raise DefinitionError(f"definition id {def_id!r} must be lower-case letters, digits, '.', '_' or '-' and start with a letter")
    if re.split(r"[.]", def_id)[-1] in FLOATING: raise DefinitionError(f"definition id {def_id!r} is a floating alias")


class DefinitionStore:
    def __init__(self, svc):
        self.svc = svc

    def _rows(self, def_id: str):
        return self.svc.store.conn().execute("SELECT version, sha256, kind FROM definition_versions WHERE def_id=? ORDER BY version", (def_id,)).fetchall()

    async def publish(self, def_id: str, content, by: str = "control_plane") -> dict:
        """Publish `content` as the next immutable version of `def_id`. Publishing identical content again returns the existing version, so versions only move when the content does."""
        lint_id(def_id)
        data, kind = encode(content); sha = sha256_hex(data)
        for r in self._rows(def_id):
            if r["sha256"] == sha: return {"id": def_id, "version": r["version"], "sha256": sha}
        rows = self._rows(def_id); version = (rows[-1]["version"] + 1) if rows else 1
        self.svc.blobs.put(data)
        await self.svc.writer.execute("INSERT INTO definition_versions(def_id,version,sha256,kind,published_by,published_at) VALUES(?,?,?,?,?,?)",
                                      (def_id, version, sha, kind, by, format_ts(self.svc.clock.now_ms())))
        return {"id": def_id, "version": version, "sha256": sha}

    def versions(self, def_id: str) -> list[dict]:
        return [{"id": def_id, "version": r["version"], "sha256": r["sha256"]} for r in self._rows(def_id)]

    def fetch(self, def_id: str, version: int, sha256: str):
        """The definition's content, after checking it against the recorded hash. Anything else raises DefinitionMismatch."""
        row = self.svc.store.conn().execute("SELECT sha256, kind FROM definition_versions WHERE def_id=? AND version=?", (def_id, version)).fetchone()
        if row is None: raise DefinitionMismatch(def_id, sha256, None, f"version {version} is not published")
        if row["sha256"] != sha256: raise DefinitionMismatch(def_id, sha256, row["sha256"], "the published version has a different hash")
        try: data = self.svc.blobs.get(sha256)
        except Exception:                                                  # the blob store refuses content that fails its own integrity check; report what is actually there
            try: raw = self.svc.blobs._path(sha256).read_bytes()
            except OSError: raise DefinitionMismatch(def_id, sha256, None, "content is missing") from None
            raise DefinitionMismatch(def_id, sha256, sha256_hex(raw), "content does not match its hash") from None
        observed = sha256_hex(data)
        if observed != sha256: raise DefinitionMismatch(def_id, sha256, observed, "content does not match its hash")
        return data.decode("utf-8") if row["kind"] == "text" else json.loads(data)


class Pins:
    """The definitions one run uses, as its manifest lists them (plus any explicit, audited migrations). An inactive Pins (a run created without a manifest) pins nothing."""
    def __init__(self, svc, pins: list[dict] | None):
        self.svc, self.store = svc, DefinitionStore(svc)
        self.active = pins is not None
        self.by_id = {p["id"]: dict(p) for p in (pins or [])}

    @classmethod
    def load(cls, svc, run_id: str) -> "Pins":
        evs = svc.store.events(run_id, types=["run.created", "definition.migrated"])
        created = next((e for e in evs if e["type"] == "run.created"), None)
        m = ((created or {}).get("payload") or {}).get("manifest")
        pins = cls(svc, ((m or {}).get("fields", {}).get("definitions", {}).get("value") if m else None))
        for e in evs:
            if e["type"] == "definition.migrated": pins.by_id[e["payload"]["id"]] = {"id": e["payload"]["id"], "version": e["payload"]["to_version"], "sha256": e["payload"]["to_sha256"]}
        return pins

    def has(self, def_id: str) -> bool:
        return def_id in self.by_id

    def resolve(self, def_id: str):
        """The pinned content of `def_id`, verified. A definition that is not in the manifest cannot be resolved: there is no 'latest' to fall back on."""
        p = self.by_id.get(def_id)
        if p is None: raise DefinitionMismatch(def_id, None, None, "not pinned in this run's manifest")
        return self.store.fetch(p["id"], p["version"], p["sha256"])

    def check_hash(self, def_id: str, content) -> None:
        """For definitions the runtime builds itself (a tool's manifest): its current content must equal the pinned hash."""
        p = self.by_id.get(def_id)
        if p is None: raise DefinitionMismatch(def_id, None, sha256_hex(encode(content)[0]), "not pinned in this run's manifest")
        observed = sha256_hex(encode(content)[0])
        if observed != p["sha256"]: raise DefinitionMismatch(def_id, p["sha256"], observed, "the definition changed after the run was pinned")

    def verify_all(self) -> list[dict]:
        """Read and hash every pinned definition. Returns the list verified; raises DefinitionMismatch on the first problem."""
        for p in self.by_id.values(): self.resolve(p["id"])
        return sorted(self.by_id.values(), key=lambda p: p["id"])

    async def migrate(self, run, def_id: str, to_version: int, by: str, reason: str) -> dict:
        """Move a running run to another published version. It is explicit and audited, and nothing else can change what a run uses."""
        rows = {r["version"]: r for r in self.store.versions(def_id)}
        if to_version not in rows: raise DefinitionError(f"{def_id} version {to_version} is not published")
        old = self.by_id.get(def_id, {})
        await run.emit("definition.migrated", {"id": def_id, "from_version": old.get("version"), "to_version": to_version, "from_sha256": old.get("sha256"), "to_sha256": rows[to_version]["sha256"], "by": by, "reason": reason})
        self.by_id[def_id] = {"id": def_id, "version": to_version, "sha256": rows[to_version]["sha256"]}
        return self.by_id[def_id]
