"""Evaluation-integrity closure (design §15.6): detector-level accountability, with the class status derived from its detectors."""
import contextlib, io, json
import pytest
from conftest import VEC, oa
from harness import cli, integrity as I

V = json.loads((VEC / "integrity-vectors.json").read_text())
NOW = "2026-10-21T12:00:00Z"
ev = lambda d, status, by="ann", ts="2026-10-21T08:00:00Z", **kw: {"type": "integrity.detector_status", "ts": ts, "payload": {"detector_id": d, "status": status, "by": by, **kw}}
PASS = lambda d, by="ann", v="1", **kw: ev(d, "PASS", by, evidence_ref=f"ev:{d}", evidence_version=v, run_id="r1", **kw)


def test_every_scenario_derives_the_same_class_status_rejections_and_history_as_the_shared_vectors():
    for c in V["scenarios"]:
        s = I.fold(c["events"], V["spec"]); r = I.report(s, c["now"], c["max_age_s"])
        got = {"classes": {k: v["status"] for k, v in r["classes"].items()}, "rejected": [x["index"] for x in s["rejected"]], "effective": {d["id"]: d["effective"] for v in r["classes"].values() for d in v["detectors"]},
               "history": {k: len(d["history"]) for k, d in s["detectors"].items()}, "blocking": {k: v["blocking"] for k, v in r["classes"].items()}, "waivers": {k: [w["detector"] for w in v["waivers"]] for k, v in r["classes"].items()}}
        assert got == c["expect"], c["name"]


def test_the_harnesss_own_classes_all_start_open_because_nothing_has_demonstrated_a_pass():
    spec = I.load_spec(); r = I.report(I.fold([]), NOW)
    assert set(r["classes"]) == {"tampering", "state_carryover", "over_granular_feedback", "selection_bias", "clock_reset", "stale_grants"} == set(spec["classes"])
    assert {c["status"] for c in r["classes"].values()} == {"OPEN"} and all(d["status"] == "NOT_RUN" for c in r["classes"].values() for d in c["detectors"])
    assert "protected_surface_hash is NOT_RUN" in r["classes"]["tampering"]["blocking"]
    for cls, c in spec["classes"].items(): assert c["required"] and all({"id", "version", "severity", "automatic", "description"} <= set(d) for d in c["required"]), cls


def test_a_class_closes_only_when_every_required_detector_passes_with_current_evidence():
    spec = V["spec"]; s = I.fold([PASS("d1")], spec); assert I.class_status(s, "c1", NOW) == "OPEN"                                                      # d2 has not run
    s = I.fold([PASS("d1"), PASS("d2")], spec); assert I.class_status(s, "c1", NOW) == "CLOSED"
    for blocker in ("FAIL", "OPEN"):
        s = I.fold([PASS("d1"), PASS("d2"), ev("d1", "FAIL", ts="2026-10-21T09:00:00Z")] + ([ev("d1", "OPEN", ts="2026-10-21T09:30:00Z")] if blocker == "OPEN" else []), spec); assert I.class_status(s, "c1", NOW) == "OPEN", blocker
    pending = I.fold([PASS("d1"), PASS("d2"), ev("d2", "FAIL", ts="2026-10-21T09:00:00Z"), ev("d2", "FIXED_PENDING_VERIFICATION", note="patched", ts="2026-10-21T09:10:00Z")], spec); assert I.class_status(pending, "c1", NOW) == "OPEN"   # awaiting verification
    for status in ("NOT_RUN", "WAIVED", "NOT_APPLICABLE"): assert I.effective({"status": status, "evidence": None, "waiver": None, "note": None, "version": "1"}, NOW) != "pass", status      # never equivalent to PASS


def test_strict_mode_raises_on_a_refused_change_and_the_default_records_it_without_changing_anything():
    bad = [ev("d1", "PASS")]                                                                                                                              # no evidence, no run
    with pytest.raises(I.IntegrityError, match="needs an evidence reference"): I.fold(bad, V["spec"], strict=True)
    s = I.fold(bad, V["spec"]); assert s["rejected"][0]["detector_id"] == "d1" and s["detectors"]["d1"]["status"] == "NOT_RUN" and s["detectors"]["d1"]["history"] == []
    for events, needle in [([ev("d2", "FAIL"), ev("d2", "FIXED_PENDING_VERIFICATION", note="x"), PASS("d2", by="ann")], "someone other than the person who made it"), ([PASS("d3", v="1")], "version"), ([ev("d2", "NOT_APPLICABLE")], "needs the reason"),
                           ([ev("d2", "FAIL", owner="ann"), ev("d2", "WAIVED", waiver={"approved_by": "ann", "rationale": "x", "expires_at": "2027-01-01T00:00:00Z"})], "other than the owner"), ([ev("nope", "FAIL")], "unknown detector")]:
        try: I.fold(events, V["spec"], strict=True)
        except I.IntegrityError as e: assert needle in str(e), events
        else: pytest.fail(f"not refused: {events}")


def test_the_history_of_every_change_is_kept_and_a_closed_class_still_shows_its_findings():
    s = I.fold([ev("d1", "FAIL", ts="2026-10-21T08:00:00Z", finding_id="F-1", note="leaked"), ev("d1", "FIXED_PENDING_VERIFICATION", note="patched", ts="2026-10-21T08:30:00Z"), PASS("d1", by="bob", ts="2026-10-21T09:00:00Z"), PASS("d2")], V["spec"])
    h = s["detectors"]["d1"]["history"]; assert [x["to"] for x in h] == ["FAIL", "FIXED_PENDING_VERIFICATION", "PASS"] and h[0]["finding_id"] == "F-1" and I.class_status(s, "c1", NOW) == "CLOSED"
    assert [(x["by"]) for x in h] == ["ann", "ann", "bob"]


async def test_the_command_reports_each_class_from_the_events_in_the_log(mk):
    h = mk([oa("d")]); run, _ = await h.create_run("u", "go")
    def call(*argv):
        out = io.StringIO()
        with contextlib.redirect_stdout(out): code = cli.main(list(argv))
        return code, out.getvalue()
    db = str(h.store.path); code, text = call("integrity", "--db", db); assert code == 0 and text.count("OPEN") >= 6 and "NOT_RUN" in text and "CLOSED" not in text
    await run.emit("integrity.detector_status", {"detector_id": "feedback_byte_cap", "status": "PASS", "by": "ci", "evidence_ref": "ev:1", "evidence_version": "1", "run_id": run.run_id})
    await run.emit("integrity.detector_status", {"detector_id": "protected_surface_hash", "status": "PASS", "by": "ci"})                                          # refused: no evidence
    code, text = call("integrity", "--db", db, "--json"); rep = json.loads(text)
    assert rep["classes"]["over_granular_feedback"]["status"] == "CLOSED" and rep["classes"]["tampering"]["status"] == "OPEN" and rep["rejected"][0]["detector_id"] == "protected_surface_hash"
    assert "rejected event 1" in call("integrity", "--db", db)[1] and call("integrity", "--db", db, "--max-age-s", "0")[0] == 0
