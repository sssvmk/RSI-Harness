"""FailureClassifier (PRD §3.7, design §15.3): a versioned taxonomy of rules, extension layers, secondary tags and human override.

Base rules are in spec/failure-rules.yaml. An agent developer (or an orchestration layer) extends the taxonomy with files in spec/failure-rules.d/*.yaml, each declaring a `layer`;
every code in it is namespaced `<layer>.<class>`. Core rules are evaluated first, so an extension can classify its own errors but cannot change how a core failure is classified."""
from __future__ import annotations
import re
import yaml
from pathlib import Path
from .canonical import canonicalize, sha256_hex
from .policy import glob_match

SPEC = Path(__file__).resolve().parents[4] / "spec"
DEFAULT_RULES = SPEC / "failure-rules.yaml"
LAYER_RE = re.compile(r"^[a-z][a-z0-9_]*$")
CORE_NAMES = {"tool", "state", "environment", "env", "architecture", "arch", "security", "sec", "eval_integrity", "selfmod"}
_EXT_KEYS, _RULE_KEYS = {"layer", "classes", "rules", "owner"}, {"reason", "last_error", "class", "code"}


class TaxonomyError(ValueError):
    pass


def build_rules(base: dict, extensions: list[dict] | None = None) -> dict:
    """Validate the base taxonomy and extension layers and return the compiled rule set."""
    codes = {c: dict(v) for c, v in (base.get("codes") or {}).items()}
    core, fallback = [], []
    for r in base["rules"]:
        if r["class"] is not None and r["code"] not in codes: raise TaxonomyError(f"base rule emits unknown code {r['code']!r}")
        (fallback if r.get("fallback") else core).append({k: r[k] for k in ("reason", "last_error", "class", "code") if k in r})
    layers, ext_rules = {}, []
    for ext in sorted(extensions or [], key=lambda e: str(e.get("layer"))):
        if set(ext) - _EXT_KEYS: raise TaxonomyError(f"extension: unknown setting(s) {sorted(set(ext) - _EXT_KEYS)}")
        layer = ext.get("layer")
        if not isinstance(layer, str) or not LAYER_RE.match(layer): raise TaxonomyError(f"extension layer {layer!r} must be a lowercase identifier")
        if layer in CORE_NAMES: raise TaxonomyError(f"layer {layer!r} reuses a core name")
        if layer in layers: raise TaxonomyError(f"layer {layer!r} is declared twice")
        layers[layer] = ext.get("owner")
        pat = re.compile(rf"^{layer}\.[a-z][a-z0-9_]*$")
        for c in ext.get("classes") or []:
            code = c["code"] if isinstance(c, dict) else c
            if not pat.match(code): raise TaxonomyError(f"{code!r} is not a valid class of layer {layer!r} (expected '{layer}.<class>')")
            if code in codes: raise TaxonomyError(f"class {code!r} is already defined")
            codes[code] = {"class": layer}
        for r in ext.get("rules") or []:
            if set(r) - _RULE_KEYS: raise TaxonomyError(f"{layer}: rule has unknown key(s) {sorted(set(r) - _RULE_KEYS)}")
            if r.get("class") != layer or r.get("code") not in codes or codes[r["code"]].get("class") != layer:
                raise TaxonomyError(f"{layer}: a rule may only emit a class of its own layer (got class={r.get('class')!r}, code={r.get('code')!r})")
            ext_rules.append(dict(r))
    ordered = core + ext_rules + fallback
    out = {"taxonomy_version": base["taxonomy_version"], "classes": sorted(set(base.get("classes") or []) | set(layers)), "codes": codes, "rules": ordered, "layers": layers}
    out["fingerprint"] = sha256_hex(canonicalize({"v": out["taxonomy_version"], "codes": sorted(codes), "rules": ordered, "layers": sorted(layers)}))
    return out


def load_rules(path: str | Path | None = None, extension_dir: str | Path | None = None) -> dict:
    p = Path(path or DEFAULT_RULES)
    base = yaml.safe_load(p.read_text())
    d = Path(extension_dir) if extension_dir else p.parent / "failure-rules.d"
    exts = [yaml.safe_load(f.read_text()) for f in sorted(d.glob("*.yaml"))] if d.is_dir() else []
    return build_rules(base, exts)


def classify(reason: str, last_error_code: str | None, rules: dict, error_codes: list[str] | None = None) -> dict | None:
    """One primary class (how the run ended) plus secondary tags (other classes whose error codes appeared earlier in the run)."""
    ver, fp = rules["taxonomy_version"], rules.get("fingerprint")
    primary = None
    for r in rules["rules"]:
        if not glob_match(r["reason"], reason): continue
        if "last_error" in r and not (last_error_code and glob_match(r["last_error"], last_error_code)): continue
        if r["class"] is None: return None                              # operator actions are not failures
        primary = r; break
    if primary is None: primary = {"class": "architecture", "code": "arch.recovery"}
    secondary: list[str] = []
    for r in rules["rules"]:
        if "last_error" in r and r.get("code") and r["code"] != primary["code"] and r["code"] not in secondary \
                and any(glob_match(r["last_error"], c) for c in (error_codes or [])): secondary.append(r["code"])
    return {"class": primary["class"], "code": primary["code"], "secondary": secondary, "taxonomy_version": ver, "taxonomy_fingerprint": fp}


def known_code(rules: dict, code: str) -> bool:
    return code in rules["codes"]


def observation(rules: dict, code: str, subject: str, evidence: dict | None = None) -> dict:
    """The payload of a `failure.observed` event: a class observed on something other than a run's termination (a candidate, a round, a held-out set)."""
    if not known_code(rules, code): raise TaxonomyError(f"unknown failure code {code!r}")
    return {"code": code, "class": rules["codes"][code]["class"], "subject": subject, "evidence": evidence or {}, "taxonomy_version": rules["taxonomy_version"]}


def reclassification(rules: dict, prior: dict | None, to_code: str, actor: str, reason: str) -> dict:
    """The payload of a `failure.reclassified` event: a human override, audited with who and why. The harness's own classification is kept in the log."""
    if not known_code(rules, to_code): raise TaxonomyError(f"unknown failure code {to_code!r}")
    if not actor or not reason: raise TaxonomyError("an override needs an actor and a reason")
    return {"from_code": (prior or {}).get("code"), "to_code": to_code, "class": rules["codes"][to_code]["class"], "actor": actor, "reason": reason, "taxonomy_version": rules["taxonomy_version"]}


def effective_classification(events: list[dict]) -> dict | None:
    """The classification in force: the latest human override if any, otherwise the harness's."""
    cur = None
    for e in events:
        if e["type"] == "failure.classified": cur = {**e["payload"], "overridden": False}
        elif e["type"] == "failure.reclassified": cur = {"class": e["payload"]["class"], "code": e["payload"]["to_code"], "secondary": (cur or {}).get("secondary", []), "overridden": True,
                                                          "taxonomy_version": e["payload"]["taxonomy_version"]}
    return cur
