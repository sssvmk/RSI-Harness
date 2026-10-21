"""Minimal CLI (design §14.4 subset): preflight, verify, inspect, task validate, demo.

Commands that act on a daemon would use the HTTP API; this implementation has no daemon, so the read-only
commands open the run database directly (as `harness inspect` is specified to) and `demo` runs a scripted agent.
"""
from __future__ import annotations
import argparse, asyncio, json, sys, tempfile
from pathlib import Path


def _store(db: str):
    from .eventlog.store import EventStore
    return EventStore(db)


def cmd_verify(a) -> int:
    s = _store(a.db)
    runs = [a.run] if a.run else [r["run_id"] for r in s.list_runs()]
    bad = 0
    for r in runs:
        v = s.verify(r, a.last_n)
        print(f"{r}  {'OK ' if v['ok'] else 'BAD'}  checked={v['checked']}" + ("" if v["ok"] else f"  seq={v['bad_seq']} {v['reason']}"))
        bad += not v["ok"]
    return 1 if bad else 0


def cmd_inspect(a) -> int:
    from .eventlog.projections import fold_all
    s = _store(a.db)
    if not a.run:
        for r in s.list_runs():
            print(f"{r['run_id']}  {r['state']:<18} {r['profile']:<9} owner={r['owner']} events={r['last_seq']} reason={r['terminal_reason']}")
        return 0
    evs = s.events(a.run)
    if not evs:
        print("no such run", file=sys.stderr)
        return 2
    if getattr(a, "telemetry", False):
        from . import telemetry as T
        print(json.dumps(T.categories(evs, a.run), indent=1)); return 0
    rs, b = fold_all("run_state", evs), fold_all("budgets", evs)["agent"]
    print(f"run {a.run}: {rs['state']} (reason: {rs['terminal_reason']}), active {rs['active_ms']} ms, excluded {rs['excluded_ms']} ms")
    print("budgets: " + ", ".join(f"{k} {v['used']:g}/{v['limit']:g}" for k, v in b.items()))
    for e in evs:
        if a.all or e["type"] in ("run.state_changed", "run.terminated", "failure.classified", "approval.requested", "approval.decided", "recovery.uncertain",
                                  "verification.completed", "submission.created", "judge.completed", "selection.made", "confirmation.completed", "budget.exhausted", "stuck_loop.detected"):
            p = e["payload"]
            brief = {k: v for k, v in p.items() if not isinstance(v, (dict, list)) or k in ("result",)}
            print(f"  {e['seq']:>4} {e['ts'][11:23]} {e['type']:<26} {json.dumps(brief)[:110]}")
    print("chain:", "OK" if s.verify(a.run)["ok"] else "BROKEN")
    return 0


def _events_or_fail(a):
    evs = _store(a.db).events(a.run)
    if not evs: print("no such run", file=sys.stderr)
    return evs


def _log_content_access(a, what: str) -> None:
    """A reviewer or admin who reads content leaves a record of who, in which role, and what (design §14.3)."""
    import getpass, time, uuid
    s = _store(a.db); c = s.conn()
    with c: c.execute("INSERT INTO content_access(id,run_id,who,role,what,at) VALUES(?,?,?,?,?,?)", (uuid.uuid4().hex, a.run, a.who or getpass.getuser(), a.role, what, time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime())))


def cmd_events(a) -> int:
    """harness events <run> [--type T] [--errors]: the recorded events, with content gated by role and everything redacted."""
    from . import telemetry as T
    evs = _events_or_fail(a)
    if not evs: return 2
    rows = T.event_rows(evs, a.run, a.role, a.type, a.errors)
    if a.role != "operator": _log_content_access(a, f"events type={a.type or '*'} errors={a.errors}")
    for r in rows: print(json.dumps(r) if a.json else f"{r['run_id']} {r['seq']:>4} {r['ts'][11:23]} {r['type']:<26} {json.dumps(r['payload'])[:140]}")
    return 0


def cmd_usage(a) -> int:
    from . import telemetry as T
    evs = _events_or_fail(a)
    if not evs: return 2
    u = T.usage(evs); print(json.dumps({"run_id": a.run, **u}) if a.json else f"run {a.run}: {u['tokens']['input_tokens']} in, {u['tokens']['output_tokens']} out, {u['tokens']['total']} total; cost: " + (f"{u['cost']['estimated_cost']} {u['cost']['currency']}" if u["cost"] else "unavailable (no price configured)"))
    return 0


def cmd_alerts(a) -> int:
    """harness alerts [--run <id>]: alerts that were raised, and whether each is still active."""
    from .selection import active_alerts
    s = _store(a.db); runs = [a.run] if a.run else [r["run_id"] for r in s.list_runs()]; n = 0
    for rid in runs:
        evs = s.events(rid); live = {(x["rule"], str((x.get("context") or {}).get("tool", ""))) for x in active_alerts(evs)}
        for e in evs:
            if e["type"] in ("alert.raised", "alert.resolved"):
                p = e["payload"]; n += 1; state = "active" if e["type"] == "alert.raised" and (p["rule"], str((p.get("context") or {}).get("tool", ""))) in live else ("resolved" if e["type"] == "alert.resolved" else "superseded")
                print(f"{rid} {e['seq']:>4} {e['type']:<15} {p['rule']:<26} {p.get('severity', ''):<9} value={p.get('value', ''):.3g} {state}" if isinstance(p.get("value"), (int, float)) else f"{rid} {e['seq']:>4} {e['type']}")
    if not n: print("no alerts recorded")
    return 0


def cmd_metrics(a) -> int:
    from . import telemetry as T
    evs = _events_or_fail(a)
    if not evs: return 2
    m = T.run_metrics(evs); print(json.dumps({"run_id": a.run, **m}, indent=None if a.json else 1)); return 0


def cmd_integrity(a) -> int:
    """harness integrity: the status of each evaluation-integrity class, derived from its detectors' recorded status. A detector nobody has run is NOT_RUN and keeps its class open."""
    import time
    from . import integrity as I
    s = _store(a.db); runs = [a.run] if a.run else [r["run_id"] for r in s.list_runs()]
    evs = sorted((e for r in runs for e in s.events(r, types=["integrity.detector_status"])), key=lambda e: (e["ts"], e["run_id"], e["seq"]))
    rep = I.report(I.fold(evs), time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime()), a.max_age_s)
    if a.json: print(json.dumps(rep)); return 0
    for cls, c in rep["classes"].items():
        print(f"{cls:<24} {c['status']}")
        for d in c["detectors"]: print(f"   {d['id']:<30} v{d['version']} {d['severity']:<8} {d['status']:<27} counts as {d['effective']}")
        for w in c["waivers"]: print(f"   waiver on {w['detector']} approved by {w['approved_by']} until {w['expires_at']}: {w['rationale']}")
    for r in rep["rejected"]: print(f"rejected event {r['index']}: {r['reason']}")
    return 0


def cmd_plugins(a) -> int:
    """harness plugins: the plugin kinds the harness defines, what each must implement, and the profile each needs."""
    if getattr(a, "contract", False):
        from .plugin_contract import render
        print(render(), end=""); return 0
    from .plugins import KINDS
    for k, v in sorted(KINDS.items()):
        print(f"{k:<13} interface {v['interface_version']}  needs {v['min_profile']:<8} grants {','.join(v['grants']):<22} {v['summary'].split('.')[0]}")
        if a.methods: print("              methods: " + ", ".join(f"{m}({'write' if s['effect'] == 'write' else 'read'})" for m, s in v["methods"].items()))
    return 0


def cmd_task_validate(a) -> int:
    from .eval.task import validate_task
    errs = validate_task(a.dir)
    print("OK" if not errs else "\n".join(f"error: {e}" for e in errs))
    return 1 if errs else 0


def cmd_preflight(a) -> int:
    from .app import Harness
    from .config import load_config
    from .gateway.gateway import ScriptedTransport
    h = Harness(load_config(a.config), ScriptedTransport([]))
    try:
        r = h.preflight()
    finally:
        h.close()
    for c in r["checks"]:
        print(f"{c['id']}  {'pass' if c['ok'] else ('FAIL' if c['severity'] == 'error' else 'warn')}  {c['message']}")
    return 0 if r["ok"] else 1


def cmd_demo(a) -> int:
    """Runs a scripted agent end to end in a temp directory (no API key or network needed)."""
    from .app import Harness
    from .config import load_config
    from .gateway.gateway import ScriptedTransport
    from .loop.verifier import CommandVerifier
    tmp = Path(tempfile.mkdtemp(prefix="harness-demo-"))

    def resp(calls):
        return (200, {"choices": [{"message": {"role": "assistant", "content": None, "tool_calls": [
            {"id": f"c{i}", "type": "function", "function": {"name": n.replace(".", "-"), "arguments": json.dumps(x)}} for i, (n, x) in enumerate(calls)]}, "finish_reason": "tool_calls"}],
            "usage": {"prompt_tokens": 100, "completion_tokens": 20}})
    script = [resp([("fs.write", {"path": "/workspace/hello.py", "content": "print('hello from the sandbox')\n"})]),
              resp([("shell.run", {"cmd": "python3 hello.py"})]), resp([("finish", {"summary": "wrote and ran hello.py"})])]
    cfg = load_config({"profile": "core", "storage": {"event_log": {"kind": "sqlite", "path": str(tmp / "log.db")}, "blobs": {"kind": "fs", "root": str(tmp / "blobs")}},
                       "models": [{"ref": "main", "provider": "openai", "model_id": "scripted", "window": 100000, "secret": "k"}], "sandbox": {"backend": "local", "allow_dev_tier": True},
                       "budgets": {"turns": 10, "tokens": 100000, "time_s": 120}})
    from .credentials import DictSecretProvider
    h = Harness(cfg, ScriptedTransport(script), DictSecretProvider({"k": "demo-not-a-real-key"}))

    async def go():
        run = await h.run_to_end("demo", "write and run a hello script", verifier=CommandVerifier(["true"]))
        return run.run_id
    rid = asyncio.run(go())
    h.close()
    print(f"database: {tmp / 'log.db'}\n")
    return cmd_inspect(argparse.Namespace(db=str(tmp / "log.db"), run=rid, all=a.all))


def main(argv: list[str] | None = None) -> int:
    p = argparse.ArgumentParser(prog="harness")
    sp = p.add_subparsers(dest="cmd", required=True)
    v = sp.add_parser("verify", help="verify the hash chain"); v.add_argument("--db", required=True); v.add_argument("--run"); v.add_argument("--last-n", type=int); v.set_defaults(fn=cmd_verify)
    i = sp.add_parser("inspect", help="timeline, budgets and failures"); i.add_argument("--db", required=True); i.add_argument("--run"); i.add_argument("--all", action="store_true"); i.add_argument("--telemetry", action="store_true", help="each telemetry category, marked available or unavailable"); i.set_defaults(fn=cmd_inspect)
    for name, fn, h in (("events", cmd_events, "recorded events (content gated by role)"), ("usage", cmd_usage, "tokens and cost"), ("alerts", cmd_alerts, "alerts raised"), ("metrics", cmd_metrics, "counters and histograms")):
        q = sp.add_parser(name, help=h); q.add_argument("--db", required=True); q.add_argument("--run", required=(name != "alerts")); q.add_argument("--json", action="store_true"); q.set_defaults(fn=fn)
        if name == "events": q.add_argument("--type"); q.add_argument("--errors", action="store_true"); q.add_argument("--role", choices=("operator", "reviewer", "admin"), default="operator"); q.add_argument("--who")
    ig = sp.add_parser("integrity", help="status of each evaluation-integrity class, from its detectors"); ig.add_argument("--db", required=True); ig.add_argument("--run"); ig.add_argument("--max-age-s", type=float); ig.add_argument("--json", action="store_true"); ig.set_defaults(fn=cmd_integrity)
    pl = sp.add_parser("plugins", help="the plugin kinds and their contracts"); pl.add_argument("--methods", action="store_true"); pl.add_argument("--contract", action="store_true"); pl.set_defaults(fn=cmd_plugins)
    t = sp.add_parser("task", help="task package tools"); ts = t.add_subparsers(dest="sub", required=True)
    tv = ts.add_parser("validate"); tv.add_argument("dir"); tv.set_defaults(fn=cmd_task_validate)
    pf = sp.add_parser("preflight", help="run PF-01..PF-16 against a config"); pf.add_argument("--config", required=True); pf.set_defaults(fn=cmd_preflight)
    d = sp.add_parser("demo", help="scripted end-to-end run, no network"); d.add_argument("--all", action="store_true"); d.set_defaults(fn=cmd_demo)
    a = p.parse_args(argv)
    return a.fn(a)


if __name__ == "__main__":
    sys.exit(main())
