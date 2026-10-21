"""Configuration (§16.1): YAML authored, validated against spec/config.schema.json, stored as canonical JSON."""
from __future__ import annotations
import copy, json
from pathlib import Path
import jsonschema, yaml
from .canonical import canonicalize, sha256_hex
from .tools.registry import load_schema

DEFAULTS = {
    "context": {"compaction": {"trigger_fraction": 0.75, "target_fraction": 0.40, "keep_recent_turns": 6}, "clear": {"keep_recent_results": 10}},
    "loop": {"max_idle_turns": 3, "stuck": {"threshold": 4, "extra_repeats": 2}, "max_verification_failures": 3},
    "gateway": {"max_attempts": 4, "timeout_s": 120},
    "tools": {"dynamic_threshold": 20, "selection_error_alert": 0.05},
    "recovery": {"max_excluded_downtime_s": 300, "verify_last_n": 1000},
    "approvals": {"channels": ["cli"], "expiry_s": 3600, "approvers": [], "allow_owner_approval": False},
    "egress": {"default": "deny", "grants": []},
    "cache": {"semantic": "off"},
}


def _merge(base: dict, over: dict) -> dict:
    out = copy.deepcopy(base)
    for k, v in over.items():
        out[k] = _merge(out[k], v) if isinstance(v, dict) and isinstance(out.get(k), dict) else v
    return out


def load_config(src: str | Path | dict) -> dict:
    raw = src if isinstance(src, dict) else yaml.safe_load(Path(src).read_text())
    jsonschema.validate(raw, load_schema("config.schema.json"))
    return _merge(DEFAULTS, raw)


def config_commit(cfg: dict) -> str:
    """The version a run is pinned to: the git commit of `config_repo` when one is configured (suffixed `+dirty` for uncommitted changes), otherwise a content hash of the canonical config."""
    if cfg.get("config_repo"):
        from .config_repo import version_of
        return version_of(cfg["config_repo"])
    return sha256_hex(canonicalize(cfg))[:40]
