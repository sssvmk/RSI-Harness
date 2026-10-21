import asyncio, json, sqlite3
import pytest
from harness.canonical import compute_hash
from harness.eventlog import EventStore, EventWriter, UnknownRunError
from harness.eventlog.projections import ProjectionManager, fold_all, NAMES
from harness.ids import ulid
from harness.retention import delete_content
from harness.blobs import BlobStore
from harness.clock import FakeClock


@pytest.fixture
def store(tmp_path):
    s = EventStore(str(tmp_path / "l.db"))
    w = EventWriter(s, FakeClock()).start()
    yield s, w
    w.close()


def created(w, rid=None):
    rid = rid or ulid()
    w.submit_sync({"run_id": rid, "type": "run.created", "payload": {"owner": "u", "profile": "core", "config_commit": "c"}})
    return rid


def test_pragmas_wal_full(store):
    s, _ = store
    assert s.pragma("journal_mode") == "wal" and s.pragma("synchronous") == 2


def test_seq_starts_at_one_and_increments_by_exactly_one(store):
    s, w = store
    rid = created(w)
    for i in range(10):
        w.submit_sync({"run_id": rid, "type": "user.message", "payload": {"i": i}})
    assert [e["seq"] for e in s.events(rid)] == list(range(1, 12))
    assert s.verify(rid)["ok"]


def test_chain_links_and_matches_independent_recompute(store):
    s, w = store
    rid = created(w)
    w.submit_sync({"run_id": rid, "type": "user.message", "payload": {"x": 1.5}})
    evs = s.events(rid)
    assert evs[1]["prev_hash"] == evs[0]["hash"]
    assert compute_hash(evs[0]["hash"], evs[1]) == evs[1]["hash"]


def test_tampering_with_a_payload_is_detected(store, tmp_path):
    s, w = store
    rid = created(w)
    for i in range(5):
        w.submit_sync({"run_id": rid, "type": "user.message", "payload": {"i": i}})
    # events table is append-only by trigger; an attacker must drop the trigger first
    with pytest.raises(sqlite3.DatabaseError):
        s.conn().execute("UPDATE events SET payload='{}' WHERE run_id=? AND seq=3", (rid,))
    c = sqlite3.connect(s.path)
    c.execute("DROP TRIGGER events_no_update")
    c.execute("UPDATE events SET payload='{\"i\":99}' WHERE run_id=? AND seq=3", (rid,))
    c.commit(); c.close()
    v = s.verify(rid)
    assert not v["ok"] and v["bad_seq"] == 3 and v["reason"] == "hash mismatch"
    assert s.verify(rid, last_n=2)["ok"]          # partial verification only covers the tail (§5.9)
    assert not s.verify(rid, last_n=4)["ok"]


def test_delete_is_blocked_by_trigger(store):
    s, w = store
    rid = created(w)
    with pytest.raises(sqlite3.DatabaseError):
        s.conn().execute("DELETE FROM events WHERE run_id=?", (rid,))


def test_unknown_run_rejected_and_failure_is_isolated_in_a_batch(store):
    s, w = store
    rid = created(w)
    futs = [w._enqueue(("event", {"run_id": rid, "type": "user.message", "payload": {"i": 1}})),
            w._enqueue(("event", {"run_id": ulid(), "type": "user.message", "payload": {}})),
            w._enqueue(("event", {"run_id": rid, "type": "user.message", "payload": {"i": 2}}))]
    res = []
    for f in futs:
        try: res.append(f.result())
        except UnknownRunError: res.append("err")
    assert res[1] == "err" and res[0]["seq"] == 2 and res[2]["seq"] == 3
    assert s.verify(rid)["ok"]


def test_rejects_bad_type_and_unsafe_integers(store):
    s, w = store
    rid = created(w)
    with pytest.raises(ValueError): w.submit_sync({"run_id": rid, "type": "BadType", "payload": {}})
    with pytest.raises(ValueError): w.submit_sync({"run_id": rid, "type": "a.b", "payload": {"n": 2**60}})
    w.submit_sync({"run_id": rid, "type": "a.b", "payload": {"n": str(2**60)}})  # decimal string is the sanctioned encoding


async def test_group_commit_many_concurrent_runs_keep_per_run_order(store):
    s, w = store
    rids = [created(w) for _ in range(5)]
    async def spam(rid):
        for i in range(40):
            await w.submit({"run_id": rid, "type": "user.message", "payload": {"i": i}})
    await asyncio.gather(*(spam(r) for r in rids))
    for r in rids:
        evs = s.events(r)
        assert [e["payload"].get("i") for e in evs[1:]] == list(range(40))
        assert s.verify(r)["ok"]
    assert w.commits < 5 * 41  # batches were grouped, not one commit per event


def test_durable_before_ack(tmp_path):
    p = str(tmp_path / "d.db")
    s = EventStore(p); w = EventWriter(s).start()
    rid = created(w)
    w.submit_sync({"run_id": rid, "type": "user.message", "payload": {"a": 1}})
    # a fresh connection (as after a crash) sees every acknowledged event
    assert len(EventStore(p).events(rid)) == 2
    w.close()


def test_fts_bm25_and_sanitised_queries(store):
    s, w = store
    rid = created(w)
    w.submit_sync({"run_id": rid, "type": "user.message", "payload": {"search_text": "the database migration failed on step three"}})
    w.submit_sync({"run_id": rid, "type": "user.message", "payload": {"search_text": "lunch menu for friday"}})
    hits = s.search(rid, "migration failing?? (step) \"quoted\" AND OR")
    assert hits and hits[0]["seq"] == 2
    assert s.search(rid, "!!!") == []


async def test_projection_cache_and_rebuild(store):
    s, w = store
    rid = created(w)
    pm = ProjectionManager(s, w)
    w.submit_sync({"run_id": rid, "type": "run.state_changed", "payload": {"from": "CREATED", "to": "PROVISIONING"}})
    st = await pm.get(rid, "run_state")
    assert st["state"] == "PROVISIONING" and s.get_projection(rid, "run_state")[0] == 2
    # corrupt the cache: a version mismatch forces a refold from seq 1
    await w.execute("UPDATE projections SET data=? WHERE run_id=? AND name='run_state'", (json.dumps({"version": 0, "state": {"state": "WRONG"}}), rid))
    assert (await pm.get(rid, "run_state"))["state"] == "PROVISIONING"
    for n in NAMES:
        assert await pm.rebuild(rid, n) == fold_all(n, s.events(rid))


async def test_content_deletion_keeps_chain_intact(store, tmp_path):
    s, w = store
    blobs = BlobStore(tmp_path / "b")
    rid = created(w)
    h = blobs.put(b"sensitive text")
    w.submit_sync({"run_id": rid, "type": "tool.call.completed", "payload": {"result_ref": h, "search_text": "sensitive text"}})

    class H:  # minimal harness facade for delete_content
        pass
    from harness.run import Run, RunManager, Services
    svc = Services(s, w, blobs, FakeClock(), {})
    hh = H(); hh.rm = type("RM", (), {"get": lambda self, r: Run(svc, r, "core", "u")})(); hh.blobs, hh.writer, hh.store, hh.clock = blobs, w, s, svc.clock
    out = await delete_content(hh, rid, [h], "admin", "gdpr")
    assert out["deleted"] == 1 and not blobs.has(h) and s.search(rid, "sensitive") == []
    assert s.events(rid)[-1]["type"] == "content.deleted" and s.verify(rid)["ok"]
