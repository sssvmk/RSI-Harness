"""Evaluation-integrity closure (design §15.6). Findings are tracked per detector, and the status of an integrity class is derived from the detectors it requires. The state is a fold over `integrity.detector_status` events, so
the history is the log: findings and closure history are preserved after a class closes. NOT_RUN, WAIVED and NOT_APPLICABLE are never equivalent to PASS."""
from __future__ import annotations
import copy, json
from datetime import datetime
from pathlib import Path

SPEC = Path(__file__).resolve().parents[4] / "spec" / "integrity" / "detectors.json"
STATUSES = ("NOT_RUN", "OPEN", "FIXED_PENDING_VERIFICATION", "PASS", "FAIL", "WAIVED", "NOT_APPLICABLE")
TRANSITIONS = {"NOT_RUN": {"PASS", "FAIL", "WAIVED", "NOT_APPLICABLE"}, "OPEN": {"FIXED_PENDING_VERIFICATION", "WAIVED", "NOT_APPLICABLE"}, "FAIL": {"OPEN", "FIXED_PENDING_VERIFICATION", "WAIVED"},
               "FIXED_PENDING_VERIFICATION": {"PASS", "FAIL"}, "PASS": {"FAIL", "WAIVED"}, "WAIVED": {"OPEN", "PASS", "FAIL"}, "NOT_APPLICABLE": {"NOT_RUN", "OPEN"}}


class IntegrityError(ValueError):
    pass


def load_spec(path: Path | str | None = None) -> dict:
    return json.loads(Path(path or SPEC).read_text())


def _t(s: str) -> datetime:
    return datetime.fromisoformat(s.replace("Z", "+00:00"))


def initial(spec: dict) -> dict:
    """Every required detector starts NOT_RUN: nothing is assumed to pass."""
    dets = {}
    for cls, c in spec["classes"].items():
        for d in c["required"]:
            dets[d["id"]] = {"class": cls, "id": d["id"], "version": d["version"], "severity": d["severity"], "status": "NOT_RUN", "evidence": None, "owner": None, "fixed_by": None, "waiver": None, "note": None, "history": []}
    return {"detectors": dets, "rejected": []}


def apply(state: dict, p: dict, at: str) -> dict:
    """One status change. Refused if the transition is not allowed or lacks what it needs: a PASS needs evidence for the detector's current version and an evaluation run, and after a fix a verifier who is not the person who
    made it; a waiver needs a rationale, an approver who is not the owner, and a future expiry; not-applicable needs a reason."""
    s = copy.deepcopy(state); d = s["detectors"].get(p.get("detector_id"))
    if d is None: raise IntegrityError(f"unknown detector {p.get('detector_id')!r}")
    to, by, frm = p.get("status"), p.get("by"), d["status"]
    if to not in STATUSES: raise IntegrityError(f"unknown status {to!r}")
    if not by: raise IntegrityError("a status change needs the person or system that made it")
    if to not in TRANSITIONS[frm]: raise IntegrityError(f"{d['id']}: {frm} cannot become {to}")
    if p.get("owner"): d["owner"] = p["owner"]
    if to == "PASS":
        if not p.get("evidence_ref") or not p.get("run_id"): raise IntegrityError(f"{d['id']}: a PASS needs an evidence reference and the evaluation run it came from")
        if p.get("evidence_version") != d["version"]: raise IntegrityError(f"{d['id']}: the evidence is for version {p.get('evidence_version')!r} and the detector is version {d['version']!r}")
        if frm == "FIXED_PENDING_VERIFICATION" and by == d["fixed_by"]: raise IntegrityError(f"{d['id']}: a fix must be verified by someone other than the person who made it")
        d["evidence"] = {"ref": p["evidence_ref"], "run_id": p["run_id"], "version": p["evidence_version"], "at": at}; d["waiver"] = None
    elif to == "FIXED_PENDING_VERIFICATION":
        if not p.get("note"): raise IntegrityError(f"{d['id']}: a fix needs a description of the remediation")
        d["fixed_by"] = by
    elif to == "WAIVED":
        w = p.get("waiver") or {}
        if not (w.get("rationale") and w.get("approved_by") and w.get("expires_at")): raise IntegrityError(f"{d['id']}: a waiver needs a rationale, an approver, and an expiry")
        if w["approved_by"] == (d["owner"] or by): raise IntegrityError(f"{d['id']}: a waiver must be approved by someone other than the owner")
        if _t(w["expires_at"]) <= _t(at): raise IntegrityError(f"{d['id']}: a waiver must expire in the future")
        d["waiver"] = {"approved_by": w["approved_by"], "rationale": w["rationale"], "expires_at": w["expires_at"], "granted_at": at}
    elif to == "NOT_APPLICABLE":
        if not p.get("note"): raise IntegrityError(f"{d['id']}: not-applicable needs the reason")
    if to != "WAIVED": d["waiver"] = None                                                  # a waiver ends when the detector leaves WAIVED
    d["note"] = p.get("note", d["note"]); d["status"] = to
    d["history"].append({"at": at, "from": frm, "to": to, "by": by, "evidence_ref": p.get("evidence_ref"), "run_id": p.get("run_id"), "note": p.get("note"), "finding_id": p.get("finding_id")})
    return s


def fold(events: list[dict], spec: dict | None = None, strict: bool = False) -> dict:
    """The state after a run of `integrity.detector_status` events. An event that is not allowed is recorded in `rejected` and does not change anything (or raises, if strict), so one bad event cannot hide the rest."""
    spec = spec or load_spec(); s = initial(spec)
    for i, e in enumerate(x for x in events if x["type"] == "integrity.detector_status"):
        try: s = apply(s, e["payload"], e["payload"].get("at") or e["ts"])
        except IntegrityError as err:
            if strict: raise
            s["rejected"].append({"index": i, "detector_id": e["payload"].get("detector_id"), "reason": str(err)})
    return s


def effective(d: dict, now: str, max_age_s: float | None = None) -> str:
    """pass, waived, not_applicable, or open. Only a PASS whose evidence is for the detector's current version (and recent enough) is a pass."""
    if d["status"] == "PASS":
        ev = d["evidence"]
        return "pass" if ev and ev["version"] == d["version"] and (max_age_s is None or (_t(now) - _t(ev["at"])).total_seconds() <= max_age_s) else "open"
    if d["status"] == "WAIVED": return "waived" if d["waiver"] and _t(d["waiver"]["expires_at"]) > _t(now) else "open"            # an expired waiver reopens the detector
    if d["status"] == "NOT_APPLICABLE": return "not_applicable" if d["note"] else "open"
    return "open"


def class_status(state: dict, cls: str, now: str, max_age_s: float | None = None) -> str:
    """CLOSED only when every required detector passes with current evidence. CLOSED_WITH_WAIVERS when the rest pass and an unexpired waiver covers the others. OPEN otherwise, including when nothing has passed."""
    eff = [effective(d, now, max_age_s) for d in state["detectors"].values() if d["class"] == cls]
    if not eff or "open" in eff or all(e == "not_applicable" for e in eff): return "OPEN"
    return "CLOSED_WITH_WAIVERS" if "waived" in eff else "CLOSED"


def report(state: dict, now: str, max_age_s: float | None = None) -> dict:
    out = {}
    for cls in sorted({d["class"] for d in state["detectors"].values()}):
        ds = [d for d in state["detectors"].values() if d["class"] == cls]
        out[cls] = {"status": class_status(state, cls, now, max_age_s), "detectors": [{"id": d["id"], "version": d["version"], "severity": d["severity"], "status": d["status"], "effective": effective(d, now, max_age_s)} for d in sorted(ds, key=lambda x: x["id"])],
                    "blocking": sorted(f"{d['id']} is {d['status']}" + ("" if effective(d, now, max_age_s) != "open" or d["status"] != "PASS" else " but its evidence is not current") for d in ds if effective(d, now, max_age_s) == "open"),
                    "waivers": sorted(({"detector": d["id"], **d["waiver"]} for d in ds if d["status"] == "WAIVED" and d["waiver"]), key=lambda w: w["detector"])}
    return {"classes": out, "rejected": state["rejected"]}
