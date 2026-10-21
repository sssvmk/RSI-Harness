"""Project identification (design §7.3.10). The harness never infers whether a run belongs to a project. When project memory is enabled, a run cannot start until the user has answered, either by supplying the answer or by
being asked, and the answer (a project name, or none) is recorded in the run manifest."""
from __future__ import annotations
import inspect, re
from typing import Any, Awaitable, Callable

NAME_RE = re.compile(r"^[A-Za-z0-9][A-Za-z0-9 ._\-]{0,62}$")
NONE_WORDS = {"", "none", "no", "n", "no project", "not a project", "not part of a project"}
QUESTION = "Is this work part of a project? Enter the project's name, or 'none' if it is not."
UNSET: Any = object()          # the caller said nothing about the project, which is different from saying "none"


class ProjectError(ValueError):
    pass


class ProjectNotDeclared(ProjectError):
    """Project memory is enabled and the user has not said whether this run belongs to a project."""


def parse_answer(text: str) -> str | None:
    """A project name, or None for 'not a project'. Anything else is refused rather than guessed."""
    t = (text or "").strip()
    if t.lower() in NONE_WORDS: return None
    if not NAME_RE.match(t): raise ProjectError(f"{t!r} is not a project name: use letters, digits, spaces, dots, dashes and underscores, starting with a letter or digit (at most 63 characters), or answer 'none'")
    return t


def enabled(config: dict) -> bool:
    return bool((((config.get("memory") or {}).get("project")) or {}).get("enabled", False))


async def declare(config: dict, project: Any = UNSET, ask: Callable[[str], str | Awaitable[str]] | None = None) -> dict | None:
    """The project declaration for a run, or None when project memory is off and nothing was said. A project the caller names is the user's own answer; otherwise the user is asked; otherwise the run is refused."""
    if project is not UNSET:
        return {"id": None if project is None else parse_answer(str(project)), "declared_by": "user", "asked": False}
    if ask is not None:
        a = ask(QUESTION)
        if inspect.isawaitable(a): a = await a
        return {"id": parse_answer(a), "declared_by": "user", "asked": True}
    if enabled(config): raise ProjectNotDeclared("project memory is enabled, so the user must say whether this run is part of a project (pass project=<name> or project=None, or a function that asks); the harness does not guess")
    return None
