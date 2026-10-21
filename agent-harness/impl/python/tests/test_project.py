"""Project identification (design §7.3.10): the harness asks the user and never infers whether a run belongs to a project."""
import pytest
from conftest import events_of, oa
from harness import manifest as M
from harness.project import ProjectError, ProjectNotDeclared, UNSET, declare, enabled, parse_answer

ON = {"memory": {"project": {"enabled": True}}}


@pytest.mark.parametrize("text,expect", [("alpha", "alpha"), ("  My Project 2  ", "My Project 2"), ("a.b-c_d", "a.b-c_d"), ("none", None), ("None", None), ("no", None), ("", None), ("  ", None), ("not a project", None), ("no project", None)])
def test_an_answer_is_a_project_name_or_not_a_project(text, expect):
    assert parse_answer(text) == expect


@pytest.mark.parametrize("bad", ["../etc", "a/b", "-leading", "x" * 64, "semi;colon", "tab\there", "<script>"])
def test_anything_that_is_not_a_clean_name_is_refused_rather_than_guessed(bad):
    with pytest.raises(ProjectError, match="not a project name"): parse_answer(bad)


async def test_without_an_answer_a_run_is_refused_only_when_project_memory_is_on():
    assert enabled(ON) and not enabled({}) and not enabled({"memory": {}}) and not enabled({"memory": {"project": {"enabled": False}}})
    assert await declare({}) is None and await declare({"memory": {"project": {"enabled": False}}}) is None                                                      # off, and nothing said: nothing to record
    with pytest.raises(ProjectNotDeclared, match="does not guess"): await declare(ON)


async def test_a_name_the_caller_supplies_and_an_answer_the_user_gives_are_both_recorded_as_the_users():
    assert await declare(ON, "alpha") == {"id": "alpha", "declared_by": "user", "asked": False}
    assert await declare(ON, None) == {"id": None, "declared_by": "user", "asked": False}                                                                          # an explicit "no project" is an answer
    seen = []
    def ask(q): seen.append(q); return "Beta Project"
    async def ask_async(q): return "none"
    assert await declare(ON, UNSET, ask) == {"id": "Beta Project", "declared_by": "user", "asked": True} and "part of a project" in seen[0]
    assert await declare(ON, UNSET, ask_async) == {"id": None, "declared_by": "user", "asked": True}
    given = await declare(ON, "alpha", lambda q: pytest.fail("the user was asked although the caller already answered")); assert given["asked"] is False                  # no second question
    with pytest.raises(ProjectError): await declare(ON, UNSET, lambda q: "a/b")


async def test_a_run_with_project_memory_on_and_no_answer_is_refused_before_anything_is_created(mk):
    h = mk([oa("d")], **ON)
    with pytest.raises(ProjectNotDeclared): await h.create_run("u", "go")
    assert h.store.list_runs() == []


async def test_the_answer_is_recorded_in_the_manifest_with_how_it_was_given(mk):
    h = mk([oa("d")] * 4, **ON)
    run, _ = await h.create_run("u", "go", project="alpha"); f = events_of(h, run, "run.created")[0]["payload"]["manifest"]["fields"]["project"]
    assert (f["status"], f["value"]) == ("recorded", {"id": "alpha", "declared_by": "user", "asked": False})
    run2, _ = await h.create_run("u", "go", ask_project=lambda q: "Gamma"); assert events_of(h, run2, "run.created")[0]["payload"]["manifest"]["fields"]["project"]["value"] == {"id": "Gamma", "declared_by": "user", "asked": True}
    run3, _ = await h.create_run("u", "go", project=None); assert events_of(h, run3, "run.created")[0]["payload"]["manifest"]["fields"]["project"]["value"]["id"] is None
    m = events_of(h, run3, "run.created")[0]["payload"]["manifest"]; assert M.validate(m, "core") == [] and "project" in M.START


async def test_when_project_memory_is_off_the_field_is_not_applicable_and_runs_start_as_before(mk):
    h = mk([oa("d")]); run, _ = await h.create_run("u", "go"); f = events_of(h, run, "run.created")[0]["payload"]["manifest"]["fields"]["project"]
    assert f["status"] == "not_applicable" and f["value"] is None and "not enabled" in f["reason"]
