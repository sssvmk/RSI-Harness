"""Outcome contract and verdicts (design §13.11, PRD §3.6).

An outcome contract is fixed before execution: the acceptance criteria, how each is verified, and what to do when the evidence is inconclusive. A verdict is derived from *evidence*, never from
the agent's own claim.

Verdict precedence (the design leaves it open, so it is fixed here and in `spec/vectors/outcome-vectors.json`), over the *required* criteria:
  some required criterion demonstrably failed, and some passed -> PARTIAL     (progress was made, the goal was not achieved)
  some required criterion demonstrably failed, none passed     -> FAILURE
  none failed, some without verified evidence                  -> INCONCLUSIVE (missing, stale, conflicting, or insufficient evidence is never SUCCESS)
  all passed with verified evidence                            -> SUCCESS"""
from __future__ import annotations
import hashlib, json
from decimal import Decimal, InvalidOperation
import jsonschema
from ..canonical import canonicalize, sha256_hex
from ..tools.registry import load_schema

VERDICTS = ("SUCCESS", "FAILURE", "PARTIAL", "INCONCLUSIVE")
PASS, FAIL = "pass", "fail"                         # any other status (unavailable, stale, conflict, error) is *not verified*
OPS = {">=": lambda a, b: a >= b, "<=": lambda a, b: a <= b, ">": lambda a, b: a > b, "<": lambda a, b: a < b, "==": lambda a, b: a == b}


class ContractError(ValueError):
    pass


def validate_contract(c: dict) -> list[str]:
    errs = []
    try: jsonschema.validate(c, load_schema("outcome.contract.v1.json"))
    except jsonschema.ValidationError as e: return [f"outcome contract invalid: {e.message}"]
    ids = [x["id"] for x in c["acceptance_criteria"]]
    for i in {i for i in ids if ids.count(i) > 1}: errs.append(f"criterion id {i!r} is used twice")
    if not any(x["required"] for x in c["acceptance_criteria"]): errs.append("an outcome contract needs at least one required criterion")
    for x in c["acceptance_criteria"]:
        p = x.get("params") or {}
        if x["verification"] == "test_result" and (p.get("op", ">=") not in OPS): errs.append(f"{x['id']}: unknown operator {p.get('op')!r}")
    return errs


def contract_hash(c: dict) -> str:
    return sha256_hex(canonicalize(c))


def derive_verdict(results: list[dict]) -> str:
    """results: [{id, required, status}]. Only required criteria decide the verdict."""
    req = [r for r in results if r["required"]]
    failed = [r for r in req if r["status"] == FAIL]; passed = [r for r in req if r["status"] == PASS]
    if failed: return "PARTIAL" if passed else "FAILURE"
    if len(passed) < len(req) or not req: return "INCONCLUSIVE"
    return "SUCCESS"


# --------------------------------------------------------------------------------------------------------------------------------- verifiers
def _num(x):
    """An exact decimal, or None for anything that is not a finite number (NaN and infinity prove nothing, and cannot be ordered)."""
    try: d = Decimal(str(x))
    except (InvalidOperation, ValueError): return None
    return d if d.is_finite() else None


def v_test_result(p: dict, ctx: dict) -> dict:
    """A measured value (the confirmed or best score, or a named metric of the score report) against a threshold."""
    metric = p.get("metric", "value"); rep = ctx.get("report") or {}
    raw = ctx.get("value") if metric == "value" else rep.get("primary") if metric == "primary" else (rep.get("metrics") or {}).get(metric)
    if raw is None: return {"status": "unavailable", "ref": f"metric:{metric}"}
    a, b = _num(raw), _num(p.get("value", 0))
    if a is None or b is None: return {"status": "unavailable", "ref": f"metric:{metric}"}
    if p.get("op") is None and "value" not in p: return {"status": PASS if rep.get("valid", True) else FAIL, "ref": f"metric:{metric}"}      # no threshold: the submission only has to be valid
    return {"status": PASS if OPS[p.get("op", ">=")](a, b) else FAIL, "ref": f"metric:{metric}={raw}"}


def v_exact_decimal_comparison(p: dict, ctx: dict) -> dict:
    """Exact decimal equality: money is never compared as a float."""
    a, b = _num(p.get("actual", (ctx.get("facts") or {}).get(p.get("actual_from", ""))) if p.get("actual") is not None or p.get("actual_from") else None), _num(p.get("expected"))
    if a is None or b is None: return {"status": "unavailable", "ref": "decimal"}
    return {"status": PASS if a == b else FAIL, "ref": f"decimal:{a}=={b}"}


def v_artifact_check(p: dict, ctx: dict) -> dict:
    """The artifact exists in the final workspace, and has the stated content hash when one is given."""
    read = ctx.get("fs_read")
    if read is None: return {"status": "unavailable", "ref": p.get("path", "?")}
    try: content = read(p["path"])
    except Exception: return {"status": FAIL, "ref": f"missing:{p['path']}"}
    if "sha256" in p and hashlib.sha256(content.encode("utf-8") if isinstance(content, str) else content).hexdigest() != p["sha256"]: return {"status": FAIL, "ref": f"hash_mismatch:{p['path']}"}
    return {"status": PASS, "ref": f"artifact:{p['path']}"}


def v_evaluator(p: dict, ctx: dict) -> dict:
    """A criterion judged by an evaluator plugin (design §13.4). The verdicts are fetched before this runs, because the call is asynchronous; with none for this criterion the evidence is unavailable and never a pass."""
    r = (ctx.get("evaluator_results") or {}).get(ctx.get("criterion_id"))
    return r if r else {"status": "unavailable", "ref": "no evaluator verdict was supplied"}


VERIFIERS = {"test_result": v_test_result, "exact_decimal_comparison": v_exact_decimal_comparison, "artifact_check": v_artifact_check, "evaluator": v_evaluator}


def register_verifier(kind: str, fn) -> None:
    """Deployments register the adapters for their own systems (authoritative_lookup, audit_log_lookup, external_confirmation). Without one, that evidence is unavailable and never a SUCCESS."""
    VERIFIERS[kind] = fn


def evaluate_contract(contract: dict, ctx: dict) -> dict:
    errs = validate_contract(contract)
    if errs: raise ContractError("; ".join(errs))
    results = []
    for x in contract["acceptance_criteria"]:
        fn = VERIFIERS.get(x["verification"])
        try: r = fn(x.get("params") or {}, {**ctx, "criterion_id": x["id"]}) if fn else {"status": "unavailable", "ref": f"no verifier for {x['verification']}"}
        except Exception as e: r = {"status": "unavailable", "ref": f"verifier error: {type(e).__name__}"}      # an adapter that fails proves nothing
        results.append({"id": x["id"], "required": x["required"], "verification": x["verification"], "status": r["status"], "evidence_ref": r.get("ref")})
    return {"verdict": derive_verdict(results), "criteria": results, "contract_hash": contract_hash(contract)}


def implicit_contract(task_id: str, spec: dict) -> dict:
    """A task without an explicit contract gets one from its pass threshold, so every evaluation run has a verdict."""
    thr = spec["scoring"].get("pass_threshold"); maxi = spec["scoring"]["primary"]["direction"] == "maximize"
    p = {"metric": "value"} if thr is None else {"metric": "value", "op": ">=" if maxi else "<=", "value": thr}
    return {"task_id": task_id, "acceptance_criteria": [{"id": "score_meets_threshold" if thr is not None else "valid_submission", "required": True, "verification": "test_result", "params": p}],
            "success_policy": "all_required_criteria_pass", "inconclusive_policy": "do_not_report_success"}


def contract_for(spec: dict) -> dict:
    """The contract a task is judged by: its explicit one, or the one implied by its pass threshold. The manifest hashes exactly this."""
    return {"task_id": spec["id"], **spec["outcome"]} if spec.get("outcome") else implicit_contract(spec["id"], spec)


# --------------------------------------------------------------------------------------------------------------------------------- metrics
def outcome_metrics(records: list[dict], exclusion_policy: str | None = None, exclude=lambda r: False) -> dict:
    """Rates by task category and risk class. The denominator is every evaluated task, failed and inconclusive ones included, unless a *documented* exclusion policy applies.
    records: [{verdict, category, risk, required, verified, audited_verdict?}]."""
    kept = [r for r in records if not exclude(r)]
    if len(kept) != len(records) and not exclusion_policy: raise ContractError("tasks were excluded from the denominator without a documented exclusion policy")
    def rates(rs: list[dict]) -> dict:
        n = len(rs); req = sum(r["required"] for r in rs); ver = sum(r["verified"] for r in rs); aud = [r for r in rs if r.get("audited_verdict")]
        return {"n": n, **{f"{v.lower()}_rate": (sum(1 for r in rs if r["verdict"] == v) / n if n else None) for v in VERDICTS}, "verification_coverage": (ver / req if req else None),
                "audited": len(aud), "evaluation_error_rate": (sum(1 for r in aud if r["audited_verdict"] != r["verdict"]) / len(aud) if aud else None)}
    groups: dict[str, list] = {}
    for r in kept: groups.setdefault(f"{r['category']}|{r['risk']}", []).append(r)
    return {"denominator": "all evaluated tasks" if len(kept) == len(records) else f"excluding: {exclusion_policy}", "overall": rates(kept), "groups": {k: rates(v) for k, v in sorted(groups.items())}}
