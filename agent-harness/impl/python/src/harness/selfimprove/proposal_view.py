"""The proposer's sandbox view (§17.6): read-only, and containing *only* feedback-set artifacts. Scoring, held-out, confirm, transfer and meta
material is never written here (§17.4 Visibility)."""
from __future__ import annotations
import json
from pathlib import Path
from .gitrepo import GitRepo
from .registry import ComponentRegistry


def build_proposal_view(dest: str | Path, repo: GitRepo, base_commit: str, registry: ComponentRegistry, tiers_allowed: list[str], edits_max_lines: int,
                        feedback: dict, attempted: list[dict] | None = None) -> Path:
    """feedback = {patterns: [...], reports: {run_id: text}, traces: {run_id: jsonl}, preserved: [...]} — already restricted to the feedback set by the caller."""
    d = Path(dest)
    for sub in ("config", "patterns", "reports", "preserved", "attempted", "traces"):
        (d / sub).mkdir(parents=True, exist_ok=True)
    for rel in repo.git("ls-tree", "-r", "--name-only", base_commit).split("\n"):
        text = repo.show_file(base_commit, rel)
        if text is not None:
            f = d / "config" / rel; f.parent.mkdir(parents=True, exist_ok=True); f.write_text(text)
    (d / "patterns" / "patterns.json").write_text(json.dumps(feedback.get("patterns", []), indent=1))
    for rid, text in feedback.get("reports", {}).items():
        (d / "reports" / f"{rid}.md").write_text(text)
    for rid, text in feedback.get("traces", {}).items():
        (d / "traces" / f"{rid}.jsonl").write_text(text)
    (d / "preserved" / "summary.json").write_text(json.dumps(feedback.get("preserved", []), indent=1))     # summaries only
    (d / "attempted" / "summary.json").write_text(json.dumps([{"summary": a.get("summary"), "reason": a.get("reason")} for a in (attempted or [])], indent=1))
    paths = sorted({e.path for e in registry.entries if e.tier in tiers_allowed})
    (d / "surface.md").write_text("# Editable surface\n\n" + "\n".join(f"- `{p}`" for p in paths) + f"\n\nEdit cap: {edits_max_lines} changed lines. Anything not listed is not editable.\n")
    return d
