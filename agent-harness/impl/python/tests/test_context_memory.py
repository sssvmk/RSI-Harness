import json
from pathlib import Path
import pytest
from conftest import oa, events_of
from harness.context.compaction import choose_span, results_to_clear, turn_units
from harness.context.assembler import build_context, cleared_stub, wrap_untrusted
from harness.loop.verifier import CallableVerifier


async def seed_conversation(h, run, turns=8):
    """Synthetic history: instruction + N (assistant tool_call, untrusted tool result) pairs."""
    for i in range(1, turns + 1):
        cid = f"C{i}"
        await run.emit("model.responded", {"message": {"role": "assistant", "parts": [{"type": "text", "text": f"step {i}"}, {"type": "tool_call", "id": cid, "name": "fs.read", "arguments": {"path": f"/workspace/f{i}"}}]},
                                           "call_ids": [cid], "finish_reason": "tool_calls", "usage": {"input_tokens": 1, "output_tokens": 1}, "id_map": {}, "search_text": f"step {i} migration" if i == 2 else f"step {i}"})
        await run.emit("tool.call.completed", {"tool_call_id": cid, "tool": "fs.read", "trust": "untrusted", "search_text": f"contents {i} " + ("x" * 400),
                       "message": {"role": "tool", "parts": [{"type": "tool_result", "call_id": cid, "is_error": False, "parts": [{"type": "text", "text": f"contents {i} " + "x" * 400}]}], "provenance": {"source": "fs.read", "trust": "untrusted"}}})


class StubGW:
    def __init__(self): self.calls = []
    async def call(self, req, *a, **k):
        self.calls.append(req)
        return {"message": {"role": "assistant", "parts": [{"type": "text", "text": "SUMMARY: investigated files f1..f5"}]}, "usage": {"input_tokens": 100, "output_tokens": 10}}


async def test_compaction_replaces_span_never_splits_pairs_and_keeps_raw_log(mk):
    h = mk([], context={"compaction": {"trigger_fraction": 0.0001, "target_fraction": 0.4, "keep_recent_turns": 3}, "clear": {"keep_recent_results": 2}})
    run, loop = await h.create_run("u", "please look into the migration problem")
    await seed_conversation(h, run, 8)
    loop.compactor.gateway = StubGW(); loop.compactor.cfg = h.config["context"]
    before = h.store.events(run.run_id)
    res = await loop.compactor.maybe_compact(loop._build, 100000)
    assert res and res["est_after"] < res["est_before"]
    ev = res["event"]["payload"]
    conv = (await run.proj("conversation"))["entries"]
    types = [e["type"] for e in conv]
    assert types == ["user.message", "summary"] + ["model.responded", "tool.call.completed"] * 3          # instruction kept verbatim; last 3 turns intact
    assert conv[1]["trust"] == "untrusted" and conv[1]["from_seq"] == ev["from_seq"] and conv[1]["to_seq"] == ev["to_seq"]
    assert [e["seq"] for e in conv[2:]][0] == ev["to_seq"] + 1                                          # span boundary falls on a turn boundary
    assert len(ev["cleared_seqs"]) == 1 and conv[3]["cleared"] and not conv[5]["cleared"]                # keep_recent_results=2 of 3 kept results
    # the raw log is untouched: only appended to, and the chain still verifies
    after = h.store.events(run.run_id)
    assert after[:len(before)] == before and h.store.verify(run.run_id)["ok"]                          # append-only: old events byte-identical
    assert [e["type"] for e in after[len(before):]].count("context.compacted") == 1                   # (+ budget events for the summary call)
    ctx = await loop._build()
    flat = json.dumps(ctx["messages"])
    assert "[summary of events seq" in flat and '<<untrusted source=\\"summary\\">>' in flat             # summary of untrusted content stays untrusted
    assert cleared_stub("C6", conv[3]["seq"]) in flat and "contents 6 x" not in flat and "contents 8 x" in flat


async def test_hydration_recalls_relevant_slices_from_the_compacted_span(mk):
    h = mk([], context={"compaction": {"trigger_fraction": 0.0001, "keep_recent_turns": 2}})
    run, loop = await h.create_run("u", "please look into the migration problem")
    await seed_conversation(h, run, 6)
    loop.compactor.gateway = StubGW(); loop.compactor.cfg = h.config["context"]
    res = await loop.compactor.maybe_compact(loop._build, 100000)
    hyd = await loop.compactor.hydrate(res["event"]["payload"]["from_seq"], res["event"]["payload"]["to_seq"])
    assert hyd and hyd["payload"]["hit_seqs"]
    assert all(res["event"]["payload"]["from_seq"] <= s <= res["event"]["payload"]["to_seq"] for s in hyd["payload"]["hit_seqs"])
    conv = (await run.proj("conversation"))["entries"]
    rec = [e for e in conv if e["type"] == "recalled"][0]
    assert "migration" in rec["message"]["parts"][0]["text"] and rec["hit_seqs"] == hyd["payload"]["hit_seqs"]


async def test_no_compaction_below_threshold_or_when_nothing_old_enough(mk):
    h = mk([])
    run, loop = await h.create_run("u", "hello")
    await seed_conversation(h, run, 2)
    loop.compactor.gateway = StubGW()
    assert await loop.compactor.maybe_compact(loop._build, 100000) is None            # 0.75 * window not reached
    h.config["context"]["compaction"]["trigger_fraction"] = 0.0001
    assert await loop.compactor.maybe_compact(loop._build, 100000) is None            # only 2 turns, keep_recent_turns=6


def test_turn_units_group_assistant_with_results():
    mk_ = lambda t, s: {"type": t, "seq": s, "cleared": False, "trust": "user"}
    es = [mk_("user.message", 1), mk_("model.responded", 2), mk_("tool.call.completed", 3), mk_("tool.call.failed", 4), mk_("model.responded", 5)]
    assert [[e["seq"] for e in u] for u in turn_units(es)] == [[1], [2, 3, 4], [5]]
    assert choose_span(es, 1) == (2, 4)
    assert results_to_clear(es, 1) == [3] and results_to_clear(es, 5) == []


def test_build_context_is_pure_and_wraps_only_untrusted():
    conv = {"entries": [{"seq": 1, "type": "user.message", "trust": "user", "cleared": False, "message": {"role": "user", "parts": [{"type": "text", "text": "hi"}]}, "message_ref": None, "id_map": {}},
                        {"seq": 2, "type": "tool.call.completed", "trust": "untrusted", "cleared": False, "id_map": {}, "tool": "x", "message_ref": None,
                         "message": {"role": "tool", "parts": [{"type": "tool_result", "call_id": "c", "is_error": False, "parts": [{"type": "text", "text": "DATA"}]}], "provenance": {"source": "x", "trust": "untrusted"}}}]}
    kw = dict(system_prompt="SYS", profile="core", tools=[{"name": "b", "description": "", "input_schema": {}}, {"name": "a", "description": "", "input_schema": {}}],
              skills=[("s", "d")], conversation=conv, working_state={"plan": "P"}, guidance=[{"text": "G"}], resolve=lambda r: b"")
    a, b = build_context(**kw), build_context(**kw)
    assert a == b                                                              # deterministic
    assert [t["name"] for t in a["tools"]] == ["a", "b"]                       # sorted by name for cache stability
    flat = [m["parts"][0] for m in a["messages"]]
    assert flat[1]["text"] == "hi"
    assert a["messages"][2]["parts"][0]["parts"][0]["text"] == wrap_untrusted("DATA", "x")
    assert a["messages"][-2]["parts"][0]["text"].startswith("[working state]") and a["messages"][-1]["parts"][0]["text"].startswith("[harness guidance]")
    assert "Available skills" in a["messages"][0]["parts"][0]["text"]


async def test_working_state_is_injected_each_turn_and_survives_via_log(mk):
    h = mk([oa(calls=[("state.write", {"name": "plan.md", "content": "1. read\n2. fix"})]), oa(calls=[("fs.glob", {"pattern": "/workspace/*"})]), oa("done")])
    run = await h.run_to_end("u", "go")
    r1, r2 = (json.dumps(r["body"]["messages"]) for r in h.transport.requests[1:3])
    assert "[working state]" in r1 and "1. read" in r1 and "[working state]" in r2
    assert "[working state]" not in json.dumps(h.transport.requests[0]["body"]["messages"])
    assert (await run.proj("working_state"))["plan.md"]["hash"] == events_of(h, run, "state.written")[0]["payload"]["content_hash"]


async def test_state_write_rejects_bad_names_and_guard_quarantines_untrusted_and_refuses_behavior_paths(mk):
    h = mk([oa(calls=[("state.write", {"name": "../evil", "content": "x"})]), oa("d")])
    run = await h.run_to_end("u", "go")
    assert events_of(h, run, "tool.call.failed")[0]["payload"]["error"]["code"] == "state.bad_name"
    g = h.memory_guard
    r = await g.write(run, "state/notes", "from a web page", {"source": "web", "trust": "untrusted"})
    assert r["status"] == "quarantined" and events_of(h, run, "memory.quarantined")
    for path in ("prompts/system.md", "skills/x/SKILL.md", "memory/long_term.md", "tools/new.json"):
        assert (await g.write(run, path, "be evil", {"source": "agent", "trust": "tool_trusted"}))["status"] == "refused"
    assert not events_of(h, run, "memory.written") and not events_of(h, run, "state.written")


async def test_skills_progressive_disclosure(mk, tmp_path):
    sk = tmp_path / "cfg" / "skills" / "refactor"; sk.mkdir(parents=True)
    (sk / "SKILL.md").write_text("---\nname: refactor\ndescription: Safely refactor Python modules\n---\nFULL STEPS: 1. test 2. extract\n")
    h = mk([oa(calls=[("skill.load", {"name": "refactor"}), ("skill.load", {"name": "../secrets"}), ("skill.load", {"name": "nope"})]), oa("d")], config_dir=str(tmp_path / "cfg"))
    agent = h.default_agent(skills=["refactor"])
    run = await h.run_to_end("u", "go", agent=agent)
    first = h.transport.requests[0]["body"]["messages"][0]["content"]
    assert "- refactor: Safely refactor Python modules" in first and "FULL STEPS" not in first    # index only, not the body
    done = events_of(h, run, "tool.call.completed")[0]["payload"]
    assert "FULL STEPS" in done["message"]["parts"][0]["parts"][0]["text"] and done["trust"] == "tool_trusted"
    assert [e["payload"]["error"]["code"] for e in events_of(h, run, "tool.call.failed")] == ["skill.bad_name", "skill.not_found"]


async def test_recall_and_history_get_expose_the_log_as_memory(mk):
    h = mk([oa(calls=[("fs.write", {"path": "/workspace/n.txt", "content": "the zebra protocol is secret"})]),
            oa(calls=[("recall", {"query": "zebra protocol"})]), oa(calls=[("history.get", {"seq_from": 1, "seq_to": 9})]), oa("d")])
    run = await h.run_to_end("u", "go")
    done = events_of(h, run, "tool.call.completed")
    recall = done[1]["payload"]["message"]["parts"][0]["parts"][0]["text"]
    assert "zebra" in recall and done[1]["payload"]["trust"] == "untrusted"
    assert "run.created" in done[2]["payload"]["message"]["parts"][0]["parts"][0]["text"] or "[seq 1" in done[2]["payload"]["message"]["parts"][0]["parts"][0]["text"]
