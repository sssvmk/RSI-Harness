"""Snapshots (§10.7, D4): manifest of file hashes + content-addressed blobs; verified restore."""
from __future__ import annotations
import os
from pathlib import Path
from ..canonical import canonicalize, sha256_hex


class SnapshotIntegrityError(Exception):
    pass


def take_snapshot(sandbox, blobs) -> dict:
    """Walk /workspace, store unseen file contents, store the manifest itself as a blob."""
    ex = sandbox.executor
    manifest = ex.workspace_manifest("/workspace", exclude=[".harness/tmp/*"])
    new_blobs = 0
    for f in manifest["files"]:
        if not blobs.has(f["sha256"]):
            blobs.put(ex.read_real("/workspace", f["path"]))
            new_blobs += 1
    manifest_hash = blobs.put(canonicalize({"schema_version": 1, **manifest}))
    return {"manifest_hash": manifest_hash, "files": len(manifest["files"]), "new_blobs": new_blobs}


def restore_snapshot(manifest_hash: str, blobs, sandbox) -> int:
    """Materialize files by hash into the sandbox /workspace, verifying every hash before use."""
    import json
    manifest = json.loads(blobs.get(manifest_hash))
    if sha256_hex(canonicalize(manifest)) != manifest_hash:
        raise SnapshotIntegrityError("manifest hash mismatch")
    root = sandbox.executor.roots["/workspace"]
    for f in manifest["files"]:
        data = blobs.get(f["sha256"])
        if sha256_hex(data) != f["sha256"]:
            raise SnapshotIntegrityError(f"blob mismatch for {f['path']}")
        dest = Path(root) / f["path"]
        if not str(dest.resolve()).startswith(os.path.realpath(root) + os.sep):
            raise SnapshotIntegrityError(f"path escapes workspace: {f['path']}")
        dest.parent.mkdir(parents=True, exist_ok=True)
        dest.write_bytes(data)
        os.chmod(dest, f["mode"])
    return len(manifest["files"])
