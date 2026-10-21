"""RFC 8785 canonical JSON (proposal P1) and the hash chain (design §5.2)."""
from __future__ import annotations
import hashlib
from typing import Any
import jcs

MAX_SAFE_INT = 2**53
ENVELOPE_FIELDS = ("run_id", "seq", "ts", "type", "actor", "causation_seq", "correlation_id", "schema_version", "payload")


def canonicalize(obj: Any) -> bytes:
    return jcs.canonicalize(obj)


def sha256_hex(data: bytes | str) -> str:
    if isinstance(data, str):
        data = data.encode("utf-8")
    return hashlib.sha256(data).hexdigest()


def assert_safe_ints(obj: Any, path: str = "$") -> None:
    """Appendix A.0 rule 2: integers outside +-2^53 must travel as decimal strings."""
    if isinstance(obj, bool):
        return
    if isinstance(obj, int):
        if abs(obj) > MAX_SAFE_INT:
            raise ValueError(f"integer at {path} exceeds +-2^53; encode as a decimal string")
    elif isinstance(obj, dict):
        for k, v in obj.items():
            assert_safe_ints(v, f"{path}.{k}")
    elif isinstance(obj, list):
        for i, v in enumerate(obj):
            assert_safe_ints(v, f"{path}[{i}]")


def genesis_hash(run_id: str) -> str:
    return sha256_hex(b"harness:run:" + run_id.encode("utf-8"))


def event_material(ev: dict) -> bytes:
    return canonicalize({k: ev.get(k) for k in ENVELOPE_FIELDS})


def compute_hash(prev_hash: str, ev: dict) -> str:
    return sha256_hex(prev_hash.encode("ascii") + b"\n" + event_material(ev))
