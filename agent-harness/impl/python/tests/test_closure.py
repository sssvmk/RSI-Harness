"""The closure ledger: open, soft closed (closed in design, waiting for code), hard closed (implemented as the PRD and design say, closed for good)."""
import copy, json
import pytest
from harness import closure as C

PRD = """## 3. Functional

### 3.1. Context
* **Alpha Rule** [Core]: Do alpha things.
* **Beta Rule** [Standard]: Do beta things.
    * **Nested Rule** [Standard, conditional]: Nested text.

### 6.2 Self
**Gamma Rule.** [Standard]
Gamma explanation line.

## Appendix A
* **Not A Requirement** [Core]: ignored
"""
DES = """# Part I
## 3. Chapter three
intro three

### 3.1 Context design
alpha design text
#### 3.1.1 Sub
sub text, a value [proposed P99]

## 6. Self chapter
### 6.2 Self design
gamma design

# Appendix B: Traceability Matrix
| ID | Requirement | Profile | Train | Component | Design § |
|---|---|---|---|---|---|
| `H-3.1-alpha-rule` | Alpha Rule | Core | 1 | X | §3.1 |
| `H-3.1-beta-rule` | Beta Rule | Standard | 1 | X | §3.1.1 |
| `H-3.1-nested-rule` | Nested Rule | Standard (conditional) | 1 | X | §3 |
| `H-6.2-gamma-rule` | Gamma Rule | Standard | 1 | X | §6.2 |
"""
IDS = ["H-3.1-alpha-rule", "H-3.1-beta-rule", "H-3.1-nested-rule", "H-6.2-gamma-rule"]


def fresh():
    prd = C.parse_prd(PRD)
    return {"schema_version": 1, "as_of": "2026-10-21", "docs": {}, "entries": [
        {"id": i, "title": prd[i]["title"], "section": prd[i]["section"], "profile": prd[i]["profile"], "state": "open", "code_status": "not_implemented", "code_note": "", "open": {"reason": "design not written"},
         "evidence": [], "verification": None, "history": [{"at": "2026-10-21", "from": None, "to": "open", "kind": "import", "by": "t", "note": "n"}]} for i in IDS]}


# ------------------------------------------------------------------ reading the documents
def test_both_requirement_styles_and_nested_bullets_are_parsed_and_appendices_are_ignored():
    p = C.parse_prd(PRD)
    assert sorted(p) == sorted(IDS) and p["H-3.1-nested-rule"]["profile"] == "Standard, conditional" and "Gamma explanation line." in p["H-6.2-gamma-rule"]["text"]
    assert "Not A Requirement" not in json.dumps(p)
    with pytest.raises(C.ClosureError, match="duplicate"): C.parse_prd(PRD.replace("**Beta Rule** [Standard]", "**Alpha Rule** [Standard]"))


def test_the_matrix_and_section_lookup():
    m = C.parse_matrix(DES)
    assert m["H-3.1-beta-rule"]["refs"] == ["3.1.1"] and m["H-3.1-alpha-rule"]["profile"] == "Core"
    full, coarse = C.section_text(DES, "3.1"); assert "alpha design text" in full and "sub text" in full and not coarse          # sub-sections are part of the section
    intro, coarse = C.section_text(DES, "3"); assert intro.strip().endswith("intro three") and "alpha design" not in intro and coarse   # a chapter-level reference takes only its introduction
    assert C.section_text(DES, "7")[0] is None and C.section_text(DES, "3.1.1")[0].startswith("#### 3.1.1")
    assert C.section_text(DES, "6")[0].startswith("## 6. Self chapter") and "gamma design" not in C.section_text(DES, "6")[0]


def test_hashes_follow_the_requirement_and_the_design_and_report_proposed_parameters():
    prd, m = C.parse_prd(PRD), C.parse_matrix(DES); h = C.hashes("H-3.1-beta-rule", prd, m, DES)
    assert h["pending"] == ["P99"] and not h["unresolved"]
    h2 = C.hashes("H-3.1-beta-rule", C.parse_prd(PRD.replace("Do beta things", "Do beta things differently")), m, DES); assert h2["req"] != h["req"] and h2["design"] == h["design"]
    h3 = C.hashes("H-3.1-beta-rule", prd, m, DES.replace("sub text", "other text")); assert h3["design"] != h["design"] and h3["req"] == h["req"]
    assert C.hashes("H-3.1-alpha-rule", prd, m, DES.replace("sub text", "other text"))["design"] != C.hashes("H-3.1-alpha-rule", prd, m, DES)["design"]      # a parent includes its sub-sections
    assert C.hashes("H-3.1-nested-rule", prd, m, DES.replace("alpha design text", "changed"))["design"] == C.hashes("H-3.1-nested-rule", prd, m, DES)["design"]   # a coarse chapter reference ignores its sub-sections
    assert C.hashes("H-3.1-alpha-rule", prd, {**m, "H-3.1-alpha-rule": {**m["H-3.1-alpha-rule"], "refs": ["9.9"]}}, DES)["unresolved"] == ["§9.9"]


# ------------------------------------------------------------------ the three states
def test_the_full_path_open_soft_hard_and_what_each_step_requires():
    led = fresh()
    with pytest.raises(C.ClosureError, match="must be soft closed"): C.hard_close(led, IDS[0], "ann", "done", [{"kind": "test", "ref": "t"}], PRD, DES)               # cannot skip the design close
    C.soft_close(led, IDS[0], "ann", "design section 3.1 written", PRD, DES)
    e = C.entry(led, IDS[0]); assert e["state"] == "soft" and e["open"] is None and e["closed_hashes"]["req"] and e["code_status"] == "not_implemented"
    with pytest.raises(C.ClosureError, match="needs evidence"): C.hard_close(led, IDS[0], "ann", "done", [], PRD, DES)
    with pytest.raises(C.ClosureError, match="implementation is outstanding"): C.soft_close(led, IDS[1], "ann", "x", PRD, DES, code_status="implemented")
    C.hard_close(led, IDS[0], "ann", "implemented and verified", [{"kind": "test", "ref": "tests/test_x.py::test_a"}], PRD, DES)
    e = C.entry(led, IDS[0]); assert e["state"] == "hard" and e["code_status"] == "implemented" and e["verification"] == "verified" and [h["to"] for h in e["history"]] == ["open", "soft", "hard"]
    assert C.check(led, PRD, DES)["errors"] == []
    with pytest.raises(C.ClosureError, match="hard closed"): C.open_item(led, IDS[0], "ann", "found a problem")
    with pytest.raises(C.ClosureError, match="hard closed"): C.soft_close(led, IDS[0], "ann", "x", PRD, DES)


def test_a_soft_close_can_be_reopened_in_design_and_every_transition_needs_an_actor_and_a_note():
    led = fresh(); C.soft_close(led, IDS[0], "ann", "written", PRD, DES, "partial")
    C.open_item(led, IDS[0], "bob", "the design contradicts the PRD"); e = C.entry(led, IDS[0])
    assert e["state"] == "open" and e["open"]["reason"] == "the design contradicts the PRD" and [h["to"] for h in e["history"]] == ["open", "soft", "open"]
    with pytest.raises(C.ClosureError, match="actor and a note"): C.soft_close(led, IDS[0], "", "n", PRD, DES)
    with pytest.raises(C.ClosureError, match="actor and a note"): C.soft_close(led, IDS[0], "ann", "", PRD, DES)
    with pytest.raises(C.ClosureError, match="actor and a note"): C.open_item(led, IDS[1], "ann", "")
    with pytest.raises(C.ClosureError, match="no ledger entry"): C.entry(led, "H-9-nope")
    assert C.check(led, PRD, DES)["errors"] == []


def test_a_refused_transition_changes_nothing():
    led = fresh(); C.soft_close(led, IDS[0], "ann", "w", PRD, DES, "partial"); C.soft_close(led, IDS[1], "ann", "w", PRD, DES); C.hard_close(led, IDS[1], "ann", "i", [{"kind": "test", "ref": "t"}], PRD, DES)
    before = copy.deepcopy(led)
    refused = [lambda: C.soft_close(led, IDS[2], "", "n", PRD, DES), lambda: C.soft_close(led, IDS[2], "a", "", PRD, DES), lambda: C.soft_close(led, IDS[1], "a", "n", PRD, DES),
               lambda: C.soft_close(led, IDS[2], "a", "n", PRD, DES, "implemented"), lambda: C.hard_close(led, IDS[2], "a", "n", [{"kind": "t", "ref": "r"}], PRD, DES),
               lambda: C.hard_close(led, IDS[0], "a", "n", [], PRD, DES), lambda: C.hard_close(led, IDS[0], "", "n", [{"kind": "t", "ref": "r"}], PRD, DES),
               lambda: C.open_item(led, IDS[0], "a", ""), lambda: C.open_item(led, IDS[1], "a", "r"), lambda: C.reopen(led, IDS[0], "a", "r"), lambda: C.reopen(led, IDS[1], "a", ""),
               lambda: C.reopen(led, IDS[1], "a", "r", "hard"), lambda: C.reattest(led, IDS[1], "a", "n", PRD, DES), lambda: C.soft_close(led, IDS[2], "a", "n", PRD, DES.replace("### 6.2", "### 6.9"), "partial") if False else C.soft_close(led, "H-9-x", "a", "n", PRD, DES)]
    for f in refused:
        with pytest.raises(C.ClosureError): f()
    assert led == before


def test_progress_on_a_requirement_that_is_not_yet_hard_closed_is_recorded_without_changing_its_state():
    led = fresh(); C.soft_close(led, IDS[0], "ann", "w", PRD, DES)
    C.update_code(led, IDS[0], "bob", "partial", "the service is built; compute metering needs sandbox accounting"); e = C.entry(led, IDS[0])
    assert (e["state"], e["code_status"], e["code_note"]) == ("soft", "partial", "the service is built; compute metering needs sandbox accounting") and e["history"][-1]["kind"] == "code" and C.check(led, PRD, DES)["errors"] == []
    for bad in (lambda: C.update_code(led, IDS[0], "", "partial", "n"), lambda: C.update_code(led, IDS[0], "a", "partial", ""), lambda: C.update_code(led, IDS[0], "a", "implemented", "n"), lambda: C.update_code(led, IDS[0], "a", "bogus", "n")):
        with pytest.raises(C.ClosureError): bad()
    C.hard_close(led, IDS[0], "ann", "i", [{"kind": "test", "ref": "t"}], PRD, DES)
    with pytest.raises(C.ClosureError, match="hard closed"): C.update_code(led, IDS[0], "a", "partial", "n")


def test_a_requirement_can_name_the_plugin_that_will_supply_it_and_the_report_counts_those_separately():
    led = fresh(); C.soft_close(led, IDS[0], "ann", "w", PRD, DES); C.soft_close(led, IDS[1], "ann", "w", PRD, DES)
    C.update_code(led, IDS[0], "bob", "not_implemented", "the contract is enforced; no plugin ships", plugin="retrieval"); e = C.entry(led, IDS[0])
    assert e["plugin"] == "retrieval" and e["code_status"] == "not_implemented" and "plugin" not in C.entry(led, IDS[1]) and C.check(led, PRD, DES)["errors"] == []
    text = C.report_markdown(led, PRD, DES); assert "awaiting a plugin" in text and "**1**" in text.split("awaiting a plugin")[1][:200] and "⧉ retrieval" in text
    C.update_code(led, IDS[0], "bob", "partial", "more"); assert C.entry(led, IDS[0])["plugin"] == "retrieval"                                                # a later update keeps it


def test_a_new_requirement_enters_open_and_must_be_soft_closed_against_the_design():
    led = fresh(); led["entries"] = [e for e in led["entries"] if e["id"] != IDS[1]]
    assert C.check(led, PRD, DES)["errors"]                                                                                                                  # a requirement with no entry is an error
    e = C.add_requirement(led, IDS[1], "ann", PRD); assert (e["state"], e["code_status"], e["title"] != "") == ("open", "not_implemented", True) and e["history"][-1]["kind"] == "add"
    with pytest.raises(C.ClosureError, match="already in the ledger"): C.add_requirement(led, IDS[1], "ann", PRD)
    with pytest.raises(C.ClosureError, match="not in the PRD"): C.add_requirement(led, "H-9-nothing", "ann", PRD)
    C.soft_close(led, IDS[1], "ann", "design closes it", PRD, DES); assert C.entry(led, IDS[1])["state"] == "soft" and C.check(led, PRD, DES)["errors"] == []


def test_a_hard_close_is_permanent_only_an_explicit_logged_reopen_takes_it_back():
    led = fresh(); C.soft_close(led, IDS[0], "ann", "w", PRD, DES); C.hard_close(led, IDS[0], "ann", "i", [{"kind": "test", "ref": "t"}], PRD, DES)
    with pytest.raises(C.ClosureError, match="needs a reason|actor and a note"): C.reopen(led, IDS[0], "bob", "")
    with pytest.raises(C.ClosureError, match="not hard closed"): C.reopen(led, IDS[1], "bob", "why")
    with pytest.raises(C.ClosureError, match="soft or open"): C.reopen(led, IDS[0], "bob", "why", to="hard")
    C.reopen(led, IDS[0], "bob", "the requirement changed in the PRD")
    e = C.entry(led, IDS[0]); assert e["state"] == "soft" and e["code_status"] == "needs_update" and e["verification"] is None
    kinds = [(h["kind"], h["to"]) for h in e["history"]]; assert kinds == [("import", "open"), ("close", "soft"), ("close", "hard"), ("reopen", "soft")]         # nothing was removed
    assert "Reopened hard closes" in C.report_markdown(led, PRD, DES) and "the requirement changed in the PRD" in C.report_markdown(led, PRD, DES)
    C.soft_close(led, IDS[0], "ann", "re-specified", PRD, DES, "needs_update"); C.hard_close(led, IDS[0], "ann", "re-implemented", [{"kind": "test", "ref": "t2"}], PRD, DES)
    assert len(C.entry(led, IDS[0])["history"]) == 6 and C.check(led, PRD, DES)["errors"] == []
    led2 = fresh(); C.soft_close(led2, IDS[1], "a", "w", PRD, DES); C.hard_close(led2, IDS[1], "a", "i", [{"kind": "test", "ref": "t"}], PRD, DES)
    C.reopen(led2, IDS[1], "bob", "design problem found", to="open"); assert C.entry(led2, IDS[1])["state"] == "open" and C.entry(led2, IDS[1])["open"]["reason"] == "design problem found"


# ------------------------------------------------------------------ staying honest as the documents change
def hard_ledger():
    led = fresh()
    for i in IDS[:2]: C.soft_close(led, i, "ann", "w", PRD, DES)
    C.hard_close(led, IDS[0], "ann", "i", [{"kind": "test", "ref": "t"}], PRD, DES, "graded"); return led


def test_a_change_to_the_requirement_or_its_design_makes_a_hard_close_stale_and_never_silently_reopens_it():
    led = hard_ledger(); assert C.check(led, PRD, DES)["stale"] == []
    r = C.check(led, PRD.replace("Do alpha things", "Do alpha things and more"), DES); assert r["stale"] == [IDS[0]] and r["errors"] == [] and C.entry(led, IDS[0])["state"] == "hard"
    assert C.check(led, PRD, DES.replace("alpha design text", "changed alpha design"))["stale"] == [IDS[0]]
    assert C.check(led, PRD, DES.replace("gamma design", "changed gamma"))["stale"] == []                                                                         # unrelated edits do not matter
    r = C.check(led, PRD, DES.replace("sub text", "different sub text")); assert r["drift"] == [IDS[1]] and IDS[0] in r["stale"]                                   # parent includes the sub-section, and a soft close notices design drift


def test_a_reviewer_can_reattest_a_stale_hard_close_without_reopening_it():
    led = hard_ledger(); new_des = DES.replace("alpha design text", "alpha design text, clarified")
    assert C.check(led, PRD, new_des)["stale"] == [IDS[0]]
    with pytest.raises(C.ClosureError, match="not stale"): C.reattest(led, IDS[0], "ann", "n", PRD, DES)
    with pytest.raises(C.ClosureError, match="not hard closed"): C.reattest(led, IDS[1], "ann", "n", PRD, DES)
    C.reattest(led, IDS[0], "carol", "read the clarification; the code still conforms", PRD, new_des)
    e = C.entry(led, IDS[0]); assert e["state"] == "hard" and C.check(led, PRD, new_des)["stale"] == [] and e["history"][-1]["kind"] == "reattest" and e["history"][-1]["by"] == "carol"


def test_check_finds_every_kind_of_structural_error():
    led = hard_ledger(); ok = lambda l, prd=PRD, des=DES: C.check(l, prd, des)["errors"]
    assert ok(led) == []
    l = copy.deepcopy(led); l["entries"].pop(); assert any("no ledger entry" in x for x in ok(l))
    l = copy.deepcopy(led); l["entries"].append({**copy.deepcopy(l["entries"][0]), "id": "H-9-extra"}); assert any("no requirement in the PRD" in x for x in ok(l))
    l = copy.deepcopy(led); l["entries"].append(copy.deepcopy(l["entries"][0])); assert any("duplicate ledger entry" in x for x in ok(l))
    assert any("traceability matrix" in x for x in ok(led, des=DES.replace("| `H-6.2-gamma-rule` | Gamma Rule | Standard | 1 | X | §6.2 |\n", "")))
    assert any("do not resolve" in x for x in ok(led, des=DES.replace("### 6.2 Self design", "### 6.9 Self design")))
    l = copy.deepcopy(led); C.entry(l, IDS[0])["evidence"] = []; assert any("needs evidence" in x for x in ok(l))
    l = copy.deepcopy(led); C.entry(l, IDS[0])["verification"] = None; assert any("verification" in x for x in ok(l))
    l = copy.deepcopy(led); C.entry(l, IDS[0])["code_status"] = "partial"; assert any("code_status implemented" in x for x in ok(l))
    l = copy.deepcopy(led); C.entry(l, IDS[2])["open"] = {"reason": ""}; assert any("say why" in x for x in ok(l))
    l = copy.deepcopy(led); C.entry(l, IDS[1])["open"] = {"reason": "x"}; assert any("only an open entry" in x for x in ok(l))
    l = copy.deepcopy(led); C.entry(l, IDS[0])["state"] = "soft"; assert any("does not match the last history" in x for x in ok(l))
    l = copy.deepcopy(led); C.entry(l, IDS[0])["history"][1]["from"] = "hard"; assert any("not continuous" in x for x in ok(l))
    l = copy.deepcopy(led); C.entry(l, IDS[0])["state"] = "closed"; assert any("not one of" in x for x in ok(l))
    l = copy.deepcopy(led); C.entry(l, IDS[0]).pop("closed_hashes"); assert any("no recorded hashes" in x for x in ok(l))
    l = copy.deepcopy(led); C.entry(l, IDS[0])["history"] = []; assert any("no history" in x for x in ok(l))


# ------------------------------------------------------------------ report and command line
def test_the_report_counts_states_and_flags_graded_closes_and_unconfirmed_parameters():
    led = hard_ledger(); text = C.report_markdown(led, PRD, DES)
    assert "4 requirements:** 1 hard closed, 1 soft closed, 2 open" in text and "0 were verified clause by clause and 1 are graded" in text
    assert "★ P99" in text and "design not written" in text and "| Core | 0 | 0 | 1 | 1 |" in text
    assert C.counts(led) == {"open": 2, "soft": 1, "hard": 1}


def test_the_command_line_runs_the_whole_cycle_and_keeps_the_ledger_on_disk(tmp_path, capsys):
    (tmp_path / "prd.md").write_text(PRD); (tmp_path / "des.md").write_text(DES); C.save(fresh(), tmp_path / "l.json")
    run = lambda *a: C.main(["--ledger", str(tmp_path / "l.json"), "--prd", str(tmp_path / "prd.md"), "--design", str(tmp_path / "des.md"), *a])
    assert run("check") == 0
    assert run("soft", IDS[0], "--by", "ann", "--note", "written", "--code", "partial") == 0
    assert run("hard", IDS[0], "--by", "ann", "--note", "done", "--evidence", "test:tests/test_x.py::test_a", "--verification", "graded") == 0
    assert run("hard", IDS[1], "--by", "ann", "--note", "done", "--evidence", "test:t") == 2                                       # not soft closed yet: refused with a non-zero exit
    assert C.load(tmp_path / "l.json")["entries"][0]["state"] == "hard" and run("check") == 0
    (tmp_path / "prd.md").write_text(PRD.replace("Do alpha things", "Do more alpha things"))
    assert run("check") == 0 and run("check", "--fail-on-stale") == 1                                                               # stale is reported, and fails only when asked
    assert run("reattest", IDS[0], "--by", "carol", "--note", "reviewed") == 0 and run("check", "--fail-on-stale") == 0
    assert run("reopen", IDS[0], "--by", "bob", "--reason", "requirement rewritten") == 0 and C.load(tmp_path / "l.json")["entries"][0]["state"] == "soft"
    assert run("show", IDS[0]) == 0 and run("report", "--out", str(tmp_path / "r.md")) == 0 and "Reopened hard closes" in (tmp_path / "r.md").read_text()
    capsys.readouterr()


# ------------------------------------------------------------------ the real ledger
def test_the_committed_ledger_agrees_with_the_committed_documents_and_the_committed_report_is_current():
    led = C.load(C.ROOT / "closure" / "ledger.json"); prd = (C.ROOT / "docs" / "harness_intent.md").read_text(encoding="utf-8"); des = (C.ROOT / "docs" / "design.md").read_text(encoding="utf-8")
    res = C.check(led, prd, des)
    assert res["errors"] == [] and res["stale"] == [], (res["errors"][:5], res["stale"][:5])
    assert len(led["entries"]) == len(C.parse_prd(prd)) == len(C.parse_matrix(des)) and sum(C.counts(led).values()) == len(led["entries"])
    assert all(e["verification"] in C.VERIFICATION and e["evidence"] for e in led["entries"] if e["state"] == "hard")
    assert all(e["open"]["reason"] for e in led["entries"] if e["state"] == "open")
    assert (C.ROOT / "GAPS.md").read_text(encoding="utf-8") == C.report_markdown(led, prd, des), "GAPS.md is stale: regenerate it with `python -m harness.closure report --out GAPS.md`"
