"""ULIDs (proposal P4) and small id helpers."""
from __future__ import annotations
import os, time

_ENC = "0123456789ABCDEFGHJKMNPQRSTVWXYZ"


def ulid(now_ms: int | None = None, rand: int | None = None) -> str:
    t = int(time.time() * 1000) if now_ms is None else now_ms
    r = int.from_bytes(os.urandom(10), "big") if rand is None else rand
    n = (t << 80) | r
    out = []
    for _ in range(26):
        out.append(_ENC[n & 31])
        n >>= 5
    return "".join(reversed(out))
