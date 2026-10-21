"""SQLite Event Log (design §5): WAL + synchronous FULL, single writer thread with group commit."""
from __future__ import annotations
import asyncio, concurrent.futures, json, queue, re, sqlite3, threading
from typing import Any
from ..canonical import assert_safe_ints, compute_hash, genesis_hash
from ..clock import Clock, format_ts, parse_ts

DDL = """
CREATE TABLE IF NOT EXISTS runs(
  run_id TEXT PRIMARY KEY, created_at TEXT NOT NULL, owner TEXT NOT NULL, profile TEXT NOT NULL,
  task_id TEXT, config_commit TEXT NOT NULL, state TEXT NOT NULL, last_seq INTEGER NOT NULL DEFAULT 0,
  terminal_reason TEXT);
CREATE TABLE IF NOT EXISTS events(
  run_id TEXT NOT NULL REFERENCES runs(run_id), seq INTEGER NOT NULL, ts TEXT NOT NULL, type TEXT NOT NULL,
  actor_kind TEXT NOT NULL, actor_id TEXT NOT NULL, causation_seq INTEGER, correlation_id TEXT,
  schema_version INTEGER NOT NULL, payload TEXT NOT NULL, prev_hash TEXT NOT NULL, hash TEXT NOT NULL,
  PRIMARY KEY(run_id, seq)) WITHOUT ROWID;
CREATE INDEX IF NOT EXISTS events_by_type ON events(run_id, type, seq);
CREATE INDEX IF NOT EXISTS events_by_corr ON events(run_id, correlation_id);
CREATE VIRTUAL TABLE IF NOT EXISTS events_fts USING fts5(text, run_id UNINDEXED, seq UNINDEXED, type UNINDEXED,
  tokenize='porter unicode61');
CREATE TABLE IF NOT EXISTS projections(run_id TEXT NOT NULL, name TEXT NOT NULL, as_of_seq INTEGER NOT NULL,
  data TEXT NOT NULL, PRIMARY KEY(run_id, name));
CREATE TABLE IF NOT EXISTS approvals(approval_id TEXT PRIMARY KEY, run_id TEXT NOT NULL, tool_call_id TEXT NOT NULL,
  rule_id TEXT, status TEXT NOT NULL, requested_at TEXT NOT NULL, decided_by TEXT, decided_at TEXT,
  expires_at TEXT NOT NULL, scope TEXT);
CREATE TABLE IF NOT EXISTS standing_approvals(id TEXT PRIMARY KEY, run_id TEXT, tool TEXT NOT NULL, args_pattern TEXT,
  expires_at TEXT NOT NULL, max_count INTEGER NOT NULL, used INTEGER NOT NULL DEFAULT 0, granted_by TEXT NOT NULL);
CREATE TABLE IF NOT EXISTS snapshots(snapshot_id TEXT PRIMARY KEY, run_id TEXT NOT NULL, taken_at_seq INTEGER NOT NULL,
  manifest_hash TEXT NOT NULL, kind TEXT NOT NULL, created_at TEXT NOT NULL);
CREATE TABLE IF NOT EXISTS blobs(hash TEXT PRIMARY KEY, size INTEGER NOT NULL, created_at TEXT NOT NULL, deleted_at TEXT);
CREATE TABLE IF NOT EXISTS content_access(id TEXT PRIMARY KEY, run_id TEXT, who TEXT NOT NULL, role TEXT NOT NULL,
  what TEXT NOT NULL, at TEXT NOT NULL);
CREATE TABLE IF NOT EXISTS definition_versions(def_id TEXT NOT NULL, version INTEGER NOT NULL, sha256 TEXT NOT NULL, kind TEXT NOT NULL, published_by TEXT NOT NULL, published_at TEXT NOT NULL, PRIMARY KEY(def_id, version));
CREATE TRIGGER IF NOT EXISTS defs_no_update BEFORE UPDATE ON definition_versions BEGIN SELECT RAISE(ABORT,'definitions are immutable'); END;
CREATE TRIGGER IF NOT EXISTS defs_no_delete BEFORE DELETE ON definition_versions BEGIN SELECT RAISE(ABORT,'definitions are immutable'); END;
CREATE TRIGGER IF NOT EXISTS events_no_update BEFORE UPDATE ON events BEGIN SELECT RAISE(ABORT,'events are append-only'); END;
CREATE TRIGGER IF NOT EXISTS events_no_delete BEFORE DELETE ON events BEGIN SELECT RAISE(ABORT,'events are append-only'); END;
"""

TYPE_RE = re.compile(r"^[a-z_]+(\.[a-z_]+)+$")
ACTOR_KINDS = {"agent", "control_plane", "user", "system", "evaluator"}


class UnknownRunError(Exception):
    pass


def _pragmas(c: sqlite3.Connection) -> None:
    c.execute("PRAGMA journal_mode=WAL")
    c.execute("PRAGMA synchronous=FULL")
    c.execute("PRAGMA foreign_keys=ON")


def row_to_event(r: sqlite3.Row) -> dict:
    return {
        "run_id": r["run_id"], "seq": r["seq"], "ts": r["ts"], "type": r["type"],
        "actor": {"kind": r["actor_kind"], "id": r["actor_id"]},
        "causation_seq": r["causation_seq"], "correlation_id": r["correlation_id"],
        "schema_version": r["schema_version"], "payload": json.loads(r["payload"]),
        "prev_hash": r["prev_hash"], "hash": r["hash"],
    }


def fts_query(text: str, max_terms: int = 12) -> str:
    words = re.findall(r"[A-Za-z0-9_]{2,}", text)[:max_terms]
    return " OR ".join(f'"{w}"' for w in words)


class EventStore:
    """Read side + schema owner. All writes go through EventWriter."""

    def __init__(self, path: str):
        self.path = path
        self._local = threading.local()
        c = sqlite3.connect(path)
        _pragmas(c)
        c.executescript(DDL)
        c.close()

    def conn(self) -> sqlite3.Connection:
        c = getattr(self._local, "c", None)
        if c is None:
            c = sqlite3.connect(self.path, check_same_thread=False, isolation_level=None)
            c.row_factory = sqlite3.Row
            _pragmas(c)
            self._local.c = c
        return c

    def pragma(self, name: str) -> Any:
        return self.conn().execute(f"PRAGMA {name}").fetchone()[0]

    def events(self, run_id: str, after_seq: int = 0, types: list[str] | None = None, limit: int | None = None,
               up_to_seq: int | None = None) -> list[dict]:
        q, args = "SELECT * FROM events WHERE run_id=? AND seq>?", [run_id, after_seq]
        if up_to_seq is not None:
            q += " AND seq<=?"; args.append(up_to_seq)
        if types:
            q += f" AND type IN ({','.join('?' * len(types))})"; args += types
        q += " ORDER BY seq"
        if limit:
            q += " LIMIT ?"; args.append(limit)
        return [row_to_event(r) for r in self.conn().execute(q, args)]

    def get_event(self, run_id: str, seq: int) -> dict | None:
        r = self.conn().execute("SELECT * FROM events WHERE run_id=? AND seq=?", (run_id, seq)).fetchone()
        return row_to_event(r) if r else None

    def last_seq(self, run_id: str) -> int:
        r = self.conn().execute("SELECT last_seq FROM runs WHERE run_id=?", (run_id,)).fetchone()
        return r["last_seq"] if r else 0

    def head_hash(self, run_id: str) -> str:
        r = self.conn().execute("SELECT hash FROM events WHERE run_id=? ORDER BY seq DESC LIMIT 1", (run_id,)).fetchone()
        return r["hash"] if r else genesis_hash(run_id)

    def run_row(self, run_id: str) -> dict | None:
        r = self.conn().execute("SELECT * FROM runs WHERE run_id=?", (run_id,)).fetchone()
        return dict(r) if r else None

    def list_runs(self, include_terminal: bool = True) -> list[dict]:
        rows = self.conn().execute("SELECT * FROM runs ORDER BY run_id").fetchall()
        out = [dict(r) for r in rows]
        if not include_terminal:
            out = [r for r in out if r["state"] not in ("COMPLETED", "TERMINATED", "FAILED_INTEGRITY")]
        return out

    def search(self, run_id: str, text: str, k: int = 5) -> list[dict]:
        q = fts_query(text)
        if not q:
            return []
        rows = self.conn().execute(
            "SELECT seq, type, text, bm25(events_fts) AS rank FROM events_fts WHERE events_fts MATCH ? AND run_id=? "
            "ORDER BY rank LIMIT ?", (q, run_id, k)).fetchall()
        return [{"seq": r["seq"], "type": r["type"], "text": r["text"], "rank": r["rank"]} for r in rows]

    def verify(self, run_id: str, last_n: int | None = None) -> dict:
        """Recompute the hash chain; last_n limits the check to the newest events (§5.9 step 1)."""
        last = self.last_seq(run_id)
        start = 1 if last_n is None else max(1, last - last_n + 1)
        evs = self.events(run_id, after_seq=start - 1)
        if start > 1:
            prev = self.get_event(run_id, start - 1)
            prev_hash = prev["hash"] if prev else None
        else:
            prev_hash = genesis_hash(run_id)
        checked = 0
        for i, ev in enumerate(evs):
            expect_seq = start + i
            if ev["seq"] != expect_seq:
                return {"ok": False, "checked": checked, "bad_seq": expect_seq, "reason": "sequence gap"}
            if ev["prev_hash"] != prev_hash:
                return {"ok": False, "checked": checked, "bad_seq": ev["seq"], "reason": "prev_hash mismatch"}
            if compute_hash(prev_hash, ev) != ev["hash"]:
                return {"ok": False, "checked": checked, "bad_seq": ev["seq"], "reason": "hash mismatch"}
            prev_hash = ev["hash"]
            checked += 1
        if last_n is None and evs and evs[-1]["seq"] != last:
            return {"ok": False, "checked": checked, "bad_seq": last, "reason": "runs.last_seq mismatch"}
        return {"ok": True, "checked": checked, "bad_seq": None, "reason": None}

    # projection cache (rebuildable)
    def get_projection(self, run_id: str, name: str) -> tuple[int, Any] | None:
        r = self.conn().execute("SELECT as_of_seq, data FROM projections WHERE run_id=? AND name=?", (run_id, name)).fetchone()
        return (r["as_of_seq"], json.loads(r["data"])) if r else None


class EventWriter:
    """Exactly one writer thread (§3.3 rule 1) with group commit (§5.3)."""

    def __init__(self, store: EventStore, clock: Clock | None = None, max_batch: int = 256):
        self.store, self.clock, self.max_batch = store, clock or Clock(), max_batch
        self._q: queue.Queue = queue.Queue()
        self._cache: dict[str, tuple[int, str, int]] = {}
        self._thread = threading.Thread(target=self._run, name="event-writer", daemon=True)
        self._started = False
        self.commits = 0

    def start(self) -> "EventWriter":
        if not self._started:
            self._started = True
            self._thread.start()
        return self

    def close(self) -> None:
        if self._started:
            self._q.put(None)
            self._thread.join()
            self._started = False

    # -- submission API --
    def _enqueue(self, op: tuple) -> concurrent.futures.Future:
        fut: concurrent.futures.Future = concurrent.futures.Future()
        self._q.put((fut, op))
        return fut

    def submit_sync(self, draft: dict) -> dict:
        return self._enqueue(("event", draft)).result()

    async def submit(self, draft: dict) -> dict:
        return await asyncio.wrap_future(self._enqueue(("event", draft)))

    async def execute(self, sql: str, params: tuple = (), fetch: bool = False):
        return await asyncio.wrap_future(self._enqueue(("sql", sql, params, fetch)))

    def execute_sync(self, sql: str, params: tuple = (), fetch: bool = False):
        return self._enqueue(("sql", sql, params, fetch)).result()

    async def put_projection(self, run_id: str, name: str, as_of: int, data: Any) -> None:
        await self.execute("INSERT OR REPLACE INTO projections(run_id,name,as_of_seq,data) VALUES(?,?,?,?)",
                           (run_id, name, as_of, json.dumps(data, separators=(",", ":"))))

    # -- writer thread --
    def _run(self) -> None:
        conn = sqlite3.connect(self.store.path, isolation_level=None, check_same_thread=False)
        conn.row_factory = sqlite3.Row
        _pragmas(conn)
        while True:
            item = self._q.get()
            if item is None:
                break
            batch = [item]
            stop = False
            while len(batch) < self.max_batch:
                try:
                    nxt = self._q.get_nowait()
                except queue.Empty:
                    break
                if nxt is None:
                    stop = True
                    break
                batch.append(nxt)
            self._process(conn, batch)
            if stop:
                break
        conn.close()

    def _process(self, conn: sqlite3.Connection, batch: list) -> None:
        pending: dict[str, tuple[int, str, int]] = {}
        results: list[tuple[concurrent.futures.Future, Any, Exception | None]] = []
        try:
            conn.execute("BEGIN IMMEDIATE")
            for fut, op in batch:
                try:
                    conn.execute("SAVEPOINT item")
                    if op[0] == "event":
                        res = self._append(conn, op[1], pending)
                    else:
                        cur = conn.execute(op[1], op[2])
                        res = [dict(r) for r in cur.fetchall()] if op[3] else cur.rowcount
                    conn.execute("RELEASE item")
                    results.append((fut, res, None))
                except Exception as e:  # isolate the failing item
                    conn.execute("ROLLBACK TO item")
                    conn.execute("RELEASE item")
                    results.append((fut, None, e))
            conn.execute("COMMIT")
            self._cache.update(pending)
            self.commits += 1
        except Exception as e:
            try:
                conn.execute("ROLLBACK")
            except Exception:
                pass
            results = [(f, None, e) for f, _ in batch]
        for fut, res, err in results:  # acknowledge only after the commit is durable
            if fut.cancelled():  # the caller gave up (task cancelled); the event is still durable
                continue
            try:
                fut.set_exception(err) if err is not None else fut.set_result(res)
            except concurrent.futures.InvalidStateError:
                pass

    def _head(self, conn, run_id: str, pending: dict) -> tuple[int, str, int]:
        if run_id in pending:
            return pending[run_id]
        if run_id in self._cache:
            return self._cache[run_id]
        r = conn.execute("SELECT seq, hash, ts FROM events WHERE run_id=? ORDER BY seq DESC LIMIT 1", (run_id,)).fetchone()
        if r:
            return (r["seq"], r["hash"], parse_ts(r["ts"]))
        return (0, genesis_hash(run_id), 0)

    def _append(self, conn, d: dict, pending: dict) -> dict:
        run_id, typ = d["run_id"], d["type"]
        actor = d.get("actor") or {"kind": "control_plane", "id": "harness"}
        payload = d.get("payload", {})
        if not TYPE_RE.match(typ):
            raise ValueError(f"invalid event type {typ!r}")
        if actor["kind"] not in ACTOR_KINDS:
            raise ValueError(f"invalid actor kind {actor['kind']!r}")
        if not isinstance(payload, dict):
            raise ValueError("payload must be an object")
        assert_safe_ints(payload)
        known = conn.execute("SELECT 1 FROM runs WHERE run_id=?", (run_id,)).fetchone()
        seq0, prev, last_ts = self._head(conn, run_id, pending)
        ts_ms = max(self.clock.now_ms(), last_ts)
        if typ == "run.created":
            if known:
                raise ValueError("run already exists")
            conn.execute("INSERT INTO runs(run_id,created_at,owner,profile,task_id,config_commit,state,last_seq) VALUES(?,?,?,?,?,?,?,0)",
                         (run_id, format_ts(ts_ms), payload["owner"], payload["profile"], payload.get("task_id"),
                          payload["config_commit"], "CREATED"))
        elif not known:
            raise UnknownRunError(run_id)
        seq = seq0 + 1
        ev = {"run_id": run_id, "seq": seq, "ts": format_ts(ts_ms), "type": typ, "actor": actor,
              "causation_seq": d.get("causation_seq"), "correlation_id": d.get("correlation_id"),
              "schema_version": d.get("schema_version", 1), "payload": payload}
        ev["prev_hash"] = prev
        ev["hash"] = compute_hash(prev, ev)
        conn.execute("INSERT INTO events VALUES(?,?,?,?,?,?,?,?,?,?,?,?)",
                     (run_id, seq, ev["ts"], typ, actor["kind"], actor["id"], ev["causation_seq"], ev["correlation_id"],
                      ev["schema_version"], json.dumps(payload, ensure_ascii=False, separators=(",", ":")), prev, ev["hash"]))
        st = None
        if typ == "run.state_changed":
            st = payload["to"]
        conn.execute("UPDATE runs SET last_seq=?, state=COALESCE(?,state), terminal_reason=COALESCE(?,terminal_reason) WHERE run_id=?",
                     (seq, st, payload.get("reason") if typ == "run.terminated" else None, run_id))
        if isinstance(payload.get("search_text"), str) and payload["search_text"]:
            conn.execute("INSERT INTO events_fts(text,run_id,seq,type) VALUES(?,?,?,?)", (payload["search_text"], run_id, seq, typ))
        pending[run_id] = (seq, ev["hash"], ts_ms)
        return ev
