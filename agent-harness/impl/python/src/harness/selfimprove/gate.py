"""The gate (§17.8.2): identical in every implementation. Pure functions of observations; no I/O.

Interpretations where the design is silent (recorded here, and fixed by the shared gate vectors):
* Holm (P14) is applied to the *target* test across the candidates of a round, per target model: candidates are ordered by the one-sided
  bootstrap p-value (share of resampled means <= 0, ties by input order); the k-th of m is tested at alpha_k = alpha / (m - k + 1) with the
  interval of S13.7 at confidence 1 - alpha_k; the first failure stops the step-down and every later candidate fails. Guardrail, preserved-behavior
  and held-out checks are non-inferiority checks at the nominal alpha.
* Each (proposal, model, variant, kind, metric) uses its own gate id, hence its own bootstrap seed.
* A model is `needs_heldout` when steps 2-5 pass but no held-out data was supplied; the held-out set is then queried once and the gate re-run.
"""
from __future__ import annotations
from ..stats import bootstrap_means, improvements, interval_from_sorted, mean, seed_for

DEFAULT_PARAMS = {"alpha": 0.05, "B": 10000, "holm": True, "margins": {"default": 0.02}, "heldout_margin": 0.02, "preserved_margin": 0.02}
VARIANTS = ("canonical", "format_varied")


def _params(p: dict | None) -> dict:
    out = {**DEFAULT_PARAMS, **(p or {})}
    out["margins"] = {**DEFAULT_PARAMS["margins"], **((p or {}).get("margins") or {})}
    return out


def _iv(x: list[float], seed: int, alpha: float, B: int) -> dict:
    means = bootstrap_means(x, seed, B)
    lo, hi, _, _ = interval_from_sorted(means, 1 - alpha)
    return {"delta": mean(x), "lower": lo, "upper": hi}


def target_p_value(x: list[float], seed: int, B: int) -> float:
    means = bootstrap_means(x, seed, B)
    return sum(1 for m in means if m <= 0) / B


def dominated(summary: dict, archive: list[dict]) -> bool:
    for a in archive:
        ge = a["target"] >= summary["target"] and a["cost"] <= summary["cost"] and a["latency"] <= summary["latency"]
        strict = a["target"] > summary["target"] or a["cost"] < summary["cost"] or a["latency"] < summary["latency"]
        if ge and strict:
            return True
    return False


def holm_levels(pvalues: list[float | None], alpha: float, holm: bool = True) -> list[float | None]:
    """Nominal per-rank levels (alpha/(m-k+1)); the step-down stop is applied by the caller once a test fails."""
    idx = sorted((i for i, p in enumerate(pvalues) if p is not None), key=lambda i: (pvalues[i], i))
    m, lv = len(idx), [None] * len(pvalues)
    for k, i in enumerate(idx, start=1):
        lv[i] = alpha / (m - k + 1) if holm else alpha
    return lv


def _check_variant(run_id: str, pid: str, model: str, variant: str, v: dict, P: dict, alpha_t: float | None, out: dict) -> bool:
    """Steps 2-4 on one variant. Returns pass."""
    ok, tag = True, f"{model}:{variant}"
    B = P["B"]
    t = v["target"]
    x = improvements(t["b"], t["c"], t["direction"])
    if alpha_t is None:
        out["reasons"].append(f"holm:stopped({tag})"); iv = _iv(x, seed_for(run_id, f"{pid}:{model}:{variant}:target"), P["alpha"], B); ok = False
    else:
        iv = _iv(x, seed_for(run_id, f"{pid}:{model}:{variant}:target"), alpha_t, B)
        if not iv["lower"] > 0:
            out["reasons"].append(f"target:no_significant_improvement({tag})"); ok = False
    iv["alpha"] = alpha_t if alpha_t is not None else P["alpha"]
    out.setdefault("target", {})[variant] = iv
    grs = []
    for g in v.get("guardrails", []):
        gx = improvements(g["b"], g["c"], g["direction"])
        margin = g.get("margin", P["margins"].get(g["metric"], P["margins"]["default"]))
        gi = _iv(gx, seed_for(run_id, f"{pid}:{model}:{variant}:guardrail:{g['metric']}"), P["alpha"], B)
        gi.update({"metric": g["metric"], "margin": margin, "pass": gi["lower"] >= -margin})
        if not gi["pass"]:
            out["reasons"].append(f"guardrail:{g['metric']}({tag})"); ok = False
        grs.append(gi)
    out.setdefault("guardrails", {})[variant] = grs
    pr = v.get("preserved")
    if pr:
        idx = [i for i, b in enumerate(pr["b"]) if b >= 1]
        if idx:
            px = [pr["c"][i] - pr["b"][i] for i in idx]
            pi = _iv(px, seed_for(run_id, f"{pid}:{model}:{variant}:preserved"), P["alpha"], B)
            pi["pass"] = pi["lower"] >= -P["preserved_margin"]
            if not pi["pass"]:
                out["reasons"].append(f"preserved:non_inferiority({tag})"); ok = False
            out.setdefault("preserved", {})[variant] = pi
        bad = [i for i in pr.get("must_pass", []) if pr["b"][i] >= 1 and pr["c"][i] < 1]
        if bad:
            out["reasons"].append(f"preserved:must_pass_regressed({tag}:{bad})"); ok = False
    return ok


def _check_heldout(run_id: str, pid: str, model: str, h: dict, P: dict, out: dict) -> bool:
    ok = True
    t = h["target"]
    x = improvements(t["b"], t["c"], t["direction"])
    iv = _iv(x, seed_for(run_id, f"{pid}:{model}:heldout:target"), P["alpha"], P["B"])
    iv["pass"] = iv["lower"] >= -P["heldout_margin"]
    out["heldout"] = iv
    if not iv["pass"]:
        out["reasons"].append(f"heldout:regressed({model})"); ok = False
    pr = h.get("preserved")
    if pr:
        bad = [i for i in pr.get("must_pass", []) if pr["b"][i] >= 1 and pr["c"][i] < 1]
        if bad:
            out["reasons"].append(f"heldout:must_pass_regressed({model}:{bad})"); ok = False
    return ok


def evaluate_candidate(run_id: str, cand: dict, params: dict | None, alpha_by_model: dict[str, float | None]) -> dict:
    """cand = {proposal_id, tier, models:{model:{variants:{canonical,format_varied}, heldout?}}, summary?, archive?}"""
    P, pid = _params(params), cand["proposal_id"]
    dec = {"proposal_id": pid, "tier": cand["tier"], "accepted": False, "needs_heldout": False, "accepted_models": [], "reasons": [], "models": {}}
    pending = []
    for model, data in cand["models"].items():
        out = {"reasons": []}
        ok = True
        for variant in VARIANTS:
            if variant not in data.get("variants", {}):
                out["reasons"].append(f"variant:missing({model}:{variant})"); ok = False; continue
            ok &= _check_variant(run_id, pid, model, variant, data["variants"][variant], P, alpha_by_model.get(model), out)
        if ok:
            if data.get("heldout") is None:
                out["status"] = "needs_heldout"; pending.append(model)
            else:
                ok = _check_heldout(run_id, pid, model, data["heldout"], P, out)
                out["status"] = "pass" if ok else "fail"
        else:
            out["status"] = "fail"
        dec["models"][model] = out
        if out["status"] == "pass":
            dec["accepted_models"].append(model)
    dec["needs_heldout"] = bool(pending) and not dec["accepted_models"]
    if cand.get("summary") is not None and cand.get("archive") is not None and dominated(cand["summary"], cand["archive"]):
        dec["reasons"].append("dominated"); dec["accepted_models"] = []; dec["needs_heldout"] = False
    dec["accepted"] = bool(dec["accepted_models"])
    for model, out in dec["models"].items():
        dec["reasons"] += out["reasons"]
        if out["status"] == "fail" and not out["reasons"]:
            dec["reasons"].append(f"model:{model}:failed")
    first = next(iter(dec["models"].values()), {})
    t = (first.get("target") or {}).get("canonical", {})
    dec["target"] = {k: t.get(k) for k in ("delta", "lower", "upper")}
    dec["guardrails"] = [{k: g[k] for k in ("metric", "delta", "lower", "upper", "margin", "pass")} for g in (first.get("guardrails") or {}).get("canonical", [])]
    return dec


def round_alphas(run_id: str, candidates: list[dict], params: dict | None = None) -> list[dict]:
    """Per candidate, per model: the Holm-adjusted level for the canonical target test (None = the step-down already stopped)."""
    P = _params(params)
    models = sorted({m for c in candidates for m in c["models"]})
    alpha_for: list[dict] = [{} for _ in candidates]
    for model in models:
        ps: list[float | None] = []
        for c in candidates:
            v = c["models"].get(model, {}).get("variants", {}).get("canonical")
            if v is None:
                ps.append(None); continue
            x = improvements(v["target"]["b"], v["target"]["c"], v["target"]["direction"])
            ps.append(target_p_value(x, seed_for(run_id, f"{c['proposal_id']}:{model}:canonical:target"), P["B"]))
        levels = holm_levels(ps, P["alpha"], P["holm"])
        stopped = False
        for i in sorted((i for i, p in enumerate(ps) if p is not None), key=lambda i: (ps[i], i)):   # step-down in ascending p
            if stopped:
                alpha_for[i][model] = None; continue
            t = candidates[i]["models"][model]["variants"]["canonical"]["target"]
            x = improvements(t["b"], t["c"], t["direction"])
            alpha_for[i][model] = levels[i]
            if not _iv(x, seed_for(run_id, f"{candidates[i]['proposal_id']}:{model}:canonical:target"), levels[i], P["B"])["lower"] > 0:
                stopped = True
    return alpha_for


def decide_round(run_id: str, candidates: list[dict], params: dict | None = None) -> list[dict]:
    """Gate every candidate of a round together, so the Holm adjustment can be applied across them (P14)."""
    alphas = round_alphas(run_id, candidates, params)
    return [evaluate_candidate(run_id, c, params, alphas[i]) for i, c in enumerate(candidates)]
