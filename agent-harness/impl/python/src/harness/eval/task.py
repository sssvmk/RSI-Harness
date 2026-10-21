"""Task package loader and validator (design §13.1)."""
from __future__ import annotations
from dataclasses import dataclass
from pathlib import Path
import jsonschema, yaml
from ..tools.registry import load_schema


@dataclass
class Task:
    dir: Path
    spec: dict

    @property
    def id(self) -> str: return self.spec["id"]
    @property
    def tests_dir(self) -> Path: return self.dir / "tests"
    @property
    def instruction(self) -> str: return (self.dir / self.spec["instruction"]).read_text()


def validate_task(path: str | Path) -> list[str]:
    d = Path(path)
    errs: list[str] = []
    f = d / "task.yaml"
    if not f.is_file():
        return [f"{f} not found"]
    try:
        spec = yaml.safe_load(f.read_text())
        jsonschema.validate(spec, load_schema("task.schema.json"))
    except (yaml.YAMLError, jsonschema.ValidationError) as e:
        return [f"task.yaml invalid: {getattr(e, 'message', e)}"]
    if not (d / spec["instruction"]).is_file():
        errs.append(f"instruction file {spec['instruction']} missing")
    build = (spec["environment"]["work_image"] or {}).get("build")
    if build and not (d / build).exists():
        errs.append(f"work_image build file {build} missing")
    users = (spec["environment"].get("users") or {})
    if len(set(users.values())) != len(users):
        errs.append("environment.users must be distinct")
    ids = [s["id"] for s in spec["scoring"]["steps"]]
    if len(ids) != len(set(ids)):
        errs.append("scoring step ids must be unique")
    for s in spec["scoring"]["steps"]:
        for arg in s["cmd"]:
            if arg.startswith("/tests/") and not (d / "tests" / arg[len("/tests/"):]).exists():
                errs.append(f"step {s['id']}: {arg} not found under tests/")
    from .visibility import validate_declaration
    errs += validate_declaration(spec)
    if spec.get("outcome") is not None:
        from .outcome import validate_contract
        errs += validate_contract({"task_id": spec["id"], **spec["outcome"]})
    if spec.get("visibility", "hidden") == "public":
        if spec.get("purpose") != "development":
            errs.append("public tests are development aids: a public task must declare purpose: development")
        if spec.get("confirmation", {}).get("mode") == "heldout_set":
            errs.append("public tests cannot support a held-out or confirmation claim: confirmation.mode heldout_set is not allowed")
    if spec.get("visibility", "hidden") == "hidden":
        if (d / "environment" / "tests").exists():
            errs.append("hidden task places tests/ inside environment/ (agent-reachable)")
        for p in (d / "environment").rglob("*") if (d / "environment").exists() else []:
            if p.is_symlink() and "tests" in str(p.resolve()):
                errs.append(f"environment symlink {p} points into tests/")
        if spec.get("candidate_outputs", "/workspace/out").startswith("/tests"):
            errs.append("candidate_outputs must not be under /tests")
    if spec.get("confirmation", {}).get("mode") == "heldout_set" and not (d / "tests" / "confirm").is_dir():
        errs.append("confirmation.mode heldout_set requires tests/confirm/")
    return errs


def load_task(path: str | Path) -> Task:
    errs = validate_task(path)
    if errs:
        raise ValueError("invalid task: " + "; ".join(errs))
    return Task(Path(path), yaml.safe_load((Path(path) / "task.yaml").read_text()))
