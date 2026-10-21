"""Builds a small but real task package on disk for evaluation tests."""
from pathlib import Path
import yaml

GRADE = r'''
import json, os
ws, scratch = os.environ["HARNESS_WORKSPACE"], os.environ["HARNESS_SCRATCH"]
try:
    base = float(open(os.path.join(ws, "out", "result.txt")).read().strip())
except Exception as e:
    raise SystemExit("SECRETSTACK: grader crashed reading result: %r" % (e,))
seed = int(os.environ.get("HARNESS_SEED", "0"))
noise = ((seed % 7) / 1000.0) if os.environ.get("HARNESS_CONFIRM") else 0.0
sub = os.environ.get("HARNESS_TESTS", "")
if os.environ.get("HARNESS_CONFIRM") and os.path.exists(os.path.join(sub, "heldout.marker")):
    noise = -0.05
json.dump({"schema_version": 1, "valid": True, "primary": round(base + noise, 6), "metrics": {"accuracy": base, "runtime_s": 1.5},
           "feedback_text": "HIDDEN-HINT: case 7 failed and the answer is 42 " + "é" * 40,
           "evaluator": {"kind": "same_model_low_independence", "model_ref": "x"}, "instances_scored": 10},
          open(os.path.join(scratch, "score.json"), "w"))
'''


def make_task(root: Path, name="t1", feedback=None, steps=None, **over) -> Path:
    d = root / name
    (d / "environment").mkdir(parents=True)
    (d / "tests").mkdir()
    (d / "tests" / "confirm").mkdir()
    (d / "instruction.md").write_text("Write /workspace/run.sh producing out/result.txt with a score.")
    (d / "environment" / "Dockerfile").write_text("FROM scratch\n")
    (d / "tests" / "prepare.sh").write_text("echo prepared > $HARNESS_SCRATCH/prepared\n")
    (d / "tests" / "grade.py").write_text(GRADE)
    (d / "tests" / "confirm" / "heldout.marker").write_text("1")
    (d / "tests" / "confirm" / "grade.py").write_text(GRADE)
    (d / "tests" / "confirm" / "prepare.sh").write_text("true\n")
    spec = {
        "schema_version": 1, "id": name, "version": "1.0.0", "instruction": "instruction.md",
        "environment": {"work_image": {"build": "environment/Dockerfile"}, "users": {"agent": 1000, "candidate": 1001, "judge": 1002}},
        "visibility": "hidden",
        "agent": {"budgets": {"turns": 30, "tokens": 2_000_000, "time_s": 600, "submissions": 5}, "tool_allowlist": ["fs.*", "shell.run", "submit", "finish", "recall"]},
        "scoring": {"steps": steps or [
            {"id": "prepare", "run_as": "judge", "cmd": ["bash", "/tests/prepare.sh"], "timeout_s": 30},
            {"id": "run", "run_as": "candidate", "cmd": ["bash", "/workspace/run.sh"], "timeout_s": 30, "cwd": "/workspace"},
            {"id": "grade", "run_as": "judge", "cmd": ["python3", "/tests/grade.py"], "timeout_s": 30}],
            "report_path": "/scratch/score.json", "primary": {"metric": "accuracy", "direction": "maximize"}},
        "submission": {"feedback": feedback or {"level": "score", "max_bytes": 2048}},
        "selection": {"rule": "best_primary", "tie_break": "earliest"},
        "confirmation": {"mode": "reseed", "trials": 3},
    }
    for k, v in over.items():
        spec[k] = {**spec.get(k, {}), **v} if isinstance(v, dict) and isinstance(spec.get(k), dict) else v
    (d / "task.yaml").write_text(yaml.safe_dump(spec))
    return d


def solution(score):
    """Agent tool calls that write a solution scoring `score`, then submit."""
    return [("fs.write", {"path": "/workspace/run.sh", "content": f"mkdir -p out && echo {score} > out/result.txt\n"}), ("submit", {"note": f"s={score}"})]
