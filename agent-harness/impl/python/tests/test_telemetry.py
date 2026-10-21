"""Operator telemetry (design §14.4, §15.2), trajectory metrics (§13.11), and selection monitoring (§9.13)."""
import io, json, contextlib
import pytest
from conftest import VEC, events_of, oa
from harness import cli, selection as S, telemetry as T
from harness.tools.runtime import ToolContext

V = json.loads((VEC / "telemetry-vectors.json").read_text()); RID = V["events"][0]["run_id"]


# ------------------------------------------------------------------ pure functions, against the shared vectors
def test_metrics_trajectory_usage_and_categories_match_the_shared_vectors():
    E = V["events"]
    assert T.run_metrics(E) == V["run_metrics"] and T.trajectory_metrics(E) == V["trajectory"] and T.usage(E) == V["usage"] and T.categories(E, RID) == V["categories"]
    assert S.selection_stats(E) == V["selection_stats"] and S.active_alerts(E) == V["active_alerts"]
    for k, kw in (("operator", {"role": "operator"}), ("reviewer", {"role": "reviewer"}), ("errors", {"role": "operator", "errors_only": True}), ("type_tool", {"role": "operator", "type_filter": "tool.call.failed"})): assert T.event_rows(E, RID, **kw) == V["rows"][k], k
    assert T.categories([e for e in E if e["type"] != "run.created"], RID) == V["rows"]["no_manifest"]


def test_the_required_metrics_are_all_reported_with_the_right_numbers():
    c = T.run_metrics(V["events"])["counters"]
    assert (c["turns"], c["tool_failures"], c["tokens"]["total"], c["retries"], c["cost"]) == (2, 3, 540, {"tool": 1, "model": 3, "total": 4}, 0.42)
    assert c["approvals"] == {"requested": 3, "granted": 2, "denied": 1, "expired": 0, "automatic": 1} and c["budget_terminations"] == {"tokens": 1} and c["stuck_loop"] == {"warnings": 1, "escalations": 0, "terminations": 1}
    assert c["eval_integrity_events"] == {"total": 1, "by_code": {"eval_integrity.over_granular_feedback": 1}} and c["selection_error_rate"] == 0.6 and c["log_integrity_alerts"] == 1
    h = T.run_metrics(V["events"])["histograms"]["model_latency_ms"]; assert (h["count"], h["min"], h["p50"], h["p95"], h["max"]) == (3, 120, 300, 900, 900)
    assert T.run_metrics([])["counters"]["cost"] is None and T.run_metrics([])["histograms"]["model_latency_ms"]["p50"] is None                                # nothing measured is not zero


def test_trajectory_metrics_cover_steps_errors_retries_budget_denials_approvals_safety_and_stuck_loops():
    t = T.trajectory_metrics(V["events"])
    assert (t["steps"], t["tool_calls"], t["tool_errors"], t["retries"], t["policy_denials"], t["approvals"], t["stuck_loops"]) == (2, 5, 3, 4, 1, 3, 2)
    assert t["budget_use"] == {"cost": {"used": 0.42, "limit": 2}, "tokens": {"used": 540, "limit": 1000}, "turns": {"used": 3, "limit": 10}}
    assert t["safety_events"] == {"policy_denials": 1, "quarantined": 1, "integrity_alerts": 1, "definition_mismatches": 1, "total": 4}


def test_operators_see_metadata_reviewers_see_content_and_secrets_are_redacted_for_everyone():
    op = json.dumps(T.event_rows(V["events"], RID, "operator")); rv = json.dumps(T.event_rows(V["events"], RID, "reviewer"))
    assert "hunter2" not in op and "hunter2" not in rv and "sk-abcdefghijklmnop12345" not in op and "sk-abcdefghijklmnop12345" not in rv and "[REDACTED]" in rv
    assert "gated" in op and "gated" not in rv and '"/workspace/a"' not in op and '"/workspace/a"' in rv and all(r["run_id"] == RID for r in T.event_rows(V["events"], RID))        # every finding carries its run id
    assert {r["type"] for r in T.event_rows(V["events"], RID, errors_only=True)} >= {"tool.call.failed", "model.failed", "definition.mismatch", "integrity.alert", "alert.raised"}


def test_a_category_is_unavailable_when_it_is_not_collected_never_inferred_from_missing_events():
    c = T.categories([e for e in V["events"] if e["type"] != "run.created"], RID)["categories"]
    assert c["model_and_version"] == {"status": "unavailable", "reason": "this run has no provenance manifest"}                                                    # not guessed from the model.requested events
    assert c["usage_and_cost"]["value"]["cost"] is not None and T.categories(V["events"][:3], RID)["categories"]["usage_and_cost"]["note"].startswith("cost is unavailable")
    quiet = T.categories(V["events"][:2], RID)["categories"]; assert quiet["interventions"]["status"] == "available" and quiet["interventions"]["value"]["approvals"]["requested"] == 0      # collected, and none happened
    T.ALERTS_COLLECTED = False
    try: assert T.categories(V["events"], RID)["categories"]["alerts"] == {"status": "unavailable", "reason": "alerting is not implemented in this build"}
    finally: T.ALERTS_COLLECTED = True


# ------------------------------------------------------------------ selection monitoring
def test_alert_rules_follow_the_shared_vectors_and_a_sustained_breach_is_one_alert():
    for c in V["monitor"]:
        m, got = S.SelectionMonitor(c["cfg"]), []
        for i, x in enumerate(c["errors"]):
            r = m.observe(bool(x), {"tool": "t"})
            if r: got.append({"i": i, "event": r["event"], "severity": r["payload"].get("severity"), "previous": r["payload"].get("previous_severity"), "value": r["payload"]["value"], "window": r["payload"]["window"]})
        assert got == c["transitions"], c["name"]
    assert not V["monitor"][0]["transitions"]                                                                                                                         # below the minimum sample nothing is raised


def test_alert_rules_are_validated_and_invalid_ones_are_refused():
    for c in V["rules"]["valid"]: assert S.rules(c["in"]) == c["out"]
    assert S.rules({}) == {"window": 200, "min_sample": 30, "warn": 0.05, "critical": 0.10}
    for bad in V["rules"]["invalid"]:
        with pytest.raises(ValueError): S.rules(bad)


def test_the_offline_benchmark_separates_the_kinds_of_selection_error():
    r = S.evaluate_selection(V["benchmark"]["benchmark"], V["benchmark"]["decisions"]); assert r == V["benchmark"]["result"]
    assert (r["wrong_tool_rate"], r["invalid_call_rate"], r["unnecessary_call_rate"], r["unsafe_selection_rate"], r["miss_rate"], r["recovery_rate"], r["error_rate"]) == (0.6, 0.2, 0.2, 0.2, 1 / 3, 1 / 3, 0.6)
    assert r["by_model"]["m2"]["decisions"] == 3 and r["by_selector_version"]["v2"]["wrong_tool"] == 1 and r["by_tool"]["a"]["wrong_tool_rate"] == 0 and r["by_task"]["t1"]["wrong_tool"] == 1       # per model, selector version, tool and task


# ------------------------------------------------------------------ in real runs
CALLS = lambda n, tool="nope.tool": oa(calls=[(tool, {}) for _ in range(n)])


async def test_a_run_of_bad_selections_raises_one_alert_and_resolves_it_once_selection_recovers(mk):
    script = [CALLS(5)] + [oa(calls=[("fs.glob", {"pattern": "/workspace/*"})] * 5) for _ in range(2)] + [oa("d")]
    h = mk(script, alerts={"selection": {"window": 10, "min_sample": 5, "warn": 0.2, "critical": 0.5}}); run = await h.run_to_end("u", "go")
    raised, resolved = events_of(h, run, "alert.raised"), events_of(h, run, "alert.resolved")
    assert [(a["payload"]["rule"], a["payload"]["severity"]) for a in raised] == [("selection.error_rate", "critical")] and len(resolved) == 1 and raised[0]["seq"] < resolved[0]["seq"]
    assert raised[0]["payload"]["context"] == {"tool": "nope.tool"} and raised[0]["payload"]["threshold"] == 0.5 and "args" not in json.dumps(raised[0]["payload"])               # the tool, never the payload
    assert not S.active_alerts(h.store.events(run.run_id))


async def test_an_unauthorized_selection_that_reaches_execution_is_blocked_and_is_an_immediate_alert(mk):
    h = mk([oa("d")]); run, loop = await h.create_run("u", "go"); tool = h.registry.get("fs.glob")
    ctx = ToolContext(run=run, svc=h.svc, sandbox=loop.sandbox(), registry=h.registry, agent=loop.agent)
    r = await h.runtime._run_with_retries(run, "c1", "fs.glob", {"pattern": "/workspace/*"}, tool, tool.manifest, ctx, 1, decision="deny")          # forced: it cannot happen by construction
    a = events_of(h, run, "alert.raised")[0]["payload"]; assert (a["rule"], a["severity"], a["context"]["tool"], a["context"]["decision"]) == ("selection.unsafe_execution", "security", "fs.glob", "deny")
    assert not events_of(h, run, "tool.call.started") and r["ok"] is False
    await h.runtime._run_with_retries(run, "c2", "fs.glob", {"pattern": "/workspace/*"}, tool, tool.manifest, ctx, 2, decision="require_approval", approved=False)
    assert [e["payload"]["context"]["decision"] for e in events_of(h, run, "alert.raised")] == ["deny", "unapproved"] and not events_of(h, run, "tool.call.started")


async def test_the_monitor_is_rebuilt_from_the_log_after_a_restart(mk):
    h = mk([CALLS(4), oa("d")], alerts={"selection": {"window": 10, "min_sample": 5, "warn": 0.2, "critical": 0.5}}); run = await h.run_to_end("u", "go")
    m = S.SelectionMonitor.from_events(h.store.events(run.run_id), {"window": 10, "min_sample": 5, "warn": 0.2, "critical": 0.5}); assert list(m.window) == [1, 1, 1, 1] and m.active is None


# ------------------------------------------------------------------ the CLI
def run_cli(*argv):
    out = io.StringIO()
    with contextlib.redirect_stdout(out): code = cli.main(list(argv))
    return code, out.getvalue()


async def cli_run(mk):
    h = mk([oa(calls=[("fs.write", {"path": "/workspace/n.txt", "content": "password=hunter2 is the secret"}), ("nope.tool", {})]), oa("done")]); run = await h.run_to_end("u", "my key is sk-abcdefghijklmnop12345")
    h.store.conn().commit(); return h, run


async def test_the_cli_lists_events_with_content_gated_redacted_and_the_reviewers_access_logged(mk):
    h, run = await cli_run(mk); db = str(h.store.path)
    code, out = run_cli("events", "--db", db, "--run", run.run_id, "--json"); rows = [json.loads(l) for l in out.splitlines()]
    assert code == 0 and rows and all(r["run_id"] == run.run_id for r in rows) and "hunter2" not in out and "sk-abcdefghijklmnop12345" not in out and "gated" in out and all(r["payload"]["args"] == "[gated: reviewer or admin role required]" for r in rows if r["type"] == "tool.call.requested")                      # a resource identifier (a reconcile subject) is metadata; the arguments and content are not
    code, out = run_cli("events", "--db", db, "--run", run.run_id, "--role", "reviewer", "--who", "rita", "--type", "tool.call.requested", "--json"); assert "hunter2" not in out and "n.txt" in out and "[REDACTED]" in out
    rows = h.store.conn().execute("SELECT who, role, run_id, what FROM content_access").fetchall(); assert [(r["who"], r["role"], r["run_id"]) for r in rows] == [("rita", "reviewer", run.run_id)] and "tool.call.requested" in rows[0]["what"]
    code, out = run_cli("events", "--db", db, "--run", run.run_id, "--errors", "--json"); types = {json.loads(l)["type"] for l in out.splitlines()}
    assert "tool.call.failed" in types and not types & {"fs.write", "tool.call.completed", "model.responded", "user.message"} and types <= {"tool.call.failed", "run.terminated", "failure.classified"}      # a run that ended abnormally explains itself
    assert run_cli("events", "--db", db, "--run", "NOSUCHRUN")[0] == 2


async def test_the_cli_reports_usage_metrics_and_each_telemetry_category(mk):
    h, run = await cli_run(mk); db = str(h.store.path)
    code, out = run_cli("usage", "--db", db, "--run", run.run_id, "--json"); u = json.loads(out); assert u["run_id"] == run.run_id and u["tokens"]["total"] > 0 and u["by_model"]
    code, out = run_cli("metrics", "--db", db, "--run", run.run_id, "--json"); m = json.loads(out); assert m["counters"]["tool_failures"] == 1 and m["counters"]["selection_error_rate"] == 0.5 and m["run_id"] == run.run_id
    code, out = run_cli("inspect", "--db", db, "--run", run.run_id, "--telemetry"); c = json.loads(out); assert c["run_id"] == run.run_id and set(c["categories"]) == {"status", "model_and_version", "tool_calls", "errors_and_termination", "usage_and_cost", "interventions", "alerts"}
    assert all(v["status"] in ("available", "unavailable") for v in c["categories"].values()) and c["categories"]["model_and_version"]["status"] == "available"


async def test_the_cli_lists_alerts_by_run_and_across_runs(mk):
    h = mk([CALLS(5), oa("d")], alerts={"selection": {"window": 10, "min_sample": 5, "warn": 0.2, "critical": 0.5}}); run = await h.run_to_end("u", "go"); db = str(h.store.path)
    code, out = run_cli("alerts", "--db", db, "--run", run.run_id); assert "selection.error_rate" in out and "critical" in out and "active" in out and run.run_id in out
    assert "selection.error_rate" in run_cli("alerts", "--db", db)[1]
    h2 = mk([oa("d")]); r2 = await h2.run_to_end("u", "go"); assert "no alerts recorded" in run_cli("alerts", "--db", str(h2.store.path), "--run", r2.run_id)[1]
