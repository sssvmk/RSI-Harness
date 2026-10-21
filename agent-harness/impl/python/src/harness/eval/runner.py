"""EvalRunner: submit -> snapshot -> fresh judge sandbox -> steps -> report -> feedback (§13.3-13.6)."""
from __future__ import annotations
from ..failure import observation
from ..telemetry import trajectory_metrics
from .outcome import contract_for, evaluate_contract
from ..plugin_consumers import run_evaluators
import json, shutil
import jsonschema
from ..budgets import BudgetController
from ..canonical import sha256_hex
from ..clock import format_ts
from ..ids import ulid
from ..loop.verifier import Verifier
from ..sandbox.snapshots import restore_snapshot, take_snapshot
from ..stats import bootstrap_ci, seed_for
from ..tools.registry import load_schema
from ..tools.runtime import ToolError
from .task import Task


def truncate_utf8(s: str, max_bytes: int) -> str:
    return s.encode("utf-8")[:max_bytes].decode("utf-8", "ignore")


def feedback_text(report: dict, level: str) -> str:
    """The feedback before any size limit: only the score and the judge-authored feedback_text ever reach the agent, never raw test output (§13.5)."""
    if level == "none":
        return "Submission received."
    if not report["valid"]:
        return "Submission was invalid: it could not be scored."
    txt = f"Score: {report['primary']}"
    if level == "score_and_summary" and report.get("feedback_text"):
        txt += "\n" + report["feedback_text"]
    return txt


def shape_feedback(report: dict, level: str, max_bytes: int) -> str:
    return truncate_utf8(feedback_text(report, level), max_bytes)


class EvalRunner:
    def __init__(self, h, task: Task):
        self.h, self.task, self.spec = h, task, task.spec
        self._schema = load_schema("score.report.v1.json")

    # ---- scoring a snapshot in a fresh judge sandbox ----
    async def _score(self, run, manifest_hash: str, env_extra: dict | None = None, tests_sub: str | None = None, events: dict | None = None) -> dict:
        h = self.h
        judge = await h.sandboxes.create(run, "judge")
        try:
            restore_snapshot(manifest_hash, h.blobs, judge)
            src = self.task.tests_dir / tests_sub if tests_sub else self.task.tests_dir
            dst = judge.executor.roots["/tests"]
            shutil.copytree(src, dst, dirs_exist_ok=True)
            if events is not None:
                await run.emit("judge.started", {"submission_id": events["submission_id"], "sandbox_id": judge.sandbox_id})
            report, errors = None, []
            for step in self.spec["scoring"]["steps"]:
                r = await judge.executor.exec_run(step["cmd"], cwd=step.get("cwd", "/workspace"), user=step["run_as"],
                                                  timeout_s=step["timeout_s"], env=env_extra or {})
                if r["timed_out"] or r["exit_code"] != 0:
                    errors.append(f"step {step['id']} {'timed out' if r['timed_out'] else 'exited ' + str(r['exit_code'])}")
                    break
            if not errors:
                try:
                    raw = judge.executor.fs_read(self.spec["scoring"]["report_path"])["content"]
                    report = json.loads(raw)
                    jsonschema.validate(report, self._schema)
                except (ValueError, jsonschema.ValidationError, Exception) as e:
                    errors.append(f"report invalid: {getattr(e, 'message', e)}")
                    report = None
            if report is None:
                report = {"schema_version": 1, "valid": False, "primary": 0.0, "metrics": {}, "evaluator": {"kind": "deterministic"}, "errors": errors}
            if self.spec.get("visibility", "hidden") == "public": report["generalization_claim"] = False   # public tests are development aids and cannot support a generalization claim (PRD §3.6)
            report["evaluator"] = {"kind": "deterministic", "model_ref": None}  # set by the control plane, never by judge scripts (§13.4)
            report.setdefault("errors", []).extend(e for e in errors if e not in report.get("errors", []))
            return report
        finally:
            await h.sandboxes.destroy(run, judge)

    # ---- submit ----
    async def submit(self, run, work, note: str | None) -> str:
        h = self.h
        bc = BudgetController(run)
        rid, ex = await bc.reserve({"submissions": 1})           # counted before the job is launched, and atomic (§7.4.4)
        if ex:
            raise ToolError("submit.budget_exhausted", "tool", "no submissions remaining", hint="Call finish; your best submission will be selected")
        sid = ulid()
        await run.emit("submission.created", {"submission_id": sid, "snapshot_id": None, "note": note})
        await bc.commit(rid, {"submissions": 1})
        await run.transition("SCORING", f"submission {sid}")
        return await self._process(run, work, sid)

    async def resume_submission(self, run, work) -> str | None:
        """Crash recovery (§5.9): continue the newest unfinished submission from wherever it stopped."""
        subs = await run.proj("submissions")
        return await self._process(run, work, list(subs)[-1]) if subs else None

    async def _process(self, run, work, sid: str) -> str:
        h = self.h
        s = (await run.proj("submissions"))[sid]
        if s["status"] == "created":
            h.backend.pause(work)
            snap = take_snapshot(work, h.blobs)
            snapshot_id = ulid()
            await h.writer.execute("INSERT INTO snapshots(snapshot_id,run_id,taken_at_seq,manifest_hash,kind,created_at) VALUES(?,?,?,?,?,?)",
                                   (snapshot_id, run.run_id, h.store.last_seq(run.run_id), snap["manifest_hash"], "submission", format_ts(h.clock.now_ms())))
            await run.emit("snapshot.created", {"snapshot_id": snapshot_id, "submission_id": sid, "manifest_hash": snap["manifest_hash"],
                                                "chain_head": h.store.head_hash(run.run_id)})
            s = (await run.proj("submissions"))[sid]
        if s["status"] in ("snapshotted", "judging"):
            row = h.store.conn().execute("SELECT manifest_hash FROM snapshots WHERE snapshot_id=?", (s["snapshot_id"],)).fetchone()
            report = await self._score(run, row["manifest_hash"], events={"submission_id": sid})
            ref = h.blobs.put_json(report)
            await run.emit("judge.completed", {"submission_id": sid, "valid": report["valid"], "primary": report["primary"], "score_report_ref": ref,
                                               "metrics": report["metrics"], "evaluator_kind": report["evaluator"]["kind"]})
            s = (await run.proj("submissions"))[sid]
        report = h.blobs.get_json(s["report_ref"])
        fb = self.spec.get("submission", {}).get("feedback", {"level": "score", "max_bytes": 2048})
        raw = feedback_text(report, fb["level"]); cap = fb.get("max_bytes", 2048); text = truncate_utf8(raw, cap)
        if s["status"] != "feedback":   # after a crash the feedback may already be recorded: rebuild it, don't re-record it
            await run.emit("feedback.delivered", {"submission_id": sid, "level": fb["level"], "bytes": len(text.encode())})
            if len(raw.encode()) > cap:   # the byte cap held, and the fact that it had to is a signal of leaked or over-granular feedback (PRD §3.7 Evaluation-Integrity Errors)
                await run.emit("failure.observed", observation(h.rm.rules, "eval_integrity.over_granular_feedback", sid, {"bytes_offered": len(raw.encode()), "cap": cap, "level": fb["level"]}))
        h.backend.resume(work)
        if await run.state() == "SCORING":
            await run.transition("RUNNING", "feedback.delivered")
        return text

    # ---- selection and confirmation ----
    async def select(self, run) -> dict | None:
        subs = await run.proj("submissions")
        scored = [(sid, s) for sid, s in subs.items() if s["valid"] and s["primary"] is not None]
        if not scored:
            return None
        maxi = self.spec["scoring"]["primary"]["direction"] == "maximize"
        tie = self.spec.get("selection", {}).get("tie_break", "earliest")
        best = scored[0]
        for sid, s in scored[1:]:
            better = s["primary"] > best[1]["primary"] if maxi else s["primary"] < best[1]["primary"]
            equal = s["primary"] == best[1]["primary"]
            if better or (equal and tie == "latest"):
                best = (sid, s)
        n_scored = len([1 for s in subs.values() if s["status"] in ("scored", "feedback")])
        await run.emit("selection.made", {"rule": self.spec.get("selection", {}).get("rule", "best_primary"), "submission_id": best[0], "n_submissions": n_scored})
        return {"submission_id": best[0], "primary": best[1]["primary"], "n_submissions": n_scored, "snapshot_id": best[1]["snapshot_id"]}

    async def confirm(self, run, sel: dict) -> dict:
        cfg = self.spec.get("confirmation", {"mode": "reseed", "trials": 3})
        row = self.h.store.conn().execute("SELECT manifest_hash FROM snapshots WHERE snapshot_id=?", (sel["snapshot_id"],)).fetchone()
        primaries = []
        for i in range(cfg.get("trials", 3)):
            if cfg["mode"] == "reseed":
                rep = await self._score(run, row["manifest_hash"], env_extra={"HARNESS_SEED": str(int(sha256_hex(f"{run.run_id}:{sel['submission_id']}:{i}")[:12], 16)), "HARNESS_CONFIRM": "1"})
            else:
                rep = await self._score(run, row["manifest_hash"], env_extra={"HARNESS_CONFIRM": "1"}, tests_sub="confirm")
            primaries.append(rep["primary"] if rep["valid"] else float("nan"))
            await run.emit("confirmation.trial", {"submission_id": sel["submission_id"], "trial": i, "valid": rep["valid"], "primary": rep["primary"]})
        valid = [p for p in primaries if p == p]
        if len(valid) != len(primaries):
            result = {"valid": False, "trials": len(primaries), "reason": "a confirmation trial was invalid"}
        else:
            ci = bootstrap_ci(valid, seed_for(run.run_id, "confirm:" + sel["submission_id"]), self.spec.get("replication", {}).get("confidence", 0.95), 2000)
            result = {"valid": True, "trials": len(valid), "mean": ci["mean"], "lower": ci["lower"], "upper": ci["upper"],
                      "unconfirmed_upper_bound": sel["primary"], "submissions_used": sel["n_submissions"]}
        await run.emit("confirmation.completed", {"rule": cfg["mode"], "submission_id": sel["submission_id"], "n_submissions": sel["n_submissions"], "result": result})
        return result


class EvalVerifier(Verifier):
    """Completion evidence for evaluation runs: selected submission passes; confirmed when >=2 were scored (§6.3, §13.6)."""

    def __init__(self, runner: EvalRunner):
        self.r = runner

    async def verify(self, run, sandbox) -> dict:
        sel = await self.r.select(run)
        value, conf, detail, evidence, report = None, None, "no valid scored submission", {}, {}
        if sel is not None:
            value, report = sel["primary"], next(({"valid": True, "primary": e["payload"]["primary"], "metrics": e["payload"].get("metrics") or {}} for e in reversed(self.r.h.store.events(run.run_id, types=["judge.completed"])) if e["payload"]["submission_id"] == sel["submission_id"]), {})
            if sel["n_submissions"] >= 2:
                conf = await self.r.confirm(run, sel)
                if not conf["valid"]: value, detail, evidence = None, conf["reason"], {"selection": sel, "confirmation": conf}
                else: value = conf["mean"]
        # the verdict comes from the outcome contract, fixed before execution, and from evidence (§13.11); a task without a contract gets one from its pass threshold
        traj = trajectory_metrics(self.r.h.store.events(run.run_id))                       # the transcript's deterministic metrics, recorded apart from the outcome (§13.11)
        await run.emit("trajectory.analyzed", {"task_id": self.r.spec["id"], "metrics": traj})
        contract = contract_for(self.r.spec)
        read = (lambda p: sandbox.executor.fs_read(p)["content"]) if sandbox is not None and hasattr(sandbox, "executor") else None
        ag = getattr(self.r, "agent", None) or self.r.h.default_agent()              # who is under test, so that an evaluator's independence can be derived
        who = {"model_ref": ag["model_ref"], "credential_ref": self.r.h.models.get(ag["model_ref"], {}).get("secret"), "context": "fresh"}
        verdicts = await run_evaluators(contract, self.r.h.plugins, who, run.profile, {"value": value, "report": report}, {"report": report}, run)
        out = evaluate_contract(contract, {"value": value, "report": report, "fs_read": read, "facts": {}, "evaluator_results": verdicts})
        await run.emit("outcome.evaluated", {"task_id": self.r.spec["id"], **out})
        if value is not None:
            detail = f"confirmed value {value}" if conf else f"score {value} (unconfirmed upper bound; single submission)"
            evidence = {"selection": sel, "confirmation": conf, "value": value}
        return {"passed": out["verdict"] == "SUCCESS", "detail": f"{detail}; outcome {out['verdict']}", "evidence": {**evidence, "outcome": {"verdict": out["verdict"], "contract_hash": out["contract_hash"]}, "trajectory": traj}}


def attach_eval(h, loop, task: Task) -> EvalRunner:
    """Give a run the submit tool and an evidence-producing verifier (§13)."""
    runner = EvalRunner(h, task)

    async def submit(note):
        return await runner.submit(loop.run, loop.sandbox(), note)

    async def submit_resume():
        return await runner.resume_submission(loop.run, loop.sandbox())
    loop.hooks["submit"], loop.hooks["submit_resume"] = submit, submit_resume
    loop.verifier = EvalVerifier(runner)
    return runner
