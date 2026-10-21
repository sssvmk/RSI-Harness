"""Content-addressed blob store (design §5.8): <root>/blobs/<hh>/<hash>, atomic writes."""
from __future__ import annotations
import os, tempfile
from pathlib import Path
from .canonical import sha256_hex, canonicalize
import json

INLINE_LIMIT = 4096  # proposal P8


class BlobStore:
    def __init__(self, root: str | Path):
        self.root = Path(root) / "blobs"
        self.root.mkdir(parents=True, exist_ok=True)

    def _path(self, h: str) -> Path:
        return self.root / h[:2] / h

    def put(self, data: bytes) -> str:
        h = sha256_hex(data)
        p = self._path(h)
        if p.exists():
            return h
        p.parent.mkdir(parents=True, exist_ok=True)
        fd, tmp = tempfile.mkstemp(dir=p.parent, prefix=".tmp-")
        try:
            with os.fdopen(fd, "wb") as f:
                f.write(data)
                f.flush()
                os.fsync(f.fileno())
            os.replace(tmp, p)
        except BaseException:
            if os.path.exists(tmp):
                os.unlink(tmp)
            raise
        return h

    def get(self, h: str) -> bytes:
        data = self._path(h).read_bytes()
        if sha256_hex(data) != h:
            raise IOError(f"blob {h} failed integrity check")
        return data

    def has(self, h: str) -> bool:
        return self._path(h).exists()

    def delete(self, h: str) -> bool:
        p = self._path(h)
        if p.exists():
            p.unlink()
            return True
        return False

    # -- helpers for the "large or sensitive payloads live in the artifact store" rule (P8) --
    def put_json(self, obj) -> str:
        return self.put(canonicalize(obj))

    def get_json(self, h: str):
        return json.loads(self.get(h))

    def inline_or_ref(self, obj, inline_key: str, ref_key: str) -> dict:
        """Return {inline_key: obj} when small, else {ref_key: hash}."""
        raw = canonicalize(obj)
        if len(raw) <= INLINE_LIMIT:
            return {inline_key: obj}
        return {ref_key: self.put(raw)}
