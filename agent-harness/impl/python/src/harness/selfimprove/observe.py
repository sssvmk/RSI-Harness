"""Detectors that turn improvement-campaign evidence into failure observations (PRD §3.7 Self-Modification Errors, Evaluation-Integrity Errors; §6.11 Failure Classes).

Six self-modification classes are detected automatically against thresholds; two (implementation drift under execution pressure, harness activation or adherence failure)
have no reliable automatic signal and are set by a human reviewer. Selection bias against the scorer is signalled by the number of held-out queries a set has served."""
from __future__ import annotations
import math
from ..failure import TaxonomyError, observation
from .search import mean_pairwise_similarity

DEFAULT_THRESHOLDS = {"overfit_gap": 0.10, "diversity_similarity": 0.60, "over_optimism_min_lower": 0.005, "selection_bias_warn_fraction": 0.80}
AUTOMATIC = ("selfmod.invalid_proposal", "selfmod.post_promotion_regression", "selfmod.overfitting", "selfmod.reward_hacking", "selfmod.diversity_collapse", "selfmod.over_optimism")
REVIEWER_LABELLED = ("selfmod.implementation_drift", "selfmod.harness_adherence")
SCREEN_PREFIX = "screen."


def th(t: dict | None) -> dict:
    return {**DEFAULT_THRESHOLDS, **(t or {})}


def from_rejection(reasons: list[dict]) -> list[tuple[str, dict]]:
    """A proposal refused before evaluation: reward hacking when a screen fired, otherwise an invalid proposal."""
    screens = [r["code"] for r in reasons if r["code"].startswith(SCREEN_PREFIX)]
    other = [r["code"] for r in reasons if not r["code"].startswith(SCREEN_PREFIX)]
    out = []
    if screens: out.append(("selfmod.reward_hacking", {"screens": sorted(set(screens))}))
    if other: out.append(("selfmod.invalid_proposal", {"reasons": sorted(set(other))}))
    return out


def from_decision(decision: dict, thresholds: dict | None = None) -> list[tuple[str, dict]]:
    t, out = th(thresholds), []
    delta = (decision.get("target") or {}).get("delta")
    held = next((m["heldout"] for m in decision.get("models", {}).values() if m.get("heldout")), None)
    if delta is not None and held is not None:
        gap = delta - held["delta"]
        if gap > t["overfit_gap"] or not held.get("pass", True): out.append(("selfmod.overfitting", {"scoring_delta": delta, "heldout_delta": held["delta"], "gap": gap, "threshold": t["overfit_gap"]}))
    lower = (decision.get("target") or {}).get("lower")
    if decision.get("accepted") and lower is not None and lower < t["over_optimism_min_lower"]:
        out.append(("selfmod.over_optimism", {"lower": lower, "threshold": t["over_optimism_min_lower"]}))
    return out


def from_breach(monitor_result: dict) -> list[tuple[str, dict]]:
    return [("selfmod.post_promotion_regression", {"breaches": monitor_result["breaches"], "action": monitor_result["action"]})] if monitor_result.get("breach") else []


def from_round(added_texts: list[str], thresholds: dict | None = None) -> list[tuple[str, dict]]:
    if len(added_texts) < 2: return []
    sim = mean_pairwise_similarity(added_texts)
    return [("selfmod.diversity_collapse", {"mean_pairwise_similarity": sim, "threshold": th(thresholds)["diversity_similarity"], "candidates": len(added_texts)})] if sim > th(thresholds)["diversity_similarity"] else []


def selection_bias(usage: int, cap: int, thresholds: dict | None = None) -> tuple[str, dict] | None:
    """Adaptive overfitting to the scorer: the more times a held-out set has been queried, the less it can be trusted (cap = the set's query limit)."""
    if usage >= math.ceil(th(thresholds)["selection_bias_warn_fraction"] * cap):
        return ("eval_integrity.selection_bias", {"queries": usage, "cap": cap, "fraction": usage / cap})
    return None


def reviewer_label(rules: dict, code: str, subject: str, evidence: dict, reviewer: str, reason: str) -> dict:
    """Only the two classes without an automatic detector are set by a reviewer; the payload is a `failure.observed` event."""
    if code not in REVIEWER_LABELLED: raise TaxonomyError(f"{code!r} is detected automatically; a reviewer may label only {list(REVIEWER_LABELLED)}")
    if not reviewer or not reason: raise TaxonomyError("a label needs a reviewer and a reason")
    return {**observation(rules, code, subject, {**evidence, "reason": reason}), "labelled_by": reviewer}
